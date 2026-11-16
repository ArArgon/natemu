// Trapped FP exceptions on Apple silicon (user mode, macOS): which FPCR trap enables fire, are they precise, what ESR/FPSR show.
// Usage: ./trap_test   (runs each case in a forked child)
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#include <sys/ucontext.h>
#define FZ (1u << 24)
#define IOE (1u << 8)
#define DZE (1u << 9)
#define OFE (1u << 10)
#define UFE (1u << 11)
#define IXE (1u << 12)
#define IDE (1u << 15)
static volatile uint64_t pc_fault, d0_at_fault, esr;
static void h(int sig, siginfo_t* si, void* uc_) {
  ucontext_t* uc = uc_;
  pc_fault = uc->uc_mcontext->__ss.__pc;
  esr = uc->uc_mcontext->__es.__esr;
  uint64_t d0; memcpy(&d0, &uc->uc_mcontext->__ns.__v[0], 8);
  uint32_t fpsr = uc->uc_mcontext->__ns.__fpsr;
  printf("    SIGFPE code %d pc %#llx esr %#llx fpsr %#x v0(dest)=%#llx\n", si->si_code, pc_fault, esr, fpsr, d0);
  fflush(stdout); _exit(1);
}
static uint64_t bits(double d) { uint64_t u; memcpy(&u, &d, 8); return u; }
static double dbl(uint64_t u) { double d; memcpy(&d, &u, 8); return d; }
// op: d0 = d1 OP d2, d0 preset to a marker so precision can be judged
static void run(const char* name, uint32_t fpcr, int op, uint64_t a, uint64_t b) {
  pid_t p = fork();
  if (!p) {
    struct sigaction sa = {0}; sa.sa_sigaction = h; sa.sa_flags = SA_SIGINFO; sigaction(SIGFPE, &sa, 0); sigaction(SIGILL, &sa, 0);
    uint64_t out, fpsr;
    uint64_t marker = 0x1234567812345678ull;
    printf("%s: fpcr %#x\n", name, fpcr); fflush(stdout);
    __asm__ volatile(
      "msr fpsr, xzr\n msr fpcr, %[c]\n fmov d1, %[a]\n fmov d2, %[b]\n fmov d0, %[m]\n"
      "cmp %w[op], #0\n b.ne 1f\n fmul d0, d1, d2\n b 2f\n1: fadd d0, d1, d2\n2:\n"
      "fmov %[o], d0\n mrs %[s], fpsr\n msr fpcr, xzr\n"
      : [o] "=r"(out), [s] "=r"(fpsr) : [c] "r"((uint64_t)fpcr), [a] "r"(a), [b] "r"(b), [m] "r"(marker), [op] "r"(op) : "v0", "v1", "v2", "cc");
    printf("    no trap: result %#llx fpsr %#llx\n", out, fpsr); fflush(stdout); _exit(0);
  }
  int st; waitpid(p, &st, 0);
}
int main(void) {
  uint64_t den = 0x0008000000000000ull;           // denormal 2^-1023
  uint64_t one = bits(1.0), half = bits(0.5);
  uint64_t mn = 0x0010000000000000ull;            // MIN_NORMAL
  // band: MIN_NORMAL*(1-2^-60) is not representable; produce via mul: (1-2^-53) * MIN_NORMAL*(1+2^-52)... use a = 1-2^-53, b = MIN_NORMAL*(1+2^-52)
  uint64_t a_band = bits(1.0 - 0x1p-53), b_band = 0x0010000000000001ull;  // exact product = MIN_NORMAL*(1+2^-52)(1-2^-53) = MIN_NORMAL*(1+2^-53-2^-105): above MIN_NORMAL, not band
  uint64_t a_b2 = bits(1.0 - 0x1p-53), b_b2 = mn;  // exact = MIN_NORMAL*(1-2^-53): Arm tiny before rounding, rounds (bounded, ties-even) to MIN_NORMAL? tie between MIN_NORMAL-2^-1074 and MIN_NORMAL
  uint64_t a_b3 = bits(1.0 - 0x1p-54 - 0x1p-60), b_b3 = 0;
  (void)a_band; (void)b_band; (void)a_b3; (void)b_b3;
  run("IDE, FZ=0, denormal*1", IDE, 0, den, one);
  run("IDE+FZ, denormal*1", IDE | FZ, 0, den, one);
  run("IDE+FZ, 1*1 (no denormal)", IDE | FZ, 0, one, one);
  run("UFE, FZ=0, MIN_NORMAL*0.5 (exact denormal)", UFE, 0, mn, half);
  run("UFE, FZ=0, (MIN_NORMAL+ulp)*0.5 (inexact denormal)", UFE, 0, 0x0010000000000001ull, half);
  run("UFE, FZ=0, (1-2^-53)*MIN_NORMAL (band tie)", UFE, 0, a_b2, b_b2);
  run("UFE+FZ, MIN_NORMAL*0.5 (exact tiny, flushed)", UFE | FZ, 0, mn, half);
  run("IXE, 1/3-ish add", IXE, 1, one, bits(0x1p-60));
  run("IOE, inf*0", IOE, 0, bits(__builtin_inf()), 0);
  return 0;
}
