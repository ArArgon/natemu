/*
 * INV-01-01 capability probe.
 *
 * Emits one JSON object to stdout describing, for this host/OS/SDK, whether
 * a set of public macOS APIs behave as the platform-facts question (Q-01-01)
 * requires, and the falsifier facts KA-01 (MAP_JIT + pthread_jit_write_protect_np)
 * needs. KA-02 (Hypervisor.framework x86-64 execution on Apple Silicon) is
 * decided documentarily, not by this probe — see INV-01-01.md "Resolution".
 * The Hypervisor section here is an input to Q-01-08/Q-01-12/F3 only: whether
 * an entitled process on this host can create a VM/vCPU and read one arm64
 * system register. Nothing here selects an architecture or a design; it only
 * records what the host does.
 *
 * Build: Apple clang only, macOS SDK headers only. No third-party
 * dependency. See ../README.md for the source-to-claim mapping and
 * ../../../docs/research/investigations/INV-01-01.md for the full method.
 *
 * Usage: probe [--negative] [--label STRING]
 *   --negative   Deliberately misreport the JIT-toggle executed_value field
 *                (see check.py) so the checker must flag the run FAIL. This
 *                is the required negative control / fault-injection mode;
 *                it produces no claim.
 *   --label      Free-text string echoed into the JSON "label" field so
 *                run.sh can tell an unentitled invocation from an
 *                ad-hoc-signed-with-entitlement invocation apart in the
 *                raw artifact, and so check.py can key its required-outcome
 *                rules on it. Escaped before interpolation (REV-039 C10).
 */

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <libkern/OSCacheControl.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <setjmp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/random.h>
#include <sys/resource.h>
#include <sys/sysctl.h>
#include <unistd.h>

#include <mach/mach.h>
#include <mach/mach_time.h>
#include <mach/mach_vm.h>
#include <mach/thread_policy.h>

#include <Hypervisor/Hypervisor.h>

/* ---- crash containment (REV-039 C3): a fault handler is installed
 * process-wide, but must only ever jump into a sigsetjmp scope that is
 * currently live. g_jmp_armed tracks that; a fault outside a live scope
 * restores the default disposition and re-raises, so the process dies with
 * the real signal instead of resuming into an already-returned frame.
 * ponytail: sigsetjmp/siglongjmp with one shared jmp_buf and an armed flag,
 * not a full signal-safe re-entrant JSON writer or a subprocess-per-section
 * design; upgrade to the latter if a real fault ever needs a saved register
 * dump (that is Q-01-06/INV-01-06 territory, not this probe's). */
static volatile sig_atomic_t g_fault_signal = 0;
static volatile sig_atomic_t g_jmp_armed = 0;
static sigjmp_buf g_jmp;

static void fault_handler(int sig) {
    if (!g_jmp_armed) {
        signal(sig, SIG_DFL);
        raise(sig);
        return; /* unreached */
    }
    g_fault_signal = sig;
    siglongjmp(g_jmp, 1);
}

static void install_fault_handlers(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = fault_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
    sigaction(SIGILL, &sa, NULL);
    sigaction(SIGTRAP, &sa, NULL);
}

/* BEGIN_GUARDED / END_GUARDED bracket one risky region so every guarded
 * section arms/disarms the same way (REV-039 C3). */
#define BEGIN_GUARDED() (g_jmp_armed = 1, sigsetjmp(g_jmp, 1))
#define END_GUARDED() (g_jmp_armed = 0)

static int g_negative = 0;
static char g_label[256] = "";

/* mov w0, #42 ; ret  (toggle path) and mov w0, #43 ; ret (callback path) —
 * deliberately different constants (REV-039 C2) so a callback that writes
 * nothing cannot be confused with one that ran: the toggle path's write
 * would otherwise leave 42 in the region for the callback path to
 * "discover" without having written anything itself. Assembled with the
 * local Apple clang arm64 backend, verified with llvm-objdump; see
 * INV-01-01 source inventory. */
static const unsigned char kJitCode42[] = {0x40, 0x05, 0x80, 0x52,
                                            0xC0, 0x03, 0x5F, 0xD6};
