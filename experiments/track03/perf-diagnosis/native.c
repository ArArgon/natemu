// (a) The six benchmark loops written in C, compiled for arm64 with clang -O2: same algorithm, data, footprint and stride.
//   native BENCH N [STRIDE MASK]  -> "ns/iter MEDIAN (min A max B) regs NAME=HEX ..." (slope t(2N)-t(N) over N, 7 rounds)
// The printed registers are the x86 registers the loop produces, for comparison with tools/bench/model.c (or mm.c).
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC_RAW, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
static uint64_t R[5]; static const char *RN[5];
static uint8_t *mem;
static uint64_t ld(uint64_t o) { uint64_t v; memcpy(&v, mem + o, 8); return v; }
static void st(uint64_t o, uint64_t v) { memcpy(mem + o, &v, 8); }

__attribute__((noinline)) static void b_int(uint64_t n) {
    uint64_t rax = 0x0123456789abcdef, rbx = 0x9e3779b97f4a7c15, rsi = 0xff51afd7ed558ccd, rdx = 0;
    for (uint64_t c = n; c; c--) { rax += rbx; rdx = rax << 13; rax ^= rdx; rdx = rax >> 7; rax ^= rdx; rax *= rsi; }
    R[0] = rax; RN[0] = "rax"; R[1] = rdx; RN[1] = "rdx";
}
__attribute__((noinline)) static void b_mem(uint64_t n, uint64_t stride, uint64_t mask) {
    uint64_t rsi = 0, r8 = 0, rax = 0;
    for (uint64_t c = n; c; c--) { rax = ld(rsi) + c; st(rsi, rax); r8 += rax; rsi = (rsi + stride) & mask; }
    R[0] = rax; RN[0] = "rax"; R[1] = r8; RN[1] = "r8"; R[2] = rsi; RN[2] = "rsi";
}
__attribute__((noinline)) uint64_t leaf(uint64_t rdi, uint64_t rsi) { return rdi * 3 + rsi; }
__attribute__((noinline)) static void b_call(uint64_t n) {
    uint64_t r8 = 0, rsi = 0x1234567, rax = 0;
    for (uint64_t c = n; c; c--) { rax = leaf(c, rsi); r8 += rax; }
    R[0] = rax; RN[0] = "rax"; R[1] = r8; RN[1] = "r8";
}
__attribute__((noinline)) static void b_branchy(uint64_t n) {
    uint64_t rax = 0x9e3779b97f4a7c15, mul = 6364136223846793005u, add = 1442695040888963407u, r8 = 0, r9 = 0, r10 = 0, rdx = 0;
    for (uint64_t c = n; c; c--) {
        rax = rax * mul + add; rdx = rax >> 40;
        if ((rdx & 1) == 0) r9 ^= rdx; else r8 += rdx;
        if ((rdx & 6) == 0) r10++;
    }
    R[0] = rax; RN[0] = "rax"; R[1] = r8; RN[1] = "r8"; R[2] = r9; RN[2] = "r9"; R[3] = r10; RN[3] = "r10"; R[4] = rdx; RN[4] = "rdx";
}
// branchy with the two branches kept (an empty volatile asm in each arm stops clang's if-conversion to csel/cinc)
__attribute__((noinline)) static void b_branchy_br(uint64_t n) {
    uint64_t rax = 0x9e3779b97f4a7c15, mul = 6364136223846793005u, add = 1442695040888963407u, r8 = 0, r9 = 0, r10 = 0, rdx = 0;
    for (uint64_t c = n; c; c--) {
        rax = rax * mul + add; rdx = rax >> 40;
        if ((rdx & 1) == 0) { __asm__ volatile(""); r9 ^= rdx; } else { __asm__ volatile("" ::: "memory"); r8 += rdx; }
        if ((rdx & 6) == 0) { __asm__ volatile(""); r10++; }
    }
    R[0] = rax; RN[0] = "rax"; R[1] = r8; RN[1] = "r8"; R[2] = r9; RN[2] = "r9"; R[3] = r10; RN[3] = "r10"; R[4] = rdx; RN[4] = "rdx";
}
__attribute__((noinline)) static void b_mixed(uint64_t n) {
    uint64_t rdi = 0, r8 = 0, r9 = 1, r10 = 0, rax = 0, rdx = 0;
    for (uint64_t c = n; c; c--) {
        rax = ld(rdi); r8 += rax; rdx = mem[rdi + 1]; r9 *= 31; r9 ^= rdx; r10 = rax >> 17; r8 ^= r10; st(rdi, r8); rdi = (rdi + 8) & 0xfff8;
    }
    R[0] = rax; RN[0] = "rax"; R[1] = r8; RN[1] = "r8"; R[2] = r9; RN[2] = "r9"; R[3] = r10; RN[3] = "r10"; R[4] = rdx; RN[4] = "rdx";
}
static void fill(const char *b) {   // the benchmark's data: mem* zero (1 MiB array), mixed the LCG buffer (README "LCG fill")
    memset(mem, 0, 16 << 20);
    if (!strcmp(b, "mixed")) { uint64_t x = 1; for (int i = 0; i < 65536; i++) { x = x * 6364136223846793005u + 1442695040888963407u; mem[i] = x >> 56; } }
}
static void run(const char *b, uint64_t n, uint64_t stride, uint64_t mask) {
    if (!strcmp(b, "int")) b_int(n); else if (!strncmp(b, "mem", 3)) b_mem(n, stride, mask); else if (!strcmp(b, "call")) b_call(n);
    else if (!strcmp(b, "branchy")) b_branchy(n); else if (!strcmp(b, "branchy_br")) b_branchy_br(n); else if (!strcmp(b, "mixed")) b_mixed(n); else exit(2);
}
int main(int argc, char **argv) {
    const char *b = argv[1]; uint64_t n = strtoull(argv[2], 0, 0);
    uint64_t stride = argc > 3 ? strtoull(argv[3], 0, 0) : (!strcmp(b, "mem4096") ? 4096 : 64), mask = argc > 4 ? strtoull(argv[4], 0, 0) : 0xfffff;
    mem = aligned_alloc(16384, 16 << 20);
    double all[7];
    for (int rep = 0; rep < 8; rep++) {   // rep 0: warm-up
        double t[2];
        for (int k = 1; k <= 2; k++) { fill(b); double t0 = now(); run(b, n * k, stride, mask); t[k - 1] = now() - t0; }
        if (rep) all[rep - 1] = (t[1] - t[0]) / n * 1e9;
    }
    fill(b); run(b, n, stride, mask);   // registers of a clean run at N
    for (int i = 0; i < 7; i++) for (int j = i + 1; j < 7; j++) if (all[j] < all[i]) { double x = all[i]; all[i] = all[j]; all[j] = x; }
    printf("ns/iter %.4f (min %.4f max %.4f) regs", all[3], all[0], all[6]);
    for (int i = 0; i < 5 && RN[i]; i++) printf(" %s=%llx", RN[i], (unsigned long long)R[i]);
    printf("\n");
}
