// Disposable conformance probe. No guest/user input; only retained temporary fixtures.
#include <Hypervisor/Hypervisor.h>
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <mach/mach_host.h>
#include <mach/task_special_ports.h>
#include <sandbox.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>

extern int sandbox_check(pid_t, const char *, int, ...);
// Exported by SDK 26.4 libsystem_kernel.tbd; signature follows XNU BSD syscall.
extern int task_read_for_pid(mach_port_name_t, int, mach_port_name_t *);
#define API_LIST(X) X(hv_vm_create) X(hv_vm_destroy) X(hv_vm_map) X(hv_vm_unmap) \
 X(hv_vm_protect) X(hv_vcpu_create) X(hv_vcpu_destroy) X(hv_vcpu_run) \
 X(hv_vcpu_set_reg) X(hv_vcpu_get_reg) X(hv_vcpu_set_sys_reg) \
 X(hv_vcpu_set_vtimer_offset) X(hv_vcpu_get_vtimer_offset) \
 X(hv_vcpu_set_vtimer_mask) X(hv_vcpu_get_vtimer_mask)
#define DECLARE(n) static __typeof__(&n) p_##n;
API_LIST(DECLARE)
static hv_vcpu_t cpu;
static hv_vcpu_exit_t *exit_info;
static void *ram;
static const size_t ram_size = 16384;
static const hv_ipa_t ipa = 0x40000000;