static const unsigned char kJitCode43[] = {0x60, 0x05, 0x80, 0x52,
                                            0xC0, 0x03, 0x5F, 0xD6};
#define JIT_EXPECTED_VALUE 42
#define JIT_CALLBACK_EXPECTED_VALUE 43

/* json_escape_into: minimal in-place-safe escaping for a string
 * interpolated into JSON (REV-039 C10 — --label was unescaped). ponytail:
 * backslash/quote/control-char only, not a general JSON encoder. */
static void json_escape_into(const char *in, char *out, size_t outsz) {
    size_t o = 0;
    for (const unsigned char *p = (const unsigned char *)in; *p && o + 2 < outsz; p++) {
        if (*p == '"' || *p == '\\') {
            out[o++] = '\\';
            out[o++] = (char)*p;
        } else if (*p < 0x20) {
            out[o++] = ' ';
        } else {
            out[o++] = (char)*p;
        }
    }
    out[o] = '\0';
}

/* ---------------------------------------------------------------------- */
/* sysctl helpers                                                          */

static int sysctl_str(const char *name, char *buf, size_t bufsz) {
    size_t len = bufsz;
    if (sysctlbyname(name, buf, &len, NULL, 0) != 0) {
        buf[0] = '\0';
        return -1;
    }
    if (len >= bufsz) len = bufsz - 1;
    buf[len] = '\0';
    return 0;
}

static int sysctl_u64(const char *name, uint64_t *out) {
    size_t len = sizeof(*out);
    return sysctlbyname(name, out, &len, NULL, 0);
}

static int sysctl_i32(const char *name, int32_t *out) {
    size_t len = sizeof(*out);
    return sysctlbyname(name, out, &len, NULL, 0);
}

/* ---------------------------------------------------------------------- */

static void print_host(void) {
    char model[128], machine[128], osversion[128], osproduct[128];
    uint64_t memsize = 0;
    int32_t ncpu = 0, pagesize = 0;
    int32_t hv_support = -1;

    sysctl_str("hw.model", model, sizeof(model));
    sysctl_str("hw.machine", machine, sizeof(machine));
    sysctl_str("kern.osversion", osversion, sizeof(osversion));
    sysctl_str("kern.osproductversion", osproduct, sizeof(osproduct));
    sysctl_u64("hw.memsize", &memsize);
    sysctl_i32("hw.ncpu", &ncpu);
    sysctl_i32("hw.pagesize", &pagesize);
    /* kern.hv_support (REV-038 M5): Apple's documented runtime discriminator
     * for Hypervisor-API availability, independent of entitlement/signing.
     * See apple-hypervisor-overview.json "Requirements" paragraph. -1 (and
     * hv_support_readable:false) means the sysctl itself was unreadable, not
     * that support is absent. */
    int hv_support_ok = (sysctl_i32("kern.hv_support", &hv_support) == 0);

    printf("  \"host\": {\n");
    printf("    \"hw_model\": \"%s\",\n", model);
    printf("    \"hw_machine\": \"%s\",\n", machine);
    printf("    \"hw_memsize\": %" PRIu64 ",\n", memsize);
    printf("    \"hw_ncpu\": %d,\n", ncpu);
    printf("    \"hw_pagesize\": %d,\n", pagesize);
    printf("    \"kern_osversion\": \"%s\",\n", osversion);
    printf("    \"kern_osproductversion\": \"%s\",\n", osproduct);
    printf("    \"kern_hv_support_readable\": %s,\n",
           hv_support_ok ? "true" : "false");
    printf("    \"kern_hv_support\": %d\n", hv_support_ok ? hv_support : -1);
    printf("  },\n");
}

/* ---------------------------------------------------------------------- */
/* MAP_JIT mmap                                                            */

static void *g_jit_region = NULL;
static size_t g_jit_size = 0;

