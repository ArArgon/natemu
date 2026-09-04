/*
 * INV-01-14 per-instance resource-budget capability probe.
 *
 * Exercises, on this host/OS/SDK, the falsifier facts Q-01-14/KA-13 need:
 * whether macOS actually enforces RLIMIT_NOFILE, RLIMIT_CPU, and
 * RLIMIT_AS/RLIMIT_DATA (setrlimit(2), SRC-035, and <sys/resource.h>,
 * SRC-125); whether a DISPATCH_SOURCE_TYPE_MEMORYPRESSURE source can be
 * registered (SRC-127); and what task_info(TASK_VM_INFO) (SRC-074) reports,
 * including phys_footprint and region_count, before and after touching
 * 1 GiB of anonymous memory.
 *
 * Apple clang only, SDK headers only, no third-party dependency. Emits one
 * JSON object to stdout per invocation. See ../README.md and
 * ../../../docs/research/investigations/INV-01-14.md for the method this
 * probe implements.
 */
#include <sys/sysctl.h>
#include <sys/resource.h>
#include <sys/mman.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <dispatch/dispatch.h>
#include <mach/mach.h>
#include <mach/task_info.h>

static int g_negative = 0;
static const char *g_label = "unlabeled";
static volatile sig_atomic_t g_sigxcpu_caught = 0;

/* ---- small helpers ---- */

static long
sysctl_int(const char *name)
{
    int64_t v = 0;
    size_t sz = sizeof(v);
    if (sysctlbyname(name, &v, &sz, NULL, 0) != 0)
        return -1;
    return (long)v;
}

static void
json_str(const char *s)
{
    putchar('"');
    for (const char *p = s; *p; p++) {
        if (*p == '"' || *p == '\\')
            putchar('\\');
        putchar(*p);
    }
    putchar('"');
}

static int
task_vm_snapshot(struct task_vm_info *out)
{
    mach_msg_type_number_t count = TASK_VM_INFO_COUNT;
    kern_return_t kr = task_info(mach_task_self(), TASK_VM_INFO,
        (task_info_t)out, &count);
    return kr == KERN_SUCCESS;
}

/* ---- RLIMIT_NOFILE: lower the soft limit, open() until EMFILE ---- */

static void
check_rlimit_nofile(void)
{
    struct rlimit before, after;
    getrlimit(RLIMIT_NOFILE, &before);

    struct rlimit want = { .rlim_cur = 8, .rlim_max = before.rlim_max };
    int set_ok = (setrlimit(RLIMIT_NOFILE, &want) == 0);
    getrlimit(RLIMIT_NOFILE, &after);

    int opened_count = 0;
    int emfile_observed = 0;
    int errno_at_failure = 0;
    int fds[64];
    for (int i = 0; i < 64; i++) {
        errno = 0;
        int fd = open("/dev/null", O_RDONLY);
        if (fd < 0) {
            errno_at_failure = errno;
            emfile_observed = (errno == EMFILE);
            break;
        }
        fds[opened_count++] = fd;
    }
    for (int i = 0; i < opened_count; i++)
        close(fds[i]);

    /* restore before continuing with other checks in this process */
    setrlimit(RLIMIT_NOFILE, &before);

    printf("\"rlimit_nofile\":{\"soft_before\":%lld,\"hard_before\":%lld,"
        "\"set_ok\":%s,\"soft_after_set\":%lld,\"opened_count\":%d,"
        "\"emfile_observed\":%s,\"errno_at_failure\":%d}",
        (long long)before.rlim_cur, (long long)before.rlim_max,
        set_ok ? "true" : "false", (long long)after.rlim_cur,
        opened_count, emfile_observed ? "true" : "false", errno_at_failure);
}

/* ---- RLIMIT_CPU: 1s limit, spin, expect SIGXCPU ---- */

static void
sigxcpu_handler(int sig)
{
    (void)sig;
    g_sigxcpu_caught = 1;
}

