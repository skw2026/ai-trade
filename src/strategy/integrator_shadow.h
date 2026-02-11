#pragma once

#include <string>

#include "core/config.h"
#include "core/types.h"

namespace ai_trade {

/**
 * @brief Integrator 影子推理器（运行态观测用）
 *
 * 设计目标：
 * 1. 提供统一的 `model_score/p_up/p_down` 可观测输出；
 * 2. 不参与下单决策，仅用于线上诊断与审计；
 * 3. 在模型文件缺失时可安全降级，不影响主流程。
 */
class IntegratorShadow {
 public:
  explicit IntegratorShadow(IntegratorShadowConfig config = {});

  /**
   * @brief 初始化影子推理器
   *
   * 当启用时尝试读取 `integrator_report.json` 中的 `model_version`，
   * 失败由调用方决定是否降级关闭。
   */
  bool Initialize(std::string* out_error);

  /**
   * @brief 执行影子推理
   *
   * 输入当前信号与 Regime，输出 `model_score/p_up/p_down`。
   * 注意：该输出当前不参与订单决策，仅用于观测。
   */
  ShadowInference Infer(const Signal& signal, const RegimeState& regime) const;

  bool enabled() const { return config_.enabled && initialized_; }
  const std::string& model_version() const { return model_version_; }

 private:
  static double Sigmoid(double x);

  IntegratorShadowConfig config_{};
  bool initialized_{false};
  std::string model_version_{"n/a"};
};

}  // namespace ai_trade

