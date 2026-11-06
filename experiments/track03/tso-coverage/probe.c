// TSO coverage probe: N-vCPU HVF VM, EL1 guest code with ACTLR_EL1 EnTSO on or off.
// usage: probe <on|off> <plan>; plan line: <cell-index> <name> <iterations> <delay-mask> <seed>
#include <Hypervisor/Hypervisor.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <time.h>

#define BASE UINT64_C(0x40000000)
#define SIZE 0x200000u
#define NCPU 4
#define DESC 0x80000u
#define XOFF 0x90000u
#define YOFF 0x90400u
extern const char guest_start[], guest_end[], boot_resume[], cell_entry[];
extern const uint64_t cell_table[];

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
struct cpu { unsigned id, seen, active; int status; hv_vcpu_t v; uint64_t boot[6], mmfr2, fault[4]; };
static struct cpu cpus[NCPU];
static unsigned gen, ready, finished, quit, tso;
static uint8_t *ram;
static uint64_t tcr, entry_pc, boot_pc;

static void fail(const char *s) { fprintf(stderr, "FAIL %s\n", s); fflush(stdout); exit(2); }
static void check(hv_return_t rc, const char *s) { if (rc != HV_SUCCESS) { fprintf(stderr, "FAIL %s rc=%u\n", s, rc); exit(2); } }
#define HV(x) check((x), #x)
#define PT(x) do { if ((x) != 0) fail(#x); } while (0)

static void *owner(void *arg) {
    struct cpu *c = arg; hv_vcpu_exit_t *out;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    HV(hv_vcpu_create(&c->v, &out, NULL));
    hv_vcpu_t v = c->v;
    HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_ID_AA64MMFR2_EL1, &c->mmfr2));
    HV(hv_vcpu_set_reg(v, HV_REG_PC, BASE)); HV(hv_vcpu_set_reg(v, HV_REG_CPSR, 0x3c5));
    HV(hv_vcpu_set_reg(v, HV_REG_X8, c->id)); HV(hv_vcpu_set_reg(v, HV_REG_X10, 0xff));
    HV(hv_vcpu_set_reg(v, HV_REG_X11, BASE + 0x40000)); // L0
    HV(hv_vcpu_set_reg(v, HV_REG_X12, tcr));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SCTLR_EL1, 0x30d00980));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_ACTLR_EL1, tso ? 2 : 0));  // EnTSO is ACTLR_EL1 bit 1
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_CNTV_CTL_EL0, 0));
    HV(hv_vcpu_run(v));
    uint64_t pc; HV(hv_vcpu_get_reg(v, HV_REG_PC, &pc));
    if (out->reason != HV_EXIT_REASON_EXCEPTION || (out->exception.syndrome >> 26) != 0x16 ||
        (out->exception.syndrome & 0xffff) != 0x43 || pc != boot_pc) fail("boot exit");
    for (unsigned j = 0; j < 6; j++) HV(hv_vcpu_get_reg(v, (hv_reg_t)(HV_REG_X9 + j), &c->boot[j]));
    PT(pthread_mutex_lock(&lock)); ++ready; PT(pthread_cond_broadcast(&changed));
    for (;;) {
        while (c->seen == gen && !quit) PT(pthread_cond_wait(&changed, &lock));
        if (quit) break;
        c->seen = gen; unsigned act = c->active;
        PT(pthread_mutex_unlock(&lock));
        c->status = 0;
        if (act) {
            HV(hv_vcpu_set_reg(v, HV_REG_CPSR, 0x3c5));
            HV(hv_vcpu_set_reg(v, HV_REG_PC, entry_pc));
            HV(hv_vcpu_set_reg(v, HV_REG_X0, BASE + DESC));
            HV(hv_vcpu_set_reg(v, HV_REG_X20, c->id));
            HV(hv_vcpu_run(v));
            uint64_t syn = out->exception.syndrome;
            if (out->reason == HV_EXIT_REASON_CANCELED) c->status = 3;
            else if (out->reason == HV_EXIT_REASON_EXCEPTION && (syn >> 26) == 0x16 && (syn & 0xffff) == 0x44) c->status = 1;
            else if (out->reason == HV_EXIT_REASON_EXCEPTION && (syn >> 26) == 0x16 && (syn & 0xffff) == 0x45) {
                c->status = 2;
                for (unsigned j = 0; j < 3; j++) HV(hv_vcpu_get_reg(v, (hv_reg_t)(HV_REG_X0 + j), &c->fault[j]));
            } else { c->status = 4; c->fault[0] = syn; c->fault[1] = out->reason; }
            if (c->status == 2 || c->status == 4) {  // a vCPU faulted: peers would wait for it forever
                hv_vcpu_t all[NCPU]; for (unsigned j = 0; j < NCPU; j++) all[j] = cpus[j].v;
                hv_vcpus_exit(all, NCPU);
            }
        }
        PT(pthread_mutex_lock(&lock)); ++finished; PT(pthread_cond_broadcast(&changed));
    }
    PT(pthread_mutex_unlock(&lock));
    HV(hv_vcpu_destroy(v));
    return NULL;
}

