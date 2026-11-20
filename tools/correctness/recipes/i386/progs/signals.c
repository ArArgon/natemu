/* signals: 32-bit signal frames and sigreturn. SA_SIGINFO handlers for faults (the saved EIP, error code, trap number
   and fault address; the handler steps over the instruction), INT3 and a single-step trap, the x87 and SSE state in
   the frame (read, changed by the handler, restored by sigreturn), an alternate stack, nesting, blocked and pending
   signals, the kernel's own return trampolines (handlers installed without SA_RESTORER, both frame kinds),
   siglongjmp, SIGCHLD, a restarted system call, and timer signals arriving during x87 code. */
#define _GNU_SOURCE
#include <errno.h>
#include <setjmp.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <ucontext.h>
#include <unistd.h>

static void install(int sig, void (*h)(int, siginfo_t *, void *), int flags)
{
    struct sigaction sa; memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = h; sa.sa_flags = SA_SIGINFO | flags; sigemptyset(&sa.sa_mask);
    sigaction(sig, &sa, 0);
}

/* --- faults: the handler records the frame and steps over the instruction --- */
/* (what a handler writes is volatile: libc declares raise, kill and sigprocmask as functions that call nothing here) */
static volatile struct { int sig, code, trap, err; unsigned long addr, eip, cr2; } got;
static int skip;
static void fault(int sig, siginfo_t *si, void *v)
{
    ucontext_t *uc = v; greg_t *g = uc->uc_mcontext.gregs;
    got.sig = sig; got.code = si->si_code; got.trap = g[REG_TRAPNO]; got.err = g[REG_ERR];
    got.addr = (unsigned long)si->si_addr; got.eip = g[REG_EIP]; got.cr2 = uc->uc_mcontext.cr2;
    g[REG_EIP] += skip; g[REG_EFL] &= ~0x100;
}
static void show(const char *what, unsigned long insn)
{
    printf("%s: sig %d code %d trap %d err %x addr ", what, got.sig, got.code, got.trap, got.err);
    if (got.addr - insn + 16 < 32) printf("insn%+ld", (long)(got.addr - insn)); else printf("%lx", got.addr);
    printf(" cr2 %lx eip insn%+ld\n", got.cr2, (long)(got.eip - insn));
    memset((void *)&got, 0, sizeof got);
}
#define AT(label) ({ extern char label[]; (unsigned long)label; })

static sigjmp_buf jb;
static void jump(int sig, siginfo_t *si, void *v) { got.sig = sig; got.addr = (unsigned long)si->si_addr; got.err = ((ucontext_t *)v)->uc_mcontext.gregs[REG_ERR]; siglongjmp(jb, 1); }

/* --- the FPU state in the frame --- */
struct fx { unsigned short cw, r0, sw, r1; unsigned tag, ipoff, cssel, dataoff, datasel; unsigned char st[8][10];
            unsigned short status, magic; unsigned fxenv[6], mxcsr, rsv; unsigned char fxst[8][16], xmm[8][16]; };
static volatile struct { unsigned short cw, top, magic; unsigned char st0[10], st1[10], xmm0[4]; unsigned mxcsr; } fp;
static void fpu(int sig, siginfo_t *si, void *v)
{
    ucontext_t *uc = v; struct fx *f = (struct fx *)uc->uc_mcontext.fpregs;
    fp.cw = f->cw; fp.top = (f->sw >> 11) & 7; fp.magic = f->magic; fp.mxcsr = f->mxcsr;
    for (int i = 0; i < 10; i++) { fp.st0[i] = f->st[0][i]; fp.st1[i] = f->st[1][i]; }
    for (int i = 0; i < 4; i++) fp.xmm0[i] = f->xmm[0][i];
    static const unsigned char two_and_half[10] = {0, 0, 0, 0, 0, 0, 0, 0xa0, 0x00, 0x40};
    memcpy(f->st[0], two_and_half, 10);                     /* sigreturn loads the x87 registers from here */
    f->xmm[0][0] = 0x99;
    volatile double clobber = 3.0; clobber = clobber * clobber / 7.0;       /* the handler's own x87 use */
    uc->uc_mcontext.gregs[REG_ESI] = 0x51;
}

