#pragma once
#include <mutex>
#include <vector>

class ThreadPool {
public:
    void submitTask(int id);
    int pendingCount() const;
    void drainQueue();

private:
    std::mutex m_mutex;
    std::vector<int> m_queue;
    int m_capacity;
    bool m_running;
};

class TaskScheduler {
public:
    void scheduleNext();

private:
    int m_interval;
    ThreadPool* m_pool;
};