static void print_map_jit(void) {
    long pagesize = sysconf(_SC_PAGESIZE);
    g_jit_size = (size_t)pagesize;
    errno = 0;
    g_jit_region = mmap(NULL, g_jit_size, PROT_READ | PROT_WRITE | PROT_EXEC,
                         MAP_JIT | MAP_ANON | MAP_PRIVATE, -1, 0);
    int ok = (g_jit_region != MAP_FAILED);
    int err = ok ? 0 : errno;

    printf("  \"map_jit\": {\n");
    printf("    \"mmap_ok\": %s,\n", ok ? "true" : "false");
    printf("    \"errno\": %d,\n", err);
    printf("    \"size\": %zu\n", g_jit_size);
    printf("  },\n");

    if (!ok) g_jit_region = NULL;
}

/* ---------------------------------------------------------------------- */
/* pthread_jit_write_protect_np toggle + execute, and the callback family.
 *
 * Both APIs are exercised in the SAME ad-hoc-signed, no-special-entitlement
 * process. Per SRC-059 (the JIT porting article), the two are mutually
 * exclusive only once the com.apple.security.cs.jit-write-allowlist
 * entitlement is added ("your app can no longer call
 * pthread_jit_write_protect_np()"); per SRC-050 (pthread.h's own
 * @discussion for pthread_jit_write_with_callback_np), without that
 * entitlement the callback function "toggles protection of the MAP_JIT to
 * writeable, calls @callback and then toggles protection back to
 * executable, without validating that @callback is an allowed function" —
 * i.e. it is documented to behave like an unenforced version of the toggle
 * path, not a mutually exclusive one, in the configuration this probe
 * actually signs. The jit-write-allowlist-entitled configuration, in which
 * the two really are exclusive, is NOT tested by this probe — see
 * INV-01-01.md Limitations and REV-038 M3. */

typedef int (*jit_fn_t)(void);

static struct jit_cb_ctx {
    void *region;
} g_jit_cb_ctx;

static int jit_write_callback(void *ctx) {
    struct jit_cb_ctx *c = (struct jit_cb_ctx *)ctx;
    memcpy(c->region, kJitCode43, sizeof(kJitCode43));
    sys_icache_invalidate(c->region, sizeof(kJitCode43));
    return 0;
}
PTHREAD_JIT_WRITE_ALLOW_CALLBACKS_NP(jit_write_callback);

static void print_jit_write_protect(void) {
    int supported_np = pthread_jit_write_protect_supported_np();
    volatile int toggle_ok = 0;
    volatile int64_t executed_value = -1;
    volatile int reprotect_write_ok = 0;
    volatile int crashed = 0;
    volatile int crash_signal = 0;

    if (g_jit_region != NULL) {
        if (BEGIN_GUARDED() == 0) {
            /* writable */
            pthread_jit_write_protect_np(0);
            memcpy(g_jit_region, kJitCode42, sizeof(kJitCode42));
            sys_icache_invalidate(g_jit_region, sizeof(kJitCode42)); /* REV-039 C4 */
            /* executable */
            pthread_jit_write_protect_np(1);
            jit_fn_t fn = (jit_fn_t)g_jit_region;
            int rv = fn();
            executed_value = rv;
            toggle_ok = (rv == JIT_EXPECTED_VALUE);

            /* back to writable: confirm the region is still writable after
             * a full write/execute/write cycle, not merely once. */
            pthread_jit_write_protect_np(0);
            memcpy(g_jit_region, kJitCode42, sizeof(kJitCode42));
            sys_icache_invalidate(g_jit_region, sizeof(kJitCode42));
            reprotect_write_ok = 1;
            pthread_jit_write_protect_np(1);
            END_GUARDED();
        } else {
            END_GUARDED();
            crashed = 1;
            crash_signal = (int)g_fault_signal;
        }
    }

    /* pthread_jit_write_with_callback_np family: exercised only if the
     * plain toggle path above did not already crash the region. Zero the
     * region first so a callback that writes nothing cannot "succeed" by
     * reading back a stale value the toggle path left (REV-039 C2). */
    volatile int callback_ok = 0;
    volatile int64_t callback_value = -1;
    if (g_jit_region != NULL && !crashed) {
        if (BEGIN_GUARDED() == 0) {
            pthread_jit_write_protect_np(0);
            memset(g_jit_region, 0, g_jit_size);
            sys_icache_invalidate(g_jit_region, g_jit_size);
            pthread_jit_write_protect_np(1);

            g_jit_cb_ctx.region = g_jit_region;
            int cb_rv = pthread_jit_write_with_callback_np(jit_write_callback,
                                                             &g_jit_cb_ctx);
            if (cb_rv == 0) {
                jit_fn_t fn = (jit_fn_t)g_jit_region;
                callback_value = fn();
                callback_ok = (callback_value == JIT_CALLBACK_EXPECTED_VALUE);
            }
            END_GUARDED();
        } else {
            END_GUARDED();
            crashed = 1;
            crash_signal = crash_signal ? crash_signal : (int)g_fault_signal;
        }
    }

    if (g_negative) {
        /* Negative control: mis-report the executed value while leaving
         * toggle_ok as measured. A truthful run can only ever print 42
         * here when toggle_ok is true; check.py asserts exactly that. */
        executed_value = 0xBAD;
    }

    printf("  \"jit_write_protect\": {\n");
    printf("    \"pthread_jit_write_protect_supported_np\": %d,\n",
           supported_np);
    printf("    \"toggle_ok\": %s,\n", toggle_ok ? "true" : "false");
    printf("    \"executed_value\": %" PRId64 ",\n", (int64_t)executed_value);
    printf("    \"expected_value\": %d,\n", JIT_EXPECTED_VALUE);
    printf("    \"write_after_reprotect_ok\": %s,\n",
           reprotect_write_ok ? "true" : "false");
    printf("    \"crashed\": %s,\n", crashed ? "true" : "false");
    printf("    \"crash_signal\": %d,\n", crash_signal);
    printf("    \"write_with_callback_np_ok\": %s,\n",
           callback_ok ? "true" : "false");
    printf("    \"write_with_callback_np_value\": %" PRId64 ",\n",
           (int64_t)callback_value);
    printf("    \"write_with_callback_np_expected_value\": %d\n",
           JIT_CALLBACK_EXPECTED_VALUE);
    printf("  },\n");

    if (g_jit_region != NULL) {
        munmap(g_jit_region, g_jit_size);
        g_jit_region = NULL;
    }
}

