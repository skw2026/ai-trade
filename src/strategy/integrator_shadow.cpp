#include "strategy/integrator_shadow.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <deque>
#include <filesystem>
#include <fstream>
#include <initializer_list>
#include <limits>
#include <optional>
#if defined(__APPLE__)
#include <CommonCrypto/CommonDigest.h>
#include <mach-o/dyld.h>
#else
#include <openssl/evp.h>
#include <unistd.h>
#endif
#include <sstream>
#include <unordered_map>
#include <unordered_set>
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

constexpr const char* kIntegratorPrimaryObjective =
    "aggregate_model_net_bps_per_unit_turnover_after_cost";

std::string BytesToHex(const unsigned char* bytes, std::size_t size) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string output(size * 2U, '0');
  for (std::size_t i = 0; i < size; ++i) {
    output[i * 2U] = kHex[(bytes[i] >> 4U) & 0x0FU];
    output[i * 2U + 1U] = kHex[bytes[i] & 0x0FU];
  }
  return output;
}

std::optional<std::string> Sha256Bytes(const void* data, std::size_t size) {
#if defined(__APPLE__)
  unsigned char digest[CC_SHA256_DIGEST_LENGTH] = {};
  if (CC_SHA256(data, static_cast<CC_LONG>(size), digest) == nullptr) {
    return std::nullopt;
  }
  return BytesToHex(digest, CC_SHA256_DIGEST_LENGTH);
#else
  EVP_MD_CTX* context = EVP_MD_CTX_new();
  if (context == nullptr) return std::nullopt;
  unsigned char digest[EVP_MAX_MD_SIZE] = {};
  unsigned int digest_size = 0;
  const bool ok =
      EVP_DigestInit_ex(context, EVP_sha256(), nullptr) == 1 &&
      EVP_DigestUpdate(context, data, size) == 1 &&
      EVP_DigestFinal_ex(context, digest, &digest_size) == 1;
  EVP_MD_CTX_free(context);
  if (!ok) return std::nullopt;
  return BytesToHex(digest, digest_size);
#endif
}

std::optional<std::string> Sha256File(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input.is_open()) return std::nullopt;
#if defined(__APPLE__)
  CC_SHA256_CTX context;
  bool ok = CC_SHA256_Init(&context) == 1;
  char buffer[64 * 1024];
  while (ok && input.good()) {
    input.read(buffer, sizeof(buffer));
    const std::streamsize count = input.gcount();
    if (count > 0) {
      ok = CC_SHA256_Update(
               &context, buffer, static_cast<CC_LONG>(count)) == 1;
    }
  }
  ok = ok && input.eof();
  unsigned char digest[CC_SHA256_DIGEST_LENGTH] = {};
  ok = ok && CC_SHA256_Final(digest, &context) == 1;
  if (!ok) return std::nullopt;
  return BytesToHex(digest, CC_SHA256_DIGEST_LENGTH);
#else
  EVP_MD_CTX* context = EVP_MD_CTX_new();
  if (context == nullptr) return std::nullopt;
  bool ok = EVP_DigestInit_ex(context, EVP_sha256(), nullptr) == 1;
  char buffer[64 * 1024];
  while (ok && input.good()) {
    input.read(buffer, sizeof(buffer));
    const std::streamsize count = input.gcount();
    if (count > 0) {
      ok = EVP_DigestUpdate(context, buffer, static_cast<std::size_t>(count)) == 1;
    }
  }
  ok = ok && input.eof();
  unsigned char digest[EVP_MAX_MD_SIZE] = {};
  unsigned int digest_size = 0;
  ok = ok && EVP_DigestFinal_ex(context, digest, &digest_size) == 1;
  EVP_MD_CTX_free(context);
  if (!ok) return std::nullopt;
  return BytesToHex(digest, digest_size);
#endif
}

std::optional<std::string> CurrentExecutablePath() {
#if defined(__APPLE__)
  std::uint32_t size = 0;
  (void)_NSGetExecutablePath(nullptr, &size);
  if (size == 0) return std::nullopt;
  std::vector<char> path(size + 1, '\0');
  if (_NSGetExecutablePath(path.data(), &size) != 0) {
    return std::nullopt;
  }
  std::error_code ec;
  const auto canonical = std::filesystem::weakly_canonical(path.data(), ec);
  return ec ? std::optional<std::string>(path.data())
            : std::optional<std::string>(canonical.string());
#else
  std::vector<char> path(4096, '\0');
  const ssize_t count =
      readlink("/proc/self/exe", path.data(), path.size() - 1);
  if (count <= 0) return std::nullopt;
  path[static_cast<std::size_t>(count)] = '\0';
  return std::string(path.data());
#endif
}

std::string TrimCopy(const std::string& text) {
  std::size_t begin = 0;
  while (begin < text.size() &&
         std::isspace(static_cast<unsigned char>(text[begin])) != 0) {
    ++begin;
  }
  std::size_t end = text.size();
  while (end > begin &&
         std::isspace(static_cast<unsigned char>(text[end - 1])) != 0) {
    --end;
  }
  return text.substr(begin, end - begin);
}

