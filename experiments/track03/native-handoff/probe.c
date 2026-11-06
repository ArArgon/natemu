// Disposable finite ownership probe; all vCPU API calls stay on owner threads.
#include <Hypervisor/Hypervisor.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/sysctl.h>

#define BASE UINT64_C(0x40000000)
#define SIZE 0x24000u
#define EPOCHS 64u
extern const char handoff_start[], handoff_end[], handoff_boot_hvc[];
extern const char handoff_reply_hvc[], handoff_boot_resume[], handoff_reply_resume[];
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
struct cpu {
    unsigned id, requested, completed;
    int stop;
    uint64_t input[8], output[10], pc, syndrome, boot[6];
};
static struct cpu cpus[2];
static unsigned ready;
static uint8_t *ram;
static uint64_t tcr, descriptor, generation;
static int direct;
static unsigned serial;
static void fail(const char *s) { fprintf(stderr, "FAIL %s\n", s); exit(2); }
static void hvcheck(hv_return_t r, const char *s) {
    // flockfile prevents interleaved owner-thread API records.
    flockfile(stdout);
    printf("{\"kind\":\"api\",\"call\":\"%s\",\"rc\":%u}\n", s, r);
    funlockfile(stdout);
    if (r != HV_SUCCESS) fail(s);
}
#define HV(x) hvcheck((x), #x)
#define PT(x) do { if ((x) != 0) fail(#x); } while (0)
static void barrier(void) { __asm__ volatile("dsb sy" ::: "memory"); }
static uint64_t address(const char *label) {
    return BASE + (uint64_t)((uintptr_t)label - (uintptr_t)handoff_start);
}
static void values(unsigned epoch, unsigned phase, uint64_t v[4]) {
    for (unsigned i = 0; i < 4; ++i)
        v[i] = UINT64_C(0xa57e91c300000000) | ((uint64_t)epoch << 16) |
               ((uint64_t)phase << 8) | i;
}
static void words(const uint64_t v[4]) {
    printf("[\"%016llx\",\"%016llx\",\"%016llx\",\"%016llx\"]",
        (unsigned long long)v[0], (unsigned long long)v[1],
        (unsigned long long)v[2], (unsigned long long)v[3]);
}
static void event(const char *kind, unsigned epoch, unsigned phase) {
    printf("{\"kind\":\"%s\",\"epoch\":%u,\"phase\":%u,\"generation\":%llu}\n",
        kind, epoch, phase, (unsigned long long)generation);
}
static void run_native(hv_vcpu_t v, hv_vcpu_exit_t *out, uint64_t expected_pc,
                       unsigned immediate, uint64_t *pc, uint64_t *syndrome) {
    HV(hv_vcpu_run(v));
    *syndrome = out->exception.syndrome;
    HV(hv_vcpu_get_reg(v, HV_REG_PC, pc));
    if (out->reason != HV_EXIT_REASON_EXCEPTION || (*syndrome >> 26) != 0x16 ||
        (*syndrome & 0xffff) != immediate || *pc != expected_pc) {
        fprintf(stderr, "unexpected exit reason=%u returned_pc=%llx syndrome=%llx\n",
            out->reason, (unsigned long long)*pc, (unsigned long long)*syndrome);
        fail("native checkpoint identity");
    }
}
static void *owner(void *arg) {
    struct cpu *c = arg;
    hv_vcpu_t v; hv_vcpu_exit_t *out;
    HV(hv_vcpu_create(&v, &out, NULL));
    HV(hv_vcpu_set_reg(v, HV_REG_PC, BASE));
    HV(hv_vcpu_set_reg(v, HV_REG_CPSR, 0x3c5));
    HV(hv_vcpu_set_reg(v, HV_REG_X8, c->id));
    HV(hv_vcpu_set_reg(v, HV_REG_X10, 0xff));
    HV(hv_vcpu_set_reg(v, HV_REG_X11, BASE + 0x4000));
    HV(hv_vcpu_set_reg(v, HV_REG_X12, tcr));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SCTLR_EL1, 0x30d00980));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_ACTLR_EL1, 2));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_CNTV_CTL_EL0, 0));
    run_native(v, out, address(handoff_boot_resume), 0x43, &c->pc, &c->syndrome);
    for (unsigned i = 0; i < 6; ++i)
        HV(hv_vcpu_get_reg(v, (hv_reg_t)(HV_REG_X9 + i), &c->boot[i]));
    HV(hv_vcpu_set_reg(v, HV_REG_PC, address(handoff_boot_resume)));
    PT(pthread_mutex_lock(&lock));
    ++ready; PT(pthread_cond_broadcast(&changed));
    for (;;) {
        while (c->requested == c->completed && !c->stop)
            PT(pthread_cond_wait(&changed, &lock));
        if (c->stop) break;
        unsigned request = c->requested;
        uint64_t input[8]; memcpy(input, c->input, sizeof(input));
        PT(pthread_mutex_unlock(&lock));
        for (unsigned i = 0; i < 8; ++i)
            HV(hv_vcpu_set_reg(v, (hv_reg_t)(HV_REG_X0 + i), input[i]));
        run_native(v, out, address(handoff_reply_resume), 0x44, &c->pc, &c->syndrome);
        for (unsigned i = 0; i < 10; ++i)
            HV(hv_vcpu_get_reg(v, (hv_reg_t)(HV_REG_X0 + i), &c->output[i]));
        // Explicit emitter-owned resume, never generic PC+4 recovery.
        HV(hv_vcpu_set_reg(v, HV_REG_PC, address(handoff_reply_resume)));
        PT(pthread_mutex_lock(&lock));
        c->completed = request; PT(pthread_cond_broadcast(&changed));
    }
    PT(pthread_mutex_unlock(&lock));
    HV(hv_vcpu_destroy(v));
    return NULL;
}
static void roundtrip(const char *tag, unsigned epoch, unsigned phase,
                      unsigned alias, unsigned mask, unsigned write, const uint64_t data[4]) {
    PT(pthread_mutex_lock(&lock));
    unsigned request = ++serial;
    for (unsigned i = 0; i < 2; ++i) if (mask & (1u << i)) {
        struct cpu *c = &cpus[i];
        c->input[0] = write; c->input[1] = epoch;
        c->input[2] = phase; c->input[3] = alias;
        for (unsigned j = 0; j < 4; ++j) c->input[4+j] = data[j];
        c->requested = request;
    }
    PT(pthread_cond_broadcast(&changed));
    for (unsigned i = 0; i < 2; ++i) if (mask & (1u << i))
        while (cpus[i].completed != request) PT(pthread_cond_wait(&changed, &lock));
    for (unsigned i = 0; i < 2; ++i) if (mask & (1u << i)) {
        struct cpu *c = &cpus[i];
        printf("{\"kind\":\"ack\",\"tag\":\"%s\",\"cpu\":%u,\"request\":%u,"
            "\"resume_pc\":%llu,\"syndrome\":%llu,\"regs\":[", tag, i, request,
            (unsigned long long)c->pc, (unsigned long long)c->syndrome);
        for (unsigned j = 0; j < 10; ++j)
            printf("%s\"%016llx\"", j ? "," : "", (unsigned long long)c->output[j]);
        puts("]}");
    }
    PT(pthread_mutex_unlock(&lock));
}
static void observe(const char *tag, unsigned e, unsigned p, const uint64_t expected[4]) {
    roundtrip(tag, e, p, 0, 3, 0, expected);
    roundtrip(tag, e, p, 1, 3, 0, expected);
}
static void host_transfer(const char *kind, unsigned e, unsigned p, uint64_t v[4], int write) {
    if (!direct) fail("host alias access in copied cell");
    barrier();
    for (unsigned i = 0; i < 4; ++i) {
        volatile uint64_t *at = (volatile uint64_t *)(ram + 0x10000 + i * 128);
        if (write) *at = v[i]; else v[i] = *at;
    }
    barrier();
    printf("{\"kind\":\"%s\",\"epoch\":%u,\"phase\":%u,\"words\":", kind,e,p);
    words(v); puts("}");
}
static int publish_source(uint64_t captured) { return captured == generation; }
static void backend_commit(unsigned e, unsigned p, uint64_t v[4]) {
    uint64_t pending = generation, snapshot[4];
    event("grant", e, p); // all owner threads are at validated DSB/HVC acknowledgements
    if (p == 1) {
        if (direct) host_transfer("source_read", e, p, snapshot, 0);
        else memcpy(snapshot, &cpus[e&1].output[4], sizeof(snapshot));
        printf("{\"kind\":\"source_capture\",\"epoch\":%u,\"phase\":1,\"generation\":%llu,\"words\":",
            e,(unsigned long long)pending); words(snapshot); puts("}");
    }
    if (direct) host_transfer("host_write", e, p, v, 1);
    else roundtrip("copy_commit", e, p, e & 1, 1u << (e & 1), 1, v);
    ++generation; event("invalidate", e, p);
    if (p == 1) {
        if (publish_source(pending)) fail("stale source publication accepted");
        event("reject_old_source", e, p);
    }
    event("complete", e, p);
    observe("released", e, p, v);
}
int main(int argc, char **argv) {
    setbuf(stdout, NULL);
    if (argc != 3 || (strcmp(argv[1], "ish") && strcmp(argv[1], "osh")) ||
        (strcmp(argv[2], "copy") && strcmp(argv[2], "direct"))) fail("arguments");
    struct rlimit limit;
    if (getrlimit(RLIMIT_CPU, &limit) || limit.rlim_cur != RLIM_INFINITY || limit.rlim_max != RLIM_INFINITY)
        fail("inherited VM CPU limit must be unlimited; no setter is used");
    uint64_t cacheline = 0; size_t cacheline_size = sizeof(cacheline);
    if (sysctlbyname("hw.cachelinesize", &cacheline, &cacheline_size, NULL, 0) ||
        (cacheline_size != 4 && cacheline_size != 8) ||
        (cacheline != 64 && cacheline != 128)) fail("unsupported host cache line size");
    direct = !strcmp(argv[2], "direct");
    unsigned sh = !strcmp(argv[1], "ish") ? 3 : 2;
    tcr = 16 | (1ul << 8) | (1ul << 10) | ((uint64_t)sh << 12) | (1ul << 23) | (2ul << 32);
    descriptor = (BASE + 0x10000) | 3 | (1ul << 10) | ((uint64_t)sh << 8) | (3ul << 53);
    ram = mmap(NULL, SIZE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (ram == MAP_FAILED) fail("mmap");
    size_t code_size = (size_t)((uintptr_t)handoff_end - (uintptr_t)handoff_start);
    if (code_size > 4096) fail("code size");
    memcpy(ram, handoff_start, code_size);
    uint64_t *l0 = (void *)(ram+0x4000), *l1 = (void *)(ram+0x5000);
    uint64_t *l2 = (void *)(ram+0x6000), *l3 = (void *)(ram+0x7000);
    l0[0] = (BASE + 0x5000) | 3; l1[1] = (BASE + 0x6000) | 3; l2[0] = (BASE + 0x7000) | 3;
    for (unsigned i = 0; i < SIZE/4096; ++i)
        l3[i] = (BASE + i*4096) | 3 | (1ul << 10) | ((uint64_t)sh << 8) | (i ? (3ul << 53) : 0);
    l3[16] = descriptor; l3[32] = descriptor;
    uint64_t v[4]; values(0,0,v);
    for (unsigned i=0;i<4;++i) *(uint64_t *)(ram+0x10000+i*128)=v[i];
    __builtin___clear_cache((char *)ram, (char *)ram+code_size); barrier();
    printf("{\"kind\":\"layout\",\"schema\":\"natemu.handoff.v2\",\"shareability\":\"%s\",\"route\":\"%s\","
        "\"epochs\":%u,\"bytes\":%u,\"cache_line_bytes\":%llu,\"word_stride\":128,\"boot_instruction_pc\":%llu,\"boot_resume_pc\":%llu,\"reply_instruction_pc\":%llu,\"reply_resume_pc\":%llu,\"tcr\":%llu,\"descriptor\":%llu}\n",
        argv[1],argv[2],EPOCHS,SIZE,(unsigned long long)cacheline,(unsigned long long)address(handoff_boot_hvc),(unsigned long long)address(handoff_boot_resume),
        (unsigned long long)address(handoff_reply_hvc),(unsigned long long)address(handoff_reply_resume),(unsigned long long)tcr,(unsigned long long)descriptor);
    HV(hv_vm_create(NULL));
    HV(hv_vm_map(ram, BASE, SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    pthread_t threads[2];
    for (unsigned i=0;i<2;++i) { cpus[i].id=i; PT(pthread_create(&threads[i],NULL,owner,&cpus[i])); }
    PT(pthread_mutex_lock(&lock));
    while (ready != 2) PT(pthread_cond_wait(&changed,&lock));
    for (unsigned i=0;i<2;++i) {
        printf("{\"kind\":\"boot\",\"cpu\":%u,\"resume_pc\":%llu,\"syndrome\":%llu,\"regs\":[",i,
            (unsigned long long)cpus[i].pc,(unsigned long long)cpus[i].syndrome);
        for (unsigned j=0;j<6;++j) printf("%s%llu",j ? "," : "",(unsigned long long)cpus[i].boot[j]);
        puts("]}");
    }
    // Retain BOTH complete observations before any value comparison can fail.
    for (unsigned i=0;i<2;++i) {
        const struct cpu *c = &cpus[i];
        if (c->boot[0] != 2 || c->boot[1] != 0xff || c->boot[2] != tcr ||
            c->boot[3] != 0x30d01985 || c->boot[4] != descriptor || c->boot[5] != descriptor)
            fail("stage1/TSO readback");
    }
    PT(pthread_mutex_unlock(&lock));
    observe("initial",0,0,v);
    for (unsigned e=1;e<=EPOCHS;++e) {
        observe("prewarm",e,0,v);
        values(e,1,v); backend_commit(e,1,v);
        values(e,2,v);
        roundtrip("resident_write",e,2,e&1,1u<<(e&1),1,v);
        ++generation; event("invalidate",e,2);
        observe("resident_drain",e,2,v);
        event("grant",e,2);
        if (direct) host_transfer("host_read",e,2,v,0);
        else { // Actual register exports from the most recent drained alias observation.
            memcpy(v,&cpus[e&1].output[4],sizeof(v));
            printf("{\"kind\":\"copy_export\",\"epoch\":%u,\"phase\":2,\"words\":",e);
            words(v); puts("}");
        }
        event("complete",e,2);
        observe("returned",e,2,v);
        values(e,3,v); backend_commit(e,3,v);
    }
    PT(pthread_mutex_lock(&lock));
    for (unsigned i=0;i<2;++i) cpus[i].stop=1;
    PT(pthread_cond_broadcast(&changed)); PT(pthread_mutex_unlock(&lock));
    for (unsigned i=0;i<2;++i) PT(pthread_join(threads[i],NULL));
    HV(hv_vm_unmap(BASE,SIZE)); HV(hv_vm_destroy());
    if (munmap(ram,SIZE)) fail("munmap");
    puts("{\"kind\":\"teardown\",\"cpus\":2,\"epochs\":64}");
    return 0;
}
