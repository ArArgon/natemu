// Stop-premise probe (S3b): does hv_vm_protect(granule, READ) from a peer thread make a RUNNING vCPU, in an exit-free
// hot loop, take a stage-2 write-fault exit?  usage: probe <protect|exit|control> <nvcpu> <kicks|seconds> [spinners] [granule 4|16]
//   protect: kick = hv_vm_protect only.  exit: hv_vm_protect then hv_vcpus_exit(v).  control: no kicks, <seconds> of running.
#include <Hypervisor/Hypervisor.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define BASE UINT64_C(0x40000000)           // guest IPA = VA of RAM (identity stage 1, one 2 MiB block)
#define SIZE 0x200000u
#define CNT 0x80000u                        // per-vCPU iteration counters (+0x100*v), outside every stop granule
#define STOP 0x100000u                      // stop granule of vCPU v: STOP + v*G
#define MAXV 4
#define OTHER_MAX 100
enum { F_STOP = 1, F_CANCEL = 2, F_OTHER = 3 };

// The hot loop: no exits, no MMIO.  x0 = stop granule, x1 = counter slot.  The first store is the "stop store".
__asm__(".text\n.balign 4\n_guest_start:\n1: str xzr,[x0]\n add x2,x2,#1\n str x2,[x1]\n b 1b\n_guest_end:\n");
extern const char guest_start[], guest_end[];

struct ev { uint64_t t; int kind; };
struct cpu {
    unsigned id; hv_vcpu_t v; _Atomic int ready; _Atomic unsigned n, cancels, other; struct ev ev[16];
    uint64_t first[4]; int have_first;      // first stage-2 exit: ESR, FAR, IPA, DFSC/WnR check
    unsigned bad_fault;                     // stage-2 exits not (data abort, write, expected granule)
    unsigned long kinds[64];                // histogram of syndrome EC of every exit
    uint64_t other_syn[2];                  // first unexpected exit: reason, syndrome
    unsigned target, kicks_done, miss1ms, miss100ms, post1ms, post100ms, recovered; uint64_t *lat, *post, *cost; };
static struct cpu cpus[MAXV];
static _Atomic int stopping;
static uint8_t *ram; static size_t G; static unsigned nv, exitmode;
static uint64_t now(void) { return clock_gettime_nsec_np(CLOCK_UPTIME_RAW); }
static void check(hv_return_t rc, const char *s) { if (rc != HV_SUCCESS) { fprintf(stderr, "FAIL %s rc=0x%x\n", s, rc); exit(2); } }
#define HV(x) check((x), #x)
static uint64_t gran(unsigned v) { return BASE + STOP + v * G; }

static void *vcpu_thread(void *arg) {
    struct cpu *c = arg; hv_vcpu_exit_t *out;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    HV(hv_vcpu_create(&c->v, &out, NULL)); hv_vcpu_t v = c->v;
    uint64_t tcr = 16 | (1ull << 8) | (1ull << 10) | (3ull << 12) | (1ull << 23) | (2ull << 32);   // 4 KiB stage 1, 48-bit VA
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_MAIR_EL1, 0xff)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_TCR_EL1, tcr));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_TTBR0_EL1, BASE + 0x10000)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SCTLR_EL1, 0x30d01985));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_ACTLR_EL1, 2));            // EnTSO, as natemu runs
    HV(hv_vcpu_set_reg(v, HV_REG_PC, BASE)); HV(hv_vcpu_set_reg(v, HV_REG_CPSR, 0x3c5));
    HV(hv_vcpu_set_reg(v, HV_REG_X0, gran(c->id))); HV(hv_vcpu_set_reg(v, HV_REG_X1, BASE + CNT + 0x100 * c->id));
    HV(hv_vcpu_set_reg(v, HV_REG_X2, 0));
    atomic_store(&c->ready, 1);
    for (;;) {
        HV(hv_vcpu_run(v)); uint64_t t = now(); int kind = F_OTHER;
        uint64_t syn = out->exception.syndrome; unsigned ec = syn >> 26;
        if (out->reason == HV_EXIT_REASON_CANCELED) kind = F_CANCEL;
        else if (out->reason == HV_EXIT_REASON_EXCEPTION) {
            c->kinds[ec & 63]++;
            if (ec == 0x24) {               // data abort from the guest: must be a write to our granule
                uint64_t ipa = out->exception.physical_address;
                if ((syn >> 6 & 1) && ipa >= gran(c->id) && ipa < gran(c->id) + G && (syn & 0x3c) == 0xc) kind = F_STOP; // WnR, DFSC = permission fault
                else c->bad_fault++;
                if (!c->have_first) { c->have_first = 1; c->first[0] = syn; c->first[1] = out->exception.virtual_address; c->first[2] = ipa; }
            }
        }
        if (kind == F_OTHER && !c->other++) { c->other_syn[0] = out->reason; c->other_syn[1] = syn; }
        HV(hv_vm_protect(gran(c->id), G, HV_MEMORY_READ | HV_MEMORY_WRITE));   // restore, then publish the exit
        struct ev *e = &c->ev[atomic_load(&c->n) & 15]; e->t = t; e->kind = kind;
        if (kind == F_CANCEL) atomic_fetch_add(&c->cancels, 1);
        atomic_fetch_add(&c->n, 1);
        if ((kind == F_CANCEL && atomic_load(&stopping)) || c->other > OTHER_MAX) break;
    }
    HV(hv_vcpu_destroy(v)); return NULL;
}

