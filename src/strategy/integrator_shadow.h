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
  
  // 新增：接收行情以更新特征引擎
  void OnMarket(const MarketEvent& event);

  ShadowInference Infer(const Signal& signal, const RegimeState& regime) const;

  bool enabled() const { return config_.enabled && initialized_; }
  std::string model_version() const { return model_version_; }
  ~IntegratorShadow();

 private:
  static double Sigmoid(double x);

  IntegratorShadowConfig config_;
  bool initialized_{false};
  std::string model_version_;

  // 在线特征计算引擎
  research::OnlineFeatureEngine feature_engine_;
  std::vector<std::string> feature_names_;
  std::vector<std::string> feature_expressions_;
  void* model_handle_{nullptr}; // CatBoost ModelCalcerHandle (void* to avoid header dependency)
  bool model_runtime_ready_{false};
};

}  // namespace ai_trade
