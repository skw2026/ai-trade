#pragma once

#include <optional>
#include <string>
#include <vector>

#include "core/config.h"
#include "core/types.h"

namespace ai_trade {

/// Gate 窗口检查结果：包含通过状态与失败原因集合。
struct GateWindowResult {
  bool pass{true};
  int raw_signals{0};
  int order_intents{0};
  int effective_signals{0};
  int fills{0};
  std::vector<std::string> fail_reasons;
};

/**
 * @brief Gate 活跃度监控器
 *
 * 作用：
 * 1. 在固定窗口内统计信号漏斗（raw/effective/fills）；
 * 2. 检测“策略装死”或“有信号无成交”等运行异常；
 * 3. 输出告警与窗口检查结果，供上层决策或审计使用。
 */
class GateMonitor {
 public:
  explicit GateMonitor(GateConfig config) : config_(config) {}

  /**
   * @brief 输入一轮决策结果并更新统计
   * @return 心跳告警码（如有），否则 `std::nullopt`
   */
  std::optional<std::string> OnDecision(const Signal& signal,
                                        const RiskAdjustedPosition& adjusted,
                                        const std::optional<OrderIntent>& intent);
  /// 记录成交事件计数。
  void OnFill(const FillEvent& fill);

  /**
   * @brief 行情 tick 驱动窗口推进
   * @return 窗口结束时返回 Gate 判定，否则 `std::nullopt`
   */
  std::optional<GateWindowResult> OnTick();

 private:
  /// 判定是否存在非零净名义敞口。
  static bool HasExposure(double notional_usd);
  /// 重置窗口计数器。
  void ResetWindow();

  GateConfig config_;  ///< Gate 配置快照。
  int tick_in_window_{0};  ///< 当前窗口已累计 tick 数。
  int no_effective_signal_ticks_{0};  ///< 连续无有效信号 tick 数。
  int raw_signals_{0};  ///< 原始信号计数。
  int order_intents_{0};  ///< 订单意图计数。
  int effective_signals_{0};  ///< 有效信号计数（风控后仍有敞口）。
  int fills_{0};  ///< 成交计数。
};

}  // namespace ai_trade