static void
check_rlimit_cpu(void)
{
    struct sigaction sa, old_sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = sigxcpu_handler;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGXCPU, &sa, &old_sa);

    struct rlimit before;
    getrlimit(RLIMIT_CPU, &before);
    struct rlimit want = { .rlim_cur = 1, .rlim_max = before.rlim_max };
    int set_ok = (setrlimit(RLIMIT_CPU, &want) == 0);

    time_t wall_start = time(NULL);
    volatile double x = 1.0;
    while (!g_sigxcpu_caught && (time(NULL) - wall_start) < 10) {
        for (int i = 0; i < 10000000; i++)
            x = x * 1.0000001 + 1.0;
    }
    double elapsed = difftime(time(NULL), wall_start);

    sigaction(SIGXCPU, &old_sa, NULL);
    setrlimit(RLIMIT_CPU, &before);

    int delivered = g_negative ? 0 /* fabricated: negative control, see check.py */
                                : g_sigxcpu_caught;

    printf("\"rlimit_cpu\":{\"limit_seconds\":1,\"set_ok\":%s,"
        "\"sigxcpu_delivered\":%s,\"wall_elapsed_seconds_approx\":%.1f}",
        set_ok ? "true" : "false", delivered ? "true" : "false", elapsed);
}

/* ---- RLIMIT_AS / RLIMIT_DATA: attempt mmap/malloc beyond the limit ---- */

static void
check_rlimit_as_data(void)
{
    struct rlimit as_before;
    getrlimit(RLIMIT_AS, &as_before);
    struct rlimit as_want = { .rlim_cur = 64 * 1024 * 1024, .rlim_max = as_before.rlim_max };
    errno = 0;
    int as_set_ok = (setrlimit(RLIMIT_AS, &as_want) == 0);
    int as_set_errno = as_set_ok ? 0 : errno;

    errno = 0;
    size_t mmap_size = 256 * 1024 * 1024; /* 4x the 64 MiB RLIMIT_AS request */
    void *m = mmap(NULL, mmap_size, PROT_READ | PROT_WRITE,
        MAP_PRIVATE | MAP_ANON, -1, 0);
    int mmap_ok = (m != MAP_FAILED);
    int mmap_errno = mmap_ok ? 0 : errno;
    if (mmap_ok)
        munmap(m, mmap_size);

    setrlimit(RLIMIT_AS, &as_before);

    struct rlimit data_before;
    getrlimit(RLIMIT_DATA, &data_before);
    struct rlimit data_want = { .rlim_cur = 64 * 1024 * 1024, .rlim_max = data_before.rlim_max };
    errno = 0;
    int data_set_ok = (setrlimit(RLIMIT_DATA, &data_want) == 0);
    int data_set_errno = data_set_ok ? 0 : errno;

    errno = 0;
    size_t malloc_size = 256 * 1024 * 1024;
    void *p = malloc(malloc_size);
    int malloc_ok = (p != NULL);
    int malloc_errno = malloc_ok ? 0 : errno;
    if (malloc_ok) {
        memset(p, 0x5A, malloc_size); /* force real commit, not just reservation */
        free(p);
    }

    setrlimit(RLIMIT_DATA, &data_before);

    printf("\"rlimit_as\":{\"limit_bytes\":%lld,\"set_ok\":%s,\"set_errno\":%d,"
        "\"mmap_attempt_bytes\":%zu,\"mmap_ok\":%s,\"mmap_errno\":%d},",
        (long long)as_want.rlim_cur, as_set_ok ? "true" : "false", as_set_errno,
        mmap_size, mmap_ok ? "true" : "false", mmap_errno);
    printf("\"rlimit_data\":{\"limit_bytes\":%lld,\"set_ok\":%s,\"set_errno\":%d,"
        "\"malloc_attempt_bytes\":%zu,\"malloc_ok\":%s,\"malloc_errno\":%d}",
        (long long)data_want.rlim_cur, data_set_ok ? "true" : "false", data_set_errno,
        malloc_size, malloc_ok ? "true" : "false", malloc_errno);
}

/* ---- DISPATCH_SOURCE_TYPE_MEMORYPRESSURE registration ---- */