static void result(const char *op, long long rc, int error, unsigned long long value) {
    printf("{\"op\":\"%s\",\"rc\":%lld,\"errno\":%d,\"value\":%llu}\n", op, rc, error, value);
    fflush(stdout);
}
static void check_hv(const char *op, hv_return_t rc) {
    result(op, rc, 0, 0);
    if (rc != HV_SUCCESS) exit(20);
}
#define HV(call) check_hv(#call, (call))
static void load_hvf(void) {
    void *h = dlopen("/System/Library/Frameworks/Hypervisor.framework/Hypervisor", RTLD_NOW | RTLD_LOCAL);
    result("hvf_dlopen", h ? 0 : -1, 0, 0);
    if (!h) { fprintf(stderr, "%s\n", dlerror()); exit(21); }
#define LOAD(n) p_##n = (__typeof__(&n))dlsym(h, #n); if (!p_##n) exit(22);
    API_LIST(LOAD)
}
static void setup_hvf(void) {
    load_hvf();
    HV(p_hv_vm_create(NULL));
    ram = mmap(NULL, ram_size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    result("anonymous_ram", ram == MAP_FAILED ? -1 : 0, errno, ram_size);
    if (ram == MAP_FAILED) exit(23);
    // mov x0,#0x42; hvc #0. Native guest code, no x86 translator or product code.
    const uint32_t code[] = {0xd2800840, 0xd4000002};
    memcpy(ram, code, sizeof(code));
    __builtin___clear_cache(ram, (char *)ram + sizeof(code));
    HV(p_hv_vm_map(ram, ipa, ram_size, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    HV(p_hv_vcpu_create(&cpu, &exit_info, NULL));
    HV(p_hv_vcpu_set_reg(cpu, HV_REG_PC, ipa));
    HV(p_hv_vcpu_set_reg(cpu, HV_REG_CPSR, 0x3c5));
    HV(p_hv_vcpu_set_sys_reg(cpu, HV_SYS_REG_CNTV_CTL_EL0, 0));
}
static void exercise_hvf(void) {
    HV(p_hv_vm_protect(ipa, ram_size, HV_MEMORY_READ | HV_MEMORY_EXEC));
    HV(p_hv_vcpu_set_vtimer_offset(cpu, 0));
    uint64_t offset = UINT64_MAX;
    HV(p_hv_vcpu_get_vtimer_offset(cpu, &offset));
    result("timer_offset_value", offset == 0 ? 0 : -1, 0, offset);
    HV(p_hv_vcpu_set_vtimer_mask(cpu, true));
    bool masked = false;
    HV(p_hv_vcpu_get_vtimer_mask(cpu, &masked));
    result("timer_mask_value", masked ? 0 : -1, 0, masked);
    HV(p_hv_vcpu_run(cpu));
    uint64_t x0 = 0;
    HV(p_hv_vcpu_get_reg(cpu, HV_REG_X0, &x0));
    result("guest_exit", (exit_info->reason == HV_EXIT_REASON_EXCEPTION &&
           (exit_info->exception.syndrome >> 26) == 0x16 && x0 == 0x42) ? 0 : -1,
           0, exit_info->exception.syndrome);
    result("guest_x0", x0 == 0x42 ? 0 : -1, 0, x0);
    HV(p_hv_vm_unmap(ipa, ram_size));
    HV(p_hv_vm_map(ram, ipa, ram_size, HV_MEMORY_READ | HV_MEMORY_EXEC));
    HV(p_hv_vm_unmap(ipa, ram_size));
    HV(p_hv_vcpu_destroy(cpu));
    HV(p_hv_vm_destroy());
    munmap(ram, ram_size);
}
static void inventory_ports(void) {
    mach_port_name_array_t names = NULL;
    mach_port_type_array_t types = NULL;
    mach_msg_type_number_t nn = 0, nt = 0;
    kern_return_t kr = mach_port_names(mach_task_self(), &names, &nn, &types, &nt);
    result("entry_mach_port_inventory", kr, 0, nn);
    if (kr == KERN_SUCCESS) {
        for (unsigned i = 0; i < nn && i < nt && i < 128; ++i)
            printf("{\"port\":%u,\"type\":%u}\n", names[i], types[i]);
        vm_deallocate(mach_task_self(), (vm_address_t)names, nn * sizeof(*names));
        vm_deallocate(mach_task_self(), (vm_address_t)types, nt * sizeof(*types));
    }
}
static bool launch_context(const char *context) {
    mach_port_t bootstrap = MACH_PORT_NULL;
    kern_return_t kr = task_get_special_port(mach_task_self(), TASK_BOOTSTRAP_PORT, &bootstrap);
    result("launch_child_bootstrap", kr, 0, bootstrap != MACH_PORT_NULL);
    bool ok = kr == KERN_SUCCESS;
    bool empty = bootstrap == MACH_PORT_NULL;
    if (MACH_PORT_VALID(bootstrap)) mach_port_deallocate(mach_task_self(), bootstrap);
    mach_port_array_t registered = NULL;
    mach_msg_type_number_t count = 0;
    kr = mach_ports_lookup(mach_task_self(), &registered, &count);
    unsigned valid = 0;
    if (kr == KERN_SUCCESS) {
        for (unsigned i = 0; i < count; ++i) {
            if (MACH_PORT_VALID(registered[i])) {
                ++valid;
                mach_port_deallocate(mach_task_self(), registered[i]);
            }
        }
        vm_deallocate(mach_task_self(), (vm_address_t)registered, count * sizeof(*registered));
    }
    result("launch_child_registered", kr, 0, valid);
    ok &= kr == KERN_SUCCESS;
    empty &= valid == 0;
    exception_mask_t masks[EXC_TYPES_COUNT];
    mach_port_t ports[EXC_TYPES_COUNT];
    exception_behavior_t behaviors[EXC_TYPES_COUNT];
    thread_state_flavor_t flavors[EXC_TYPES_COUNT];
    thread_t thread = mach_thread_self();
    for (int scope = 0; scope < 2; ++scope) {
        count = EXC_TYPES_COUNT;
        kr = scope == 0 ? task_get_exception_ports(mach_task_self(), EXC_MASK_ALL,
             masks, &count, ports, behaviors, flavors) :
             thread_get_exception_ports(thread, EXC_MASK_ALL, masks, &count, ports, behaviors, flavors);
        valid = 0;
        if (kr == KERN_SUCCESS) {
            for (unsigned i = 0; i < count; ++i) {
                if (MACH_PORT_VALID(ports[i])) {
                    ++valid;
                    mach_port_deallocate(mach_task_self(), ports[i]);
                }
            }
        }
        result(scope == 0 ? "launch_child_exceptions" : "launch_child_thread_exceptions", kr, 0, valid);
        ok &= kr == KERN_SUCCESS;
        empty &= valid == 0;
    }
    mach_port_deallocate(mach_task_self(), thread);
    return ok && (!strcmp(context, "ordinary") || empty);
}
static void restrict_worker(const char *profile) {
    char *error = NULL;
    errno = 0;
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    int rc = sandbox_init(profile, 0, &error);
    result("sandbox_init", rc, errno, 0);
    if (error) { fprintf(stderr, "%s\n", error); sandbox_free_error(error); }
#pragma clang diagnostic pop
    if (rc) exit(24);
    result("sandbox_after", sandbox_check(getpid(), NULL, 0), 0, 0);
}
static void resources(int own, int channel, const char *peer_path, pid_t peer, int port) {
    const char payload[] = "own-disk-marker";
    errno = 0; ssize_t n = pwrite(own, payload, sizeof(payload), 0);
    result("own_pwrite", n, errno, sizeof(payload));
    char buf[sizeof(payload)] = {0};
    errno = 0; n = pread(own, buf, sizeof(buf), 0);
    result("own_pread", n, errno, n == sizeof(buf) && !memcmp(buf, payload, sizeof(buf)));
    errno = 0; int rc = fcntl(own, F_FULLFSYNC);
    result("own_fullfsync", rc, errno, 0);
    char ch = 0;
    errno = 0; n = read(channel, &ch, 1);
    result("channel_read", n, errno, ch == 'A');
    errno = 0; n = write(channel, "B", 1);
    result("channel_write", n, errno, 0);
    errno = 0; int fd = open(peer_path, O_RDWR | O_NOFOLLOW);
    result("peer_open_rw", fd < 0 ? -1 : 0, errno, 0);
    if (fd >= 0) close(fd);
    errno = 0; rc = kill(peer, SIGUSR1);
    result("peer_signal", rc, errno, 0);
    mach_port_t task = MACH_PORT_NULL;
    kern_return_t kr = task_for_pid(mach_task_self(), peer, &task);
    result("peer_task_for_pid", kr, 0, task != MACH_PORT_NULL);
    if (task != MACH_PORT_NULL) mach_port_deallocate(mach_task_self(), task);
    task = MACH_PORT_NULL;
    errno = 0;
    rc = task_read_for_pid(mach_task_self(), peer, &task);
    result("peer_task_read_for_pid", rc, errno, task != MACH_PORT_NULL);
    if (task != MACH_PORT_NULL) mach_port_deallocate(mach_task_self(), task);
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    int saved = errno;
    struct sockaddr_in address = {.sin_len = sizeof(address), .sin_family = AF_INET,
        .sin_port = htons((uint16_t)port), .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    errno = 0; rc = sock < 0 ? -1 : connect(sock, (struct sockaddr *)&address, sizeof(address));
    result("new_loopback_connection", rc, sock < 0 ? saved : errno, sock >= 0);
    if (sock >= 0) close(sock);
}
static void direct_self_acquisition(const char *name, int64_t number) {
    mach_port_t port = MACH_PORT_NULL;
    const mach_port_t own_task = mach_task_self();
    const pid_t own_pid = getpid();
    register uint64_t x0 __asm__("x0") = own_task;
    register uint64_t x1 __asm__("x1") = (uint64_t)own_pid;
    register uint64_t x2 __asm__("x2") = (uint64_t)&port;
    register uint64_t x16 __asm__("x16") = (uint64_t)number;
    unsigned carry;
    // Darwin arm64: negative Mach trap / positive BSD number in x16.
    __asm__ volatile("svc #0x80\n\tcset %w[carry], cs"
        : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x16), [carry] "=&r"(carry)
        : : "memory", "cc");
    int error = number > 0 && carry ? (int)x0 : 0;
    result(name, error ? -1 : (long long)x0, error, MACH_PORT_VALID(port));
    if (MACH_PORT_VALID(port)) mach_port_deallocate(mach_task_self(), port);
}
static void self_acquisition(void) {
    mach_port_t port = MACH_PORT_NULL;
    kern_return_t kr = task_for_pid(mach_task_self(), getpid(), &port);
    result("self_task_control", kr, 0, MACH_PORT_VALID(port));
    if (MACH_PORT_VALID(port)) mach_port_deallocate(mach_task_self(), port);
    port = MACH_PORT_NULL;
    errno = 0;
    int rc = task_read_for_pid(mach_task_self(), getpid(), &port);
    result("self_task_read", rc, errno, MACH_PORT_VALID(port));
    if (MACH_PORT_VALID(port)) mach_port_deallocate(mach_task_self(), port);
    direct_self_acquisition("direct_self_control", -45);
    direct_self_acquisition("direct_self_read", 539);
    mach_port_type_t type = 0;
    kr = mach_port_type(mach_task_self(), mach_task_self(), &type);
    result("self_task_facility", kr == KERN_SUCCESS && (type & MACH_PORT_TYPE_SEND) ? 0 : -1, 0, type);
}
static void mach96_copyout(void) {
    const uint32_t seeds[] = {0x13579bdf, 0x2468ace1};
    const pid_t own_pid = getpid();
    for (unsigned i = 0; i < 2; i++) {
        volatile mach_port_name_t word = seeds[i];
        char name[64];
        snprintf(name, sizeof(name), "mach96_%u_pre", i);
        result(name, word == seeds[i] ? 0 : -1, 0, word);
        register uint64_t x0 __asm__("x0") = MACH_PORT_NULL;
        register uint64_t x1 __asm__("x1") = (uint64_t)own_pid;
        register uint64_t x2 __asm__("x2") = (uint64_t)&word;
        register uint64_t x16 __asm__("x16") = (uint64_t)-96;
        __asm__ volatile("svc #0x80"
            : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x16) : : "memory", "cc");
        const long long rc = (long long)x0;
        const mach_port_name_t observed = word;
        snprintf(name, sizeof(name), "mach96_%u_call", i);
        result(name, rc, 0, observed);
        // This is an output-word discriminator, not an acquired port name.
        word = seeds[i] ^ UINT32_MAX;
        snprintf(name, sizeof(name), "mach96_%u_post", i);
        result(name, word == (seeds[i] ^ UINT32_MAX) ? 0 : -1, 0, word);
    }
}
// MIG205 only: query an ordinary host capability, never use the acquired IOKit port.
static bool iomain_acquisition(void) {
    result("iomain_euid", geteuid() != 0 ? 0 : -1, 0, geteuid());
    if (geteuid() == 0) return false;
    mach_port_t host = mach_host_self(), io = MACH_PORT_NULL;
    result("iomain_host", MACH_PORT_VALID(host) ? 0 : -1, 0, MACH_PORT_VALID(host));
    if (!MACH_PORT_VALID(host)) return false;
    kern_return_t kr = host_get_io_main(host, &io);
    result("iomain_call", kr, 0, io);
    mach_port_type_t type = 0;
    kern_return_t type_kr = MACH_PORT_VALID(io) ? mach_port_type(mach_task_self(), io, &type) : KERN_SUCCESS;
    result("iomain_right", type_kr, 0, type);
    bool send = (kr == KERN_SUCCESS && MACH_PORT_VALID(io)) ||
                (type_kr == KERN_SUCCESS && (type & MACH_PORT_TYPE_SEND));
    kern_return_t release = send ? mach_port_deallocate(mach_task_self(), io) : KERN_SUCCESS;
    result("iomain_release", release, 0, send);
    kern_return_t host_release = mach_port_deallocate(mach_task_self(), host);
    result("iomain_host_release", host_release, 0, 0);
    return release == KERN_SUCCESS && host_release == KERN_SUCCESS;
}
static void signal_marker(int sig) { (void)sig; (void)write(STDOUT_FILENO, "S\n", 2); }
int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc == 2 && !strcmp(argv[1], "peer")) {
        struct sigaction action = {.sa_handler = signal_marker};
        sigemptyset(&action.sa_mask);
        if (sigaction(SIGUSR1, &action, NULL)) return 2;
        printf("%d\n", getpid());
        for (;;) pause();
    }
    if (argc != 8 && argc != 9 && argc != 10) return 2;
    if (argc == 10 && strcmp(argv[9], "task-filter") && strcmp(argv[9], "mach96-filter") && strcmp(argv[9], "iomain-filter")) return 2;
    if (argc >= 9) {
        if (strcmp(argv[8], "ordinary") && strcmp(argv[8], "sanitized")) return 2;
        if (!launch_context(argv[8])) return 26;
    }
    const char *mode = argv[1];
    if (strcmp(mode, "control") && strcmp(mode, "before") && strcmp(mode, "after")) return 2;
    int own = atoi(argv[3]), channel = atoi(argv[4]), peer = atoi(argv[6]), port = atoi(argv[7]);
    if (own < 3 || channel < 3 || peer <= 1 || port <= 0 || port > 65535) return 2;
    FILE *file = fopen(argv[2], "r");
    if (!file) return 3;
    char profile[8192] = {0};
    size_t size = fread(profile, 1, sizeof(profile) - 1, file);
    bool valid = !ferror(file) && feof(file) && size > 0;
    fclose(file);
    if (!valid) return 3;
    int inherited = sandbox_check(getpid(), NULL, 0);
    result("sandbox_before", inherited, 0, 0);
    inventory_ports();
    // An outer sandbox makes this standalone-policy matrix inapplicable.
    if (inherited != 0) return 25;
    if (!strcmp(mode, "after")) setup_hvf();
    if (strcmp(mode, "control")) restrict_worker(profile);
    resources(own, channel, argv[5], (pid_t)peer, port);
    if (argc == 10) self_acquisition();
    if (argc == 10 && !strcmp(argv[9], "mach96-filter")) mach96_copyout();
    if (argc == 10 && !strcmp(argv[9], "iomain-filter") && !iomain_acquisition()) return 27;
    if (strcmp(mode, "after")) setup_hvf();
    exercise_hvf();
    return 0;
}
