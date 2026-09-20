#include "execution/async_executor.h"

#include <utility>

namespace ai_trade {

AsyncExecutor::AsyncExecutor(ExchangeAdapter* adapter, Mode mode)
    : adapter_(adapter), mode_(mode) {}

AsyncExecutor::~AsyncExecutor() {
  Stop();
}

void AsyncExecutor::Start() {
  if (mode_ == Mode::kInlineReplay || worker_.joinable()) {
    return;
  }
  worker_ = std::thread(&AsyncExecutor::WorkerLoop, this);
}

void AsyncExecutor::Stop() {
  if (mode_ == Mode::kInlineReplay) {
    return;
  }
  // 通过投递 stop 任务优雅退出，避免直接中断工作线程。
  {
    std::lock_guard<std::mutex> lock(queue_mutex_);
    task_queue_.push(Task{.type = Task::kStop, .intent = {}, .cancel_id = ""});
  }
  queue_cv_.notify_one();
  if (worker_.joinable()) {
    worker_.join();
  }
}

void AsyncExecutor::Submit(const OrderIntent& intent) {
  if (mode_ == Mode::kInlineReplay) {
    ExecuteTask(Task{.type = Task::kSubmit, .intent = intent, .cancel_id = ""});
    return;
  }
  {
    std::lock_guard<std::mutex> lock(queue_mutex_);
    task_queue_.push(Task{.type = Task::kSubmit, .intent = intent, .cancel_id = ""});
  }
  queue_cv_.notify_one();
}

void AsyncExecutor::Cancel(const std::string& client_order_id) {
  if (mode_ == Mode::kInlineReplay) {
    ExecuteTask(Task{.type = Task::kCancel, .intent = {},
                     .cancel_id = client_order_id});
    return;
  }
  {
    std::lock_guard<std::mutex> lock(queue_mutex_);
    task_queue_.push(Task{.type = Task::kCancel, .intent = {}, .cancel_id = client_order_id});
  }
  queue_cv_.notify_one();
}

void AsyncExecutor::PollResults(std::vector<AsyncResult>* out_results) {
  if (out_results == nullptr) return;
  out_results->clear();
  std::lock_guard<std::mutex> lock(result_mutex_);
  if (results_.empty()) {
    return;
  }
  // swap 方式把锁持有时间降到最短。
  out_results->swap(results_);
}

void AsyncExecutor::WorkerLoop() {
  while (true) {
    Task task;
    {
      std::unique_lock<std::mutex> lock(queue_mutex_);
      // 条件变量阻塞等待，避免空转占用 CPU。
      queue_cv_.wait(lock, [this] { return !task_queue_.empty(); });
      task = task_queue_.front();
      task_queue_.pop();
    }

    // 统一由 stop 任务驱动退出，保证线程收敛路径可控。
    if (task.type == Task::kStop) {
      break;
    }

    ExecuteTask(task);
  }
}

void AsyncExecutor::ExecuteTask(const Task& task) {
  AsyncResult result;
  if (task.type == Task::kSubmit) {
    result.client_order_id = task.intent.client_order_id;
    result.is_cancel = false;
    if (adapter_) {
      result.success = adapter_->SubmitOrder(task.intent);
      if (!result.success) {
        result.error = "SubmitOrder returned false";
      }
    }
  } else if (task.type == Task::kCancel) {
    result.client_order_id = task.cancel_id;
    result.is_cancel = true;
    if (adapter_) {
      result.success = adapter_->CancelOrder(task.cancel_id);
      if (!result.success) {
        result.error = "CancelOrder returned false";
      }
    }
  }

  std::lock_guard<std::mutex> lock(result_mutex_);
  // Both modes preserve the same result/error path; inline mode does not
  // fabricate an ACK or modify the adapter's fill payload.
  results_.push_back(std::move(result));
}

}  // namespace ai_trade