static void
check_memorypressure_source(void)
{
    dispatch_queue_t q = dispatch_queue_create("inv-01-14.memorypressure", NULL);
    dispatch_source_t src = dispatch_source_create(DISPATCH_SOURCE_TYPE_MEMORYPRESSURE,
        0, DISPATCH_MEMORYPRESSURE_NORMAL | DISPATCH_MEMORYPRESSURE_WARN |
        DISPATCH_MEMORYPRESSURE_CRITICAL, q);
    int create_ok = (src != NULL);
    int resume_ok = 0;
    if (create_ok) {
        dispatch_source_set_event_handler(src, ^{ /* no-op: registration test only */ });
        dispatch_resume(src);
        resume_ok = 1;
        dispatch_source_cancel(src);
        dispatch_release(src);
    }
    printf("\"memorypressure_source\":{\"create_ok\":%s,\"resume_ok\":%s}",
        create_ok ? "true" : "false", resume_ok ? "true" : "false");
}

/* ---- task_info(TASK_VM_INFO) before/after touching 1 GiB ---- */

static void
print_task_vm_info(const char *key, const struct task_vm_info *v, int ok)
{
    printf("\"%s\":{\"ok\":%s,\"virtual_size\":%llu,\"resident_size\":%llu,"
        "\"phys_footprint\":%llu,\"region_count\":%d}",
        key, ok ? "true" : "false",
        (unsigned long long)v->virtual_size,
        (unsigned long long)v->resident_size,
        (unsigned long long)v->phys_footprint,
        v->region_count);
}

static void
check_task_vm_info(void)
{
    struct task_vm_info before;
    memset(&before, 0, sizeof(before));
    int before_ok = task_vm_snapshot(&before);

    size_t touch_size = 1024ULL * 1024 * 1024; /* 1 GiB */
    void *m = mmap(NULL, touch_size, PROT_READ | PROT_WRITE,
        MAP_PRIVATE | MAP_ANON, -1, 0);
    int mmap_ok = (m != MAP_FAILED);
    if (mmap_ok) {
        unsigned char *p = (unsigned char *)m;
        long pagesize = sysctl_int("hw.pagesize");
        if (pagesize <= 0)
            pagesize = 4096;
        for (size_t off = 0; off < touch_size; off += (size_t)pagesize)
            p[off] = 0x7E; /* first-touch every page to force commit */
    }

    struct task_vm_info after;
    memset(&after, 0, sizeof(after));
    int after_ok = task_vm_snapshot(&after);

    if (mmap_ok)
        munmap(m, touch_size);

    long long increase = (before_ok && after_ok)
        ? (long long)after.phys_footprint - (long long)before.phys_footprint
        : -1;
    /* Allow headroom below the full 1 GiB for compressor/accounting skew;
     * 800 MiB is a conservative floor for "the touch was actually charged". */
    int increase_ok = (before_ok && after_ok && mmap_ok &&
        increase >= (800LL * 1024 * 1024));

    printf("\"mmap_touch_1gib_ok\":%s,", mmap_ok ? "true" : "false");
    print_task_vm_info("task_vm_info_before", &before, before_ok);
    printf(",");
    print_task_vm_info("task_vm_info_after_touch_1gib", &after, after_ok);
    printf(",\"phys_footprint_increase_bytes\":%lld,\"increase_at_least_800mib\":%s",
        increase, increase_ok ? "true" : "false");
}

int
main(int argc, char **argv)
{
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--negative") == 0) {
            g_negative = 1;
        } else if (strcmp(argv[i], "--label") == 0 && i + 1 < argc) {
            g_label = argv[++i];
        }
    }

    printf("{");
    printf("\"mode\":\"%s\",", g_negative ? "negative" : "normal");
    printf("\"label\":"); json_str(g_label); printf(",");
    printf("\"host\":{\"hw_pagesize\":%ld,\"hw_ncpu\":%ld,\"hw_memsize\":%ld},",
        sysctl_int("hw.pagesize"), sysctl_int("hw.ncpu"), sysctl_int("hw.memsize"));

    check_rlimit_nofile(); printf(",");
    check_rlimit_cpu(); printf(",");
    check_rlimit_as_data(); printf(",");
    check_memorypressure_source(); printf(",");
    check_task_vm_info();

    printf("}\n");
    return 0;
}