std::string ToUpperCopy(const std::string& text) {
  std::string out = TrimCopy(text);
  std::transform(out.begin(), out.end(), out.begin(), [](unsigned char ch) {
    return static_cast<char>(std::toupper(ch));
  });
  return out;
}

std::vector<std::string> SplitCsvLine(const std::string& line) {
  std::vector<std::string> fields;
  std::string field;
  bool quoted = false;
  for (std::size_t i = 0; i < line.size(); ++i) {
    const char ch = line[i];
    if (ch == '"') {
      if (quoted && i + 1 < line.size() && line[i + 1] == '"') {
        field.push_back('"');
        ++i;
      } else {
        quoted = !quoted;
      }
    } else if (ch == ',' && !quoted) {
      fields.push_back(TrimCopy(field));
      field.clear();
    } else {
      field.push_back(ch);
    }
  }
  fields.push_back(TrimCopy(field));
  return fields;
}

bool ParseFiniteDouble(const std::string& text, double* out) {
  if (out == nullptr) {
    return false;
  }
  try {
    std::size_t consumed = 0;
    const double value = std::stod(TrimCopy(text), &consumed);
    if (consumed != TrimCopy(text).size() || !std::isfinite(value)) {
      return false;
    }
    *out = value;
    return true;
  } catch (const std::exception&) {
    return false;
  }
}

bool ParsePositiveTimestamp(const std::string& text, std::int64_t* out) {
  if (out == nullptr) {
    return false;
  }
  try {
    std::size_t consumed = 0;
    const std::string normalized = TrimCopy(text);
    const std::int64_t value = std::stoll(normalized, &consumed);
    if (consumed != normalized.size() || value <= 0) {
      return false;
    }
    *out = value;
    return true;
  } catch (const std::exception&) {
    return false;
  }
}

std::int64_t CurrentTimestampMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

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

std::string MinerFeatureKey(int idx) {
  std::ostringstream oss;
  oss << "miner_";
  if (idx < 10) {
    oss << "0";
  }
  oss << idx;
  return oss.str();
}

bool LoadMinerExpressionsFromReport(
    const std::filesystem::path& report_path,
    std::unordered_map<std::string, std::string>* out_expressions,
    std::string* out_error) {
  if (out_expressions == nullptr) {
    if (out_error != nullptr) {
      *out_error = "out_expressions 为空";
    }
    return false;
  }
  std::ifstream miner_input(report_path);
  if (!miner_input.is_open()) {
    if (out_error != nullptr) {
      *out_error = "无法打开文件";
    }
    return false;
  }
  std::ostringstream miner_buffer;
  miner_buffer << miner_input.rdbuf();
  JsonValue miner_root;
  std::string miner_err;
  if (!ParseJson(miner_buffer.str(), &miner_root, &miner_err)) {
    if (out_error != nullptr) {
      *out_error = "JSON 解析失败: " + miner_err;
    }
    return false;
  }

  const JsonValue* factors = JsonObjectField(&miner_root, "factors");
  if (factors == nullptr || factors->type != JsonType::kArray) {
    if (out_error != nullptr) {
      *out_error = "缺少 factors 数组";
    }
    return false;
  }

  out_expressions->clear();
  int idx = 0;
  for (const auto& factor : factors->array_value) {
    auto expr = JsonAsString(JsonObjectField(&factor, "expression"));
    auto invert = JsonAsBool(JsonObjectField(&factor, "invert_signal"));
    if (expr.has_value() && !expr->empty()) {
      std::string final_expr = *expr;
      if (invert.value_or(false)) {
        final_expr = "-(" + final_expr + ")";
      }
      (*out_expressions)[MinerFeatureKey(idx)] = std::move(final_expr);
    }
    ++idx;
  }
  if (out_expressions->empty()) {
    if (out_error != nullptr) {
      *out_error = "factors 为空或表达式缺失";
    }
    return false;
  }
  return true;
}

