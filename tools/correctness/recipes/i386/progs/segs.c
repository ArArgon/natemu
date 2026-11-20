/* segs: DS, ES, FS and GS loaded with a thread-area (GDT) data segment whose descriptor the program then clears with
   set_thread_area. The kernel reloads the register, which faults inside the kernel and ends up null; the program goes
   on, reads the null selector back and restores the register (the kernel selftest ldt_gdt's last test). */
#define _GNU_SOURCE
#include <asm/ldt.h>
#include <setjmp.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <ucontext.h>
#include <unistd.h>

static sigjmp_buf jb;
static volatile int got_sig, got_trap, got_err; static volatile unsigned long got_eip;
static volatile int marker = 42;
static void fault(int sig, siginfo_t *si, void *v)
{
    greg_t *g = ((ucontext_t *)v)->uc_mcontext.gregs;
    got_sig = sig; got_trap = g[REG_TRAPNO]; got_err = g[REG_ERR]; got_eip = g[REG_EIP];
    siglongjmp(jb, 1);
}

#define TEST(seg) do { \
    extern char seg##_start[]; \
    unsigned short prev, sel = idx * 8 + 3; long eax = SYS_set_thread_area; \
    d.entry_number = idx; syscall(SYS_set_thread_area, &d); \
    if (!sigsetjmp(jb, 1)) { \
        __asm__ volatile(#seg "_start: movw %%" #seg ", %[prev]\n movw %[sel], %%" #seg "\n pushl %%ebx\n movl %[arg], %%ebx\n int $0x80\n popl %%ebx\n" \
                         "movw %%" #seg ", %[sel]\n movw %[prev], %%" #seg \
                         : [prev] "=&r"(prev), [sel] "+r"(sel), "+a"(eax) : [arg] "r"(&clear) : "memory"); \
        printf(#seg " after its descriptor was cleared: %x, set_thread_area %ld, memory %d\n", sel, eax, marker); \
    } else \
        printf(#seg ": signal %d trap %d error %x at start+%ld\n", got_sig, got_trap, got_err, (long)(got_eip - (unsigned long)seg##_start)); \
} while (0)

int main(void)
{
    struct sigaction sa; memset(&sa, 0, sizeof sa); sa.sa_sigaction = fault; sa.sa_flags = SA_SIGINFO | SA_NODEFER;
    sigaction(SIGSEGV, &sa, 0); sigaction(SIGBUS, &sa, 0);
    struct user_desc d = { .entry_number = -1, .limit = 0xfffff, .seg_32bit = 1, .limit_in_pages = 1 }, clear;
    if (syscall(SYS_set_thread_area, &d)) { puts("no thread area"); return 1; }
    int idx = d.entry_number;
    memset(&clear, 0, sizeof clear); clear.entry_number = idx; clear.read_exec_only = 1; clear.seg_not_present = 1;
    TEST(ds); TEST(es); TEST(fs); TEST(gs);
    return 0;
}
