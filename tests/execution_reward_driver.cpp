// No market/account adapters, config loading, credentials, HTTP or file inputs.
#include "evolution/execution_reward_ledger.h"
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
using ai_trade::synthetic::ExecutionRewardLedger;
using ai_trade::synthetic::RewardEvent;
int main(int argc, char** argv) {
  if (argc != 2 || std::string(argv[1]) != "--synthetic-only") return 2;
  std::map<std::string, ExecutionRewardLedger> books;
  std::string line;
  while (std::getline(std::cin, line)) {
    try {
      if (line.size() > 1024) throw std::invalid_argument("LINE_LIMIT");
      std::istringstream in(line);
      std::string op, book, extra;
      in >> op >> book;
      if (!in || !ExecutionRewardLedger::ValidId(book)) throw std::invalid_argument("COMMAND");
      if (op == "NEW") {
        int multiplier; in >> multiplier;
        if (!in || (in >> extra) || books.count(book) || books.size() >= 256) throw std::invalid_argument("NEW");
        auto [it, inserted] = books.emplace(book, ExecutionRewardLedger(multiplier));
        (void)inserted;
        std::cout << it->second.Snapshot() << '\n';
      } else if (op == "CLONE") {
        std::string dest; in >> dest;
        if (!in || (in >> extra) || !ExecutionRewardLedger::ValidId(dest) || books.count(dest) || books.size() >= 256)
          throw std::invalid_argument("CLONE");
        const auto source = books.at(book);
        auto [it, inserted] = books.emplace(dest, source); (void)inserted;
        std::cout << it->second.Snapshot() << '\n';
      } else if (op == "VIEW") {
        if (in >> extra) throw std::invalid_argument("VIEW");
        std::cout << books.at(book).Snapshot() << '\n';
      } else if (op == "EV") {
        RewardEvent e;
        in >> e.id >> e.time >> e.kind >> e.bucket >> e.version >> e.a >> e.b >> e.c >> e.d >> e.order >> e.fill >> e.side;
        if (!in || (in >> extra)) throw std::invalid_argument("EVENT_FIELDS");
        std::cout << books.at(book).Apply(e) << '\n';
      } else throw std::invalid_argument("COMMAND_KIND");
    } catch (const std::out_of_range&) {
      std::cout << "{\"ok\":false,\"error\":\"UNKNOWN_BOOK\"}\n";
    } catch (const std::invalid_argument& error) {
      // Error strings are fixed constants, not untrusted payloads.
      std::cout << "{\"ok\":false,\"error\":\"" << error.what() << "\"}\n";
    }
    std::cout.flush();
  }
}