static void *kicker(void *arg) {
    struct cpu *c = arg;
    uint64_t rng = 0x9e3779b97f4a7c15ull * (c->id + 1);
    for (unsigned k = 0; k < c->target; k++) {
        rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
        for (uint64_t g = now() + rng % 100000; now() < g;) ;   // 0-100 us gap so kicks land at varied loop points
        unsigned e0 = atomic_load(&c->n), c0 = atomic_load(&c->cancels); int cancelled = exitmode;
        uint64_t t0 = now();
        HV(hv_vm_protect(gran(c->id), G, HV_MEMORY_READ));
        uint64_t t1 = now();
        if (exitmode) HV(hv_vcpus_exit(&c->v, 1));
        for (unsigned i = 0; atomic_load(&c->n) == e0; i++) {
            if (i > 64 && now() - t1 > 100000000ull) {   // 100 ms after protect returned: unstick with the fallback, count it
                if (!cancelled) { HV(hv_vcpus_exit(&c->v, 1)); cancelled = 1; c->recovered++; }
                if (now() - t1 > 2000000000ull) { fprintf(stderr, "FAIL vCPU %u never exited\n", c->id); exit(3); }
            }
            if (i > 2000) usleep(20);
        }
        uint64_t tobs = c->ev[e0 & 15].t, lat = tobs - t0, post = tobs > t1 ? tobs - t1 : 0;   // post: 0 = seen before protect returned
        if (cancelled) while (atomic_load(&c->cancels) == c0) usleep(20);   // exactly one cancel exit is owed
        c->lat[k] = lat; c->post[k] = post; c->cost[k] = t1 - t0; c->kicks_done = k + 1;
        c->miss1ms += lat > 1000000; c->miss100ms += lat > 100000000; c->post1ms += post > 1000000; c->post100ms += post > 100000000;
        if (c->miss100ms > 20) break;       // premise clearly fails: stop early
    }
    return NULL;
}

static void *spinner(void *a) {
    (void)a;
    for (volatile uint64_t x = 0;; x++) {}
}
static int cmp(const void *a, const void *b) { uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b; return x < y ? -1 : x > y; }
static void dist(const char *name, uint64_t *a, size_t n) {
    qsort(a, n, 8, cmp);
#define P(q) a[(size_t)((n - 1) * (q))]
    printf("%s_ns n=%zu min=%llu p50=%llu p99=%llu p99.9=%llu max=%llu\n", name, n, (unsigned long long)a[0], (unsigned long long)P(0.5),
           (unsigned long long)P(0.99), (unsigned long long)P(0.999), (unsigned long long)a[n - 1]);
}

