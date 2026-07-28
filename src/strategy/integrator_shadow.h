#pragma once

#include <string>
#include <vector>

#include "core/config.h"
#include "core/types.h"
#include "research/online_feature_engine.h"

namespace ai_trade {

class IntegratorShadow {
 public:
  explicit IntegratorShadow(IntegratorShadowConfig config);

  bool Initialize(bool strict_takeover, std::string* out_error);
  bool BootstrapHistory(std::string* out_error);
  
  // 新增：接收行情以更新特征引擎
  void OnMarket(const MarketEvent& event);

  ShadowInference Infer(const Signal& signal, const RegimeState& regime) const;

  bool enabled() const { return config_.enabled && initialized_; }
  std::string model_version() const { return model_version_; }
  const std::string& activation_transaction_id() const {
    return activation_transaction_id_;
  }
  const std::string& runtime_config_sha256() const {
    return runtime_config_sha256_;
  }
  const std::string& trade_bot_sha256() const {
    return trade_bot_sha256_;
  }
  const std::string& training_symbol() const { return training_symbol_; }
  std::int64_t feature_bar_interval_ms() const {
    return feature_bar_interval_ms_;
  }
  size_t feature_sample_count() const { return feature_engine_.SampleCount(); }
  ~IntegratorShadow();

 private:
  static double Sigmoid(double x);

  IntegratorShadowConfig config_;
  bool initialized_{false};
  std::string model_version_;
  std::string activation_transaction_id_;
  std::string runtime_config_sha256_;
  std::string trade_bot_sha256_;
  bool expected_net_edge_available_{false};
  double expected_net_edge_per_trade_bps_{0.0};
  std::string training_symbol_;
  std::string training_csv_path_;
  std::int64_t feature_bar_interval_ms_{0};
  std::int64_t last_observed_market_ts_ms_{0};
  std::int64_t last_completed_bar_ts_ms_{0};

  // 在线特征计算引擎
  research::OnlineFeatureEngine feature_engine_;
  std::vector<std::string> feature_names_;
  std::vector<std::string> feature_expressions_;
  bool feature_clipping_enabled_{false};
  std::vector<double> feature_clip_lower_;
  std::vector<double> feature_clip_upper_;
  bool feature_normalization_enabled_{false};
  std::vector<double> feature_norm_center_;
  std::vector<double> feature_norm_scale_;
  std::vector<double> feature_norm_max_abs_;
  void* model_handle_{nullptr}; // CatBoost ModelCalcerHandle (void* to avoid header dependency)
  bool model_runtime_ready_{false};
};

}  // namespace ai_trade
