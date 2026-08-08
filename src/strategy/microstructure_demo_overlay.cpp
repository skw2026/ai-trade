#include "strategy/microstructure_demo_overlay.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <optional>
#include <sstream>
#include <vector>

#if defined(__APPLE__)
#include <CommonCrypto/CommonDigest.h>
#else
#include <openssl/evp.h>
#endif

#include "core/json_utils.h"
#include "core/log.h"

namespace ai_trade {
namespace {

constexpr const char* kSignalSchema = "microstructure_demo_signal_v2";
constexpr const char* kStateSchema =
    "microstructure_alpha_lifecycle_state_v1";

std::int64_t EpochNowMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

std::string BytesToHex(const unsigned char* bytes, std::size_t size) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string output(size * 2U, '0');
  for (std::size_t i = 0; i < size; ++i) {
    output[i * 2U] = kHex[(bytes[i] >> 4U) & 0x0FU];
    output[i * 2U + 1U] = kHex[bytes[i] & 0x0FU];
  }
  return output;
}

std::optional<std::string> Sha256File(const std::filesystem::path& path) {
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
      ok = CC_SHA256_Update(&context, buffer,
                            static_cast<CC_LONG>(count)) == 1;
    }
  }
  ok = ok && input.eof();
  unsigned char digest[CC_SHA256_DIGEST_LENGTH] = {};
  ok = ok && CC_SHA256_Final(digest, &context) == 1;
  return ok ? std::optional<std::string>(
                  BytesToHex(digest, CC_SHA256_DIGEST_LENGTH))
            : std::nullopt;
#else
  EVP_MD_CTX* context = EVP_MD_CTX_new();
  if (context == nullptr) return std::nullopt;
  bool ok = EVP_DigestInit_ex(context, EVP_sha256(), nullptr) == 1;
  char buffer[64 * 1024];
  while (ok && input.good()) {
    input.read(buffer, sizeof(buffer));
    const std::streamsize count = input.gcount();
    if (count > 0) {
      ok = EVP_DigestUpdate(context, buffer,
                            static_cast<std::size_t>(count)) == 1;
    }
  }
  ok = ok && input.eof();
  unsigned char digest[EVP_MAX_MD_SIZE] = {};
  unsigned int digest_size = 0;
  ok = ok && EVP_DigestFinal_ex(context, digest, &digest_size) == 1;
  EVP_MD_CTX_free(context);
  return ok ? std::optional<std::string>(BytesToHex(digest, digest_size))
            : std::nullopt;
#endif
}

bool ReadJson(const std::filesystem::path& path, JsonValue* out,
              std::string* out_error) {
  std::ifstream input(path);
  if (!input.is_open()) {
    if (out_error != nullptr) *out_error = "cannot open " + path.string();
    return false;
  }
  std::ostringstream buffer;
  buffer << input.rdbuf();
  std::string parse_error;
  if (!ParseJson(buffer.str(), out, &parse_error)) {
    if (out_error != nullptr) {
      *out_error = "invalid JSON " + path.string() + ": " + parse_error;
    }
    return false;
  }
  return true;
}

bool IsSha256(const std::string& value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](unsigned char ch) {
           return (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f');
         });
}

std::optional<std::int64_t> JsonInt(const JsonValue* value) {
  const auto number = JsonAsNumber(value);
  if (!number.has_value() || !std::isfinite(*number)) return std::nullopt;
  const double rounded = std::round(*number);
  if (std::fabs(*number - rounded) > 1e-6) return std::nullopt;
  return static_cast<std::int64_t>(rounded);
}

std::string ArtifactSha(const JsonValue* artifacts, const std::string& name) {
  const JsonValue* reference = JsonObjectField(artifacts, name);
  return JsonAsString(JsonObjectField(reference, "sha256")).value_or("");
}

bool VerifyFile(const std::filesystem::path& path,
                const std::string& expected_sha) {
  if (!IsSha256(expected_sha)) return false;
  const auto actual = Sha256File(path);
  return actual.has_value() && *actual == expected_sha;
}

}  // namespace

MicrostructureDemoOverlay::MicrostructureDemoOverlay(
    MicrostructureDemoConfig config)
    : config_(std::move(config)) {}