/* ---------------------------------------------------------------------- */
/* mach_vm_remap page aliasing. Guarded (REV-039 C3): this section had no
 * sigsetjmp of its own and one injected fault here made the process loop
 * forever inside the shared handler. */

static void print_mach_vm_remap(void) {
    long pagesize = sysconf(_SC_PAGESIZE);
    size_t size = (size_t)pagesize;
    volatile int ok = 0;
    volatile kern_return_t kr = KERN_FAILURE;
    volatile int alias_write_visible = 0;
    volatile int crashed = 0;
    volatile int crash_signal = 0;

    if (BEGIN_GUARDED() == 0) {
        void *src = mmap(NULL, size, PROT_READ | PROT_WRITE,
                          MAP_ANON | MAP_PRIVATE, -1, 0);
        if (src != MAP_FAILED) {
            *(volatile uint64_t *)src = 0x1111111111111111ULL;

            mach_vm_address_t target = 0;
            vm_prot_t cur_prot = 0, max_prot = 0;
            kern_return_t r =
                mach_vm_remap(mach_task_self(), &target, (mach_vm_size_t)size,
                               0, VM_FLAGS_ANYWHERE, mach_task_self(),
                               (mach_vm_address_t)(uintptr_t)src, FALSE,
                               &cur_prot, &max_prot, VM_INHERIT_DEFAULT);
            kr = r;
            ok = (r == KERN_SUCCESS);
            if (ok) {
                uint64_t via_alias = *(volatile uint64_t *)(uintptr_t)target;
                int read_matches = (via_alias == 0x1111111111111111ULL);

                *(volatile uint64_t *)(uintptr_t)target = 0x2222222222222222ULL;
                uint64_t via_src = *(volatile uint64_t *)src;
                int write_matches = (via_src == 0x2222222222222222ULL);

                alias_write_visible = read_matches && write_matches;
                munmap((void *)(uintptr_t)target, size);
            }
            munmap(src, size);
        }
        END_GUARDED();
    } else {
        END_GUARDED();
        crashed = 1;
        crash_signal = (int)g_fault_signal;
    }

    printf("  \"mach_vm_remap\": {\n");
    printf("    \"ok\": %s,\n", ok ? "true" : "false");
    printf("    \"kr\": %d,\n", (int)kr);
    printf("    \"alias_write_visible\": %s,\n",
           alias_write_visible ? "true" : "false");
    printf("    \"crashed\": %s,\n", crashed ? "true" : "false");
    printf("    \"crash_signal\": %d\n", crash_signal);
    printf("  },\n");
}

