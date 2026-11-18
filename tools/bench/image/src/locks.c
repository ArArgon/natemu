/* locks THREADS ITERS: THREADS threads each do ITERS rounds of {lock a shared pthread mutex, bump a shared counter,
   unlock; atomic add to a second shared counter; every 64th round sched_yield()}. Contended mutexes go to futex
   wait/wake (cross-CPU wakeups), the atomics bounce one cache line. Prints the wall time (CLOCK_MONOTONIC) and checks
   both counters. Exit 1 on a wrong count. */
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
static long locked, atomic_count, iters;

static void *worker(void *arg) {
    (void)arg;
    for (long i = 0; i < iters; i++) {
        pthread_mutex_lock(&m);
        locked++;
        pthread_mutex_unlock(&m);
        __atomic_fetch_add(&atomic_count, 1, __ATOMIC_SEQ_CST);
        if ((i & 63) == 63) sched_yield();
    }
    return NULL;
}

int main(int argc, char **argv) {
    if (argc != 3) { fprintf(stderr, "usage: locks THREADS ITERS\n"); return 2; }
    int n = atoi(argv[1]);
    iters = atol(argv[2]);
    if (n < 1 || n > 64 || iters < 1) return 2;
    pthread_t t[64];
    struct timespec a, b;
    clock_gettime(CLOCK_MONOTONIC, &a);
    for (int i = 0; i < n; i++) pthread_create(&t[i], NULL, worker, NULL);
    for (int i = 0; i < n; i++) pthread_join(t[i], NULL);
    clock_gettime(CLOCK_MONOTONIC, &b);
    long want = (long)n * iters;
    printf("locks threads=%d iters=%ld time=%.3f s %s\n", n, iters,
           (b.tv_sec - a.tv_sec) + (b.tv_nsec - a.tv_nsec) / 1e9, locked == want && atomic_count == want ? "ok" : "BAD");
    return locked == want && atomic_count == want ? 0 : 1;
}
