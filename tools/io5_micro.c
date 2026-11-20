/* Cost per event of the things that make natemu trap (run by `run_io5.py profile --steps micro`, and natively for
   reference): prints "NAME ns-per-iteration". `m PREFIX [SCALE]` runs only the tests whose name starts with PREFIX, SCALE
   times as many iterations. Builds without the x86-only tests on other machines. */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <string.h>
#ifdef __x86_64__
#include <nmmintrin.h>
#include <x86intrin.h>
#include <cpuid.h>
#endif
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1e9 + t.tv_nsec; }
static volatile long sink;
__attribute__((noinline, aligned(4096))) static long far1(long x) { return x + 1; }
__attribute__((noinline, aligned(4096))) static long far2(long x) { return x + 2; }
static const char *only = "";
static long scale = 1;
static int on(const char *name) { return !strncmp(name, only, strlen(only)); }
#define RUN(name, N, body) do { if (!on(name)) break; long n_ = (N) * scale; double t0 = now(); for (long i = 0; i < n_; i++) { body; } \
    printf("%s %.1f\n", name, (now() - t0) / n_); fflush(stdout); } while (0)
int main(int argc, char **argv) {
    if (argc > 1) only = argv[1];
    if (argc > 2) scale = atol(argv[2]);
    long (*volatile fp)(long) = far1;
    static char buf[4096];
    RUN("empty", 200000000L, sink += i);
    RUN("call_other_page", 100000000L, sink += far2(i));
    RUN("indirect_call", 100000000L, sink += fp(i));
#ifdef __x86_64__
    {
        __m128i a = _mm_set1_epi8('a'), b = _mm_set1_epi8('b');
        unsigned aux, e0, e1, e2, e3;
        RUN("pcmpistri", 3000000L, { __asm__ volatile("" : "+x"(a), "+x"(b)); sink += _mm_cmpistri(a, b, 0); });
        RUN("pcmpestri", 3000000L, { __asm__ volatile("" : "+x"(a), "+x"(b)); sink += _mm_cmpestri(a, 16, b, 16, 0); });
        RUN("x87_fsin", 1000000L, { long double x = i; __asm__ volatile("fsin" : "+t"(x)); sink += (long)x; });
        RUN("rdtsc", 3000000L, sink += __rdtsc());
        RUN("rdtscp", 3000000L, sink += __rdtscp(&aux));
        RUN("seg_read", 3000000L, { unsigned long s; __asm__ volatile("mov %%ds,%0" : "=r"(s)); sink += s; });
        RUN("nop5x3", 3000000L, __asm__ volatile(".byte 0x0f,0x1f,0x44,0,0,0x0f,0x1f,0x44,0,0,0x0f,0x1f,0x44,0,0"));
        RUN("cpuid", 300000L, { __cpuid(0, e0, e1, e2, e3); sink += e0; });
        RUN("rep_stosb_4k", 1000000L, { void *d = buf; long c = 4096; __asm__ volatile("rep stosb" : "+D"(d), "+c"(c) : "a"(0) : "memory"); });
    }
#endif
    RUN("syscall_getppid", 2000000L, sink += syscall(SYS_getppid));
    if (on("page")) {
        long n = 16384;
        char *p = mmap(0, n * 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        double t0 = now();
        for (long i = 0; i < n; i++) p[i * 4096] = 1;
        printf("page_first_touch %.1f\n", (now() - t0) / n);
        t0 = now();
        for (int r = 0; r < 20; r++) for (long i = 0; i < n; i++) sink += p[i * 4096];
        printf("page_retouch %.1f\n", (now() - t0) / n / 20);
    }
    if (on("fork_exit_wait")) {
        long n = 2000 * scale; double t0 = now();
        for (long i = 0; i < n; i++) { pid_t c = fork(); if (!c) _exit(0); waitpid(c, 0, 0); }
        printf("fork_exit_wait %.1f\n", (now() - t0) / n);
    }
    if (on("fork_exec_true")) {
        long n = 500 * scale; double t0 = now();
        for (long i = 0; i < n; i++) { pid_t c = fork(); if (!c) { execl("/bin/true", "true", (char *)0); _exit(1); } waitpid(c, 0, 0); }
        printf("fork_exec_true %.1f\n", (now() - t0) / n);
    }
    return 0;
}
