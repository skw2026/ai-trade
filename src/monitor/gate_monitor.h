#pragma once

#include <optional>
#include <string>
#include <vector>

#include "core/config.h"
#include "core/types.h"

namespace ai_trade {

struct GateWindowResult {
  bool pass{true};
  int raw_signals{0};
  int order_intents{0};
  int effective_signals{0};
  int fills{0};
  std::vector<std::string> fail_reasons;
};

class GateMonitor {
 public:
  explicit GateMonitor(GateConfig config) : config_(config) {}

  // 输入一轮决策结果，更新漏斗计数与信号心跳。
  std::optional<std::string> OnDecision(const Signal& signal,
                                        const RiskAdjustedPosition& adjusted,
                                        const std::optional<OrderIntent>& intent);
  void OnFill(const FillEvent& fill);

  // 每个行情tick调用一次；到达窗口末尾时返回 Gate 判定结果。
  std::optional<GateWindowResult> OnTick();

 private:
  static bool HasExposure(double notional_usd);
  void ResetWindow();

  GateConfig config_;
  int tick_in_window_{0};
  int no_effective_signal_ticks_{0};
  int raw_signals_{0};
  int order_intents_{0};
  int effective_signals_{0};
  int fills_{0};
};

}  // namespace ai_trade
