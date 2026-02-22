#include "strategy/integrator_shadow.h"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <initializer_list>
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
typedef bool (*Proc_LoadFullModelFromFile)(ModelCalcerHandle handle, const char* filename);
typedef bool (*Proc_ModelCalcerCalc)(ModelCalcerHandle handle, const float* features, size_t featuresSize, double* result, size_t resultSize);
typedef bool (*Proc_CalcModelPredictionSingle)(ModelCalcerHandle handle,
                                                const float* features,
                                                size_t features_size,
                                                const char** cat_features,
                                                size_t cat_features_size,
                                                double* result,
                                                size_t result_size);
typedef const char* (*Proc_ModelCalcerGetErrorString)(ModelCalcerHandle handle);
typedef const char* (*Proc_GetErrorString)();

struct CatBoostDynamicApi {
  Proc_ModelCalcerCreate create{nullptr};
  Proc_ModelCalcerDelete remove{nullptr};
  Proc_ModelCalcerLoadSingleModelFromFile load_single{nullptr};
  Proc_LoadFullModelFromFile load_full{nullptr};
  Proc_ModelCalcerCalc calc{nullptr};
  Proc_CalcModelPredictionSingle calc_single{nullptr};
  Proc_ModelCalcerGetErrorString error_with_handle{nullptr};
  Proc_GetErrorString error_global{nullptr};
  std::string load_symbol_name;
  std::string calc_symbol_name;
  std::string error_symbol_name;
  bool resolved{false};
};

// 全局持有 dlopen 句柄，避免重复加载
static void* g_catboost_lib_handle = nullptr;
static bool g_catboost_lib_loaded = false;
static CatBoostDynamicApi g_catboost_api;

void* ResolveSymbol(void* handle, const char* symbol) {
  if (handle == nullptr || symbol == nullptr) {
    return nullptr;
  }
  dlerror();
  void* ptr = dlsym(handle, symbol);
  if (dlerror() != nullptr) {
    return nullptr;
  }
  return ptr;
}

template <typename Fn>
Fn ResolveFirstSymbol(void* handle,
                      std::initializer_list<const char*> candidates,
                      std::string* out_name) {
  for (const char* symbol : candidates) {
    void* ptr = ResolveSymbol(handle, symbol);
    if (ptr != nullptr) {
      if (out_name != nullptr) {
        *out_name = symbol;
      }
      return reinterpret_cast<Fn>(ptr);
    }
  }
  return nullptr;
}

bool ResolveCatBoostApi(void* handle, CatBoostDynamicApi* out_api, std::string* out_error) {
  if (handle == nullptr || out_api == nullptr) {
    if (out_error != nullptr) {
      *out_error = "catboost 动态库句柄无效";
    }
    return false;
  }
  CatBoostDynamicApi api;
  api.create = ResolveFirstSymbol<Proc_ModelCalcerCreate>(
      handle, {"ModelCalcerCreate"}, nullptr);
  api.remove = ResolveFirstSymbol<Proc_ModelCalcerDelete>(
      handle, {"ModelCalcerDelete"}, nullptr);
  api.load_single = ResolveFirstSymbol<Proc_ModelCalcerLoadSingleModelFromFile>(
      handle, {"ModelCalcerLoadSingleModelFromFile"}, &api.load_symbol_name);
  if (api.load_single == nullptr) {
    api.load_full = ResolveFirstSymbol<Proc_LoadFullModelFromFile>(
        handle, {"LoadFullModelFromFile"}, &api.load_symbol_name);
  }
  api.calc = ResolveFirstSymbol<Proc_ModelCalcerCalc>(
      handle, {"ModelCalcerCalc"}, &api.calc_symbol_name);
  if (api.calc == nullptr) {
    api.calc_single = ResolveFirstSymbol<Proc_CalcModelPredictionSingle>(
        handle, {"CalcModelPredictionSingle"}, &api.calc_symbol_name);
  }
  api.error_with_handle = ResolveFirstSymbol<Proc_ModelCalcerGetErrorString>(
      handle, {"ModelCalcerGetErrorString"}, &api.error_symbol_name);
  if (api.error_with_handle == nullptr) {
    api.error_global = ResolveFirstSymbol<Proc_GetErrorString>(
        handle, {"GetErrorString"}, &api.error_symbol_name);
  }

  std::vector<std::string> missing;
  if (api.create == nullptr) {
    missing.push_back("ModelCalcerCreate");
  }
  if (api.remove == nullptr) {
    missing.push_back("ModelCalcerDelete");
  }
  if (api.load_single == nullptr && api.load_full == nullptr) {
    missing.push_back("ModelCalcerLoadSingleModelFromFile/LoadFullModelFromFile");
  }
  if (api.calc == nullptr && api.calc_single == nullptr) {
    missing.push_back("ModelCalcerCalc/CalcModelPredictionSingle");
  }
  if (api.error_with_handle == nullptr && api.error_global == nullptr) {
    missing.push_back("ModelCalcerGetErrorString/GetErrorString");
  }

  if (!missing.empty()) {
    if (out_error != nullptr) {
      std::ostringstream oss;
      oss << "libcatboostmodel.so 缺少必要符号: ";
      for (std::size_t i = 0; i < missing.size(); ++i) {
        if (i > 0) {
          oss << ", ";
        }
        oss << missing[i];
      }
      *out_error = oss.str();
    }
    return false;
  }

  api.resolved = true;
  *out_api = std::move(api);
  return true;
}

