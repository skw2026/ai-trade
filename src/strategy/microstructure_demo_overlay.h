#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include "core/config.h"
#include "core/types.h"

namespace ai_trade {

// Reads the credential-free Python policy sidecar output.  The sidecar owns
// order-book feature construction/model inference; this class owns the final
// lifecycle/artifact/staleness checks before the risk/execution pipeline.
class MicrostructureDemoOverlay {
 public:
  explicit MicrostructureDemoOverlay(MicrostructureDemoConfig config);

  ShadowInference Infer(const MarketEvent& event);
  bool configured() const { return config_.enabled; }
  bool candidate_ready() const { return candidate_ready_; }
  const std::string& candidate_id() const { return candidate_id_; }

 private:
  bool RefreshLifecycle(std::int64_t now_ms, std::string* out_error);
  ShadowInference FailClosed(const std::string& reason) const;
  void LogAccepted(const std::string& status, int direction,
                   double predicted_net_edge_bps) const;

  MicrostructureDemoConfig config_;
  bool candidate_ready_{false};
  bool candidate_was_ready_{false};
  std::int64_t last_refresh_epoch_ms_{0};
  std::string candidate_id_;
  std::string lifecycle_state_sha256_;
  std::string model_sha256_;
  std::string development_report_sha256_;
  double policy_threshold_bps_{0.0};
  int execution_latency_seconds_{0};
  std::vector<std::pair<int, int>> actions_;
  mutable std::string last_failure_reason_;
  mutable std::string last_accepted_fingerprint_;
};

}  // namespace ai_trade