int main(int argc, char **argv) {
    setbuf(stdout, NULL);
    if (argc != 3 || (strcmp(argv[1], "on") && strcmp(argv[1], "off"))) fail("usage: probe on|off plan");
    tso = !strcmp(argv[1], "on");
    struct rlimit lim;
    if (getrlimit(RLIMIT_CPU, &lim) || lim.rlim_cur != RLIM_INFINITY) fail("inherited CPU limit must be unlimited");
    tcr = 16 | (UINT64_C(1) << 8) | (UINT64_C(1) << 10) | (UINT64_C(3) << 12) | (UINT64_C(1) << 23) | (UINT64_C(2) << 32);
    ram = mmap(NULL, SIZE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (ram == MAP_FAILED) fail("mmap");
    size_t code = (size_t)(guest_end - guest_start);
    if (code > 0x40000) fail("code size");
    memcpy(ram, guest_start, code);
    boot_pc = BASE + (boot_resume - guest_start); entry_pc = BASE + (cell_entry - guest_start);
    uint64_t *l0 = (void *)(ram + 0x40000), *l1 = (void *)(ram + 0x41000), *l2 = (void *)(ram + 0x42000);
    uint64_t *l3a = (void *)(ram + 0x43000), *l3b = (void *)(ram + 0x44000);
    l0[0] = (BASE + 0x41000) | 3; l1[1] = (BASE + 0x42000) | 3;
    l2[0] = (BASE + 0x43000) | 3; l2[1] = (BASE + 0x44000) | 3;   // second 2 MiB: alias of the first
    for (unsigned j = 0; j < SIZE / 4096; j++) {
        uint64_t d = (BASE + j * 4096ull) | 3 | (UINT64_C(1) << 10) | (UINT64_C(3) << 8);
        l3a[j] = d | (j < code / 4096 + 1 ? 0 : UINT64_C(3) << 53);  // only code pages executable
        l3b[j] = d | (UINT64_C(3) << 53);
    }
    __builtin___clear_cache((char *)ram, (char *)ram + code); __asm__ volatile("dsb sy" ::: "memory");
    uint64_t ncells = cell_table[0];
    HV(hv_vm_create(NULL)); HV(hv_vm_map(ram, BASE, SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    pthread_t th[NCPU];
    for (unsigned j = 0; j < NCPU; j++) { cpus[j].id = j; PT(pthread_create(&th[j], NULL, owner, &cpus[j])); }
    PT(pthread_mutex_lock(&lock)); while (ready != NCPU) PT(pthread_cond_wait(&changed, &lock));
    printf("{\"kind\":\"config\",\"tso\":\"%s\",\"cells_in_binary\":%llu,\"code_bytes\":%zu}\n", argv[1], (unsigned long long)ncells, code);
    for (unsigned j = 0; j < NCPU; j++) {
        uint64_t *b = cpus[j].boot;
        printf("{\"kind\":\"boot\",\"cpu\":%u,\"mmfr2\":\"%016llx\",\"actlr\":\"%llx\",\"mair\":\"%llx\",\"tcr\":\"%llx\",\"sctlr\":\"%llx\",\"vbar\":\"%llx\"}\n",
               j, (unsigned long long)cpus[j].mmfr2, (unsigned long long)b[0], (unsigned long long)b[1], (unsigned long long)b[2], (unsigned long long)b[3], (unsigned long long)b[4]);
        if (((cpus[j].mmfr2 >> 32) & 15) != 1 || b[0] != (tso ? 2u : 0u) || b[1] != 0xff || b[2] != tcr || b[3] != 0x30d01985) fail("native setup mismatch");
    }
    PT(pthread_mutex_unlock(&lock));
    FILE *plan = fopen(argv[2], "r"); if (!plan) fail("plan");
    char name[128]; unsigned long long idx, iters, mask, seed;
    while (fscanf(plan, "%llu %127s %llu %llu %llu", &idx, name, &iters, &mask, &seed) == 5) {
        if (idx >= ncells) fail("cell index");
        const uint64_t *ct = cell_table + 1 + idx * 10;
        unsigned n = (unsigned)ct[0]; int atom = (int)ct[1];
        memset(ram + DESC, 0, 0x4000); memset(ram + XOFF, 0, 0x2000);
        uint64_t *d = (uint64_t *)(ram + DESC);
        d[0] = iters; d[1] = n; d[2] = mask; d[3] = seed;
        for (unsigned t = 0; t < 4; t++) d[4 + t] = ct[4 + t] ? BASE + ct[4 + t] : 0;
        d[8] = ct[8] ? BASE + ct[8] : 0; d[9] = ct[2]; d[10] = ct[3];
        d[11] = BASE + XOFF + ct[9]; d[12] = BASE + YOFF; d[13] = atom; d[14] = iters;
        __asm__ volatile("dsb sy" ::: "memory");
        struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);
        PT(pthread_mutex_lock(&lock));
        finished = 0; ++gen;
        for (unsigned j = 0; j < NCPU; j++) cpus[j].active = j < n;
        PT(pthread_cond_broadcast(&changed));
        struct timespec dl; clock_gettime(CLOCK_REALTIME, &dl);
        double limit = 60 + (double)iters * (atom ? 2e-6 : 2e-5); dl.tv_sec += (time_t)limit;
        while (finished != NCPU) {
            if (pthread_cond_timedwait(&changed, &lock, &dl)) {
                hv_vcpu_t all[NCPU]; for (unsigned j = 0; j < NCPU; j++) all[j] = cpus[j].v;
                printf("{\"kind\":\"timeout\",\"cell\":\"%s\"}\n", name); hv_vcpus_exit(all, NCPU);
                fail("cell timeout");
            }
        }
        PT(pthread_mutex_unlock(&lock));
        clock_gettime(CLOCK_MONOTONIC, &t1);
        __asm__ volatile("dsb sy" ::: "memory");
        int bad = 0;
        for (unsigned j = 0; j < n; j++) if (cpus[j].status != 1) bad = 1;
        printf("{\"kind\":\"cell\",\"name\":\"%s\",\"idx\":%llu,\"tso\":\"%s\",\"iters\":%llu,\"seed\":%llu,\"mask\":%llu,\"secs\":%.3f,\"ok\":%d",
               name, idx, argv[1], iters, seed, mask, (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) * 1e-9, !bad);
        if (bad) {
            printf(",\"status\":[");
            for (unsigned j = 0; j < n; j++) printf("%s[%d,\"%llx\",\"%llx\",\"%llx\"]", j ? "," : "", cpus[j].status, (unsigned long long)cpus[j].fault[0], (unsigned long long)cpus[j].fault[1], (unsigned long long)cpus[j].fault[2]);
            printf("]}\n"); fail("cell failed");
        }
        if (!atom) {
            printf(",\"hist\":{"); int first = 1;
            volatile uint64_t *h = (volatile uint64_t *)(ram + DESC + 0x2000);
            for (unsigned j = 0; j < 1024; j++) if (h[j]) { printf("%s\"%u\":%llu", first ? "" : ",", j, (unsigned long long)h[j]); first = 0; }
            printf("}");
        } else {
            printf(",\"res\":[");
            for (unsigned j = 0; j < n; j++) {
                volatile uint64_t *r = (volatile uint64_t *)(ram + DESC + 0x1000 + 0x200 + 128 * j);
                printf("%s[%llu,%llu,\"%llx\",\"%llx\",%llu]", j ? "," : "", (unsigned long long)r[0], (unsigned long long)r[1], (unsigned long long)r[2], (unsigned long long)r[3], (unsigned long long)r[4]);
            }
            printf("]");
        }
        puts("}");
    }
    PT(pthread_mutex_lock(&lock)); quit = 1; PT(pthread_cond_broadcast(&changed)); PT(pthread_mutex_unlock(&lock));
    for (unsigned j = 0; j < NCPU; j++) PT(pthread_join(th[j], NULL));
    HV(hv_vm_unmap(BASE, SIZE)); HV(hv_vm_destroy()); munmap(ram, SIZE);
    puts("{\"kind\":\"complete\"}");
    return 0;
}