bool MicrostructureDemoOverlay::RefreshLifecycle(std::int64_t now_ms,
                                                 std::string* out_error) {
  last_refresh_epoch_ms_ = now_ms;
  namespace fs = std::filesystem;
  const fs::path root(config_.lifecycle_root);
  JsonValue state;
  std::string error;
  if (!ReadJson(root / "state.json", &state, &error)) {
    candidate_ready_ = false;
    if (out_error != nullptr) *out_error = error;
    return false;
  }
  const std::string schema =
      JsonAsString(JsonObjectField(&state, "schema_version")).value_or("");
  const std::string phase =
      JsonAsString(JsonObjectField(&state, "phase")).value_or("");
  if (schema != kStateSchema || phase != "demo_ready") {
    candidate_ready_ = false;
    if (out_error != nullptr) *out_error = "candidate_not_demo_ready";
    return false;
  }
  if (!JsonAsBool(JsonObjectField(&state, "demo_entry_eligible"))
           .value_or(false) ||
      JsonAsBool(JsonObjectField(&state, "live_promotion_eligible"))
          .value_or(true)) {
    candidate_ready_ = false;
    if (out_error != nullptr) *out_error = "demo/live eligibility mismatch";
    return false;
  }
  const std::string candidate =
      JsonAsString(JsonObjectField(&state, "candidate_id")).value_or("");
  if (!IsSha256(candidate)) {
    candidate_ready_ = false;
    if (out_error != nullptr) *out_error = "invalid candidate id";
    return false;
  }
  if (candidate_was_ready_ && !candidate_id_.empty() &&
      candidate != candidate_id_) {
    candidate_ready_ = false;
    if (out_error != nullptr) *out_error = "demo candidate identity changed";
    return false;
  }

  JsonValue checkpoint;
  if (!ReadJson(root / "checkpoint.json", &checkpoint, &error)) {
    candidate_ready_ = false;
    if (out_error != nullptr) *out_error = error;
    return false;
  }
  const std::string state_sha =
      JsonAsString(JsonObjectField(&checkpoint, "state_sha256")).value_or("");
  const auto event_count = JsonInt(JsonObjectField(&checkpoint, "event_count"));
  const std::string event_hash =
      JsonAsString(JsonObjectField(&checkpoint, "last_event_hash"))
          .value_or("");
  if (!IsSha256(state_sha) || !IsSha256(event_hash) ||
      !event_count.has_value() || *event_count <= 0 ||
      !fs::is_regular_file(root / "events.jsonl")) {
    candidate_ready_ = false;
    if (out_error != nullptr) *out_error = "lifecycle checkpoint incomplete";
    return false;
  }

  const JsonValue* artifacts = JsonObjectField(&state, "artifacts");
  const std::string report_sha = ArtifactSha(artifacts, "development_report");
  const std::string model_sha = ArtifactSha(artifacts, "model");
  const std::string manifest_sha = ArtifactSha(artifacts, "candidate_manifest");
  const fs::path candidate_dir = root / "candidates" / candidate;
  const fs::path report_path = candidate_dir / "development_report.json";
  const fs::path model_path = candidate_dir / "model.cbm";
  const fs::path manifest_path = candidate_dir / "candidate_manifest.json";
  if (!VerifyFile(report_path, report_sha) ||
      !VerifyFile(model_path, model_sha) ||
      !VerifyFile(manifest_path, manifest_sha)) {
    candidate_ready_ = false;
    if (out_error != nullptr) *out_error = "candidate artifact hash mismatch";
    return false;
  }

  const JsonValue* evidence = JsonObjectField(&state, "evidence");
  const JsonValue* replay_ref = JsonObjectField(evidence, "raw_replay_passed");
  const std::string replay_sha =
      JsonAsString(JsonObjectField(replay_ref, "sha256")).value_or("");
  const fs::path replay_path = candidate_dir / "raw_replay_report.json";
  JsonValue replay;
  if (!VerifyFile(replay_path, replay_sha) ||
      !ReadJson(replay_path, &replay, &error) ||
      JsonAsString(JsonObjectField(&replay, "status")).value_or("") !=
          "PASS" ||
      JsonAsString(JsonObjectField(&replay, "candidate_id")).value_or("") !=
          candidate ||
      !JsonAsBool(JsonObjectField(&replay, "raw_to_feature_parity"))
           .value_or(false) ||
      !JsonAsBool(JsonObjectField(
                      &replay,
                      "fixed_model_prediction_economics_deterministic"))
           .value_or(false) ||
      !JsonAsBool(JsonObjectField(&replay, "demo_entry_eligible"))
           .value_or(false) ||
      JsonAsBool(JsonObjectField(&replay, "live_promotion_eligible"))
          .value_or(true)) {
    candidate_ready_ = false;
    if (out_error != nullptr) *out_error = "raw replay evidence mismatch";
    return false;
  }

  JsonValue report;
  if (!ReadJson(report_path, &report, &error)) {
    candidate_ready_ = false;
    if (out_error != nullptr) *out_error = error;
    return false;
  }
  const JsonValue* frozen = JsonObjectField(&report, "frozen_candidate");
  const JsonValue* target = JsonObjectField(&report, "target_contract");
  const auto threshold =
      JsonAsNumber(JsonObjectField(frozen, "policy_threshold_bps"));
  const auto latency =
      JsonInt(JsonObjectField(target, "execution_latency_seconds"));
  const JsonValue* actions = JsonObjectField(target, "actions");
  std::vector<std::pair<int, int>> parsed_actions;
  if (actions != nullptr && actions->type == JsonType::kArray) {
    for (const auto& action : actions->array_value) {
      const std::string direction =
          JsonAsString(JsonObjectField(&action, "direction")).value_or("");
      const auto horizon =
          JsonInt(JsonObjectField(&action, "horizon_seconds"));
      if ((direction != "long" && direction != "short") ||
          !horizon.has_value() || *horizon <= 0 || *horizon > 3600) {
        parsed_actions.clear();
        break;
      }
      parsed_actions.emplace_back(direction == "long" ? 1 : -1,
                                  static_cast<int>(*horizon));
    }
  }
  if (!threshold.has_value() || !std::isfinite(*threshold) ||
      !latency.has_value() || *latency < 1 || parsed_actions.empty()) {
    candidate_ready_ = false;
    if (out_error != nullptr) *out_error = "frozen policy contract incomplete";
    return false;
  }

  candidate_id_ = candidate;
  lifecycle_state_sha256_ = state_sha;
  model_sha256_ = model_sha;
  development_report_sha256_ = report_sha;
  policy_threshold_bps_ = *threshold;
  execution_latency_seconds_ = static_cast<int>(*latency);
  actions_ = std::move(parsed_actions);
  candidate_ready_ = true;
  candidate_was_ready_ = true;
  return true;
}

