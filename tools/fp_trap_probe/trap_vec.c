// UFE trap on vector and other op classes: is it precise (destination unchanged, FPSR untouched), and what does ESR say?
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#include <sys/ucontext.h>
static void h(int sig, siginfo_t* si, void* uc_) {
  ucontext_t* uc = uc_; __uint128_t v0 = uc->uc_mcontext->__ns.__v[0];
  printf("    sig %d trap: esr %#llx (ISS %#llx) fpsr %#x v0 = %016llx_%016llx\n", sig, (unsigned long long)uc->uc_mcontext->__es.__esr,
         (unsigned long long)(uc->uc_mcontext->__es.__esr & 0x1ffffff), uc->uc_mcontext->__ns.__fpsr, (unsigned long long)(v0 >> 64), (unsigned long long)v0);
  fflush(stdout); _exit(1);
}
typedef void (*fn)(void);
#define CASE(name, body) static void name(void) { __asm__ volatile( \
  "movz x0, #0x1111\n dup v0.2d, x0\n" body : : : "x0","x1","v0","v1","v2","v3","memory"); }
// lane0 normal (1.5*1.5, inexact? exact), lane1 = MIN_NORMAL*0.5 + inexact (tiny)
CASE(vmul2d, "fmov d1, #1.5\n movz x1, #0x10, lsl #48\n movk x1, #1\n mov v1.d[1], x1\n fmov v2.2d, #0.5\n movz x1, #0x3fd5, lsl #48\n movk x1, #0x5555, lsl #32\n movk x1, #0x5555, lsl #16\n movk x1, #0x5555\n mov v2.d[0], x1\n fmul v0.2d, v1.2d, v2.2d\n")
CASE(vmul4s, "fmov v1.4s, #1.5\n movz w1, #0x80, lsl #16\n movk w1, #1\n mov v1.s[2], w1\n fmov v2.4s, #0.5\n fmul v0.4s, v1.4s, v2.4s\n")
CASE(fmadd_tiny, "mov x1, #0x0010000000000000\n fmov d1, x1\n fmov d2, #0.5\n movi d3, #0\n fmadd d0, d1, d2, d3\n")
CASE(fcvt_ds, "mov x1, #0x3800000000000000\n fmov d1, x1\n fcvt s0, d1\n")
CASE(fcvtn, "mov x1, #0x3800000000000000\n fmov d1, x1\n dup v1.2d, x1\n fcvtn v0.2s, v1.2d\n")
CASE(fdiv_tiny, "mov x1, #0x0010000000000000\n fmov d1, x1\n fmov d2, #3.0\n fdiv d0, d1, d2\n")
CASE(frecpe_huge, "mov w1, #0x7f000000\n fmov s1, w1\n frecpe s0, s1\n")
CASE(fsub_exact_tiny, "movz x1, #0x10, lsl #48\n movk x1, #1\n fmov d1, x1\n mov x1, #0x0010000000000000\n fmov d2, x1\n fsub d0, d1, d2\n")
static void run(const char* n, fn f) { pid_t p = fork(); if (!p) { struct sigaction sa = {0}; sa.sa_sigaction = h; sa.sa_flags = SA_SIGINFO; sigaction(SIGFPE, &sa, 0); sigaction(SIGILL, &sa, 0);
  printf("%s\n", n); fflush(stdout); __asm__ volatile("msr fpsr, xzr\n msr fpcr, %0" :: "r"((uint64_t)0x800)); f(); uint64_t s; __asm__ volatile("mrs %0, fpsr" : "=r"(s)); printf("    no trap, fpsr %#llx\n", s); _exit(0); }
  int st; waitpid(p, &st, 0); if (WIFSIGNALED(st)) printf("    killed by signal %d\n", WTERMSIG(st)); }
int main(void) { setvbuf(stdout, 0, _IONBF, 0); run("fmul v.2d (lane1 tiny)", vmul2d); run("fmul v.4s (lane2 tiny)", vmul4s); run("fmadd tiny", fmadd_tiny); run("fcvt s<-d tiny", fcvt_ds);
  run("fcvtn tiny", fcvtn); run("fdiv tiny", fdiv_tiny); run("frecpe huge", frecpe_huge); run("fsub exact tiny", fsub_exact_tiny); }
