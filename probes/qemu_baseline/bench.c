/*
 * bench.c -- portable single-file microbenchmark suite for the QEMU TCG baseline
 * measurement (docs/research/15-qemu-tcg-baseline.md).
 *
 * Design intent: every workload is compiled from THIS source for all four
 * environments (aarch64-linux-musl, x86_64-linux-musl, aarch64-macos,
 * x86_64-macos) with the same compiler (zig cc) and the same flags, so that a
 * ratio between two environments is a property of the execution substrate and
 * not of the toolchain.
 *
 * The workload set is deliberately structured as an ABLATION, not a
 * benchmark suite:
 *
 *   alu      - register-only integer dependency chain, ILP=1. Latency bound.
 *              A translator's extra instructions hide in the multiply-latency
 *              shadow here, so this is the BEST case for an emulator.
 *   alu4     - 4 independent chains, ILP=4. Throughput/issue bound. This is
 *              where code-generation quality actually shows.
 *   ls4      - byte-for-byte the same loop as alu4 except that each of the 4
 *              chains additionally performs one load and one store into a
 *              4 KiB array (always L1- and TLB-resident). The ONLY difference
 *              between alu4 and ls4 is 4 loads + 4 stores per iteration; the
 *              index arithmetic is identical in both. Therefore
 *                  (t_ls4 - t_alu4) / (8 * iters)
 *              is the marginal cost of one guest memory access in that
 *              environment, measured in vivo with a real register allocator,
 *              real I-cache pressure and real branch prediction. Comparing
 *              that marginal cost between TCG and HVF is the direct
 *              measurement of QEMU's softmmu FAST-path overhead.
 *   ls4tlb   - identical to ls4 but the 4 KiB array is replaced by a 32 MiB
 *              span of which only the first 8 bytes of each 4 KiB page are
 *              ever touched. Data footprint stays ~64 KiB (L2-resident) while
 *              the address footprint is 8192 pages -- past the M2 L2 dTLB and
 *              past QEMU's software TLB. (t_ls4tlb - t_ls4) is TLB/slow-path
 *              cost with cache effects held nearly constant.
 *   ptr8     - pointer chase over 8 MiB (L2/SLC resident, ~2048 4K pages:
 *              exceeds the 512-entry hw dTLB but fits QEMU's 4096-entry
 *              software TLB comfortably).
 *   ptr256   - pointer chase over 256 MiB (65536 4K pages: blows out QEMU's
 *              software TLB as well). ptr256 vs ptr8 exposes the softmmu
 *              SLOW path / victim-TLB / page-table-walk cost.
 *   stream   - sequential 64-bit sum over 128 MiB. Bandwidth bound natively;
 *              under TCG it is one softmmu sequence per 8 bytes.
 *   matmul   - 320x320 int32 matmul, ikj order. Mixed ALU + regular memory.
 *   nqueens  - N-queens backtracking, N=12. Branchy, tiny footprint. This is
 *              the workload most sensitive to indirect-branch / TB-chaining
 *              quality.
 *   syscall  - getppid() in a loop. Guest user<->kernel transitions; under TCG
 *              this exercises privilege-level changes and kernel-side code.
 *
 * All iteration counts are COMPILE-TIME CONSTANTS so that every environment
 * performs bit-identical work. Do not make them adaptive.
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifndef BENCH_VERSION
#define BENCH_VERSION "4"
#endif

static double now_s(void)
{
    struct timespec ts;
#if defined(CLOCK_MONOTONIC_RAW) && !defined(__APPLE__)
    clock_gettime(CLOCK_MONOTONIC, &ts);
#else
    clock_gettime(CLOCK_MONOTONIC, &ts);
#endif
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

/* ------------------------------------------------------------------ */
/* deterministic PRNG (splitmix64) used to build all data sets         */
/* ------------------------------------------------------------------ */
static uint64_t sm_state;
static uint64_t sm_next(void)
{
    uint64_t z = (sm_state += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}

/* ------------------------------------------------------------------ */
/* 1. alu : register-only integer chain                                */
/* ------------------------------------------------------------------ */
#define ALU_ITERS 300000000ULL

static uint64_t bench_alu(void)
{
    uint64_t a = 1, b = 2, c = 3, d = 4;
    for (uint64_t i = 0; i < ALU_ITERS; i++) {
        a = a * 6364136223846793005ULL + 1442695040888963407ULL;
        b ^= a >> 13;
        b += c;
        c = (c << 7) | (c >> 57);
        c ^= b;
        d += a ^ c;
        d = d * 31 + 7;
    }
    return a + b + c + d;
}

/* ------------------------------------------------------------------ */
/* 2. alu4 / ls4 / ls4tlb : the softmmu ablation triple                */
/*                                                                     */
/* One macro generates all three loop bodies so that the ALU work and  */
/* the index arithmetic are provably identical and the ONLY difference */
/* is the presence of the guest load + guest store.                    */
/* ------------------------------------------------------------------ */
#define K64 6364136223846793005ULL
#define LS4_ITERS 300000000ULL
#define TLB_ITERS 60000000ULL

/* ls4: 512 slots = 4 KiB, one page */
#define LS4_MASK 511ULL
#define LS4_SLOTS 512
/* ls4tlb: 8192 pages * 512 slots; only slot p*512+off (off<8) ever touched */
#define TLB_PAGES 8192ULL
#define TLB_SLOTS (TLB_PAGES * 512ULL)

/* index generator, identical in all three variants */
#define IDX_SMALL(a) (((a) >> 20) & LS4_MASK)
#define IDX_TLB(a) (((((a) >> 20) & (TLB_PAGES - 1)) << 9) | (((a) >> 8) & 7ULL))

#define CHAIN4(NITER, IDXF, DOMEM)                                                  \
    uint64_t a0 = 1, a1 = 2, a2 = 3, a3 = 4;                                 \
    uint64_t s0 = 0, s1 = 0, s2 = 0, s3 = 0;                                 \
    for (uint64_t i = 0; i < (NITER); i++) {                                 \
        a0 = a0 * K64 + 1442695040888963407ULL;                              \
        a1 = a1 * K64 + 1442695040888963409ULL;                              \
        a2 = a2 * K64 + 1442695040888963411ULL;                              \
        a3 = a3 * K64 + 1442695040888963413ULL;                              \
        uint64_t i0 = IDXF(a0), i1 = IDXF(a1);                               \
        uint64_t i2 = IDXF(a2), i3 = IDXF(a3);                               \
        DOMEM(0, i0)                                                         \
        DOMEM(1, i1)                                                         \
        DOMEM(2, i2)                                                         \
        DOMEM(3, i3)                                                         \
    }                                                                        \
    return s0 + s1 + s2 + s3 + a0 + a1 + a2 + a3;

/* no memory: consume the index in a register */
#define MEM_NONE(C, I) s##C += (I);
/* one load + one store per chain */
#define MEM_LDST(C, I)                                                       \
    s##C += arr[(I)];                                                        \
    arr[(I)] = s##C;

static uint64_t bench_alu4(void)
{
    CHAIN4(LS4_ITERS, IDX_SMALL, MEM_NONE)
}

static uint64_t bench_ls4(uint64_t *arr)
{
    CHAIN4(LS4_ITERS, IDX_SMALL, MEM_LDST)
}

static uint64_t bench_ls4tlb(uint64_t *arr)
{
    CHAIN4(TLB_ITERS, IDX_TLB, MEM_LDST)
}

/* ------------------------------------------------------------------ */
/* 3. pointer chase                                                    */
/* ------------------------------------------------------------------ */
static void build_chase(uint64_t *idx, size_t n)
{
    /* single cycle covering all n slots (Sattolo) */
    for (size_t i = 0; i < n; i++)
        idx[i] = i;
    for (size_t i = n - 1; i > 0; i--) {
        size_t j = (size_t)(sm_next() % i);
        uint64_t t = idx[i];
        idx[i] = idx[j];
        idx[j] = t;
    }
}

static uint64_t bench_chase(size_t bytes, uint64_t steps)
{
    size_t n = bytes / sizeof(uint64_t);
    uint64_t *a = malloc(bytes);
    if (!a) {
        fprintf(stderr, "OOM %zu\n", bytes);
        exit(2);
    }
    sm_state = 0x1234567890ABCDEFULL;
    build_chase(a, n);
    /* turn the permutation into a next-index chain */
    uint64_t *next = malloc(bytes);
    if (!next) {
        fprintf(stderr, "OOM %zu\n", bytes);
        exit(2);
    }
    for (size_t i = 0; i < n; i++)
        next[a[i]] = a[(i + 1) % n];
    free(a);

    uint64_t p = 0, sum = 0;
    for (uint64_t s = 0; s < steps; s++) {
        p = next[p];
        sum += p;
    }
    free(next);
    return sum;
}

#define CHASE8_BYTES (8UL << 20)
#define CHASE8_STEPS 30000000ULL
#define CHASE256_BYTES (256UL << 20)
#define CHASE256_STEPS 8000000ULL

/* ------------------------------------------------------------------ */
/* 4. stream : sequential sum                                          */
/* ------------------------------------------------------------------ */
#define STREAM_BYTES (128UL << 20)
#define STREAM_REPS 100

static uint64_t bench_stream(void)
{
    size_t n = STREAM_BYTES / sizeof(uint64_t);
    uint64_t *a = malloc(STREAM_BYTES);
    if (!a) {
        fprintf(stderr, "OOM\n");
        exit(2);
    }
    for (size_t i = 0; i < n; i++)
        a[i] = i * 2654435761ULL;
    uint64_t s0 = 0, s1 = 0, s2 = 0, s3 = 0;
    for (int r = 0; r < STREAM_REPS; r++) {
        for (size_t i = 0; i + 3 < n; i += 4) {
            s0 += a[i];
            s1 += a[i + 1];
            s2 += a[i + 2];
            s3 += a[i + 3];
        }
    }
    free(a);
    return s0 + s1 + s2 + s3;
}

/* ------------------------------------------------------------------ */
/* 5. matmul : 320x320 int32, ikj                                      */
/* ------------------------------------------------------------------ */
#define MM_N 320
#define MM_REPS 60

static uint64_t bench_matmul(void)
{
    int32_t *A = malloc(sizeof(int32_t) * MM_N * MM_N);
    int32_t *B = malloc(sizeof(int32_t) * MM_N * MM_N);
    int32_t *C = malloc(sizeof(int32_t) * MM_N * MM_N);
    if (!A || !B || !C) {
        fprintf(stderr, "OOM\n");
        exit(2);
    }
    sm_state = 42;
    for (int i = 0; i < MM_N * MM_N; i++) {
        A[i] = (int32_t)(sm_next() & 0xFFFF);
        B[i] = (int32_t)(sm_next() & 0xFFFF);
    }
    uint64_t acc = 0;
    for (int r = 0; r < MM_REPS; r++) {
        memset(C, 0, sizeof(int32_t) * MM_N * MM_N);
        for (int i = 0; i < MM_N; i++) {
            for (int k = 0; k < MM_N; k++) {
                int32_t aik = A[i * MM_N + k];
                const int32_t *bp = &B[k * MM_N];
                int32_t *cp = &C[i * MM_N];
                for (int j = 0; j < MM_N; j++)
                    cp[j] += aik * bp[j];
            }
        }
        acc += (uint64_t)(uint32_t)C[(r * 7919) % (MM_N * MM_N)];
    }
    free(A);
    free(B);
    free(C);
    return acc;
}

/* ------------------------------------------------------------------ */
/* 6. nqueens : branchy backtracking                                   */
/* ------------------------------------------------------------------ */
#define NQ_N 13
#define NQ_REPS 12

static uint64_t nq_solve(int n, uint32_t cols, uint32_t d1, uint32_t d2)
{
    uint32_t all = (1u << n) - 1;
    if (cols == all)
        return 1;
    uint64_t cnt = 0;
    uint32_t avail = all & ~(cols | d1 | d2);
    while (avail) {
        uint32_t bit = avail & (~avail + 1u);
        avail -= bit;
        cnt += nq_solve(n, cols | bit, ((d1 | bit) << 1) & all, (d2 | bit) >> 1);
    }
    return cnt;
}

static uint64_t bench_nqueens(void)
{
    /* `n` is volatile so LLVM cannot CSE the whole pure recursive call away
       (it did exactly that when NQ_N was a literal -- 200 reps cost the same
       as one). Verified by checking the reported time scales with NQ_REPS. */
    volatile int n = NQ_N;
    uint64_t t = 0;
    for (int r = 0; r < NQ_REPS; r++)
        t += nq_solve(n, 0, 0, 0);
    return t;
}

/* ------------------------------------------------------------------ */
/* 7. syscall : getppid loop                                           */
/* ------------------------------------------------------------------ */
#define SYS_ITERS 3000000ULL

static uint64_t bench_syscall(void)
{
    uint64_t s = 0;
    for (uint64_t i = 0; i < SYS_ITERS; i++)
        s += (uint64_t)getppid();
    return s;
}

/* ------------------------------------------------------------------ */

struct entry {
    const char *name;
    const char *unit;
    double work; /* nominal work units, for a rate */
};

static void run_one(const char *name)
{
    double t0, t1;
    uint64_t r = 0;
    double work = 0;
    const char *unit = "Mops";

    if (!strcmp(name, "alu")) {
        t0 = now_s();
        r = bench_alu();
        t1 = now_s();
        work = (double)ALU_ITERS / 1e6;
    } else if (!strcmp(name, "alu4")) {
        t0 = now_s();
        r = bench_alu4();
        t1 = now_s();
        work = (double)LS4_ITERS / 1e6;
    } else if (!strcmp(name, "ls4")) {
        uint64_t *arr = calloc(LS4_SLOTS, sizeof(uint64_t));
        if (!arr) { fprintf(stderr, "OOM\n"); exit(2); }
        t0 = now_s();
        r = bench_ls4(arr);
        t1 = now_s();
        work = (double)LS4_ITERS / 1e6;
        free(arr);
    } else if (!strcmp(name, "ls4tlb")) {
        uint64_t *arr = calloc(TLB_SLOTS, sizeof(uint64_t));
        if (!arr) { fprintf(stderr, "OOM\n"); exit(2); }
        /* pre-fault only the pages we will touch */
        for (uint64_t p = 0; p < TLB_PAGES; p++)
            arr[p * 512] = p;
        t0 = now_s();
        r = bench_ls4tlb(arr);
        t1 = now_s();
        work = (double)TLB_ITERS / 1e6;
        free(arr);
    } else if (!strcmp(name, "ptr8")) {
        t0 = now_s();
        r = bench_chase(CHASE8_BYTES, CHASE8_STEPS);
        t1 = now_s();
        work = (double)CHASE8_STEPS / 1e6;
    } else if (!strcmp(name, "ptr256")) {
        t0 = now_s();
        r = bench_chase(CHASE256_BYTES, CHASE256_STEPS);
        t1 = now_s();
        work = (double)CHASE256_STEPS / 1e6;
    } else if (!strcmp(name, "stream")) {
        t0 = now_s();
        r = bench_stream();
        t1 = now_s();
        work = (double)STREAM_BYTES * STREAM_REPS / 1e6; /* MB touched */
        unit = "MB/s";
    } else if (!strcmp(name, "matmul")) {
        t0 = now_s();
        r = bench_matmul();
        t1 = now_s();
        work = 2.0 * MM_N * MM_N * MM_N * MM_REPS / 1e6;
    } else if (!strcmp(name, "nqueens")) {
        t0 = now_s();
        r = bench_nqueens();
        t1 = now_s();
        work = (double)NQ_REPS;
        unit = "solves/s";
    } else if (!strcmp(name, "syscall")) {
        t0 = now_s();
        r = bench_syscall();
        t1 = now_s();
        work = (double)SYS_ITERS / 1e6;
    } else {
        fprintf(stderr, "unknown bench '%s'\n", name);
        exit(2);
    }

    double dt = t1 - t0;
    printf("BENCH %-8s %10.4f s  %12.3f %s  chk=%016llx\n", name, dt,
           dt > 0 ? work / dt : 0.0, unit, (unsigned long long)r);
    fflush(stdout);
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    static const char *all[] = { "alu",    "alu4",   "ls4",     "ls4tlb",
                                 "ptr8",   "ptr256", "stream",  "matmul",
                                 "nqueens", "syscall" };
    if (argc < 2 || !strcmp(argv[1], "all")) {
        printf("BENCHVER %s\n", BENCH_VERSION);
        for (unsigned i = 0; i < sizeof(all) / sizeof(all[0]); i++)
            run_one(all[i]);
    } else {
        printf("BENCHVER %s\n", BENCH_VERSION);
        for (int i = 1; i < argc; i++)
            run_one(argv[i]);
    }
    printf("BENCHDONE\n");
    return 0;
}
