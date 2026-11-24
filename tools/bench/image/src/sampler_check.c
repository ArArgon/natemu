// sampler_check: the EL0 sampler's validity program (docs/restructure/el0-sampler-plan.md, chunk 0's validity case).
//
//     sampler_check RUN SUMSFILE        the timed phases (init's bench=sampler-check; RUN 1 or 2: run 2's PHASE lines are PHASE2)
//     sampler_check --sums              each phase's batch result only, untimed (build.sh pins them in init)
//
// Phases, each timed for PHASE_S seconds of the guest's CLOCK_MONOTONIC (cross: CROSS_S), in batches of fixed work started from the same
// state, so every batch of a phase gives the same result (checked; that result is the phase's checksum):
//   1 alu    a one-block ALU loop (inline assembly: the body and its backward jnz are one block)
//   2 call   a loop calling a small non-inlined function through a pointer (an indirect call, then its return)
//   3 cross  a loop storing 8 bytes across a page boundary (offset 4092 of a page-aligned pair of pages)
//   4 chase  dependent loads that miss the cache (a 128 MiB single-cycle permutation) with cheap words between them (skid)
//   5 ctrl   the control port: the alu loop with sampling on (window e1), then a stop (port F8H, value 2) and the loop
//            again (e2), then a start (F8H, 1) and the loop once more (e3); marks 5, 6, 7 before the three windows
// Before each phase a mark (port F9H) with the phase number: marks 1-7. Mark numbers in a bench boot: 1-127 are the
// program's (these), 0x80 + N (129, 130) the bench init's run N (tools/bench/image/scripts/init says the same). The ports are written (ioperm, outb; root) only when the
// kernel command line holds the word natemu.profile=1 (the runner adds it in sampling runs only, plan section 3): the
// program never probes for the device. Without it every phase runs the same and no port is touched.
//
// Console lines (stdout, line by line), in init's style:
//   NATEMU-SAMPLER begin run=R ports=on|off phase_s=S
//   NATEMU-SAMPLER start run=R phase=N name=NAME t=SECONDS                   (CLOCK_MONOTONIC at the phase's start)
//   NATEMU-BENCH-PHASE[2] bench=sampler-check jobs=1 name=NAME rc=0|1 guest_s=SECONDS   (tools/bench/run.py times these;
//                         it reads name, rc and guest_s and skips the bench= and jobs= words; jobs=1: one thread)
//   NATEMU-SAMPLER end run=R phase=N name=NAME t=SECONDS iters=I batches=B batch=HEX ok=1|0
//   NATEMU-SAMPLER sums alu=HEX call=HEX cross=HEX chase=HEX ctrl=HEX     (also written to SUMSFILE, for init's check)
// Exit 0, or 1 if a batch differed from the phase's first, 2 on a usage or set-up error.
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/io.h>
#include <sys/mman.h>
#include <time.h>

#define PHASE_S 1.0
// The crossing store spends about 99% of its time outside generated code (a resident trip), so its window is longer: at
// 250 us, 3 to 60 EL0 samples a second were seen (guest-sampler-check-sample, M2 Max); 8 s gives 20 or more.
#define CROSS_S 8.0
#define ALU_N (1u << 20)          // iterations per batch
#define CALL_N (1u << 18)
#define CROSS_N (1u << 16)        // a crossing store is a shared probe: about 0.3 us under natemu
#define CHASE_N (1u << 14)        // dependent loads per batch
#define CHASE_LOG2 24             // 2^24 entries of 8 bytes: 128 MiB
#define PORT_CTL 0xf8
#define PORT_MARK 0xf9

static int ports;                 // natemu.profile=1 on the kernel command line
static int run = 1;

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

static void out(uint16_t port, uint8_t v) {
    if (ports) __asm__ volatile("outb %0, %1" : : "a"(v), "Nd"(port));
}