// 将经典特征名映射为 OnlineFeatureEngine 支持的表达式
std::optional<std::string> MapClassicFeatureToExpression(const std::string& name) {
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
  return std::nullopt;
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
  activation_transaction_id_.clear();
  runtime_config_sha256_.clear();
  trade_bot_sha256_.clear();
  expected_net_edge_available_ = false;
  expected_net_edge_per_trade_bps_ = 0.0;
  training_symbol_.clear();
  training_csv_path_.clear();
  feature_bar_interval_ms_ = 0;
  last_observed_market_ts_ms_ = 0;
  last_completed_bar_ts_ms_ = 0;
  feature_names_.clear();
  feature_expressions_.clear();
  feature_clipping_enabled_ = false;
  feature_clip_lower_.clear();
  feature_clip_upper_.clear();
  feature_normalization_enabled_ = false;
  feature_norm_center_.clear();
  feature_norm_scale_.clear();
  feature_norm_max_abs_.clear();
  model_runtime_ready_ = false;

  if (!config_.enabled) {
    initialized_ = true;
    return true;
  }

  const bool candidate_validation =
      strict_takeover && config_.candidate_validation_mode;
  const bool require_model_file = strict_takeover || config_.require_model_file;
  const bool require_active_meta =
      (strict_takeover && !candidate_validation) || config_.require_active_meta;
  const bool require_active_gate_pass =
      !candidate_validation && (strict_takeover || config_.require_gate_pass);
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
  const std::string report_json = buffer.str();
  const auto report_sha256 =
      Sha256Bytes(report_json.data(), report_json.size());
  JsonValue root;
  std::string parse_error;
  if (!ParseJson(report_json, &root, &parse_error)) {
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

  feature_clip_lower_.assign(feature_names_.size(),
                             -std::numeric_limits<double>::infinity());
  feature_clip_upper_.assign(feature_names_.size(),
                             std::numeric_limits<double>::infinity());
  feature_norm_center_.assign(feature_names_.size(), 0.0);
  feature_norm_scale_.assign(feature_names_.size(), 1.0);
  feature_norm_max_abs_.assign(feature_names_.size(),
                               std::numeric_limits<double>::infinity());
  const JsonValue* feature_transform = JsonObjectField(&root, "feature_transform");
  const auto transform_enabled =
      JsonAsBool(JsonObjectField(feature_transform, "feature_clipping_enabled"));
  const auto normalization_enabled =
      JsonAsBool(JsonObjectField(feature_transform, "feature_normalization_enabled"));
  const auto normalization_max_abs =
      JsonAsNumber(JsonObjectField(feature_transform, "normalization_max_abs"));
  const JsonValue* clip_bounds = JsonObjectField(feature_transform, "clip_bounds");
  if ((transform_enabled.value_or(false) || normalization_enabled.value_or(false)) &&
      clip_bounds != nullptr &&
      clip_bounds->type == JsonType::kArray && !feature_names_.empty()) {
    std::unordered_map<std::string, size_t> feature_index;
    for (size_t i = 0; i < feature_names_.size(); ++i) {
      feature_index[feature_names_[i]] = i;
    }
    int loaded_clip_bounds = 0;
    int loaded_normalization_bounds = 0;
    for (const auto& item : clip_bounds->array_value) {
      const auto name = JsonAsString(JsonObjectField(&item, "feature"));
      if (!name.has_value()) {
        continue;
      }
      const auto it = feature_index.find(*name);
      if (it == feature_index.end()) {
        continue;
      }
      const size_t index = it->second;
      const auto enabled = JsonAsBool(JsonObjectField(&item, "enabled"));
      const auto lower = JsonAsNumber(JsonObjectField(&item, "lower"));
      const auto upper = JsonAsNumber(JsonObjectField(&item, "upper"));
      if (enabled.value_or(false) && lower.has_value() && upper.has_value() &&
          std::isfinite(*lower) && std::isfinite(*upper) && *lower <= *upper) {
        feature_clip_lower_[index] = *lower;
        feature_clip_upper_[index] = *upper;
        ++loaded_clip_bounds;
      }
      const auto norm_enabled =
          JsonAsBool(JsonObjectField(&item, "normalization_enabled"));
      const auto center = JsonAsNumber(JsonObjectField(&item, "center"));
      const auto scale = JsonAsNumber(JsonObjectField(&item, "scale"));
      const auto item_max_abs = JsonAsNumber(JsonObjectField(&item, "normalized_max_abs"));
      const double max_abs =
          (item_max_abs.has_value() && std::isfinite(*item_max_abs) && *item_max_abs > 0.0)
              ? *item_max_abs
              : (normalization_max_abs.has_value() && std::isfinite(*normalization_max_abs) &&
                         *normalization_max_abs > 0.0
                     ? *normalization_max_abs
                     : std::numeric_limits<double>::infinity());
      if (norm_enabled.value_or(false) && center.has_value() && scale.has_value() &&
          std::isfinite(*center) && std::isfinite(*scale) && *scale > 1e-12) {
        feature_norm_center_[index] = *center;
        feature_norm_scale_[index] = *scale;
        feature_norm_max_abs_[index] = max_abs;
        ++loaded_normalization_bounds;
      }
    }
    feature_clipping_enabled_ = loaded_clip_bounds > 0;
    feature_normalization_enabled_ = loaded_normalization_bounds > 0;
    if (feature_clipping_enabled_) {
      LogInfo("INTEGRATOR_FEATURE_TRANSFORM_LOADED: clip_bounds=" +
              std::to_string(loaded_clip_bounds) + "/" +
              std::to_string(feature_names_.size()));
    }
    if (feature_normalization_enabled_) {
      LogInfo("INTEGRATOR_FEATURE_NORMALIZATION_LOADED: normalization_bounds=" +
              std::to_string(loaded_normalization_bounds) + "/" +
              std::to_string(feature_names_.size()));
    }
  }

  // 2. 获取 Miner 报告路径
  std::string miner_report_path;
  const JsonValue* data_section = JsonObjectField(&root, "data");
  if (auto path = JsonAsString(JsonObjectField(data_section, "miner_report_path")); path.has_value()) {
    miner_report_path = *path;
  }

  std::vector<std::string> quality_failures;
  if (const auto symbol =
          JsonAsString(JsonObjectField(data_section, "training_symbol"));
      symbol.has_value()) {
    training_symbol_ = ToUpperCopy(*symbol);
  }
  if (const auto path = JsonAsString(JsonObjectField(data_section, "csv_path"));
      path.has_value()) {
    training_csv_path_ = TrimCopy(*path);
  }
  if (const auto interval =
          JsonAsNumber(JsonObjectField(data_section, "bar_interval_ms"));
      interval.has_value() && std::isfinite(*interval) && *interval > 0.0) {
    feature_bar_interval_ms_ =
        static_cast<std::int64_t>(std::llround(*interval));
  }
  bool legacy_feature_contract_used = false;
  if (training_symbol_.empty() &&
      config_.allow_legacy_feature_contract) {
    training_symbol_ = ToUpperCopy(config_.legacy_training_symbol);
    legacy_feature_contract_used = true;
  }
  if (training_symbol_.empty()) {
    quality_failures.push_back("data.training_symbol 缺失");
  }
  if (training_csv_path_.empty()) {
    quality_failures.push_back("data.csv_path 缺失");
  }
  if (feature_bar_interval_ms_ <= 0 &&
      config_.allow_legacy_feature_contract) {
    feature_bar_interval_ms_ = config_.legacy_bar_interval_ms;
    legacy_feature_contract_used = true;
  }
  if (feature_bar_interval_ms_ <= 0) {
    quality_failures.push_back("data.bar_interval_ms 必须 > 0");
  }
  const auto online_bar_source =
      JsonAsString(JsonObjectField(data_section, "online_bar_source"));
  if ((!online_bar_source.has_value() ||
       *online_bar_source != "closed_ohlcv") &&
      !config_.allow_legacy_feature_contract) {
    quality_failures.push_back(
        "data.online_bar_source 必须为 closed_ohlcv");
  }
  const auto source_venue =
      JsonAsString(JsonObjectField(data_section, "source_venue"));
  const auto source_category =
      JsonAsString(JsonObjectField(data_section, "source_category"));
  const auto price_type =
      JsonAsString(JsonObjectField(data_section, "price_type"));
  const auto volume_unit =
      JsonAsString(JsonObjectField(data_section, "volume_unit"));
  if (!source_venue.has_value() || *source_venue != "bybit") {
    quality_failures.push_back("data.source_venue 必须为 bybit");
  }
  if (!source_category.has_value() || *source_category != "linear") {
    quality_failures.push_back("data.source_category 必须为 linear");
  }
  if (!price_type.has_value() || *price_type != "trade_price") {
    quality_failures.push_back("data.price_type 必须为 trade_price");
  }
  if (!volume_unit.has_value() || *volume_unit != "base_asset") {
    quality_failures.push_back("data.volume_unit 必须为 base_asset");
  }
  if (legacy_feature_contract_used) {
    LogInfo("INTEGRATOR_LEGACY_FEATURE_CONTRACT: training_symbol=" +
            training_symbol_ + ", bar_interval_ms=" +
            std::to_string(feature_bar_interval_ms_) +
            ", model_version=" + model_version_);
  }
  feature_engine_.Reset(feature_bar_interval_ms_);

  const JsonValue* metrics = JsonObjectField(&root, "metrics_oos");
  if (metrics == nullptr || metrics->type != JsonType::kObject) {
    quality_failures.push_back("缺少 metrics_oos");
  } else {
    const auto primary_objective =
        JsonAsString(JsonObjectField(metrics, "primary_objective"));
    auto auc_mean = JsonAsNumber(JsonObjectField(metrics, "auc_mean"));
    auto delta_auc =
        JsonAsNumber(JsonObjectField(metrics, "delta_auc_vs_baseline"));
    auto split_trained_count =
        JsonAsNumber(JsonObjectField(metrics, "split_trained_count"));
    auto split_count = JsonAsNumber(JsonObjectField(metrics, "split_count"));
    auto expected_net_edge_per_trade = JsonAsNumber(
        JsonObjectField(metrics, "mean_model_net_edge_bps_per_round_trip"));
    if (expected_net_edge_per_trade.has_value() &&
        std::isfinite(*expected_net_edge_per_trade)) {
      expected_net_edge_available_ = true;
      expected_net_edge_per_trade_bps_ = *expected_net_edge_per_trade;
    } else {
      quality_failures.push_back(
          "缺少 metrics_oos.mean_model_net_edge_bps_per_round_trip");
    }
    if (!primary_objective.has_value() ||
        *primary_objective != kIntegratorPrimaryObjective) {
      quality_failures.push_back(
          "metrics_oos.primary_objective 必须为 "
          + std::string(kIntegratorPrimaryObjective));
    }

    if (!auc_mean.has_value() || !std::isfinite(*auc_mean)) {
      LogInfo("INTEGRATOR_QUALITY_DIAGNOSTIC: missing metrics_oos.auc_mean");
    } else if (*auc_mean < config_.min_auc_mean) {
      LogInfo("INTEGRATOR_QUALITY_DIAGNOSTIC: auc_mean=" +
              std::to_string(*auc_mean) + " < min_auc_mean=" +
              std::to_string(config_.min_auc_mean));
    }

    if (!delta_auc.has_value() || !std::isfinite(*delta_auc)) {
      LogInfo(
          "INTEGRATOR_QUALITY_DIAGNOSTIC: missing "
          "metrics_oos.delta_auc_vs_baseline");
    } else if (*delta_auc < config_.min_delta_auc_vs_baseline) {
      LogInfo("INTEGRATOR_QUALITY_DIAGNOSTIC: delta_auc_vs_baseline=" +
              std::to_string(*delta_auc) +
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

  const JsonValue* governance = JsonObjectField(&root, "governance");
  const auto governance_pass =
      JsonAsBool(JsonObjectField(governance, "pass"));
  const auto governance_primary_objective =
      JsonAsString(JsonObjectField(governance, "primary_objective"));
  if (!governance_pass.value_or(false)) {
    quality_failures.push_back("governance.pass 必须为 true");
  }
  if (!governance_primary_objective.has_value() ||
      *governance_primary_objective != kIntegratorPrimaryObjective) {
    quality_failures.push_back(
        "governance.primary_objective 必须为 "
        + std::string(kIntegratorPrimaryObjective));
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
  }
  const auto model_sha256_before_load =
      model_file_ok ? Sha256File(config_.model_path) : std::nullopt;
  if (model_file_ok && !model_sha256_before_load.has_value()) {
    return fail("integrator 模型加载前 SHA-256 计算失败: " +
                config_.model_path);
  }
  if (model_file_ok) {
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
  const auto model_sha256 =
      model_file_ok ? Sha256File(config_.model_path) : std::nullopt;
  if (model_file_ok && !model_sha256.has_value()) {
    return fail("integrator 模型 SHA-256 计算失败: " + config_.model_path);
  }
  if (model_file_ok &&
      *model_sha256_before_load != *model_sha256) {
    return fail("integrator 模型文件在加载期间发生变化: " +
                config_.model_path);
  }
  if (!report_sha256.has_value()) {
    return fail("integrator 报告 SHA-256 计算失败: " +
                config_.model_report_path);
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
      const auto expected_model_sha256 =
          JsonAsString(JsonObjectField(&active_root, "model_sha256"));
      const auto expected_report_sha256 =
          JsonAsString(JsonObjectField(&active_root, "report_sha256"));
      const auto expected_runtime_config_sha256 =
          JsonAsString(JsonObjectField(&active_root, "runtime_config_sha256"));
      const auto expected_trade_bot_sha256 =
          JsonAsString(JsonObjectField(&active_root, "trade_bot_sha256"));
      const JsonValue* activation_transaction =
          JsonObjectField(&active_root, "activation_transaction");
      const auto activation_transaction_id =
          JsonAsString(JsonObjectField(activation_transaction, "run_id"));
      if (require_active_meta) {
        if (!expected_model_sha256.has_value() ||
            expected_model_sha256->empty() || !model_sha256.has_value() ||
            *expected_model_sha256 != *model_sha256) {
          return fail("active_meta.model_sha256 与实际模型文件不一致");
        }
        if (!expected_report_sha256.has_value() ||
            expected_report_sha256->empty() ||
            *expected_report_sha256 != *report_sha256) {
          return fail("active_meta.report_sha256 与实际报告文件不一致");
        }
        const auto runtime_config_sha256 =
            config_.runtime_config_path.empty()
                ? std::nullopt
                : Sha256File(config_.runtime_config_path);
        if (!expected_runtime_config_sha256.has_value() ||
            expected_runtime_config_sha256->empty() ||
            !runtime_config_sha256.has_value() ||
            *expected_runtime_config_sha256 != *runtime_config_sha256) {
          return fail(
              "active_meta.runtime_config_sha256 与实际运行配置不一致");
        }
        const auto executable_path = CurrentExecutablePath();
        const auto trade_bot_sha256 =
            executable_path.has_value()
                ? Sha256File(*executable_path)
                : std::nullopt;
        if (!expected_trade_bot_sha256.has_value() ||
            expected_trade_bot_sha256->empty() ||
            !trade_bot_sha256.has_value() ||
            *expected_trade_bot_sha256 != *trade_bot_sha256) {
          return fail("active_meta.trade_bot_sha256 与当前进程不一致");
        }
        if (!activation_transaction_id.has_value() ||
            activation_transaction_id->empty()) {
          return fail("active_meta.activation_transaction.run_id 缺失");
        }
        activation_transaction_id_ = *activation_transaction_id;
        runtime_config_sha256_ = *runtime_config_sha256;
        trade_bot_sha256_ = *trade_bot_sha256;
        LogInfo(
            "INTEGRATOR_RUNTIME_IDENTITY: runtime_config_sha256=" +
            *runtime_config_sha256 + ", trade_bot_sha256=" +
            *trade_bot_sha256 + ", activation_transaction_id=" +
            activation_transaction_id_);
      }

      if (require_active_gate_pass) {
        const JsonValue* gate = JsonObjectField(&active_root, "gate");
        const auto gate_pass = JsonAsBool(JsonObjectField(gate, "pass"));
        if (!gate_pass.value_or(false)) {
          return fail("active_meta.gate.pass != true，不允许进入接管模式");
        }
      }
    } else if (require_active_meta || require_active_gate_pass) {
      return fail("缺少 integrator active_meta: " + config_.active_meta_path);
    }
  } else if (require_active_meta || require_active_gate_pass) {
    return fail("integrator.active_meta_path 为空");
  }

  if (require_active_meta && !active_meta_found) {
    return fail("require_active_meta=true 但未找到 active_meta");
  }
  if (model_sha256.has_value()) {
    LogInfo("INTEGRATOR_ARTIFACT_IDENTITY: model_version=" + model_version_ +
            ", model_sha256=" + *model_sha256 +
            ", report_sha256=" + *report_sha256);
  }

  // 3. 加载 Miner 报告并构建特征表达式映射
  std::unordered_map<std::string, std::string> miner_expressions;
  std::vector<std::string> miner_candidate_paths;
  std::vector<std::string> miner_load_errors;
  std::string resolved_miner_report_path;
  std::unordered_set<std::string> miner_candidate_dedup;
  auto append_candidate = [&](const std::filesystem::path& candidate) {
    if (candidate.empty()) {
      return;
    }
    const std::string normalized = candidate.lexically_normal().string();
    if (normalized.empty()) {
      return;
    }
    if (miner_candidate_dedup.insert(normalized).second) {
      miner_candidate_paths.push_back(normalized);
    }
  };

  // 优先采用 integrator_report 记录的路径。
  if (!miner_report_path.empty()) {
    const std::filesystem::path raw_path(miner_report_path);
    append_candidate(raw_path);
    // 如果是相对路径，同时尝试“相对 model_report 所在目录”的解析。
    if (raw_path.is_relative() && !config_.model_report_path.empty()) {
      const std::filesystem::path report_dir =
          std::filesystem::path(config_.model_report_path).parent_path();
      if (!report_dir.empty()) {
        append_candidate(report_dir / raw_path);
      }
    }
  }
  // 兼容路径 1：和 active report 同目录。
  if (!config_.model_report_path.empty()) {
    const std::filesystem::path report_dir =
        std::filesystem::path(config_.model_report_path).parent_path();
    if (!report_dir.empty()) {
      append_candidate(report_dir / "miner_report.json");
    }
  }
  // 兼容路径 2：固定稳定路径（激活时由 model_registry 同步）。
  append_candidate(std::filesystem::path("./data/research/miner_report.json"));

  for (const auto& candidate : miner_candidate_paths) {
    std::string load_error;
    if (!LoadMinerExpressionsFromReport(candidate, &miner_expressions, &load_error)) {
      if (!load_error.empty()) {
        miner_load_errors.push_back(candidate + ": " + load_error);
      }
      continue;
    }
    resolved_miner_report_path = candidate;
    break;
  }
  if (!resolved_miner_report_path.empty()) {
    LogInfo("INTEGRATOR_MINER_REPORT_RESOLVED: path=" + resolved_miner_report_path +
            ", expression_count=" + std::to_string(miner_expressions.size()));
  }

  // 4. 构建最终的表达式列表
  std::vector<std::string> missing_miner_features;
  for (const auto& name : feature_names_) {
    if (name.rfind("miner_", 0) == 0) {
      auto it = miner_expressions.find(name);
      if (it == miner_expressions.end() || it->second.empty()) {
        missing_miner_features.push_back(name);
        continue;
      }
      feature_expressions_.push_back(it->second);
    } else {
      const auto expression = MapClassicFeatureToExpression(name);
      if (!expression.has_value() || expression->empty()) {
        return fail("integrator 经典特征不受支持: " + name);
      }
      feature_expressions_.push_back(*expression);
    }
  }
  if (!missing_miner_features.empty()) {
    std::ostringstream oss;
    oss << "integrator 特征映射缺失: " << JoinReasons(missing_miner_features);
    if (!resolved_miner_report_path.empty()) {
      oss << ", resolved_miner_report=" << resolved_miner_report_path;
    } else {
      oss << ", miner_report_candidates=" << JoinReasons(miner_candidate_paths);
    }
    if (!miner_load_errors.empty()) {
      oss << ", load_errors=" << JoinReasons(miner_load_errors);
    }
    return fail(oss.str());
  }

  initialized_ = true;
  return true;
}

bool IntegratorShadow::BootstrapHistory(std::string* out_error) {
  if (!enabled()) {
    if (out_error != nullptr) {
      *out_error = "integrator 未初始化";
    }
    return false;
  }
  if (training_csv_path_.empty() || training_symbol_.empty() ||
      feature_bar_interval_ms_ <= 0) {
    if (out_error != nullptr) {
      *out_error = "integrator 训练数据契约不完整";
    }
    return false;
  }

  std::ifstream input(training_csv_path_);
  if (!input.is_open()) {
    if (out_error != nullptr) {
      *out_error = "无法打开 integrator 训练 CSV: " + training_csv_path_;
    }
    return false;
  }
  std::string header_line;
  if (!std::getline(input, header_line)) {
    if (out_error != nullptr) {
      *out_error = "integrator 训练 CSV 为空: " + training_csv_path_;
    }
    return false;
  }
  const auto headers = SplitCsvLine(header_line);
  std::unordered_map<std::string, std::size_t> header_index;
  for (std::size_t i = 0; i < headers.size(); ++i) {
    std::string key = TrimCopy(headers[i]);
    std::transform(key.begin(), key.end(), key.begin(), [](unsigned char ch) {
      return static_cast<char>(std::tolower(ch));
    });
    header_index[key] = i;
  }
  const auto find_index = [&](const char* name) -> std::optional<std::size_t> {
    const auto it = header_index.find(name);
    return it == header_index.end() ? std::nullopt
                                    : std::optional<std::size_t>(it->second);
  };
  const auto timestamp_idx = find_index("timestamp");
  const auto open_idx = find_index("open");
  const auto high_idx = find_index("high");
  const auto low_idx = find_index("low");
  const auto close_idx = find_index("close");
  const auto volume_idx = find_index("volume");
  if (!timestamp_idx || !open_idx || !high_idx || !low_idx || !close_idx ||
      !volume_idx) {
    if (out_error != nullptr) {
      *out_error =
          "integrator 训练 CSV 缺少 timestamp/open/high/low/close/volume";
    }
    return false;
  }

  struct Bar {
    std::int64_t ts_ms{0};
    double open{0.0};
    double high{0.0};
    double low{0.0};
    double close{0.0};
    double volume{0.0};
  };
  const std::size_t required_samples =
      static_cast<std::size_t>(std::max(1, config_.feature_window_ticks));
  std::deque<Bar> recent;
  const std::int64_t now_ms = CurrentTimestampMs();
  std::string line;
  while (std::getline(input, line)) {
    if (TrimCopy(line).empty()) {
      continue;
    }
    const auto fields = SplitCsvLine(line);
    const auto field = [&](std::size_t index) -> std::string {
      return index < fields.size() ? fields[index] : std::string();
    };
    Bar bar;
    if (!ParsePositiveTimestamp(field(*timestamp_idx), &bar.ts_ms) ||
        !ParseFiniteDouble(field(*open_idx), &bar.open) ||
        !ParseFiniteDouble(field(*high_idx), &bar.high) ||
        !ParseFiniteDouble(field(*low_idx), &bar.low) ||
        !ParseFiniteDouble(field(*close_idx), &bar.close) ||
        !ParseFiniteDouble(field(*volume_idx), &bar.volume)) {
      continue;
    }
    // CSV timestamp 是 bar open；当前未闭合 bar 不得用于生产预热。
    if (bar.ts_ms + feature_bar_interval_ms_ > now_ms) {
      continue;
    }
    recent.push_back(bar);
    if (recent.size() > required_samples) {
      recent.pop_front();
    }
  }
  if (recent.size() < required_samples) {
    if (out_error != nullptr) {
      *out_error = "integrator 历史预热样本不足: samples=" +
                   std::to_string(recent.size()) +
                   ", required=" + std::to_string(required_samples);
    }
    return false;
  }
  feature_engine_.Reset(feature_bar_interval_ms_);
  for (const auto& bar : recent) {
    feature_engine_.AddCompletedBar(
        bar.open, bar.high, bar.low, bar.close, bar.volume);
    last_completed_bar_ts_ms_ = bar.ts_ms;
  }
  LogInfo("INTEGRATOR_HISTORY_BOOTSTRAP: model_version=" + model_version_ +
          ", training_symbol=" + training_symbol_ +
          ", bar_interval_ms=" + std::to_string(feature_bar_interval_ms_) +
          ", samples=" + std::to_string(feature_engine_.SampleCount()) +
          ", last_bar_ts_ms=" + std::to_string(last_completed_bar_ts_ms_) +
          ", csv_path=" + training_csv_path_);
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
  if (!training_symbol_.empty() &&
      ToUpperCopy(event.symbol) != training_symbol_) {
    return;
  }
  last_observed_market_ts_ms_ =
      std::max(last_observed_market_ts_ms_, event.ts_ms);
  const bool has_explicit_ohlc =
      std::isfinite(event.open_price) && event.open_price > 0.0 &&
      std::isfinite(event.high_price) && event.high_price > 0.0 &&
      std::isfinite(event.low_price) && event.low_price > 0.0;
  if (feature_bar_interval_ms_ > 0) {
    if (!has_explicit_ohlc ||
        std::llabs(event.interval_ms - feature_bar_interval_ms_) > 1 ||
        event.ts_ms <= last_completed_bar_ts_ms_) {
      return;
    }
  }
  feature_engine_.OnMarket(event);
  if (has_explicit_ohlc) {
    last_completed_bar_ts_ms_ = event.ts_ms;
  }
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
  if (!training_symbol_.empty() &&
      ToUpperCopy(signal.symbol) != training_symbol_) {
    out.enabled = false;
    return out;
  }
  if (feature_bar_interval_ms_ > 0 &&
      (last_completed_bar_ts_ms_ <= 0 ||
       last_observed_market_ts_ms_ - last_completed_bar_ts_ms_ >
           feature_bar_interval_ms_ * 2)) {
    if (config_.log_model_score) {
      static int stale_log_counter = 0;
      if (stale_log_counter++ % 100 == 0) {
        LogInfo("INTEGRATOR_FEATURE_STALE: training_symbol=" +
                training_symbol_ + ", last_bar_ts_ms=" +
                std::to_string(last_completed_bar_ts_ms_) +
                ", observed_ts_ms=" +
                std::to_string(last_observed_market_ts_ms_) +
                ", bar_interval_ms=" +
                std::to_string(feature_bar_interval_ms_) +
                ", model_version=" + model_version_);
      }
    }
    out.enabled = false;
    return out;
  }
  if (!model_runtime_ready_ || model_handle_ == nullptr) {
    out.enabled = false;
    return out;
  }

  // 1. 计算特征向量
  std::vector<double> features;
  const std::size_t warmup_ticks =
      static_cast<std::size_t>(std::max(1, config_.feature_window_ticks));
  if (feature_engine_.SampleCount() < warmup_ticks) {
    if (config_.log_model_score) {
      static int warmup_log_counter = 0;
      if (warmup_log_counter++ % 100 == 0) {
        LogInfo("INTEGRATOR_WARMUP: samples=" +
                std::to_string(feature_engine_.SampleCount()) +
                ", required=" + std::to_string(warmup_ticks) +
                ", model_version=" + model_version_);
      }
    }
    out.enabled = false;
    return out;
  }
  if (feature_engine_.IsReady()) {
    features = feature_engine_.EvaluateBatch(feature_expressions_);
  }
  if (features.empty()) {
    out.enabled = false;
    return out;
  }

  // 关键防御：检查特征向量是否存在 NaN/Inf。
  // live 特征偶发非有限值时不再整次跳过推理；先归零并打审计日志，
  // 避免少量 miner/live 特征错位把 Integrator 长期锁死为 unavailable。
  int sanitized_count = 0;
  for (size_t i = 0; i < features.size(); ++i) {
    if (!std::isfinite(features[i])) {
      static int sanitize_warn_counter = 0;
      const int sanitize_count = ++sanitize_warn_counter;
      const double raw_value = features[i];
      features[i] = 0.0;
      ++sanitized_count;
      // 限频日志：预热期可能连续 NaN，但前几个样本必须完整暴露定位信息。
      if (config_.log_model_score &&
          (sanitize_count <= 10 || sanitize_count % 100 == 0)) {
        const std::string feature_name =
            i < feature_names_.size() ? feature_names_[i] : "unknown";
        std::ostringstream oss;
        oss << "INTEGRATOR_FEATURE_SANITIZED: nonfinite feature"
            << ", sanitize_count=" << sanitize_count
            << ", symbol=" << signal.symbol
            << ", regime=" << ToString(regime.regime)
            << ", bucket=" << ToString(regime.bucket)
            << ", raw_regime=" << ToString(regime.raw_regime)
            << ", raw_bucket=" << ToString(regime.raw_bucket)
            << ", feature_index=" << i
            << ", feature_name=" << feature_name
            << ", raw_value=" << raw_value
            << ", sanitized_value=0"
            << ", model_version=" << model_version_;
        LogInfo(oss.str());
      }
    }
  }
  if (config_.log_model_score && sanitized_count > 0) {
    static int sanitized_summary_counter = 0;
    if (sanitized_summary_counter++ % 100 == 0) {
      LogInfo("INTEGRATOR_FEATURE_SANITIZE_SUMMARY: sanitized=" +
              std::to_string(sanitized_count) + "/" +
              std::to_string(features.size()));
    }
  }
  if (feature_clipping_enabled_ && feature_clip_lower_.size() == features.size() &&
      feature_clip_upper_.size() == features.size()) {
    int clipped_count = 0;
    for (size_t i = 0; i < features.size(); ++i) {
      const double lower = feature_clip_lower_[i];
      const double upper = feature_clip_upper_[i];
      if (!std::isfinite(lower) || !std::isfinite(upper) || lower > upper) {
        continue;
      }
      const double before = features[i];
      features[i] = std::clamp(features[i], lower, upper);
      if (features[i] != before) {
        ++clipped_count;
      }
    }
    if (config_.log_model_score && clipped_count > 0) {
      static int clip_warn_counter = 0;
      if (clip_warn_counter++ % 100 == 0) {
        LogInfo("INTEGRATOR_FEATURE_CLIP: clipped=" + std::to_string(clipped_count) +
                "/" + std::to_string(features.size()));
      }
    }
  }
  if (feature_normalization_enabled_ &&
      feature_norm_center_.size() == features.size() &&
      feature_norm_scale_.size() == features.size() &&
      feature_norm_max_abs_.size() == features.size()) {
    int normalized_count = 0;
    for (size_t i = 0; i < features.size(); ++i) {
      const double center = feature_norm_center_[i];
      const double scale = feature_norm_scale_[i];
      if (!std::isfinite(center) || !std::isfinite(scale) || scale <= 1e-12) {
        continue;
      }
      double normalized = (features[i] - center) / scale;
      const double max_abs = feature_norm_max_abs_[i];
      if (std::isfinite(max_abs) && max_abs > 0.0) {
        normalized = std::clamp(normalized, -max_abs, max_abs);
      }
      features[i] = normalized;
      ++normalized_count;
    }
    if (config_.log_model_score && normalized_count > 0) {
      static int normalize_warn_counter = 0;
      if (normalize_warn_counter++ % 100 == 0) {
        LogInfo("INTEGRATOR_FEATURE_NORMALIZE: normalized=" +
                std::to_string(normalized_count) + "/" +
                std::to_string(features.size()));
      }
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
  out.expected_net_edge_available = expected_net_edge_available_;
  out.expected_net_edge_per_trade_bps =
      expected_net_edge_per_trade_bps_;
  return out;
}

}  // namespace ai_trade
