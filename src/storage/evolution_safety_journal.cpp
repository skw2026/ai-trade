#include "storage/evolution_safety_journal.h"

#include <cerrno>
#include <cstring>
#include <filesystem>
#include <fcntl.h>
#include <sstream>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

namespace ai_trade {
namespace {
bool Fail(std::string* error, const char* message) {
  if (error) *error = message;
  return false;
}
}
EvolutionSafetyJournal::~EvolutionSafetyJournal() {
  if (fd_ >= 0) ::close(fd_);
}
bool EvolutionSafetyJournal::Append(const char* record, std::string* error) {
  const std::size_t size = std::strlen(record);
  std::size_t offset = 0;
  while (offset < size) {
    const auto n = ::write(fd_, record + offset, size - offset);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) {
      withdrawn_ = true;
      return Fail(error, "SAFETY_JOURNAL_WRITE_FAILED");
    }
    offset += static_cast<std::size_t>(n);
  }
  if (::fsync(fd_) != 0) {
    withdrawn_ = true;
    return Fail(error, "SAFETY_JOURNAL_SYNC_FAILED");
  }
  return true;
}
bool EvolutionSafetyJournal::Open(const std::string& directory, bool enabled,
                                   std::string* error) {
  if (fd_ >= 0) return Fail(error, "SAFETY_JOURNAL_ALREADY_OPEN");
  const auto path = std::filesystem::path(directory) / "evolution_safety_v1.journal";
  struct stat state{};
  const bool exists = ::lstat(path.c_str(), &state) == 0;
  if (!exists && errno != ENOENT) return Fail(error, "SAFETY_JOURNAL_STAT_FAILED");
  if (!enabled && !exists) return true;
  if (exists && !S_ISREG(state.st_mode))
    return Fail(error, "SAFETY_JOURNAL_NOT_REGULAR");
  // Directory is created by WAL initialization; never creates a new arbitrary
  // data root or follows a journal symlink. Hold a single-writer lock for life.
  fd_ = ::open(path.c_str(), O_RDWR | O_APPEND | O_CLOEXEC | O_NOFOLLOW |
               (exists ? 0 : O_CREAT | O_EXCL), 0600);
  if (fd_ < 0 || ::flock(fd_, LOCK_EX | LOCK_NB) != 0)
    return Fail(error, "SAFETY_JOURNAL_OPEN_OR_LOCK_FAILED");
  if (!exists) {
    if (!Append("AI_TRADE_EVOLUTION_SAFETY_V1\n", error)) return false;
    const int dir = ::open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    const bool synced = dir >= 0 && ::fsync(dir) == 0;
    if (dir >= 0) ::close(dir);
    if (!synced) return Fail(error, "SAFETY_JOURNAL_DIRECTORY_SYNC_FAILED");
  } else {
    // Bounded read; malformed, truncated, empty or oversized state is NOT clear.
    constexpr std::size_t kMaxBytes = 1024 * 1024;
    std::string content;
    char buffer[4096];
    for (;;) {
      const auto n = ::read(fd_, buffer, sizeof(buffer));
      if (n < 0 && errno == EINTR) continue;
      if (n < 0) return Fail(error, "SAFETY_JOURNAL_READ_FAILED");
      if (n == 0) break;
      content.append(buffer, static_cast<std::size_t>(n));
      if (content.size() > kMaxBytes) { withdrawn_ = true; break; }
    }
    std::istringstream lines(content);
    std::string line;
    bool valid = std::getline(lines, line) && line == "AI_TRADE_EVOLUTION_SAFETY_V1";
    bool active = false;
    bool terminal = false;
    while (std::getline(lines, line)) {
      if (line == "ARMED" && !active && !terminal) active = true;
      else if (line == "CLEAN" && active && !terminal) active = false;
      else if (line == "WITHDRAWN") terminal = true;
      else valid = false;
    }
    withdrawn_ = withdrawn_ || !valid || content.empty() || content.back() != '\n' ||
                 active || terminal;
    // Existing header alone means first arm was interrupted, not an unused gate.
    if (content == "AI_TRADE_EVOLUTION_SAFETY_V1\n") withdrawn_ = true;
  }
  if (withdrawn_) return true;  // Keep reductions available; never rewrite corruption.
  if (enabled) {
    if (!Append("ARMED\n", error)) return false;
    armed_ = true;
  }
  return true;
}
bool EvolutionSafetyJournal::Withdraw(std::string* error) {
  withdrawn_ = true;  // In-memory gate wins even when storage fails.
  if (fd_ < 0) return Fail(error, "SAFETY_JOURNAL_NOT_OPEN");
  return Append("WITHDRAWN\n", error);
}
bool EvolutionSafetyJournal::CloseClean(std::string* error) {
  if (!armed_ || withdrawn_) return true;
  if (!Append("CLEAN\n", error)) return false;
  armed_ = false;
  return true;
}
}  // namespace ai_trade