static int profiled(void) {
    char buf[4096];
    FILE *f = fopen("/proc/cmdline", "r");
    if (!f) return 0;
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[n] = 0;
    for (char *w = strtok(buf, " \t\n"); w; w = strtok(NULL, " \t\n"))
        if (!strcmp(w, "natemu.profile=1")) return 1;
    return 0;
}

// 1 and 5: one block, ALU only.
static uint64_t alu(void) {
    uint64_t a = 0x0123456789abcdefull, b = 0xfedcba9876543210ull, c = 0x0f1e2d3c4b5a6978ull, m = 0x9e3779b97f4a7c15ull;
    uint64_t n = ALU_N;
    __asm__ volatile("1:\n\t"
                     "add %[a], %[b]\n\t"
                     "xor %[b], %[c]\n\t"
                     "rol $13, %[c]\n\t"
                     "imul %[m], %[a]\n\t"
                     "add %[c], %[a]\n\t"
                     "dec %[n]\n\t"
                     "jnz 1b"
                     : [a] "+r"(a), [b] "+r"(b), [c] "+r"(c), [n] "+r"(n)
                     : [m] "r"(m)
                     : "cc");
    return a ^ b ^ c;
}

// 2: an indirect call of a small function that is never inlined.
__attribute__((noipa)) static uint64_t small_fn(uint64_t x, uint64_t i) { return (x ^ i) * 0x2545f4914f6cdd1dull + 1; }
static uint64_t (*volatile call_ptr)(uint64_t, uint64_t) = small_fn;
static uint64_t call(void) {
    uint64_t x = 1;
    for (uint64_t i = 0; i < CALL_N; i++) x = call_ptr(x, i);
    return x;
}

// 3: an 8-byte store across a page boundary.
static uint8_t pages[8192] __attribute__((aligned(4096)));
static uint64_t cross(void) {
    uint8_t *p = pages + 4092;
    uint64_t v = 0x1122334455667788ull, n = CROSS_N, got;
    __asm__ volatile("1:\n\t"
                     "mov %[v], (%[p])\n\t"
                     "add $3, %[v]\n\t"
                     "dec %[n]\n\t"
                     "jnz 1b"
                     : [v] "+r"(v), [n] "+r"(n)
                     : [p] "r"(p)
                     : "cc", "memory");
    memcpy(&got, p, 8);                  // the last value stored, read back across the boundary
    return got;
}

