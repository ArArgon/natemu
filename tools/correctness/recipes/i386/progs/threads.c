/* threads: eight threads (clone with CLONE_SETTLS: a TLS segment each), each with its own thread-local data and its
   own x87 rounding mode, kept across context switches; a mutex, 32- and 64-bit atomics, a condition variable, a
   barrier, thread-specific keys. Every printed value is independent of scheduling. */
#define _GNU_SOURCE
#include <fenv.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <semaphore.h>
#include <stdio.h>

#define N 8
#define ROUNDS 20000
static __thread int me = -1;
static __thread double acc;
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_barrier_t bar;
static pthread_key_t key;
static long shared; static int a32; static long long a64 = 0x7ffffffffff00000ll;
static long sums[N]; static int tls_ok[N], key_ok[N], mode_ok[N];
static const int modes[4] = { FE_TONEAREST, FE_DOWNWARD, FE_UPWARD, FE_TOWARDZERO };

static void *work(void *arg)
{
    int id = (int)(long)arg; me = id; acc = id * 0.5;
    pthread_setspecific(key, (void *)(long)(id + 100));
    fesetround(modes[id % 4]);
    pthread_barrier_wait(&bar);
    long sum = 0;
    for (int k = 0; k < ROUNDS; k++) {
        pthread_mutex_lock(&mu); shared++; pthread_mutex_unlock(&mu);
        __sync_fetch_and_add(&a32, 1);
        __atomic_fetch_add(&a64, 3, __ATOMIC_SEQ_CST);
        volatile double x = (k % 7) - 3 + 0.5;          /* -2.5 .. 3.5: rounds by this thread's mode */
        sum += lrint(x) + (int)x; acc += 0.25;
        if (k % 1000 == 0) sched_yield();
    }
    sums[id] = sum;
    tls_ok[id] = me == id && acc == id * 0.5 + ROUNDS * 0.25;
    key_ok[id] = (long)pthread_getspecific(key) == id + 100;
    mode_ok[id] = fegetround() == modes[id % 4];
    return (void *)(long)(id * 3);
}

static pthread_cond_t cv = PTHREAD_COND_INITIALIZER; static pthread_mutex_t cm = PTHREAD_MUTEX_INITIALIZER;
static int turn, pings; static sem_t done;
static void *ping(void *arg)
{
    int mine = (int)(long)arg;
    for (int i = 0; i < 2000; i++) {
        pthread_mutex_lock(&cm);
        while (turn != mine) pthread_cond_wait(&cv, &cm);
        turn = !mine; pings++;
        pthread_cond_broadcast(&cv); pthread_mutex_unlock(&cm);
    }
    if (mine) sem_post(&done);
    return 0;
}

int main(void)
{
    pthread_t t[N]; long joined = 0;
    pthread_barrier_init(&bar, 0, N); pthread_key_create(&key, 0);
    for (long i = 0; i < N; i++) pthread_create(&t[i], 0, work, (void *)i);
    for (int i = 0; i < N; i++) { void *r; pthread_join(t[i], &r); joined += (long)r; }
    printf("shared %ld a32 %d a64 %llx joined %ld main-me %d\n", shared, a32, a64, joined, me);
    for (int i = 0; i < N; i++) printf("thread %d sum %ld tls %d key %d mode %d\n", i, sums[i], tls_ok[i], key_ok[i], mode_ok[i]);
    pthread_t p, q; pthread_attr_t at; pthread_attr_init(&at); pthread_attr_setdetachstate(&at, PTHREAD_CREATE_DETACHED);
    sem_init(&done, 0, 0);
    pthread_create(&p, 0, ping, (void *)0); pthread_create(&q, &at, ping, (void *)1);
    pthread_join(p, 0); sem_wait(&done);
    printf("pings %d round %d\n", pings, fegetround() == FE_TONEAREST);
    return 0;
}
