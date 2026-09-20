#define _GNU_SOURCE
#include <sys/ptrace.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/user.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <cpuid.h>
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#if !defined(__linux__) || !defined(__x86_64__)
#error This producer requires genuine Linux x86-64 headers and an x86-64 compiler.
#endif

/* Never called from C: the tracer supplies RIP, RSP and RDI at the initial stop. */
__asm__(".text\n"
        ".global plain_f1,plain_after,watch_ss,watch_after,fused_ss,fused_after\n"
        ".global enter_alloc,enter_alloc_after,enter_zero,enter_zero_after\n"
        "plain_f1: .byte 0xf1\nplain_after: .byte 0xcc\n"
        "watch_ss: .byte 0x8e,0x17,0x90\nwatch_after: .byte 0xcc\n"
        "fused_ss: .byte 0x8e,0x17,0xf1\nfused_after: .byte 0xcc\n"
        "enter_alloc: .byte 0xc8,0x00,0x10,0x00\n"
        "enter_alloc_after: .byte 0xcc\n"
        "enter_zero: .byte 0xc8,0x00,0x00,0x00\n"
        "enter_zero_after: .byte 0xcc\n");
extern const unsigned char plain_f1[], plain_after[], watch_ss[], watch_after[];
extern const unsigned char fused_ss[], fused_after[], enter_alloc[], enter_alloc_after[];
extern const unsigned char enter_zero[], enter_zero_after[];

static pid_t child = -1;
static double now(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) { perror("clock_gettime"); exit(2); }
    return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static int wait_bounded(pid_t p, int *status, double seconds)
{
    double until = now() + seconds;
    const struct timespec pause_time = {0, 10000000};
    do {
        pid_t r = waitpid(p, status, WNOHANG);
        if (r == p) return 0;
        if (r < 0 && errno != EINTR) return -1;
        if (nanosleep(&pause_time, NULL) != 0 && errno != EINTR) return -1;
    } while (now() < until);
    errno = ETIMEDOUT;
    return -1;
}

static void reap(void)
{
    if (child > 0) {
        int status;
        if (kill(child, SIGKILL) != 0 && errno != ESRCH) { perror("kill"); exit(2); }
        if (wait_bounded(child, &status, 2) != 0) { perror("reap"); exit(2); }
        printf("cleanup pid=%ld status=0x%x\n", (long)child, status);
        child = -1;
    }
}

static void fail(const char *what)
{
    perror(what);
    reap();
    exit(2);
}

static long peek(enum __ptrace_request request, uintptr_t address)
{
    errno = 0;
    long v = ptrace(request, child, (void *)address, NULL);
    if (v == -1 && errno != 0) fail("ptrace peek");
    return v;
}

static void poke_dr(unsigned index, unsigned long value)
{
    uintptr_t offset = offsetof(struct user, u_debugreg) + index * sizeof(unsigned long);
    if (ptrace(PTRACE_POKEUSER, child, (void *)offset, (void *)value) != 0)
        fail("PTRACE_POKEUSER");
}

static unsigned long read_dr(unsigned index)
{
    return (unsigned long)peek(PTRACE_PEEKUSER,
                             offsetof(struct user, u_debugreg) + index * sizeof(unsigned long));
}

static void dump(const char *phase, const struct user_regs_struct *r)
{
    printf("%s rip=%#llx rsp=%#llx rbp=%#llx eflags=%#llx "
           "rax=%#llx rbx=%#llx rcx=%#llx rdx=%#llx rsi=%#llx rdi=%#llx "
           "r8=%#llx r9=%#llx r10=%#llx r11=%#llx r12=%#llx r13=%#llx "
           "r14=%#llx r15=%#llx orig_rax=%#llx cs=%#llx ss=%#llx "
           "ds=%#llx es=%#llx fs=%#llx gs=%#llx fs_base=%#llx gs_base=%#llx\n",
           phase, r->rip, r->rsp, r->rbp, r->eflags, r->rax, r->rbx, r->rcx,
           r->rdx, r->rsi, r->rdi, r->r8, r->r9, r->r10, r->r11, r->r12,
           r->r13, r->r14, r->r15, r->orig_rax, r->cs, r->ss, r->ds, r->es,
           r->fs, r->gs, r->fs_base, r->gs_base);
}

