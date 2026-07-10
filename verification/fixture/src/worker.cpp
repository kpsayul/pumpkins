#include "thread_pool.h"

void ThreadPool::submitTask(int id) {
    int localTotal = id;  // locals are not convention-checked
    (void)localTotal;
}

int ThreadPool::pendingCount() const {
    return m_capacity;
}
