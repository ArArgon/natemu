// coldtrip: the cost of one resident trip to guest code (docs/restructure/el0-sampler-plan.md, chunk 3).
//
//     coldtrip RUN SUMSFILE     the measured phases (init's bench=coldtrip; RUN 1 or 2)
//     coldtrip --sums           each footprint's work result only, untimed and without trips (build.sh pins them in init)
//
// A fixed work loop of ITERS iterations; every K iterations one CPUID (leaf 0, at CPL 3: natemu runs it in its resident,
// a full trip) in the `trip` variant, or in the `ctrl` variant the same branch (a countdown from K, the same test as
// i % K == K - 1 without a division) with an inline cheap instruction instead, the same registers named. K runs from
// "never" (k=0) down a ladder to every iteration. Two footprints:
//   small  a loop of a few blocks (the body, the rarely taken trip block, the join), about 16 multiply-rotate steps
//   large  FUNCS = 512 distinct functions, each STEPS multiply-rotate steps with its own constants: 256 KiB of code, in
//          64 chunks of 8 called in sequence (direct calls); iteration i runs chunk i mod 64 (through a table), so 64
//          iterations run the whole of it and the code an iteration runs was last run 64 iterations before. They are
//          generated here by the preprocessor and marked noipa, so the compiler can neither inline nor merge them
//          (gcc's identical-code folding needs equal bodies, and no two are).
// Every phase is preceded by a GAP_MS sleep, so the vCPU halts between phases: the host's bucket snapshots (every 5 ms) in the
// performance log show each phase as one busy stretch between two halted ones (tests/bench/coldtrip_report.py). The
// whole set runs ROUNDS + 1 times, round 0 being a warm-up (its translations); in each round, for each footprint and K,
// the trip variant and then the control.
//
// Console lines (stdout):
//   NATEMU-COLDTRIP begin run=R phases=P rounds=ROUNDS gap_ms=G funcs=F steps=S code_bytes=B vendor=V
//   NATEMU-COLDTRIP phase=I run=R round=N fp=small|large variant=trip|ctrl k=K iters=N trips=T tsc0=C tsc_ns=D sum=HEX
//   NATEMU-COLDTRIP end run=R ok=1|0
//   NATEMU-COLDTRIP sums small=HEX large=HEX          (also written to SUMSFILE, for init's check)
// tsc0 is the TSC at the phase's start and tsc_ns its length (natemu's TSC counts nanoseconds of the host's clock, from an
// offset fixed at the VM's start). trips counts the K branch's instructions
// executed (CPUID in trip phases): ITERS / K, 0 for k=0. Exit 0, 1 if a phase's sum or trip count was wrong, 2 on a
// usage error.
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define ROUNDS 3
#define GAP_MS 100
#define SMALL_ITERS 2000000ull
#define LARGE_ITERS 1024000ull
#define FUNCS 512
#define STEPS 16
static const uint64_t K_SMALL[] = {0, 64, 16, 4, 2, 1};
static const uint64_t K_LARGE[] = {0, 64, 16, 4, 2, 1};
#define NK(a) (sizeof(a) / sizeof(a[0]))

static inline uint64_t rotl(uint64_t x, unsigned r) { return (x << r) | (x >> (64 - r)); }
static inline uint64_t rdtsc(void) {
    uint32_t lo, hi;
    __asm__ volatile("rdtsc" : "=a"(lo), "=d"(hi));
    return (uint64_t)hi << 32 | lo;
}

// One multiply-rotate step with constants from a seed s and a step number j (both compile-time constants).
#define STEP(s, j)                                                                                                     \
    x = rotl(x ^ ((uint64_t)(s) * 0x9e3779b97f4a7c15ull + (uint64_t)(j) * 0xd1b54a32d192ed03ull),                       \
             (unsigned)(((s) + (j) * 7) % 63 + 1)) *                                                                    \
        (((uint64_t)(s) * 0x2545f4914f6cdd1dull + (uint64_t)(j) * 0x94d049bb133111ebull) | 1);