ShadowInference MicrostructureDemoOverlay::FailClosed(
    const std::string& reason) const {
  ShadowInference out;
  out.enabled = true;
  out.model_version = candidate_id_.empty() ? "microstructure-unavailable"
                                             : candidate_id_;
  out.source = "microstructure_demo";
  out.target_position_signal = true;
  out.target_direction = 0;
  out.target_notional_usd = 0.0;
  out.fail_closed = true;
  if (config_.log_signal && reason != last_failure_reason_) {
    LogInfo("MICROSTRUCTURE_DEMO_FAIL_CLOSED: candidate_id=" +
            out.model_version + ", reason=" + reason);
    last_failure_reason_ = reason;
  }
  last_accepted_fingerprint_.clear();
  return out;
}

void MicrostructureDemoOverlay::LogAccepted(
    const std::string& status, int direction,
    double predicted_net_edge_bps) const {
  const std::string fingerprint =
      candidate_id_ + ":" + status + ":" + std::to_string(direction);
  if (config_.log_signal && fingerprint != last_accepted_fingerprint_) {
    LogInfo("MICROSTRUCTURE_DEMO_SIGNAL_ACCEPTED: candidate_id=" +
            candidate_id_ + ", status=" + status +
            ", direction=" + std::to_string(direction) +
            ", predicted_net_edge_bps=" +
            std::to_string(predicted_net_edge_bps));
  }
  last_accepted_fingerprint_ = fingerprint;
  last_failure_reason_.clear();
}