static int run_case(unsigned number)
{
    static const char *names[] = {"plain_f1", "watch_movss_nop", "watch_movss_f1",
                                  "enter_final_absent", "enter_final_writable", "enter_alloc_zero"};
    const uint64_t sentinel = UINT64_C(0xa5c3e18796b4d2f0);
    const uint64_t old_bp = UINT64_C(0x1122334455667788);
    unsigned char *mapping = mmap(NULL, 3 * 4096, PROT_READ | PROT_WRITE,
                                 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (mapping == MAP_FAILED) fail("mmap");
    uintptr_t initial_sp = (uintptr_t)mapping + 8192;
    uintptr_t frame_word = initial_sp - 8;
    uintptr_t final_sp = initial_sp - 8 - 4096;
    uint16_t ss;
    __asm__ volatile("mov %%ss,%0" : "=r"(ss));
    memcpy(mapping + 8192, &ss, sizeof(ss));
    memcpy((void *)frame_word, &sentinel, sizeof(sentinel));
    if (number == 3 && mprotect(mapping, 4096, PROT_NONE) != 0) fail("mprotect");
    const unsigned char *entry = number == 0 ? plain_f1 : number == 1 ? watch_ss :
        number == 2 ? fused_ss : number == 5 ? enter_zero : enter_alloc;
    uintptr_t expected_rip = (uintptr_t)(number == 0 ? plain_after : number == 1 ?
        watch_after : number == 2 ? fused_after : number == 5 ? enter_zero_after : enter_alloc_after);
    if (number >= 3) expected_rip = number == 3 ? (uintptr_t)entry : expected_rip + 1;
    printf("case=%s entry=%#" PRIxPTR " expected_rip=%#" PRIxPTR
           " initial_sp=%#" PRIxPTR " final_sp=%#" PRIxPTR " frame_word=%#" PRIxPTR
           " sentinel=%#" PRIx64 " old_bp=%#" PRIx64 " selector=%#x\n",
           names[number], (uintptr_t)entry, expected_rip, initial_sp, final_sp,
           frame_word, sentinel, old_bp, ss);
    fflush(stdout);
    pid_t parent = getpid();
    child = fork();
    if (child < 0) fail("fork");
    if (child == 0) {
        if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() != parent) _exit(120);
        /* Resolve inherited code/selector pages and the frame page's COW now. */
        volatile unsigned char touched = 0;
        for (unsigned j = 0; j < 8; ++j)
            touched ^= ((const volatile unsigned char *)entry)[j];
        touched ^= *(volatile unsigned char *)(mapping + 8192);
        (void)touched;
        *(volatile uint64_t *)frame_word = sentinel;
        if (ptrace(PTRACE_TRACEME, 0, NULL, NULL) != 0) _exit(121);
        if (raise(SIGSTOP) != 0) _exit(122);
        _exit(123); /* Tracer must replace the stopped context. */
    }
    int status;
    if (wait_bounded(child, &status, 3) != 0) fail("initial wait");
    if (!WIFSTOPPED(status) || WSTOPSIG(status) != SIGSTOP) {
        fprintf(stderr, "invalid initial stop %#x\n", status); reap(); exit(2);
    }
    if (ptrace(PTRACE_SETOPTIONS, child, NULL, (void *)(uintptr_t)PTRACE_O_EXITKILL) != 0)
        fail("PTRACE_SETOPTIONS EXITKILL");
    struct user_regs_struct regs;
    if (ptrace(PTRACE_GETREGS, child, NULL, &regs) != 0) fail("GETREGS initial");
    regs.rip = (uintptr_t)entry;
    regs.rsp = initial_sp;
    regs.rbp = old_bp;
    regs.rdi = (uintptr_t)mapping + 8192;
    regs.orig_rax = (unsigned long long)-1;
    regs.eflags &= ~((1ULL << 8) | (1ULL << 16)); /* TF=RF=0. */
    if (ptrace(PTRACE_SETREGS, child, NULL, &regs) != 0) fail("SETREGS");
    poke_dr(7, 0);
    poke_dr(0, (unsigned long)regs.rdi);
    poke_dr(6, 0xffff0ff0UL);
    unsigned long dr7 = (number == 1 || number == 2) ? 1UL | (3UL << 16) | (1UL << 18) : 0;
    poke_dr(7, dr7);
    if (ptrace(PTRACE_GETREGS, child, NULL, &regs) != 0) fail("GETREGS readback");
    dump("armed", &regs);
    printf("armed dr0=%#lx dr6=%#lx dr7=%#lx\n", read_dr(0), read_dr(6), read_dr(7));
    if (regs.rip != (uintptr_t)entry || regs.rsp != initial_sp || regs.rbp != old_bp ||
        regs.rdi != (uintptr_t)mapping + 8192 || regs.orig_rax != (unsigned long long)-1 ||
        (regs.eflags & ((1ULL << 8) | (1ULL << 16))) || read_dr(0) != regs.rdi ||
        (read_dr(7) & ~0x400UL) != dr7 || (read_dr(6) & 0xfUL)) {
        fprintf(stderr, "context readback mismatch\n"); reap(); exit(2);
    }
    uint64_t before_word = (uint64_t)(unsigned long)peek(PTRACE_PEEKDATA, frame_word);
    unsigned long before_selector = (unsigned long)peek(PTRACE_PEEKDATA, (uintptr_t)mapping + 8192);
    printf("before frame_value=%#" PRIx64 " selector_word=%#lx entry_word=%#lx\n",
           before_word, before_selector, (unsigned long)peek(PTRACE_PEEKTEXT, (uintptr_t)entry));
    if (before_word != sentinel || (before_selector & 0xffffUL) != ss) {
        fprintf(stderr, "fixture readback mismatch\n"); reap(); exit(2);
    }
    if (ptrace(PTRACE_CONT, child, NULL, NULL) != 0) fail("CONT");
    if (wait_bounded(child, &status, 3) != 0) fail("observation wait");
    printf("first_stop status=%#x\n", status);
    if (!WIFSTOPPED(status)) { fprintf(stderr, "child did not stop\n"); reap(); exit(2); }
    siginfo_t info;
    if (ptrace(PTRACE_GETSIGINFO, child, NULL, &info) != 0) fail("GETSIGINFO");
    if (ptrace(PTRACE_GETREGS, child, NULL, &regs) != 0) fail("GETREGS observed");
    unsigned long dr6 = read_dr(6);
    uint64_t word = (uint64_t)(unsigned long)peek(PTRACE_PEEKDATA, frame_word);
    dump("observed", &regs);
    printf("signal=%d si_signo=%d si_code=%d si_errno=%d si_addr=%p "
           "dr0=%#lx dr6=%#lx dr7=%#lx B0=%lu BS=%lu frame_value=%#" PRIx64 "\n",
           WSTOPSIG(status), info.si_signo, info.si_code, info.si_errno, info.si_addr,
           read_dr(0), dr6, read_dr(7), dr6 & 1UL, (dr6 >> 14) & 1UL, word);
    int ok = regs.rip == expected_rip && info.si_signo == WSTOPSIG(status) && WSTOPSIG(status) == (number == 3 ? SIGSEGV : SIGTRAP);
    if (number == 0) ok = ok && !(dr6 & 0xfUL) && !(dr6 & (1UL << 14));
    if (number == 1) ok = ok && (dr6 & 0xfUL) == 1 && !(dr6 & (1UL << 14));
    if (number == 2) ok = ok && !(dr6 & 0xeUL) && !(dr6 & (1UL << 14));
    if (number == 3) ok = ok && (uintptr_t)info.si_addr == final_sp &&
        regs.rsp == initial_sp && regs.rbp == old_bp;
    if (number == 4 || number == 5) ok = ok && word == old_bp &&
        regs.rbp == frame_word && regs.rsp == (number == 4 ? final_sp : frame_word);
    printf("method=%s disputed_observation=%s\n", ok ? "valid" : "invalid",
           number == 2 ? "raw_DR6_not_a_PASS_oracle" : number == 3 ?
           "raw_frame_value_not_a_PASS_oracle" : "control");
    reap();
    if (munmap(mapping, 3 * 4096) != 0) fail("munmap");
    return ok;
}