#define STEPS16(s)                                                                                                     \
    STEP(s, 0) STEP(s, 1) STEP(s, 2) STEP(s, 3) STEP(s, 4) STEP(s, 5) STEP(s, 6) STEP(s, 7) STEP(s, 8) STEP(s, 9)      \
    STEP(s, 10) STEP(s, 11) STEP(s, 12) STEP(s, 13) STEP(s, 14) STEP(s, 15)

// The large footprint: FUNCS = 512 functions f_000 .. f_777 (octal names; the name read as an octal number is the
// function's seed), chunks g_00 .. g_77 each calling the eight f_ that share its digits, and the table of chunks.
#define DEF(d) __attribute__((noipa)) uint64_t f_##d(uint64_t x) { STEPS16(0##d + 1000) return x; }
#define CALL(d) x = f_##d(x);
#define CHUNK(p) __attribute__((noipa)) static uint64_t g_##p(uint64_t x) { Q8(CALL, p) return x; }
#define Q8(M, p) M(p##0) M(p##1) M(p##2) M(p##3) M(p##4) M(p##5) M(p##6) M(p##7)   /* O8 again: a macro cannot nest itself */
#define REF(p) g_##p,
#define O8(M, p) M(p##0) M(p##1) M(p##2) M(p##3) M(p##4) M(p##5) M(p##6) M(p##7)
#define O64(M, p) O8(M, p##0) O8(M, p##1) O8(M, p##2) O8(M, p##3) O8(M, p##4) O8(M, p##5) O8(M, p##6) O8(M, p##7)
#define O512(M) O64(M, 0) O64(M, 1) O64(M, 2) O64(M, 3) O64(M, 4) O64(M, 5) O64(M, 6) O64(M, 7)
#define P64(M) O8(M, 0) O8(M, 1) O8(M, 2) O8(M, 3) O8(M, 4) O8(M, 5) O8(M, 6) O8(M, 7)
O512(DEF)
P64(CHUNK)
static uint64_t (*const CHUNKS[64])(uint64_t) = {P64(REF)};
static inline uint64_t large_work(uint64_t x, uint64_t i) { return CHUNKS[i & 63](x); }

// The small footprint's body: one seed, inlined into its loop.
static inline uint64_t small_work(uint64_t x, uint64_t i) {
    (void)i;
    STEPS16(7)
    return x;
}

static volatile uint32_t sink;

// The K branch: CPUID leaf 0, or in the control the same registers through one inline XOR.
#define TRIP                                                                                                           \
    {                                                                                                                  \
        uint32_t a = 0, b, c = 0, d;                                                                                   \
        __asm__ volatile("cpuid" : "+a"(a), "=b"(b), "+c"(c), "=d"(d));                                               \
        sink = a;                                                                                                      \
    }
#define CTRL                                                                                                           \
    {                                                                                                                  \
        uint32_t a = 0, b, c = 0, d;                                                                                   \
        __asm__ volatile("xor %%ebx, %%ebx" : "+a"(a), "=b"(b), "+c"(c), "=d"(d));                                    \
        sink = a;                                                                                                      \
    }
// A loop: n iterations of WORK(x ^ i, i), the K branch taken when the countdown from k reaches 0 (never for k = 0).
#define LOOP(name, WORK, INSN)                                                                                         \
    __attribute__((noipa)) static uint64_t name(uint64_t x, uint64_t n, uint64_t k, uint64_t *trips) {                 \
        uint64_t c = k ? k : n + 1, t = 0;                                                                             \
        for (uint64_t i = 0; i < n; i++) {                                                                             \
            x = WORK(x ^ i, i);                                                                                        \
            if (__builtin_expect(--c == 0, 0)) {                                                                       \
                c = k;                                                                                                 \
                INSN t++;                                                                                              \
            }                                                                                                          \
        }                                                                                                              \
        *trips = t;                                                                                                    \
        return x;                                                                                                      \
    }
LOOP(small_trip, small_work, TRIP)
LOOP(small_ctrl, small_work, CTRL)
LOOP(large_trip, large_work, TRIP)
LOOP(large_ctrl, large_work, CTRL)

#define SEED 0x243f6a8885a308d3ull