static volatile char log_[64]; static volatile int nlog;
static void note(char c) { log_[nlog] = c; nlog = nlog + 1; }
static void outer(int sig, siginfo_t *si, void *v) { note('1'); raise(SIGUSR2); note('3'); }
static void inner(int sig, siginfo_t *si, void *v) { note('2'); }

static char alt[16384]; static volatile int on_alt, code_usr, pid_ok;
static void onalt(int sig, siginfo_t *si, void *v) { char here; on_alt = &here >= alt && &here < alt + sizeof alt; code_usr = si->si_code; pid_ok = si->si_pid == getpid(); }

static volatile int old_seen, rt_seen;
static void old_handler(int sig) { old_seen = sig; }
static void rt_handler(int sig, siginfo_t *si, void *v) { rt_seen = si->si_signo; }

static volatile int chld_code, chld_status;
static void chld(int sig, siginfo_t *si, void *v) { chld_code = si->si_code; chld_status = si->si_status; }

static int wfd; static volatile int ticks;
static void feed(int sig, siginfo_t *si, void *v) { write(wfd, "r", 1); }
static void tick(int sig, siginfo_t *si, void *v)
{
    ticks++;
    volatile double a = 1.0; for (int i = 0; i < 20; i++) a = a * 1.5 + ticks;   /* disturbs x87 and its stack */
    char scratch[64]; memset(scratch, ticks, sizeof scratch);                    /* and the SSE registers libc uses */
}
static volatile int work_n = 300000;
__attribute__((noinline)) static double x87work(void)
{
    int n = work_n;
    double s = 0, p = 1;
    for (int i = 1; i <= n; i++) { p = p * 1.0000001 + 1.0 / i; s += p / (i + 0.5) - (double)(int)(s / 3); }
    return s;
}