int main(int argc, char **argv)
{
    int explore = argc == 2 && strcmp(argv[1], "--explore") == 0;
    if (argc != 1 && !explore) {
        fprintf(stderr, "usage: %s [--explore]\n", argv[0]); return 2;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("purpose=exploratory applicability_mode=%s\n",
           explore ? "explicit_any_x86_64" : "native_intel_guard");
    if (sysconf(_SC_PAGESIZE) != 4096) { fprintf(stderr, "requires 4096-byte pages\n"); return 2; }
    unsigned a, b, c, d, maxleaf;
    maxleaf = __get_cpuid_max(0, NULL);
    if (maxleaf < 1) { fprintf(stderr, "CPUID unavailable\n"); return 2; }
    __cpuid(0, a, b, c, d);
    char vendor[13];
    memcpy(vendor, &b, 4); memcpy(vendor + 4, &d, 4); memcpy(vendor + 8, &c, 4); vendor[12] = 0;
    __cpuid(1, a, b, c, d);
    unsigned family = (a >> 8) & 15U, model = (a >> 4) & 15U;
    if (family == 6 || family == 15) model |= ((a >> 16) & 15U) << 4;
    if (family == 15) family += (a >> 20) & 255U;
    printf("format=natemu.x86-semantics.v2 vendor=%s family=%u model=%u stepping=%u "
           "cpuid1_eax=%#x hypervisor=%u page_size=4096 pid=%ld\n",
           vendor, family, model, a & 15U, a, c >> 31, (long)getpid());
    if (!explore && (strcmp(vendor, "GenuineIntel") || (c & (1U << 31)))) {
        fprintf(stderr, "inapplicable native Intel context; no cases executed\n"); return 3;
    }
    for (unsigned i = 0; i < 6; ++i) if (!run_case(i)) return 2;
    puts("complete=6 controls_valid=1 architectural_verdict=unassigned");
    return 0;
}