int main(int argc, char **argv) {
    setbuf(stdout, NULL);
    if (argc < 4) { fprintf(stderr, "usage: probe protect|exit|control nvcpu kicks|seconds [spinners] [granule 4|16]\n"); return 1; }
    int control = !strcmp(argv[1], "control"); exitmode = !strcmp(argv[1], "exit");
    nv = atoi(argv[2]); unsigned long arg = strtoul(argv[3], 0, 0); unsigned spin = argc > 4 ? atoi(argv[4]) : 0;
    ram = mmap(NULL, SIZE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    memcpy(ram, guest_start, guest_end - guest_start);
    uint64_t *l0 = (void *)(ram + 0x10000), *l1 = (void *)(ram + 0x11000), *l2 = (void *)(ram + 0x12000);
    l0[0] = (BASE + 0x11000) | 3; l1[1] = (BASE + 0x12000) | 3;
    l2[0] = BASE | 1 | (1ull << 10) | (3ull << 8);                 // 2 MiB block: normal WB (attr 0), AF, inner-shareable, EL1 RWX
    __builtin___clear_cache((char *)ram, (char *)ram + 4096); __asm__ volatile("dsb sy" ::: "memory");
    hv_vm_config_t cfg = hv_vm_config_create(); hv_ipa_granule_t def = 0, gr = 0;
    HV(hv_vm_config_get_default_ipa_granule(&def));
    if (argc > 5) HV(hv_vm_config_set_ipa_granule(cfg, atoi(argv[5]) == 4 ? HV_IPA_GRANULE_4KB : HV_IPA_GRANULE_16KB));
    HV(hv_vm_config_get_ipa_granule(cfg, &gr)); G = gr == HV_IPA_GRANULE_4KB ? 4096 : 16384;
    printf("mode=%s vcpus=%u arg=%lu spinners=%u ipa_granule=%zuK (default %s)\n", argv[1], nv, arg, spin, G / 1024, def == HV_IPA_GRANULE_4KB ? "4K" : "16K");
    HV(hv_vm_create(cfg)); HV(hv_vm_map(ram, BASE, SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    pthread_t th[MAXV], kt[MAXV], sp;
    for (unsigned j = 0; j < nv; j++) { cpus[j].id = j; pthread_create(&th[j], NULL, vcpu_thread, &cpus[j]); }
    for (unsigned j = 0; j < nv; j++) while (!atomic_load(&cpus[j].ready)) usleep(1000);
    for (unsigned j = 0; j < spin; j++) pthread_create(&sp, NULL, spinner, NULL);   // busy host threads (never joined)
    usleep(300000);                                                   // let every vCPU settle into its loop
    volatile uint64_t *cnt = (volatile uint64_t *)(ram + CNT);
    uint64_t c0[MAXV]; for (unsigned j = 0; j < nv; j++) c0[j] = cnt[32 * j];
    if (control) sleep(arg);
    else {
        for (unsigned j = 0; j < nv; j++) { cpus[j].target = arg; cpus[j].lat = calloc(arg, 8); cpus[j].post = calloc(arg, 8); cpus[j].cost = calloc(arg, 8); }
        for (unsigned j = 0; j < nv; j++) pthread_create(&kt[j], NULL, kicker, &cpus[j]);
        for (unsigned j = 0; j < nv; j++) pthread_join(kt[j], NULL);
    }
    uint64_t c1[MAXV]; for (unsigned j = 0; j < nv; j++) c1[j] = cnt[32 * j];
    unsigned exits_before_stop[MAXV]; for (unsigned j = 0; j < nv; j++) exits_before_stop[j] = atomic_load(&cpus[j].n);
    atomic_store(&stopping, 1); hv_vcpu_t all[MAXV]; for (unsigned j = 0; j < nv; j++) all[j] = cpus[j].v;
    HV(hv_vcpus_exit(all, nv)); for (unsigned j = 0; j < nv; j++) pthread_join(th[j], NULL);
    unsigned long exits = 0, bad = 0, other = 0; uint64_t progress = 1;
    for (unsigned j = 0; j < nv; j++) {
        exits += exits_before_stop[j]; bad += cpus[j].bad_fault; other += cpus[j].other; progress &= c1[j] > c0[j];
        if (cpus[j].have_first) printf("vcpu%u first stage-2 exit: ESR=%#llx EC=%#llx WnR=%llu DFSC=%#llx FAR=%#llx IPA=%#llx (granule %#llx)\n", j,
            (unsigned long long)cpus[j].first[0], (unsigned long long)(cpus[j].first[0] >> 26), (unsigned long long)(cpus[j].first[0] >> 6 & 1),
            (unsigned long long)(cpus[j].first[0] & 63), (unsigned long long)cpus[j].first[1], (unsigned long long)cpus[j].first[2], (unsigned long long)gran(j));
        if (cpus[j].other) printf("vcpu%u unexpected exit: reason=%llu syndrome=%#llx\n", j, (unsigned long long)cpus[j].other_syn[0], (unsigned long long)cpus[j].other_syn[1]);
    }
    printf("guest progress: counters advanced on all vCPUs=%d (vcpu0 %llu -> %llu)\n", (int)progress, (unsigned long long)c0[0], (unsigned long long)c1[0]);
    if (control) printf("control exits_while_running=%lu (excludes the final cancel)\n", exits);
    else {
        size_t n = 0, kicks = 0; unsigned m1 = 0, m100 = 0, p1 = 0, p100 = 0, rec = 0; uint64_t *lat = malloc(nv * arg * 8), *post = malloc(nv * arg * 8), *cost = malloc(nv * arg * 8);
        for (unsigned j = 0; j < nv; j++) {
            size_t d = cpus[j].kicks_done;
            memcpy(lat + n, cpus[j].lat, d * 8); memcpy(post + n, cpus[j].post, d * 8); memcpy(cost + n, cpus[j].cost, d * 8); n += d;
            m1 += cpus[j].miss1ms; m100 += cpus[j].miss100ms; p1 += cpus[j].post1ms; p100 += cpus[j].post100ms; rec += cpus[j].recovered; kicks += d;
        }
        printf("kicks=%zu missed_1ms=%u missed_100ms=%u (from before the call) missed_1ms_after_return=%u missed_100ms_after_return=%u recovered_by_hv_vcpus_exit=%u unexpected_faults=%lu other_exits=%lu\n", kicks, m1, m100, p1, p100, rec, bad, other);
        dist("latency", lat, n); dist("after_return", post, n); dist("protect_call", cost, n);
        printf("verdict_mode_%s=%s\n", argv[1], m1 == 0 && bad == 0 && other == 0 ? "ALL_WITHIN_1MS" : p1 == 0 && bad == 0 && other == 0 ? "ALL_WITHIN_1MS_AFTER_CALL_RETURNED" : "MISSES");
    }
    printf("exit_ec_histogram:"); for (unsigned e = 0; e < 64; e++) { unsigned long s = 0; for (unsigned j = 0; j < nv; j++) s += cpus[j].kinds[e]; if (s) printf(" EC%#x=%lu", e, s); } puts("");
    _exit(0);       // spinners never end
}
