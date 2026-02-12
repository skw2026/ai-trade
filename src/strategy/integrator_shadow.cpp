#include "strategy/integrator_shadow.h"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <unordered_map>
#include <utility>
#include <vector>

#include "core/json_utils.h"
#include "core/log.h"

#if defined(AI_TRADE_ENABLE_CATBOOST)
#if !defined(_WIN32)
#include <dlfcn.h>
#else
#error "Windows dlopen not implemented"
#endif

// 定义函数指针类型
typedef void* ModelCalcerHandle;
typedef ModelCalcerHandle (*Proc_ModelCalcerCreate)();
typedef void (*Proc_ModelCalcerDelete)(ModelCalcerHandle handle);
typedef bool (*Proc_ModelCalcerLoadSingleModelFromFile)(ModelCalcerHandle handle, const char* filename);
typedef bool (*Proc_ModelCalcerCalc)(ModelCalcerHandle handle, const float* features, size_t featuresSize, double* result, size_t resultSize);
typedef const char* (*Proc_ModelCalcerGetErrorString)(ModelCalcerHandle handle);

// 全局持有 dlopen 句柄，避免重复加载
static void* g_catboost_lib_handle = nullptr;
static bool g_catboost_lib_loaded = false;
#endif

namespace ai_trade {

namespace {

std::string JoinReasons(const std::vector<std::string>& reasons) {
  std::ostringstream oss;
  for (std::size_t i = 0; i < reasons.size(); ++i) {
    if (i > 0) {
      oss << "; ";
    }
    oss << reasons[i];
  }
  return oss.str();
}

bool IsRegularFileNonEmpty(const std::string& path, std::string* out_error) {
  namespace fs = std::filesystem;
  std::error_code ec;
  const fs::path p(path);
  const bool exists = fs::exists(p, ec);
  if (ec || !exists) {
    if (out_error != nullptr) {
      *out_error = "文件不存在: " + path;
    }
    return false;
  }
  const bool regular = fs::is_regular_file(p, ec);
  if (ec || !regular) {
    if (out_error != nullptr) {
      *out_error = "不是普通文件: " + path;
    }
    return false;
  }
  const auto size = fs::file_size(p, ec);
  if (ec || size == 0U) {
    if (out_error != nullptr) {
      *out_error = "文件为空: " + path;
    }
    return false;
  }
  return true;
}

// 将经典特征名映射为 OnlineFeatureEngine 支持的表达式
std::string MapClassicFeatureToExpression(const std::string& name) {
  if (name == "ret_1") {
    return "ts_delta(close,1)/(abs(ts_delay(close,1))+1e-9)";
  }
  if (name == "ret_3") {
    return "ts_delta(close,3)/(abs(ts_delay(close,3))+1e-9)";
  }
  if (name == "vol_delta_1") {
    return "ts_delta(volume,1)";
  }
  if (name.find("rsi_") == 0) {
    try {
      // 映射 rsi_14 -> rsi(close, 14)
      int period = std::stoi(name.substr(4));
      return "rsi(close," + std::to_string(period) + ")";
    } catch (...) {}
  }
  if (name == "macd_line") {
    return "ema(close,12)-ema(close,26)";
  }
  if (name == "macd_signal") {
    return "ema(ema(close,12)-ema(close,26),9)";
  }
  if (name == "macd_hist") {
    return "(ema(close,12)-ema(close,26))-ema(ema(close,12)-ema(close,26),9)";
  }
  // 默认返回 0
  return "0";
}

}  // namespace

IntegratorShadow::IntegratorShadow(IntegratorShadowConfig config)
    : config_(std::move(config)),
      feature_engine_(config_.feature_window_ticks > 0 ? config_.feature_window_ticks : 300) {}

IntegratorShadow::~IntegratorShadow() {
#ifdef AI_TRADE_ENABLE_CATBOOST
  if (model_handle_) {
    // 析构时需要确保库还未卸载，或者容忍泄漏。
    // 为简单起见，我们不 dlclose，让 OS 回收。
    // 如果 g_catboost_lib_handle 有效，则调用 Delete。
    if (g_catboost_lib_handle) {
        auto func = reinterpret_cast<Proc_ModelCalcerDelete>(dlsym(g_catboost_lib_handle, "ModelCalcerDelete"));
        if (func) func(static_cast<ModelCalcerHandle>(model_handle_));
    }
    model_handle_ = nullptr;
  }
#endif
}

bool IntegratorShadow::Initialize(bool strict_takeover, std::string* out_error) {
  initialized_ = false;
  model_version_ = "n/a";
  feature_names_.clear();
  feature_expressions_.clear();

  if (!config_.enabled) {
    initialized_ = true;
    return true;
  }

  const bool require_model_file = strict_takeover || config_.require_model_file;
  const bool require_active_meta = strict_takeover || config_.require_active_meta;
  const bool require_gate_pass = strict_takeover || config_.require_gate_pass;
  const bool require_report_quality = strict_takeover || config_.require_gate_pass;

  auto fail = [&](const std::string& message) {
    if (out_error != nullptr) {
      *out_error = message;
    }
    return false;
  };

  std::ifstream input(config_.model_report_path);
  if (!input.is_open()) {
    return fail("无法打开 integrator 报告: " + config_.model_report_path);
  }

  std::ostringstream buffer;
  buffer << input.rdbuf();
  JsonValue root;
  std::string parse_error;
  if (!ParseJson(buffer.str(), &root, &parse_error)) {
    return fail("integrator 报告解析失败: " + parse_error);
  }

  const auto version = JsonAsString(JsonObjectField(&root, "model_version"));
  if (!version.has_value() || version->empty()) {
    return fail("integrator 报告缺少 model_version");
  }
  model_version_ = *version;

  // 1. 解析特征列表
  const JsonValue* feature_names_json = JsonObjectField(&root, "feature_names");
  if (feature_names_json != nullptr && feature_names_json->type == JsonType::kArray) {
    for (const auto& item : feature_names_json->array_value) {
      if (auto name = JsonAsString(&item); name.has_value()) {
        feature_names_.push_back(*name);
      }
    }
  }

  // 2. 获取 Miner 报告路径
  std::string miner_report_path;
  const JsonValue* data_section = JsonObjectField(&root, "data");
  if (auto path = JsonAsString(JsonObjectField(data_section, "miner_report_path")); path.has_value()) {
    miner_report_path = *path;
  }

  std::vector<std::string> quality_failures;
  const JsonValue* metrics = JsonObjectField(&root, "metrics_oos");
  if (metrics == nullptr || metrics->type != JsonType::kObject) {
    quality_failures.push_back("缺少 metrics_oos");
  } else {
    auto auc_mean = JsonAsNumber(JsonObjectField(metrics, "auc_mean"));
    auto delta_auc =
        JsonAsNumber(JsonObjectField(metrics, "delta_auc_vs_baseline"));
    auto split_trained_count =
        JsonAsNumber(JsonObjectField(metrics, "split_trained_count"));
    auto split_count = JsonAsNumber(JsonObjectField(metrics, "split_count"));

    if (!auc_mean.has_value() || !std::isfinite(*auc_mean)) {
      quality_failures.push_back("缺少 metrics_oos.auc_mean");
    } else if (*auc_mean < config_.min_auc_mean) {
      quality_failures.push_back("auc_mean=" + std::to_string(*auc_mean) +
                                 " < min_auc_mean=" +
                                 std::to_string(config_.min_auc_mean));
    }

    if (!delta_auc.has_value() || !std::isfinite(*delta_auc)) {
      quality_failures.push_back("缺少 metrics_oos.delta_auc_vs_baseline");
    } else if (*delta_auc < config_.min_delta_auc_vs_baseline) {
      quality_failures.push_back(
          "delta_auc_vs_baseline=" + std::to_string(*delta_auc) +
          " < min_delta_auc_vs_baseline=" +
          std::to_string(config_.min_delta_auc_vs_baseline));
    }

    int trained = 0;
    int total = 0;
    if (split_trained_count.has_value() && std::isfinite(*split_trained_count)) {
      trained = static_cast<int>(std::llround(*split_trained_count));
    } else {
      quality_failures.push_back("缺少 metrics_oos.split_trained_count");
    }
    if (split_count.has_value() && std::isfinite(*split_count)) {
      total = static_cast<int>(std::llround(*split_count));
    } else {
      quality_failures.push_back("缺少 metrics_oos.split_count");
    }

    if (trained < config_.min_split_trained_count) {
      quality_failures.push_back(
          "split_trained_count=" + std::to_string(trained) +
          " < min_split_trained_count=" +
          std::to_string(config_.min_split_trained_count));
    }
    if (total <= 0) {
      quality_failures.push_back("split_count 必须 > 0");
    } else {
      const double trained_ratio =
          static_cast<double>(trained) / static_cast<double>(total);
      if (trained_ratio < config_.min_split_trained_ratio) {
        quality_failures.push_back(
            "split_trained_ratio=" + std::to_string(trained_ratio) +
            " < min_split_trained_ratio=" +
            std::to_string(config_.min_split_trained_ratio));
      }
    }
  }

  if (require_report_quality && !quality_failures.empty()) {
    return fail("integrator 报告治理门槛未通过: " + JoinReasons(quality_failures));
  }

  if (require_model_file) {
    std::string file_error;
    if (!IsRegularFileNonEmpty(config_.model_path, &file_error)) {
      return fail("integrator 模型文件校验失败: " + file_error);
    }

#ifdef AI_TRADE_ENABLE_CATBOOST
    // 1. 延迟加载库
    if (!g_catboost_lib_loaded) {
        g_catboost_lib_handle = dlopen("libcatboostmodel.so", RTLD_LAZY | RTLD_GLOBAL);
        if (!g_catboost_lib_handle) {
             // 尝试默认路径
             g_catboost_lib_handle = dlopen("/usr/local/lib/libcatboostmodel.so", RTLD_LAZY | RTLD_GLOBAL);
        }
        if (!g_catboost_lib_handle) {
            return fail("无法加载 libcatboostmodel.so: " + std::string(dlerror()));
        }
        g_catboost_lib_loaded = true;
    }

    // 2. 获取函数指针
    auto p_Create = reinterpret_cast<Proc_ModelCalcerCreate>(dlsym(g_catboost_lib_handle, "ModelCalcerCreate"));
    auto p_Delete = reinterpret_cast<Proc_ModelCalcerDelete>(dlsym(g_catboost_lib_handle, "ModelCalcerDelete"));
    auto p_Load = reinterpret_cast<Proc_ModelCalcerLoadSingleModelFromFile>(dlsym(g_catboost_lib_handle, "ModelCalcerLoadSingleModelFromFile"));
    auto p_Error = reinterpret_cast<Proc_ModelCalcerGetErrorString>(dlsym(g_catboost_lib_handle, "ModelCalcerGetErrorString"));

    if (!p_Create || !p_Delete || !p_Load || !p_Error) {
        return fail("libcatboostmodel.so 缺少必要符号");
    }

    if (model_handle_) {
      p_Delete(static_cast<ModelCalcerHandle>(model_handle_));
      model_handle_ = nullptr;
    }
    
    model_handle_ = p_Create();
    if (!p_Load(static_cast<ModelCalcerHandle>(model_handle_), 
                                            config_.model_path.c_str())) {
      const char* msg = "";
      msg = p_Error(static_cast<ModelCalcerHandle>(model_handle_));
      return fail("CatBoost 模型加载失败: " + std::string(msg ? msg : "unknown error"));
    }
    LogInfo("CatBoost 模型加载成功: " + config_.model_path);
#endif
  }

  bool active_meta_found = false;
  if (!config_.active_meta_path.empty()) {
    namespace fs = std::filesystem;
    std::error_code ec;
    const fs::path active_meta(config_.active_meta_path);
    const bool exists = fs::exists(active_meta, ec);
    if (!ec && exists) {
      active_meta_found = true;
      std::ifstream active_input(config_.active_meta_path);
      if (!active_input.is_open()) {
        return fail("无法打开 integrator active_meta: " +
                    config_.active_meta_path);
      }
      std::ostringstream active_buffer;
      active_buffer << active_input.rdbuf();
      JsonValue active_root;
      std::string active_parse_error;
      if (!ParseJson(active_buffer.str(), &active_root, &active_parse_error)) {
        return fail("integrator active_meta 解析失败: " + active_parse_error);
      }

      const auto active_model_version =
          JsonAsString(JsonObjectField(&active_root, "model_version"));
      if (active_model_version.has_value() && !active_model_version->empty() &&
          *active_model_version != model_version_) {
        return fail("active_meta 与 report model_version 不一致: active_meta=" +
                    *active_model_version + ", report=" + model_version_);
      }

      if (require_gate_pass) {
        const JsonValue* gate = JsonObjectField(&active_root, "gate");
        const auto gate_pass = JsonAsBool(JsonObjectField(gate, "pass"));
        if (!gate_pass.value_or(false)) {
          return fail("active_meta.gate.pass != true，不允许进入接管模式");
        }
      }
    } else if (require_active_meta || require_gate_pass) {
      return fail("缺少 integrator active_meta: " + config_.active_meta_path);
    }
  } else if (require_active_meta || require_gate_pass) {
    return fail("integrator.active_meta_path 为空");
  }

  if (require_active_meta && !active_meta_found) {
    return fail("require_active_meta=true 但未找到 active_meta");
  }

  // 3. 加载 Miner 报告并构建特征表达式映射
  std::unordered_map<std::string, std::string> miner_expressions;
  if (!miner_report_path.empty()) {
    std::ifstream miner_input(miner_report_path);
    if (miner_input.is_open()) {
      std::ostringstream miner_buffer;
      miner_buffer << miner_input.rdbuf();
      JsonValue miner_root;
      std::string miner_err;
      if (ParseJson(miner_buffer.str(), &miner_root, &miner_err)) {
        const JsonValue* factors = JsonObjectField(&miner_root, "factors");
        if (factors != nullptr && factors->type == JsonType::kArray) {
          int idx = 0;
          for (const auto& factor : factors->array_value) {
            auto expr = JsonAsString(JsonObjectField(&factor, "expression"));
            auto invert = JsonAsBool(JsonObjectField(&factor, "invert_signal"));
            if (expr.has_value()) {
              std::string final_expr = *expr;
              if (invert.value_or(false)) {
                final_expr = "-(" + final_expr + ")";
              }
              // key 格式需与 integrator_train.py 中的命名一致: miner_00, miner_01...
              std::string key = std::string("miner_") + (idx < 10 ? "0" : "") + std::to_string(idx);
              miner_expressions[key] = final_expr;
            }
            idx++;
          }
        }
      }
    }
  }

  // 4. 构建最终的表达式列表
  for (const auto& name : feature_names_) {
    if (name.rfind("miner_", 0) == 0) {
      auto it = miner_expressions.find(name);
      feature_expressions_.push_back(it != miner_expressions.end() ? it->second : "0");
    } else {
      feature_expressions_.push_back(MapClassicFeatureToExpression(name));
    }
  }

  initialized_ = true;
  return true;
}

double IntegratorShadow::Sigmoid(double x) {
  if (x >= 0.0) {
    const double z = std::exp(-x);
    return 1.0 / (1.0 + z);
  }
  const double z = std::exp(x);
  return z / (1.0 + z);
}

void IntegratorShadow::OnMarket(const MarketEvent& event) {
  feature_engine_.OnMarket(event);
}

ShadowInference IntegratorShadow::Infer(const Signal& signal,
                                        const RegimeState& regime) const {
  ShadowInference out;
  if (!enabled()) {
    return out;
  }

  out.enabled = true;
  out.model_version = model_version_;

  // 1. 计算特征向量
  // 注意：如果数据不足，EvaluateBatch 会返回 0 或 NaN
  std::vector<double> features;
  if (feature_engine_.IsReady()) {
    features = feature_engine_.EvaluateBatch(feature_expressions_);
  }

  // 关键防御：检查特征向量是否存在 NaN/Inf。
  // 任何脏数据都可能导致模型输出不可预测的极端值，必须在此熔断。
  for (size_t i = 0; i < features.size(); ++i) {
    if (!std::isfinite(features[i])) {
      static int nan_warn_counter = 0;
      // 限频日志：避免因数据预热期的连续 NaN 刷屏
      if (config_.log_model_score && nan_warn_counter++ % 100 == 0) {
        LogInfo("INTEGRATOR_SKIP: NaN feature detected at index " + std::to_string(i) +
                " (" + (i < feature_names_.size() ? feature_names_[i] : "unknown") + ")");
      }
      out.enabled = false;
      return out;
    }
  }

  // 2. 模型推理 (Real vs Mock)
  double raw = 0.0;

#ifdef AI_TRADE_ENABLE_CATBOOST
  if (model_handle_ && !features.empty()) {
    // CatBoost C API 需要 float 数组
    std::vector<float> float_features(features.begin(), features.end());
    const float* row_ptr = float_features.data();
    double result = 0.0;
    
    auto p_Calc = reinterpret_cast<Proc_ModelCalcerCalc>(dlsym(g_catboost_lib_handle, "ModelCalcerCalc"));
    if (p_Calc && p_Calc(static_cast<ModelCalcerHandle>(model_handle_), 
                        row_ptr, features.size(), &result, 1)) {
      raw = result;
    } else {
      LogInfo("INTEGRATOR_ERROR: CatBoost inference failed");
    }
  } else {
    // Fallback if model not loaded
    raw = std::clamp(signal.suggested_notional_usd / 1000.0, -2.0, 2.0);
  }
#else
  // Mock 逻辑：混合原始信号与特征计算结果
  raw = std::clamp(signal.suggested_notional_usd / 1000.0, -2.0, 2.0);
#endif

  // 3. Regime 修正
  if (regime.regime == Regime::kUptrend) {
    raw += 0.20;
  } else if (regime.regime == Regime::kDowntrend) {
    raw -= 0.20;
  }
  if (regime.bucket == RegimeBucket::kRange) {
    raw *= 0.75;
  } else if (regime.bucket == RegimeBucket::kExtreme) {
    raw *= 0.55;
  }
  if (regime.warmup) {
    raw *= 0.60;
  }

  // 4. 特征影响 (Mock): 如果计算出了有效特征，微调分数
  if (!features.empty()) {
    // 简单示例：如果 ret_1 (通常是列表后部的特征) 为正，略微增加分数
    // 仅用于验证 pipeline 连通性
    if (features.size() > 5 && features.back() > 0.001) {
      raw += 0.1;
    }

    // 增加特征值日志 (Sample logging)
    if (config_.log_model_score) {
      std::ostringstream oss;
      oss << "FEATURES: ";
      for (size_t i = 0; i < std::min<size_t>(5, features.size()); ++i) {
        if (i > 0) oss << ", ";
        oss << feature_names_[i] << "=" << features[i];
      }
      // 仅在调试或详细模式下输出特征值
      LogInfo(oss.str());
    }
  }

  out.model_score = std::clamp(raw * config_.score_gain, -6.0, 6.0);
  out.p_up = Sigmoid(out.model_score);
  out.p_down = 1.0 - out.p_up;
  return out;
}

}  // namespace ai_trade