// 4: a single cycle over 2^CHASE_LOG2 slots: slot i holds (A i + C) mod 2^k, a full-period LCG (C odd, A = 1 mod 4).
static uint64_t *chain;
static int chase_setup(void) {
    size_t n = (size_t)1 << CHASE_LOG2;
    chain = mmap(NULL, n * 8, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (chain == MAP_FAILED) return -1;
    for (size_t i = 0; i < n; i++) chain[i] = (i * 0x5851f42d4c957f2dull + 0x14057b7ef767814full) & (n - 1);
    return 0;
}
static uint64_t chase(void) {
    uint64_t idx = 0, x = 1, y = 2, z = 3, n = CHASE_N;
    __asm__ volatile("1:\n\t"
                     "mov (%[base],%[idx],8), %[idx]\n\t"
                     "add $1, %[x]\n\t"
                     "xor %[x], %[y]\n\t"
                     "add %[y], %[z]\n\t"
                     "dec %[n]\n\t"
                     "jnz 1b"
                     : [idx] "+r"(idx), [x] "+r"(x), [y] "+r"(y), [z] "+r"(z), [n] "+r"(n)
                     : [base] "r"(chain)
                     : "cc", "memory");
    return idx ^ z;
}

struct phase { const char *name; uint64_t (*fn)(void); uint64_t iters; };
static const struct phase PHASES[] = {
    {"alu", alu, ALU_N}, {"call", call, CALL_N}, {"cross", cross, CROSS_N}, {"chase", chase, CHASE_N}};

// Batches of fn for `seconds` (at least two; untimed with seconds 0): the first batch's result, or ok = 0 when any
// batch differed from it.
static uint64_t timed(uint64_t (*fn)(void), double seconds, uint64_t *batches, int *ok) {
    double t0 = now();
    uint64_t first = fn(), b = 1;
    *ok = 1;
    do {
        if (fn() != first) *ok = 0;
        b++;
    } while (seconds > 0 && now() - t0 < seconds);
    *batches = b;
    return first;
}

// One timed window: its start line, the batches, its PHASE and end lines.
static uint64_t window(int number, const char *name, uint64_t (*fn)(void), uint64_t iters, double seconds, int *all_ok) {
    double t0 = now();
    printf("NATEMU-SAMPLER start run=%d phase=%d name=%s t=%.6f\n", run, number, name, t0);
    fflush(stdout);
    uint64_t batches;
    int ok;
    uint64_t r = timed(fn, seconds, &batches, &ok);
    double t1 = now();
    printf("NATEMU-BENCH-PHASE%s bench=sampler-check jobs=1 name=%s rc=%d guest_s=%.3f\n", run == 1 ? "" : "2", name, !ok, t1 - t0);
    printf("NATEMU-SAMPLER end run=%d phase=%d name=%s t=%.6f iters=%llu batches=%llu batch=%016llx ok=%d\n", run, number, name,
           t1, (unsigned long long)(iters * batches), (unsigned long long)batches, (unsigned long long)r, ok);
    fflush(stdout);
    *all_ok &= ok;
    return r;
}

int main(int argc, char **argv) {
    int sums_only = argc == 2 && !strcmp(argv[1], "--sums");
    if (!sums_only && argc != 3) {
        fprintf(stderr, "usage: sampler_check RUN SUMSFILE | sampler_check --sums\n");
        return 2;
    }
    if (chase_setup()) { perror("sampler_check: mmap"); return 2; }
    memset(pages, 0, sizeof pages);
    uint64_t r[5];
    int all_ok = 1;
    if (sums_only) {
        for (int i = 0; i < 4; i++) {
            uint64_t b;
            int ok;
            r[i] = timed(PHASES[i].fn, 0, &b, &ok);
            all_ok &= ok;
        }
        r[4] = r[0];
    } else {
        run = atoi(argv[1]);
        ports = profiled();
        if (ports && ioperm(PORT_CTL, 2, 1)) { perror("NATEMU-SAMPLER ioperm"); return 2; }
        printf("NATEMU-SAMPLER begin run=%d ports=%s phase_s=%.1f\n", run, ports ? "on" : "off", PHASE_S);
        fflush(stdout);
        for (int i = 0; i < 4; i++) {
            out(PORT_MARK, i + 1);
            r[i] = window(i + 1, PHASES[i].name, PHASES[i].fn, PHASES[i].iters, PHASES[i].fn == cross ? CROSS_S : PHASE_S, &all_ok);
        }
        out(PORT_MARK, 5);                 // phase 5: the control port
        r[4] = window(5, "ctrl-on", alu, ALU_N, PHASE_S, &all_ok);
        out(PORT_CTL, 2);                  // stop
        out(PORT_MARK, 6);
        if (window(6, "ctrl-off", alu, ALU_N, PHASE_S, &all_ok) != r[4]) all_ok = 0;
        out(PORT_CTL, 1);                  // start
        out(PORT_MARK, 7);
        if (window(7, "ctrl-on-again", alu, ALU_N, PHASE_S, &all_ok) != r[4]) all_ok = 0;
    }
    char line[256];
    snprintf(line, sizeof line, "alu=%016llx call=%016llx cross=%016llx chase=%016llx ctrl=%016llx", (unsigned long long)r[0],
             (unsigned long long)r[1], (unsigned long long)r[2], (unsigned long long)r[3], (unsigned long long)r[4]);
    printf("%s%s\n", sums_only ? "" : "NATEMU-SAMPLER sums ", line);
    fflush(stdout);
    if (!sums_only) {
        FILE *f = fopen(argv[2], "w");
        if (!f || fprintf(f, "%s\n", line) < 0 || fclose(f)) { perror("sampler_check: sums file"); return 2; }
    }
    return all_ok ? 0 : 1;
}
