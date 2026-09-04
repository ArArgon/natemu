/*
 * INV-01-01 capability probe.
 *
 * Emits one JSON object to stdout describing, for this host/OS/SDK, whether
 * a set of public macOS APIs behave as the platform-facts question (Q-01-01)
 * and the kill-assumption register (KA-01, KA-02) require. Nothing here
 * selects an architecture or a design; it only records what the host does.
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
 *                raw artifact; it does not affect what the probe measures.
 */

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
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

/* ---- crash containment for the two risky sections (JIT execute, HVF vcpu
 * ops): a probe that crashes must not lose every earlier section's JSON.
 * ponytail: sigsetjmp/siglongjmp only, no full signal-safe re-entrant JSON
 * writer; upgrade to a subprocess-per-section design if a real fault ever
 * needs a saved register dump (that is Q-01-06/INV-01-06 territory, not
 * this probe's). */
static sig_atomic_t g_fault_signal = 0;
static sigjmp_buf g_jmp;

static void fault_handler(int sig) {
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

static int g_negative = 0;
static const char *g_label = "";

/* mov w0, #42 ; ret  (assembled with the local Apple clang arm64 backend,
 * verified with llvm-objdump; see INV-01-01 source inventory). */
static const unsigned char kJitCode[] = {0x40, 0x05, 0x80, 0x52,
                                          0xC0, 0x03, 0x5F, 0xD6};
#define JIT_EXPECTED_VALUE 42

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

    sysctl_str("hw.model", model, sizeof(model));
    sysctl_str("hw.machine", machine, sizeof(machine));
    sysctl_str("kern.osversion", osversion, sizeof(osversion));
    sysctl_str("kern.osproductversion", osproduct, sizeof(osproduct));
    sysctl_u64("hw.memsize", &memsize);
    sysctl_i32("hw.ncpu", &ncpu);
    sysctl_i32("hw.pagesize", &pagesize);

    printf("  \"host\": {\n");
    printf("    \"hw_model\": \"%s\",\n", model);
    printf("    \"hw_machine\": \"%s\",\n", machine);
    printf("    \"hw_memsize\": %" PRIu64 ",\n", memsize);
    printf("    \"hw_ncpu\": %d,\n", ncpu);
    printf("    \"hw_pagesize\": %d,\n", pagesize);
    printf("    \"kern_osversion\": \"%s\",\n", osversion);
    printf("    \"kern_osproductversion\": \"%s\"\n", osproduct);
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
/* pthread_jit_write_protect_np toggle + execute, and the callback family  */

typedef int (*jit_fn_t)(void);

static struct jit_cb_ctx {
    void *region;
} g_jit_cb_ctx;

static int jit_write_callback(void *ctx) {
    struct jit_cb_ctx *c = (struct jit_cb_ctx *)ctx;
    memcpy(c->region, kJitCode, sizeof(kJitCode));
    return 0;
}
PTHREAD_JIT_WRITE_ALLOW_CALLBACKS_NP(jit_write_callback);

static void print_jit_write_protect(void) {
    int supported_np = pthread_jit_write_protect_supported_np();
    int toggle_ok = 0;
    int64_t executed_value = -1;
    int reprotect_write_ok = 0;
    int crashed = 0;
    int crash_signal = 0;

    if (g_jit_region != NULL) {
        if (sigsetjmp(g_jmp, 1) == 0) {
            /* writable */
            pthread_jit_write_protect_np(0);
            memcpy(g_jit_region, kJitCode, sizeof(kJitCode));
            /* executable */
            pthread_jit_write_protect_np(1);
            jit_fn_t fn = (jit_fn_t)g_jit_region;
            int rv = fn();
            executed_value = rv;
            toggle_ok = (rv == JIT_EXPECTED_VALUE);

            /* back to writable: confirm the region is still writable after
             * a full write/execute/write cycle, not merely once. */
            pthread_jit_write_protect_np(0);
            memcpy(g_jit_region, kJitCode, sizeof(kJitCode));
            reprotect_write_ok = 1;
            pthread_jit_write_protect_np(1);
        } else {
            crashed = 1;
            crash_signal = (int)g_fault_signal;
        }
    }

    /* pthread_jit_write_with_callback_np family: exercised only if the
     * plain toggle path above did not already crash the region. */
    int callback_ok = 0;
    int64_t callback_value = -1;
    if (g_jit_region != NULL && !crashed) {
        if (sigsetjmp(g_jmp, 1) == 0) {
            g_jit_cb_ctx.region = g_jit_region;
            int cb_rv = pthread_jit_write_with_callback_np(jit_write_callback,
                                                             &g_jit_cb_ctx);
            if (cb_rv == 0) {
                jit_fn_t fn = (jit_fn_t)g_jit_region;
                callback_value = fn();
                callback_ok = (callback_value == JIT_EXPECTED_VALUE);
            }
        } else {
            /* leave callback_ok = 0; crash already recorded above only if
             * it happens in this block too. */
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
    printf("    \"executed_value\": %" PRId64 ",\n", executed_value);
    printf("    \"expected_value\": %d,\n", JIT_EXPECTED_VALUE);
    printf("    \"write_after_reprotect_ok\": %s,\n",
           reprotect_write_ok ? "true" : "false");
    printf("    \"crashed\": %s,\n", crashed ? "true" : "false");
    printf("    \"crash_signal\": %d,\n", crash_signal);
    printf("    \"write_with_callback_np_ok\": %s,\n",
           callback_ok ? "true" : "false");
    printf("    \"write_with_callback_np_value\": %" PRId64 "\n",
           callback_value);
    printf("  },\n");

    if (g_jit_region != NULL) {
        munmap(g_jit_region, g_jit_size);
        g_jit_region = NULL;
    }
}

/* ---------------------------------------------------------------------- */
/* mach_vm_remap page aliasing                                             */

static void print_mach_vm_remap(void) {
    long pagesize = sysconf(_SC_PAGESIZE);
    size_t size = (size_t)pagesize;
    void *src = mmap(NULL, size, PROT_READ | PROT_WRITE,
                      MAP_ANON | MAP_PRIVATE, -1, 0);
    int ok = 0;
    kern_return_t kr = KERN_FAILURE;
    int alias_write_visible = 0;

    if (src != MAP_FAILED) {
        *(volatile uint64_t *)src = 0x1111111111111111ULL;

        mach_vm_address_t target = 0;
        vm_prot_t cur_prot = 0, max_prot = 0;
        kr = mach_vm_remap(mach_task_self(), &target, (mach_vm_size_t)size,
                            0, VM_FLAGS_ANYWHERE, mach_task_self(),
                            (mach_vm_address_t)(uintptr_t)src, FALSE,
                            &cur_prot, &max_prot, VM_INHERIT_DEFAULT);
        ok = (kr == KERN_SUCCESS);
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

    printf("  \"mach_vm_remap\": {\n");
    printf("    \"ok\": %s,\n", ok ? "true" : "false");
    printf("    \"kr\": %d,\n", (int)kr);
    printf("    \"alias_write_visible\": %s\n",
           alias_write_visible ? "true" : "false");
    printf("  },\n");
}

/* ---------------------------------------------------------------------- */

static void print_mach_timebase(void) {
    mach_timebase_info_data_t info = {0, 0};
    kern_return_t kr = mach_timebase_info(&info);
    uint64_t t0 = mach_absolute_time();
    uint64_t t1 = mach_absolute_time();

    printf("  \"mach_timebase\": {\n");
    printf("    \"kr\": %d,\n", (int)kr);
    printf("    \"numer\": %u,\n", info.numer);
    printf("    \"denom\": %u,\n", info.denom);
    printf("    \"monotonic_nondecreasing\": %s\n",
           (t1 >= t0) ? "true" : "false");
    printf("  },\n");
}

/* ---------------------------------------------------------------------- */

static void print_qos_affinity(void) {
    int qos_rv = pthread_set_qos_class_self_np(QOS_CLASS_USER_INITIATED, 0);
    int qos_errno = (qos_rv != 0) ? qos_rv : 0;

    thread_affinity_policy_data_t policy = {1};
    kern_return_t aff_kr = thread_policy_set(
        mach_thread_self(), THREAD_AFFINITY_POLICY, (thread_policy_t)&policy,
        THREAD_AFFINITY_POLICY_COUNT);

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

    rlim_t requested = before.rlim_cur * 2;
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
/* Hypervisor.framework                                                    */

static void print_hypervisor(void) {
    int crashed = 0;
    int crash_signal = 0;
    hv_return_t vm_create_hr = HV_ERROR;
    int vm_create_ok = 0;

    hv_return_t max_vcpu_hr = HV_ERROR;
    int max_vcpu_ok = 0;
    uint32_t max_vcpu_count = 0;

    int vcpu_attempted = 0;
    hv_return_t vcpu_create_hr = HV_ERROR;
    int vcpu_create_ok = 0;

    hv_return_t sysreg_hr = HV_ERROR;
    int sysreg_ok = 0;
    uint64_t mmfr0 = 0;
    int par_range = -1, tgran4 = -1, tgran16 = -1, tgran64 = -1;

    if (sigsetjmp(g_jmp, 1) == 0) {
        vm_create_hr = hv_vm_create(NULL);
        vm_create_ok = (vm_create_hr == HV_SUCCESS);

        if (vm_create_ok) {
            max_vcpu_hr = hv_vm_get_max_vcpu_count(&max_vcpu_count);
            max_vcpu_ok = (max_vcpu_hr == HV_SUCCESS);

            vcpu_attempted = 1;
            hv_vcpu_t vcpu;
            hv_vcpu_exit_t *exit_info = NULL;
            vcpu_create_hr = hv_vcpu_create(&vcpu, &exit_info, NULL);
            vcpu_create_ok = (vcpu_create_hr == HV_SUCCESS);

            if (vcpu_create_ok) {
                sysreg_hr = hv_vcpu_get_sys_reg(
                    vcpu, HV_SYS_REG_ID_AA64MMFR0_EL1, &mmfr0);
                sysreg_ok = (sysreg_hr == HV_SUCCESS);
                if (sysreg_ok) {
                    par_range = (int)(mmfr0 & 0xF);
                    tgran16 = (int)((mmfr0 >> 20) & 0xF);
                    tgran64 = (int)((mmfr0 >> 24) & 0xF);
                    tgran4 = (int)((mmfr0 >> 28) & 0xF);
                }
                hv_vcpu_destroy(vcpu);
            }
            hv_vm_destroy();
        }
    } else {
        crashed = 1;
        crash_signal = (int)g_fault_signal;
    }

    printf("  \"hypervisor\": {\n");
    printf("    \"hv_vm_create_hr\": %d,\n", (int)vm_create_hr);
    printf("    \"hv_vm_create_ok\": %s,\n", vm_create_ok ? "true" : "false");
    printf("    \"hv_vm_get_max_vcpu_count_hr\": %d,\n", (int)max_vcpu_hr);
    printf("    \"hv_vm_get_max_vcpu_count_ok\": %s,\n",
           max_vcpu_ok ? "true" : "false");
    printf("    \"hv_vm_get_max_vcpu_count_value\": %u,\n", max_vcpu_count);
    printf("    \"vcpu_attempted\": %s,\n", vcpu_attempted ? "true" : "false");
    printf("    \"hv_vcpu_create_hr\": %d,\n", (int)vcpu_create_hr);
    printf("    \"hv_vcpu_create_ok\": %s,\n",
           vcpu_create_ok ? "true" : "false");
    printf("    \"hv_vcpu_get_sys_reg_id_aa64mmfr0_el1_hr\": %d,\n",
           (int)sysreg_hr);
    printf("    \"hv_vcpu_get_sys_reg_id_aa64mmfr0_el1_ok\": %s,\n",
           sysreg_ok ? "true" : "false");
    printf("    \"id_aa64mmfr0_el1_raw\": \"0x%016" PRIx64 "\",\n", mmfr0);
    printf("    \"id_aa64mmfr0_el1_parange\": %d,\n", par_range);
    printf("    \"id_aa64mmfr0_el1_tgran4\": %d,\n", tgran4);
    printf("    \"id_aa64mmfr0_el1_tgran16\": %d,\n", tgran16);
    printf("    \"id_aa64mmfr0_el1_tgran64\": %d,\n", tgran64);
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
            g_label = argv[++i];
        }
    }

    install_fault_handlers();

    printf("{\n");
    printf("  \"schema_version\": 1,\n");
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
    fflush(stdout);
    return 0;
}