/* ---------------------------------------------------------------------- */
/* mach_absolute_time / mach_timebase_info. Two adjacent reads are a smoke
 * check only (REV-038 M9): they show the clock did not go backward between
 * two calls microseconds apart, not that it is monotonic in general, and
 * they measure no resolution. Q-01-07 owns resolution and cross-thread
 * latency. */

static void print_mach_timebase(void) {
    mach_timebase_info_data_t info = {0, 0};
    kern_return_t kr = mach_timebase_info(&info);
    uint64_t t0 = mach_absolute_time();
    uint64_t t1 = mach_absolute_time();

    printf("  \"mach_timebase\": {\n");
    printf("    \"kr\": %d,\n", (int)kr);
    printf("    \"numer\": %u,\n", info.numer);
    printf("    \"denom\": %u,\n", info.denom);
    printf("    \"two_sample_smoke_check_nondecreasing\": %s\n",
           (t1 >= t0) ? "true" : "false");
    printf("  },\n");
}

/* ---------------------------------------------------------------------- */
/* QoS and P/E-core affinity. thread_policy_set's raw kern_return_t is
 * reported for a future claim to interpret (REV-038 M8); this probe makes
 * no claim about it. The thread port thread_policy_set/mach_thread_self
 * opens is deallocated (REV-039 C10 advisory). */

static void print_qos_affinity(void) {
    int qos_rv = pthread_set_qos_class_self_np(QOS_CLASS_USER_INITIATED, 0);
    int qos_errno = (qos_rv != 0) ? qos_rv : 0;

    thread_affinity_policy_data_t policy = {1};
    thread_port_t self_thread = mach_thread_self();
    kern_return_t aff_kr = thread_policy_set(
        self_thread, THREAD_AFFINITY_POLICY, (thread_policy_t)&policy,
        THREAD_AFFINITY_POLICY_COUNT);
    mach_port_deallocate(mach_task_self(), self_thread);

    printf("  \"qos_affinity\": {\n");
    printf("    \"pthread_set_qos_class_self_np_rv\": %d,\n", qos_rv);
    printf("    \"pthread_set_qos_class_self_np_errno\": %d,\n", qos_errno);
    printf("    \"thread_policy_set_affinity_kr\": %d\n", (int)aff_kr);
    printf("  },\n");
}

/* ---------------------------------------------------------------------- */

static void print_getentropy(void) {
    unsigned char buf[32];
    memset(buf, 0, sizeof(buf));
    errno = 0;
    int rv = getentropy(buf, sizeof(buf));
    int err = (rv != 0) ? errno : 0;
    int all_zero = 1;
    for (size_t i = 0; i < sizeof(buf); i++) {
        if (buf[i] != 0) {
            all_zero = 0;
            break;
        }
    }

    printf("  \"getentropy\": {\n");
    printf("    \"ok\": %s,\n", (rv == 0) ? "true" : "false");
    printf("    \"errno\": %d,\n", err);
    printf("    \"bytes_requested\": %zu,\n", sizeof(buf));
    printf("    \"all_zero_suspicious\": %s\n", all_zero ? "true" : "false");
    printf("  },\n");
}

/* ---------------------------------------------------------------------- */

static void print_posix_openpt(void) {
    errno = 0;
    int fd = posix_openpt(O_RDWR | O_NOCTTY);
    int ok = (fd >= 0);
    int err = ok ? 0 : errno;
    if (ok) close(fd);

    printf("  \"posix_openpt\": {\n");
    printf("    \"ok\": %s,\n", ok ? "true" : "false");
    printf("    \"errno\": %d\n", err);
    printf("  },\n");
}

/* ---------------------------------------------------------------------- */