int main(void)
{
    /* SIGSEGV: a store to an unmapped address (C7 00 imm32, 6 bytes) */
    install(SIGSEGV, fault, 0); skip = 6;
    __asm__ volatile("segv_insn: movl $1, (%%eax)" : : "a"(0x10) : "memory");
    show("store to 0x10", AT(segv_insn));
    /* SIGFPE: IDIV by zero (F7 F9) */
    install(SIGFPE, fault, 0); skip = 2;
    int q = 100, rem = 0; __asm__ volatile("fpe_insn: idivl %%ecx" : "+a"(q), "+d"(rem) : "c"(0) : "cc");
    show("idiv by zero", AT(fpe_insn));
    /* SIGILL: UD2 (0F 0B) */
    install(SIGILL, fault, 0); skip = 2;
    __asm__ volatile("ill_insn: ud2");
    show("ud2", AT(ill_insn));
    /* SIGTRAP: INT3 (the saved EIP is after it), then one single-step trap after POPF sets TF */
    install(SIGTRAP, fault, 0); skip = 0;
    __asm__ volatile("int3\nafter_int3:");
    show("int3", AT(after_int3));
    __asm__ volatile("pushfl\n orl $0x100, (%%esp)\n popfl\n nop\n after_step: nop" : : : "cc", "memory");
    show("single step", AT(after_step));

    /* a jump to an unmapped address: the fault is at the target; leave through siglongjmp */
    install(SIGSEGV, jump, SA_NODEFER);
    if (!sigsetjmp(jb, 1)) ((void (*)(void))0x20)();
    printf("call 0x20: sig %d addr %lx err %x\n", got.sig, got.addr, got.err);
    if (!sigsetjmp(jb, 1)) { volatile int *p = 0; got.code = *p; }
    printf("read of 0: sig %d addr %lx err %x\n", got.sig, got.addr, got.err);
    signal(SIGSEGV, SIG_DFL);

    /* the x87 and SSE state in the frame: 1.5 and 1.0 on the x87 stack and 0x11223344 in XMM0 when the signal arrives */
    install(SIGUSR2, fpu, 0);
    double d0 = 1.5, r0, r1; unsigned x0 = 0x11223344, xr; long esi = 0, eax = SYS_kill;
    __asm__ volatile("movd %[x0], %%xmm0\n fld1\n fldl %[d0]\n int $0x80\n fstpl %[r0]\n fstpl %[r1]\n movd %%xmm0, %[xr]"
                     : [r0] "=m"(r0), [r1] "=m"(r1), [xr] "=m"(xr), "+S"(esi), "+a"(eax)
                     : "b"(getpid()), "c"(SIGUSR2), [x0] "m"(x0), [d0] "m"(d0) : "memory", "cc");
    printf("frame: cw %x top %d magic %x mxcsr %x st0 ", fp.cw, fp.top, fp.magic, fp.mxcsr);
    for (int i = 9; i >= 0; i--) printf("%02x", fp.st0[i]);
    printf(" st1 ");
    for (int i = 9; i >= 0; i--) printf("%02x", fp.st1[i]);
    printf(" xmm0 %02x%02x%02x%02x\n", fp.xmm0[3], fp.xmm0[2], fp.xmm0[1], fp.xmm0[0]);
    printf("after sigreturn: st0 %g st1 %g xmm0 %x esi %lx\n", r0, r1, xr, esi);

    /* nesting, an alternate stack, kill's siginfo */
    install(SIGUSR1, outer, 0); install(SIGUSR2, inner, 0);
    raise(SIGUSR1); note('4'); printf("nested %s\n", (char *)log_);
    stack_t ss = { .ss_sp = alt, .ss_size = sizeof alt }; sigaltstack(&ss, 0);
    install(SIGUSR1, onalt, SA_ONSTACK); kill(getpid(), SIGUSR1);
    printf("alternate stack %d si_code %d si_pid %d\n", on_alt, code_usr, pid_ok);

    /* blocked, pending, delivered at the unblock */
    sigset_t set, old, pend; sigemptyset(&set); sigaddset(&set, SIGUSR2);
    nlog = 0; memset((char *)log_, 0, sizeof log_);
    sigprocmask(SIG_BLOCK, &set, &old); raise(SIGUSR2); note('a'); sigpending(&pend);
    sigprocmask(SIG_SETMASK, &old, 0); note('b');
    printf("blocked %s pending %d\n", (char *)log_, sigismember(&pend, SIGUSR2));

    /* handlers without SA_RESTORER: the kernel returns through its own trampolines (sigreturn and rt_sigreturn) */
    struct { void (*h)(int); unsigned long mask, flags; void *restorer; } osa = { old_handler, 0, 0, 0 };
    syscall(SYS_sigaction, SIGUSR1, &osa, 0); raise(SIGUSR1);
    struct { void *h; unsigned long flags; void *restorer; unsigned long mask[2]; } ksa = { rt_handler, SA_SIGINFO, 0, {0, 0} };
    syscall(SYS_rt_sigaction, SIGUSR2, &ksa, 0, 8); raise(SIGUSR2);
    printf("kernel trampolines: sigreturn %d rt_sigreturn %d\n", old_seen, rt_seen);

    /* SIGCHLD */
    install(SIGCHLD, chld, 0);
    pid_t c = fork(); if (!c) _exit(7);
    int st; while (waitpid(c, &st, 0) < 0 && errno == EINTR) ;
    printf("SIGCHLD code %d status %d\n", chld_code, chld_status);
    signal(SIGCHLD, SIG_DFL);

    /* a blocking read interrupted by SIGALRM: restarted with SA_RESTART (the handler feeds the pipe), EINTR without */
    int pfd[2]; char b; pipe(pfd); wfd = pfd[1];
    struct itimerval once = { {0, 0}, {0, 20000} }, off = { {0, 0}, {0, 0} };
    install(SIGALRM, feed, SA_RESTART); setitimer(ITIMER_REAL, &once, 0);
    int n = read(pfd[0], &b, 1); printf("restarted read %d '%c'\n", n, b);
    install(SIGALRM, rt_handler, 0); setitimer(ITIMER_REAL, &once, 0);
    errno = 0; n = read(pfd[0], &b, 1); printf("interrupted read %d errno %d\n", n, errno);

    /* timer signals during x87 code: the result is the undisturbed one */
    volatile double quiet = x87work(), noisy;       /* volatile: compared as doubles, not as x87 registers */
    struct itimerval fast = { {0, 1000}, {0, 1000} };
    install(SIGALRM, tick, 0); setitimer(ITIMER_REAL, &fast, 0);
    noisy = x87work(); int seen = ticks;
    for (int i = 0; i < 5000 && noisy == quiet && seen < 5; i++) { noisy = x87work(); seen = ticks; }
    setitimer(ITIMER_REAL, &off, 0);
    printf("x87 under timer signals: same %d signals %d\n", noisy == quiet, seen >= 5);
    return 0;
}
