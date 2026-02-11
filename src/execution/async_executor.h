#pragma once

#include <condition_variable>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <vector>

#include "core/types.h"
#include "exchange/exchange_adapter.h"

namespace ai_trade {

// 异步执行结果：由工作线程写入，主线程轮询消费。
struct AsyncResult {
  std::string client_order_id;
  bool success{false};
  std::string error;
  bool is_cancel{false};
};

/**
 * @brief 异步执行器（单工作线程串行执行）
 *
 * 设计目的：
 * 1. 主线程只负责“投递任务”，不阻塞在网络调用；
 * 2. 交易所接口统一在单线程串行执行，避免并发下单竞态；
 * 3. 执行结果由主线程轮询消费，简化状态机一致性。
 */
class AsyncExecutor {
 public:
  /**
   * @param adapter 交易所适配器，生命周期由外部管理（不持有所有权）
   */
  explicit AsyncExecutor(ExchangeAdapter* adapter);
  ~AsyncExecutor();

  /// 启动后台工作线程；重复调用无副作用。
  void Start();
  /// 投递 stop 任务并等待线程退出（幂等）。
  void Stop();

  /// 异步提交下单任务。
  void Submit(const OrderIntent& intent);
  /// 异步提交撤单任务。
  void Cancel(const std::string& client_order_id);

  /// 非阻塞轮询执行结果；返回后 `out_results` 持有本轮所有结果。
  void PollResults(std::vector<AsyncResult>* out_results);

 private:
  void WorkerLoop();

  struct Task {
    enum Type { kSubmit, kCancel, kStop } type;
    OrderIntent intent;  ///< submit 任务有效载荷。
    std::string cancel_id;  ///< cancel 任务目标 client_order_id。
  };

  ExchangeAdapter* adapter_{nullptr};  ///< 外部注入适配器（不拥有所有权）。
  std::thread worker_;  ///< 后台执行线程。
  std::mutex queue_mutex_;  ///< 任务队列互斥锁。
  std::condition_variable queue_cv_;  ///< 任务到达通知。
  std::queue<Task> task_queue_;  ///< 待执行任务队列。

  std::mutex result_mutex_;  ///< 结果缓冲区互斥锁。
  std::vector<AsyncResult> results_;  ///< 待主线程消费的执行结果。
};

}  // namespace ai_trade