static void print_setrlimit_nofile(void) {
    struct rlimit before, after;
    getrlimit(RLIMIT_NOFILE, &before);

    /* REV-039 C10: guard against RLIM_INFINITY*2 wrapping (which would
     * silently lower the limit while still reporting set_ok true). */
    rlim_t requested;
    if (before.rlim_cur == RLIM_INFINITY || before.rlim_cur > RLIM_INFINITY / 2) {
        requested = before.rlim_cur;
    } else {
        requested = before.rlim_cur * 2;
    }
    if (before.rlim_max != RLIM_INFINITY && requested > before.rlim_max) {
        requested = before.rlim_max;
    }
    struct rlimit want = {requested, before.rlim_max};
    errno = 0;
    int rv = setrlimit(RLIMIT_NOFILE, &want);
    int err = (rv != 0) ? errno : 0;
    getrlimit(RLIMIT_NOFILE, &after);

    printf("  \"setrlimit_nofile\": {\n");
    printf("    \"soft_before\": %lld,\n", (long long)before.rlim_cur);
    printf("    \"hard_before\": %lld,\n", (long long)before.rlim_max);
    printf("    \"requested_soft\": %lld,\n", (long long)requested);
    printf("    \"set_ok\": %s,\n", (rv == 0) ? "true" : "false");
    printf("    \"errno\": %d,\n", err);
    printf("    \"soft_after\": %lld\n", (long long)after.rlim_cur);
    printf("  },\n");
}

/* ---------------------------------------------------------------------- */
/* Hypervisor.framework. KA-02 (x86-64 execution on Apple Silicon) is NOT
 * decided by anything in this function — see INV-01-01.md "Resolution":
 * the arm64 SDK build this probe links against has no x86-64 VM surface to
 * call at all (Hypervisor.h #ifdef __arm64__ vs #elif __x86_64__), so no
 * probe on this platform could exercise it. This function is an input to
 * Q-01-08/Q-01-12/family F3 only: whether an entitled process can create a
 * VM/vCPU and read one arm64 system register. Each call not attempted
 * reports null, not a plausible-looking error code (REV-039 C5); every
 * hv_return_t is rendered as unsigned hex (REV-039 C6) so the artifact
 * literally carries the documented constants (HV_SUCCESS "0x00000000",
 * HV_DENIED "0xfae94007"). */

static void print_hv_hr(const char *key, int attempted, hv_return_t hr,
                         int trailing_comma) {
    if (attempted) {
        printf("    \"%s\": \"0x%08x\"%s\n", key, (unsigned)hr,
               trailing_comma ? "," : "");
    } else {
        printf("    \"%s\": null%s\n", key, trailing_comma ? "," : "");
    }
}