const char* CatBoostErrorString(ModelCalcerHandle handle) {
  if (g_catboost_api.error_with_handle != nullptr) {
    return g_catboost_api.error_with_handle(handle);
  }
  if (g_catboost_api.error_global != nullptr) {
    return g_catboost_api.error_global();
  }
  return nullptr;
}

bool CatBoostLoadModel(ModelCalcerHandle handle, const char* model_path) {
  if (g_catboost_api.load_single != nullptr) {
    return g_catboost_api.load_single(handle, model_path);
  }
  if (g_catboost_api.load_full != nullptr) {
    return g_catboost_api.load_full(handle, model_path);
  }
  return false;
}

bool CatBoostCalcPrediction(ModelCalcerHandle handle,
                            const float* features,
                            std::size_t features_size,
                            double* result,
                            std::size_t result_size) {
  if (g_catboost_api.calc != nullptr) {
    return g_catboost_api.calc(handle, features, features_size, result, result_size);
  }
  if (g_catboost_api.calc_single != nullptr) {
    return g_catboost_api.calc_single(handle, features, features_size,
                                      nullptr, 0, result, result_size);
  }
  return false;
}
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
    if (g_catboost_lib_handle && g_catboost_api.remove != nullptr) {
      g_catboost_api.remove(static_cast<ModelCalcerHandle>(model_handle_));
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
  model_runtime_ready_ = false;

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

  const bool has_model_path = !config_.model_path.empty();
  bool model_file_ok = false;
  std::string model_file_error;
  if (has_model_path) {
    model_file_ok = IsRegularFileNonEmpty(config_.model_path, &model_file_error);
  } else {
    model_file_error = "model_path 为空";
  }
  if (!model_file_ok) {
    if (require_model_file) {
      return fail("integrator 模型文件校验失败: " + model_file_error);
    }
    LogInfo("INTEGRATOR_DEGRADED: 模型文件不可用，shadow 推理将降级关闭: " +
            model_file_error);
  } else {
#ifdef AI_TRADE_ENABLE_CATBOOST
    // 1. 延迟加载库
    if (!g_catboost_lib_loaded) {
        g_catboost_lib_handle = dlopen("libcatboostmodel.so", RTLD_LAZY | RTLD_GLOBAL);
        if (!g_catboost_lib_handle) {
             // 尝试默认路径
             g_catboost_lib_handle = dlopen("/usr/local/lib/libcatboostmodel.so", RTLD_LAZY | RTLD_GLOBAL);
        }
        if (!g_catboost_lib_handle) {
            if (require_model_file) {
              return fail("无法加载 libcatboostmodel.so: " + std::string(dlerror()));
            }
            LogInfo("INTEGRATOR_DEGRADED: 无法加载 libcatboostmodel.so，shadow 推理将降级关闭");
        } else {
          g_catboost_lib_loaded = true;
        }
    }

    if (g_catboost_lib_handle && !g_catboost_api.resolved) {
      std::string resolve_error;
      if (!ResolveCatBoostApi(g_catboost_lib_handle, &g_catboost_api,
                              &resolve_error)) {
        if (require_model_file) {
          return fail(resolve_error);
        }
        LogInfo("INTEGRATOR_DEGRADED: " + resolve_error +
                "，shadow 推理将降级关闭");
      } else {
        LogInfo("INTEGRATOR_CATBOOST_API_READY: load_symbol=" +
                g_catboost_api.load_symbol_name +
                ", calc_symbol=" + g_catboost_api.calc_symbol_name +
                ", error_symbol=" + g_catboost_api.error_symbol_name);
      }
    }

    if (g_catboost_lib_handle && g_catboost_api.resolved) {
      if (model_handle_) {
        g_catboost_api.remove(static_cast<ModelCalcerHandle>(model_handle_));
        model_handle_ = nullptr;
      }
      model_handle_ = g_catboost_api.create();
      if (model_handle_ == nullptr) {
        if (require_model_file) {
          return fail("CatBoost 模型句柄创建失败");
        }
        LogInfo("INTEGRATOR_DEGRADED: CatBoost 模型句柄创建失败，shadow 推理将降级关闭");
      } else if (!CatBoostLoadModel(static_cast<ModelCalcerHandle>(model_handle_),
                                    config_.model_path.c_str())) {
        const char* msg = CatBoostErrorString(static_cast<ModelCalcerHandle>(model_handle_));
        if (require_model_file) {
          return fail("CatBoost 模型加载失败: " +
                      std::string(msg ? msg : "unknown error"));
        }
        LogInfo("INTEGRATOR_DEGRADED: CatBoost 模型加载失败，shadow 推理将降级关闭");
        model_handle_ = nullptr;
      } else {
        model_runtime_ready_ = true;
        LogInfo("CatBoost 模型加载成功: " + config_.model_path);
      }
    }
#else
    if (require_model_file) {
      return fail(
          "当前构建未启用 AI_TRADE_ENABLE_CATBOOST，无法加载模型进入接管模式");
    }
    LogInfo("INTEGRATOR_DEGRADED: 未启用 AI_TRADE_ENABLE_CATBOOST，shadow 推理将降级关闭");
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
  (void)signal;
  (void)regime;
  if (!enabled()) {
    return out;
  }

  out.model_version = model_version_;
  if (!model_runtime_ready_ || model_handle_ == nullptr) {
    out.enabled = false;
    return out;
  }

  // 1. 计算特征向量
  std::vector<double> features;
  if (feature_engine_.IsReady()) {
    features = feature_engine_.EvaluateBatch(feature_expressions_);
  }
  if (features.empty()) {
    out.enabled = false;
    return out;
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

  // 2. 模型推理
  double raw = 0.0;

#ifdef AI_TRADE_ENABLE_CATBOOST
  if (!g_catboost_lib_handle || !g_catboost_api.resolved || !model_handle_) {
    out.enabled = false;
    return out;
  }
  std::vector<float> float_features(features.begin(), features.end());
  const float* row_ptr = float_features.data();
  double result = 0.0;

  if (!CatBoostCalcPrediction(static_cast<ModelCalcerHandle>(model_handle_),
                              row_ptr,
                              features.size(),
                              &result,
                              1)) {
    LogInfo("INTEGRATOR_ERROR: CatBoost inference failed");
    out.enabled = false;
    return out;
  }
  raw = result;
#else
  out.enabled = false;
  return out;
#endif

  if (config_.log_model_score) {
    std::ostringstream oss;
    oss << "FEATURES: ";
    for (size_t i = 0; i < std::min<size_t>(5, features.size()); ++i) {
      if (i > 0) oss << ", ";
      const std::string feature_name =
          i < feature_names_.size() ? feature_names_[i] : ("f" + std::to_string(i));
      oss << feature_name << "=" << features[i];
    }
    LogInfo(oss.str());
  }

  out.enabled = true;
  out.model_score = std::clamp(raw * config_.score_gain, -6.0, 6.0);
  out.p_up = Sigmoid(out.model_score);
  out.p_down = 1.0 - out.p_up;
  return out;
}

}  // namespace ai_trade
