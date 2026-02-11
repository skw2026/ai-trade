#include "strategy/integrator_shadow.h"

#include <algorithm>
#include <cmath>
#include <fstream>
#include <sstream>
#include <utility>

#include "core/json_utils.h"

namespace ai_trade {

IntegratorShadow::IntegratorShadow(IntegratorShadowConfig config)
    : config_(std::move(config)) {}

bool IntegratorShadow::Initialize(std::string* out_error) {
  initialized_ = false;
  model_version_ = "n/a";

  if (!config_.enabled) {
    initialized_ = true;
    return true;
  }

  std::ifstream input(config_.model_report_path);
  if (!input.is_open()) {
    if (out_error != nullptr) {
      *out_error = "无法打开 integrator 报告: " + config_.model_report_path;
    }
    return false;
  }

  std::ostringstream buffer;
  buffer << input.rdbuf();
  JsonValue root;
  std::string parse_error;
  if (!ParseJson(buffer.str(), &root, &parse_error)) {
    if (out_error != nullptr) {
      *out_error = "integrator 报告解析失败: " + parse_error;
    }
    return false;
  }
  const auto version =
      JsonAsString(JsonObjectField(&root, "model_version"));
  model_version_ = version.value_or(std::string("unknown"));
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