static void print_hypervisor(void) {
    volatile int crashed = 0;
    volatile int crash_signal = 0;

    volatile hv_return_t vm_create_hr = HV_ERROR;
    volatile int vm_create_ok = 0;

    volatile int max_vcpu_attempted = 0;
    volatile hv_return_t max_vcpu_hr = HV_ERROR;
    volatile int max_vcpu_ok = 0;
    volatile uint32_t max_vcpu_count = 0;

    volatile int vcpu_attempted = 0;
    volatile hv_return_t vcpu_create_hr = HV_ERROR;
    volatile int vcpu_create_ok = 0;

    volatile int sysreg_attempted = 0;
    volatile hv_return_t sysreg_hr = HV_ERROR;
    volatile int sysreg_ok = 0;
    volatile uint64_t mmfr0 = 0;

    if (BEGIN_GUARDED() == 0) {
        hv_return_t r = hv_vm_create(NULL);
        vm_create_hr = r;
        vm_create_ok = (r == HV_SUCCESS);

        if (vm_create_ok) {
            max_vcpu_attempted = 1;
            uint32_t count = 0;
            r = hv_vm_get_max_vcpu_count(&count);
            max_vcpu_hr = r;
            max_vcpu_ok = (r == HV_SUCCESS);
            max_vcpu_count = count;

            vcpu_attempted = 1;
            hv_vcpu_t vcpu;
            hv_vcpu_exit_t *exit_info = NULL;
            r = hv_vcpu_create(&vcpu, &exit_info, NULL);
            vcpu_create_hr = r;
            vcpu_create_ok = (r == HV_SUCCESS);

            if (vcpu_create_ok) {
                sysreg_attempted = 1;
                uint64_t value = 0;
                r = hv_vcpu_get_sys_reg(vcpu, HV_SYS_REG_ID_AA64MMFR0_EL1,
                                         &value);
                sysreg_hr = r;
                sysreg_ok = (r == HV_SUCCESS);
                mmfr0 = value;
                hv_vcpu_destroy(vcpu);
            }
            hv_vm_destroy();
        }
        END_GUARDED();
    } else {
        END_GUARDED();
        crashed = 1;
        crash_signal = (int)g_fault_signal;
    }

    printf("  \"hypervisor\": {\n");
    print_hv_hr("hv_vm_create_hr", 1, vm_create_hr, 1);
    printf("    \"hv_vm_create_ok\": %s,\n", vm_create_ok ? "true" : "false");
    printf("    \"hv_vm_get_max_vcpu_count_attempted\": %s,\n",
           max_vcpu_attempted ? "true" : "false");
    print_hv_hr("hv_vm_get_max_vcpu_count_hr", max_vcpu_attempted,
                max_vcpu_hr, 1);
    printf("    \"hv_vm_get_max_vcpu_count_ok\": %s,\n",
           max_vcpu_ok ? "true" : "false");
    if (max_vcpu_attempted) {
        printf("    \"hv_vm_get_max_vcpu_count_value\": %u,\n",
               (unsigned)max_vcpu_count);
    } else {
        printf("    \"hv_vm_get_max_vcpu_count_value\": null,\n");
    }
    printf("    \"vcpu_attempted\": %s,\n", vcpu_attempted ? "true" : "false");
    print_hv_hr("hv_vcpu_create_hr", vcpu_attempted, vcpu_create_hr, 1);
    printf("    \"hv_vcpu_create_ok\": %s,\n",
           vcpu_create_ok ? "true" : "false");
    printf("    \"hv_vcpu_get_sys_reg_id_aa64mmfr0_el1_attempted\": %s,\n",
           sysreg_attempted ? "true" : "false");
    print_hv_hr("hv_vcpu_get_sys_reg_id_aa64mmfr0_el1_hr", sysreg_attempted,
                sysreg_hr, 1);
    printf("    \"hv_vcpu_get_sys_reg_id_aa64mmfr0_el1_ok\": %s,\n",
           sysreg_ok ? "true" : "false");
    if (sysreg_ok) {
        printf("    \"id_aa64mmfr0_el1_raw\": \"0x%016" PRIx64 "\",\n",
               (uint64_t)mmfr0);
    } else {
        printf("    \"id_aa64mmfr0_el1_raw\": null,\n");
    }
    printf("    \"crashed\": %s,\n", crashed ? "true" : "false");
    printf("    \"crash_signal\": %d\n", crash_signal);
    printf("  }\n");
}

/* ---------------------------------------------------------------------- */

int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--negative") == 0) {
            g_negative = 1;
        } else if (strcmp(argv[i], "--label") == 0 && i + 1 < argc) {
            json_escape_into(argv[++i], g_label, sizeof(g_label));
        }
    }

    install_fault_handlers();

    printf("{\n");
    printf("  \"schema_version\": 2,\n");
    printf("  \"negative_control\": %s,\n", g_negative ? "true" : "false");
    printf("  \"label\": \"%s\",\n", g_label);

    print_host();
    print_map_jit();
    print_jit_write_protect();
    print_mach_vm_remap();
    print_mach_timebase();
    print_qos_affinity();
    print_getentropy();
    print_posix_openpt();
    print_setrlimit_nofile();
    print_hypervisor();

    printf("}\n");
    if (fflush(stdout) != 0) {
        /* REV-039 C10: a short/failed write must not look like a clean
         * empty run to a downstream reader that only checks the exit code
         * — signal it on stderr and a distinct exit status. */
        fprintf(stderr, "probe: fflush(stdout) failed: %s\n", strerror(errno));
        return 3;
    }
    return 0;
}
