#pragma once

#include <string>

namespace ai_trade {
// Crash-conservative, process-portfolio latch, unrelated to model/config hashes.
// ARMED is synced BEFORE risk may be sent. Only explicit clean shutdown can
// append CLEAN; destructor/crash never does. WITHDRAWN is permanently sticky.
class EvolutionSafetyJournal {
 public:
  ~EvolutionSafetyJournal();
  EvolutionSafetyJournal() = default;
  EvolutionSafetyJournal(const EvolutionSafetyJournal&) = delete;
  EvolutionSafetyJournal& operator=(const EvolutionSafetyJournal&) = delete;
  bool Open(const std::string& directory, bool enabled, std::string* error);
  bool Withdraw(std::string* error);
  bool CloseClean(std::string* error);
  bool withdrawn() const { return withdrawn_; }
 private:
  bool Append(const char* record, std::string* error);
  int fd_{-1};
  bool armed_{false};
  bool withdrawn_{false};
};
}  // namespace ai_trade