ShadowInference MicrostructureDemoOverlay::Infer(const MarketEvent& event) {
  ShadowInference unavailable;
  unavailable.source = "microstructure_demo";
  if (!config_.enabled || event.symbol != "SOLUSDT") return unavailable;
  const std::int64_t now_ms = EpochNowMs();
  if (last_refresh_epoch_ms_ <= 0 ||
      now_ms - last_refresh_epoch_ms_ >= config_.lifecycle_refresh_ms) {
    std::string refresh_error;
    if (!RefreshLifecycle(now_ms, &refresh_error)) {
      if (candidate_was_ready_) return FailClosed(refresh_error);
      return unavailable;
    }
  }
  if (!candidate_ready_) {
    return candidate_was_ready_ ? FailClosed("candidate_lifecycle_unavailable")
                                : unavailable;
  }

  JsonValue signal;
  std::string error;
  if (!ReadJson(config_.signal_path, &signal, &error)) {
    return FailClosed(error);
  }
  const std::string schema =
      JsonAsString(JsonObjectField(&signal, "schema_version")).value_or("");
  const std::string status =
      JsonAsString(JsonObjectField(&signal, "status")).value_or("");
  const std::string symbol =
      JsonAsString(JsonObjectField(&signal, "symbol")).value_or("");
  const std::string candidate =
      JsonAsString(JsonObjectField(&signal, "candidate_id")).value_or("");
  const std::string state_sha = JsonAsString(
      JsonObjectField(&signal, "lifecycle_state_sha256"))
                                    .value_or("");
  const std::string model_sha =
      JsonAsString(JsonObjectField(&signal, "model_sha256")).value_or("");
  const std::string report_sha = JsonAsString(
      JsonObjectField(&signal, "development_report_sha256"))
                                     .value_or("");
  const auto generated =
      JsonInt(JsonObjectField(&signal, "generated_at_epoch_ms"));
  const auto exchange_ts =
      JsonInt(JsonObjectField(&signal, "exchange_timestamp_ms"));
  if (schema != kSignalSchema || symbol != event.symbol ||
      candidate != candidate_id_ || state_sha != lifecycle_state_sha256_ ||
      model_sha != model_sha256_ || report_sha != development_report_sha256_ ||
      !generated.has_value() || !exchange_ts.has_value() ||
      JsonAsBool(JsonObjectField(&signal, "live_promotion_eligible"))
          .value_or(true) ||
      !JsonAsBool(JsonObjectField(&signal, "demo_entry_eligible"))
           .value_or(false)) {
    return FailClosed("signal_identity_contract_mismatch");
  }
  const std::int64_t wall_age = now_ms - *generated;
  const std::int64_t exchange_age = event.ts_ms - *exchange_ts;
  if (wall_age < -2000 || wall_age > config_.max_signal_stale_ms ||
      exchange_age < -5000 || exchange_age > config_.max_signal_stale_ms) {
    return FailClosed("signal_stale");
  }
  if (status == "FAIL_CLOSED") return FailClosed("sidecar_fail_closed");
  if (status == "FLAT") {
    ShadowInference flat;
    flat.enabled = true;
    flat.model_version = candidate_id_;
    flat.source = "microstructure_demo";
    flat.target_position_signal = true;
    flat.target_direction = 0;
    flat.target_notional_usd = 0.0;
    flat.target_valid_until_ms = *generated + config_.max_signal_stale_ms;
    flat.fail_closed = false;
    LogAccepted("FLAT", 0, 0.0);
    return flat;
  }
  if (status != "ACTIVE") return FailClosed("unsupported_signal_status");

  const JsonValue* action = JsonObjectField(&signal, "action");
  const auto action_started =
      JsonInt(JsonObjectField(action, "started_exchange_ms"));
  const auto direction = JsonInt(JsonObjectField(action, "direction"));
  const auto horizon = JsonInt(JsonObjectField(action, "horizon_seconds"));
  const auto action_index = JsonInt(JsonObjectField(action, "action_index"));
  const auto predicted =
      JsonAsNumber(JsonObjectField(action, "predicted_net_edge_bps"));
  const auto threshold =
      JsonAsNumber(JsonObjectField(action, "policy_threshold_bps"));
  const auto latency =
      JsonInt(JsonObjectField(action, "execution_latency_seconds"));
  const auto active_until =
      JsonInt(JsonObjectField(&signal, "active_until_exchange_ms"));
  if (!action_started.has_value() || !direction.has_value() ||
      !horizon.has_value() ||
      !action_index.has_value() || !predicted.has_value() ||
      !threshold.has_value() || !latency.has_value() ||
      !active_until.has_value() || *action_index < 0 ||
      static_cast<std::size_t>(*action_index) >= actions_.size()) {
    return FailClosed("active_action_contract_incomplete");
  }
  const auto& expected_action = actions_[static_cast<std::size_t>(*action_index)];
  const std::int64_t expected_until =
      *action_started + (*latency + *horizon) * 1000;
  if (*direction != expected_action.first || *horizon != expected_action.second ||
      *latency != execution_latency_seconds_ ||
      !std::isfinite(*predicted) || !std::isfinite(*threshold) ||
      std::fabs(*threshold - policy_threshold_bps_) > 1e-9 ||
      *predicted + 1e-9 < policy_threshold_bps_ ||
      *action_started > *exchange_ts || *exchange_ts >= *active_until ||
      *active_until != expected_until || event.ts_ms < *action_started ||
      event.ts_ms >= *active_until) {
    return FailClosed("active_action_identity_or_expiry_mismatch");
  }

  ShadowInference out;
  out.enabled = true;
  out.model_version = candidate_id_;
  out.source = "microstructure_demo";
  out.model_score = *direction > 0 ? 6.0 : -6.0;
  out.p_up = *direction > 0 ? 1.0 : 0.0;
  out.p_down = 1.0 - out.p_up;
  out.expected_net_edge_available = true;
  out.expected_net_edge_per_trade_bps = *predicted;
  out.target_position_signal = true;
  out.target_direction = static_cast<int>(*direction);
  out.target_notional_usd = config_.target_notional_usd;
  out.target_valid_until_ms = *active_until;
  out.fail_closed = false;
  LogAccepted("ACTIVE", static_cast<int>(*direction), *predicted);
  return out;
}

}  // namespace ai_trade