struct fp {
    const char *name;
    uint64_t iters;
    const uint64_t *ks;
    size_t nk;
    uint64_t (*loop[2])(uint64_t, uint64_t, uint64_t, uint64_t *);
};
static const struct fp FPS[] = {{"small", SMALL_ITERS, K_SMALL, NK(K_SMALL), {small_trip, small_ctrl}},
                                {"large", LARGE_ITERS, K_LARGE, NK(K_LARGE), {large_trip, large_ctrl}}};
static const char *const VARIANTS[] = {"trip", "ctrl"};

int main(int argc, char **argv) {
    uint64_t sums[2], t;
    if (argc == 2 && !strcmp(argv[1], "--sums")) {      // k = 0 in the control loop: the work alone
        for (int f = 0; f < 2; f++) sums[f] = FPS[f].loop[1](SEED, FPS[f].iters, 0, &t);
        printf("small=%016llx large=%016llx\n", (unsigned long long)sums[0], (unsigned long long)sums[1]);
        return 0;
    }
    if (argc != 3) {
        fprintf(stderr, "usage: coldtrip RUN SUMSFILE | coldtrip --sums\n");
        return 2;
    }
    int run = atoi(argv[1]), ok = 1, phase = 0, phases = 0;
    for (int f = 0; f < 2; f++) phases += (ROUNDS + 1) * (int)FPS[f].nk * 2;
    uintptr_t lo = (uintptr_t)f_000, hi = (uintptr_t)f_000;
#define SPAN(d) lo = (uintptr_t)f_##d < lo ? (uintptr_t)f_##d : lo; hi = (uintptr_t)f_##d > hi ? (uintptr_t)f_##d : hi;
    O512(SPAN)
    uint32_t v[4] = {0};
    __asm__ volatile("cpuid" : "=a"(v[0]), "=b"(v[1]), "=c"(v[3]), "=d"(v[2]) : "a"(0), "c"(0));
    char vendor[13];
    memcpy(vendor, &v[1], 12);
    vendor[12] = 0;
    printf("NATEMU-COLDTRIP begin run=%d phases=%d rounds=%d gap_ms=%d funcs=%d steps=%d code_bytes=%llu vendor=%s\n", run,
           phases, ROUNDS, GAP_MS, FUNCS, STEPS, (unsigned long long)(hi - lo), vendor);
    fflush(stdout);
    const struct timespec gap = {0, GAP_MS * 1000000L};
    uint64_t want[2] = {0, 0};
    int seen[2] = {0, 0};
    for (int round = 0; round <= ROUNDS; round++)
        for (int f = 0; f < 2; f++)
            for (size_t i = 0; i < FPS[f].nk; i++)
                for (int var = 0; var < 2; var++) {
                    uint64_t k = FPS[f].ks[i], trips;
                    nanosleep(&gap, NULL);
                    uint64_t t0 = rdtsc();
                    uint64_t sum = FPS[f].loop[var](SEED, FPS[f].iters, k, &trips);
                    uint64_t t1 = rdtsc();
                    if (!seen[f]) { want[f] = sum; seen[f] = 1; }
                    if (sum != want[f] || trips != (k ? FPS[f].iters / k : 0)) ok = 0;
                    printf("NATEMU-COLDTRIP phase=%d run=%d round=%d fp=%s variant=%s k=%llu iters=%llu trips=%llu tsc0=%llu "
                           "tsc_ns=%llu sum=%016llx\n", phase++, run, round, FPS[f].name, VARIANTS[var], (unsigned long long)k,
                           (unsigned long long)FPS[f].iters, (unsigned long long)trips, (unsigned long long)t0, (unsigned long long)(t1 - t0),
                           (unsigned long long)sum);
                    fflush(stdout);
                }
    nanosleep(&gap, NULL);
    printf("NATEMU-COLDTRIP end run=%d ok=%d\n", run, ok);
    char line[128];
    snprintf(line, sizeof line, "small=%016llx large=%016llx", (unsigned long long)want[0], (unsigned long long)want[1]);
    printf("NATEMU-COLDTRIP sums %s\n", line);
    fflush(stdout);
    FILE *fo = fopen(argv[2], "w");
    if (!fo || fprintf(fo, "%s\n", line) < 0 || fclose(fo)) { perror("coldtrip: sums file"); return 2; }
    return ok ? 0 : 1;
}
