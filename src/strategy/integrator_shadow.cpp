#include "strategy/integrator_shadow.h"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <utility>
#include <vector>

#include "core/json_utils.h"

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

}  // namespace

IntegratorShadow::IntegratorShadow(IntegratorShadowConfig config)
    : config_(std::move(config)) {}

bool IntegratorShadow::Initialize(bool strict_takeover, std::string* out_error) {
  initialized_ = false;
  model_version_ = "n/a";

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

ShadowInference IntegratorShadow::Infer(const Signal& signal,
                                        const RegimeState& regime) const {
  ShadowInference out;
  if (!enabled()) {
    return out;
  }

  out.enabled = true;
  out.model_version = model_version_;

  // 影子分数定义（观测用途）：
  // 1) 信号强度项：将净名义值缩放到可解释区间；
  // 2) Regime 偏置项：趋势桶给予方向先验，极端桶降低置信度；
  // 3) warmup 惩罚：样本不足时收缩到中性概率。
  double raw = std::clamp(signal.suggested_notional_usd / 1000.0, -2.0, 2.0);
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

  out.model_score = std::clamp(raw * config_.score_gain, -6.0, 6.0);
  out.p_up = Sigmoid(out.model_score);
  out.p_down = 1.0 - out.p_up;
  return out;
}

}  // namespace ai_trade
