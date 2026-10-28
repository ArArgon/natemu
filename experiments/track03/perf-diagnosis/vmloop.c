// Runs a build_native.py image (same .cfg format as tools/bench/attribution/h2.c) inside a Hypervisor.framework VM at EL1,
// with the knobs natemu fixes: TSO (ACTLR_EL1 bit 1) and the stage-1 mapping of the data (4 KiB pages or 2 MiB blocks).
// MAP host: no VM; the same image, data and buffer layout run natively in this process (h2.c's trampoline, tramp2.s).
//   vmloop CFG TSO(0|1) MAP(4k|2m|16k|host) [REPS [IPA_GRANULE(16k|4k) [EL(1|0)]]]   -> "ns/iter MEDIAN (min A max B)", slope t(2N)-t(N) over N, host wall clock.
// Identity map VA = IPA; 4k/2m: 4 KiB granule (4 KiB pages, or 2 MiB blocks above the first 2 MiB); 16k: 16 KiB granule,
// 16 KiB pages (the host's page size). Attributes: normal WB (MAIR attr 0 = 0xff), inner shareable: natemu's shadow-leaf attributes.
#include <Hypervisor/Hypervisor.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <pthread.h>
#include <libkern/OSCacheControl.h>
extern const char g_start[], g_exit[], g_dispatch[], g_end[];
extern void run_block(uint64_t *regs, uint64_t entry); extern void exit_stub(void); extern void native_dispatch(void);
#define BASE UINT64_C(0x40000000)
#define RAM (16u << 20)
#define IMG 0x10000u      /* block image */
#define FRAME 0x20000u    /* x28 frame, 32 KiB */
#define L1T 0x30000u      /* dispatcher L1 table, 64 KiB */
#define FAST 0x40000u     /* dispatcher hit counter */
#define STACK 0x50000u    /* 64 KiB; SP_EL1 at its top */
#define BUF 0x200000u     /* data buffer, 2 MiB aligned */
static const int SRA[16] = {4,7,5,6,8,9,10,11,12,13,14,15,16,17,19,29};
static void die(const char *s, uint64_t a, uint64_t b) { fprintf(stderr, "FAIL %s %#llx %#llx\n", s, (unsigned long long)a, (unsigned long long)b); exit(2); }
#define HV(x) do { hv_return_t r_ = (x); if (r_ != HV_SUCCESS) die(#x, (uint64_t)r_, 0); } while (0)
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC_RAW, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
static hv_vcpu_t v; static hv_vcpu_exit_t *ex;
static void run_to(unsigned imm) {
    HV(hv_vcpu_run(v));
    uint64_t pc; HV(hv_vcpu_get_reg(v, HV_REG_PC, &pc));
    if (ex->reason != HV_EXIT_REASON_EXCEPTION || (ex->exception.syndrome >> 26) != 0x16 || (ex->exception.syndrome & 0xffff) != imm)
        die("unexpected exit (reason/syndrome, pc)", ((uint64_t)ex->reason << 32) | (uint32_t)ex->exception.syndrome, pc);
}
int main(int argc, char **argv) {
    if (argc < 4) die("usage: vmloop CFG TSO MAP [REPS]", 0, 0);
    int tso = atoi(argv[2]), map2m = !strcmp(argv[3], "2m"), map16k = !strcmp(argv[3], "16k"), reps = argc > 4 ? atoi(argv[4]) : 7;
    int g4k = argc > 5 && !strcmp(argv[5], "4k"), el0 = argc > 6 && !strcmp(argv[6], "0");
    FILE *f = fopen(argv[1], "r"); if (!f) die("cfg", 0, 0);
    char line[512], image[400] = "", fill[64] = ""; uint32_t entry = 0, shift = 0; uint64_t n = 1; int ctr = 1; size_t bufbytes = 1 << 20;
    struct { int r; char v[64]; } rs[16]; int nreg = 0; struct { uint64_t rip; uint32_t off; } l1[16]; int nl1 = 0;
    while (fgets(line, sizeof line, f)) {
        char a[64], c[64];
        if (sscanf(line, "image %399s", image) == 1) continue;
        if (sscanf(line, "entry %u", &entry) == 1) continue;
        if (sscanf(line, "n %llu", (unsigned long long *)&n) == 1) continue;
        if (sscanf(line, "ctr %d", &ctr) == 1) continue;
        if (sscanf(line, "shift %u", &shift) == 1) continue;
        if (sscanf(line, "bufbytes %zu", &bufbytes) == 1) continue;
        if (sscanf(line, "fill %63s", fill) == 1) continue;   // zero | lcg (README "LCG fill") | chain:STRIDE:MASK; default i*7 as h2.c
        if (sscanf(line, "reg %63s %63s", a, c) == 2) { rs[nreg].r = atoi(a); strcpy(rs[nreg++].v, c); continue; }
        if (sscanf(line, "l1 %63s %63s", a, c) == 2) { l1[nl1].rip = strtoull(a, 0, 0); l1[nl1++].off = atoi(c); continue; }
    }
    if (BUF + bufbytes > RAM) die("bufbytes", bufbytes, 0);
    uint8_t *ram = mmap(NULL, RAM, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0); if (ram == MAP_FAILED) die("mmap", 0, 0);
    if (g_end - g_start > 0x800) die("stub size", 0, 0);
    memcpy(ram, g_start, (size_t)(g_end - g_start));
    // EL0 runs: VBAR_EL1 = BASE + 0x800; the block's exit `hvc #2` is UNDEFINED at EL0 and enters the lower-EL sync
    // vector (+0x400), which executes hvc #2 at EL1. Every other vector: hvc #3.
    for (unsigned o = 0x800; o < 0x1000; o += 4) *(uint32_t *)(ram + o) = o == 0xc00 ? 0xd4000042 : 0xd4000062;
    FILE *im = fopen(image, "rb"); if (!im) die("image", 0, 0);
    size_t len = fread(ram + IMG, 1, 0x10000, im); fclose(im);
    // page tables at 0x1000 (L0), 0x2000 (L1), 0x3000 (L2), 0x4000 + i*0x1000 (L3 of 2 MiB region i)
    uint64_t *L0 = (void *)(ram + 0x1000), *L1 = (void *)(ram + 0x2000), *L2 = (void *)(ram + 0x3000);
    const uint64_t AF = 1u << 10, ISH = 3u << 8;
    L0[0] = (BASE + 0x2000) | 3; L1[1] = (BASE + 0x3000) | 3;
    for (unsigned i = 0; i < RAM >> 21; i++) {
        if (i && map2m) { L2[i] = (BASE + ((uint64_t)i << 21)) | 1 | AF | ISH | (el0 ? 1u << 6 : 0); continue; }
        uint64_t *L3 = (void *)(ram + 0x4000 + i * 0x1000); L2[i] = (BASE + 0x4000 + i * 0x1000) | 3;
        for (unsigned j = 0; j < 512; j++) L3[j] = (BASE + ((uint64_t)i << 21) + j * 4096u) | 3 | AF | ISH | (el0 && (i || j) ? 1u << 6 : 0);
    }
    uint8_t *frame = ram + FRAME + shift; uint64_t frame_va = BASE + FRAME + shift;
    *(uint64_t *)(frame + 0xb88) = BASE + (uint64_t)(g_exit - g_start);
    *(uint64_t *)(frame + 0xb68) = BASE + (uint64_t)(g_dispatch - g_start);
    uint64_t *l1t = (void *)(ram + L1T);
    for (int i = 0; i < nl1; i++) { l1t[(l1[i].rip & 0xfff) * 2] = l1[i].rip; l1t[(l1[i].rip & 0xfff) * 2 + 1] = BASE + IMG + l1[i].off; }
    *(uint64_t *)(frame + 0xa8) = BASE + L1T; *(uint64_t *)(frame + 0xb0) = BASE + FAST;
    uint8_t *orig = malloc(bufbytes); uint64_t x = 1, cs = 0, cm = 0;
    if (sscanf(fill, "chain:%llu:%llx", (unsigned long long *)&cs, (unsigned long long *)&cm) == 2) {
        // pointer chase as offsets: the 8 bytes at o hold (o + stride) & mask (the chased cells; everything else 0)
        memset(orig, 0, bufbytes); for (uint64_t o = 0;; ) { uint64_t nx = (o + cs) & cm; memcpy(orig + o, &nx, 8); o = nx; if (!o) break; }
    } else
    for (size_t i = 0; i < bufbytes; i++) {
        x = x * 6364136223846793005u + 1442695040888963407u;
        orig[i] = !strcmp(fill, "zero") ? 0 : !strcmp(fill, "lcg") ? (uint8_t)(x >> 56) : (uint8_t)(i * 7);
    }
    __builtin___clear_cache((char *)ram, (char *)ram + IMG + len); __asm__ volatile("dsb sy" ::: "memory");

    if (!strcmp(argv[3], "host")) {
        uint8_t *code = mmap(0, 1 << 16, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANON | MAP_JIT, -1, 0);
        pthread_jit_write_protect_np(0); memcpy(code, ram + IMG, len); pthread_jit_write_protect_np(1); sys_icache_invalidate(code, len);
        *(uint64_t *)(frame + 0xb88) = (uint64_t)exit_stub; *(uint64_t *)(frame + 0xb68) = (uint64_t)native_dispatch;
        for (int i = 0; i < nl1; i++) l1t[(l1[i].rip & 0xfff) * 2 + 1] = (uint64_t)code + l1[i].off;
        *(uint64_t *)(frame + 0xa8) = (uint64_t)l1t; *(uint64_t *)(frame + 0xb0) = (uint64_t)(ram + FAST);
        double all[64]; int nr = 0; uint64_t regs[32]; if (reps > 64) reps = 64;
        for (int rep = 0; rep < reps + 1; rep++) {
            double t[2];
            for (int k = 1; k <= 2; k++) {
                memset(regs, 0, sizeof regs); regs[28] = (uint64_t)frame;
                for (int i = 0; i < nreg; i++) {
                    const char *s = rs[i].v; uint64_t val;
                    if (!strncmp(s, "buf+", 4)) val = (uint64_t)(ram + BUF) + strtoull(s + 4, 0, 0);
                    else if (!strncmp(s, "stack+", 6)) val = (uint64_t)(ram + STACK) + strtoull(s + 6, 0, 0);
                    else val = strtoull(s, 0, 0);
                    regs[SRA[rs[i].r]] = val;
                }
                regs[SRA[ctr]] = n * k; memcpy(ram + BUF, orig, bufbytes);
                double t0 = now(); run_block(regs, (uint64_t)code + entry); t[k - 1] = now() - t0;
                if (regs[SRA[ctr]]) die("counter not zero", regs[SRA[ctr]], 0);
            }
            if (rep) all[nr++] = (t[1] - t[0]) / n * 1e9;
        }
        for (int i = 0; i < nr; i++) for (int j = i + 1; j < nr; j++) if (all[j] < all[i]) { double y = all[i]; all[i] = all[j]; all[j] = y; }
        printf("map=host regs"); for (int i = 0; i < 16; i++) printf(" %llx", (unsigned long long)regs[SRA[i]]);
        printf(" fast=%llu ns/iter %.4f (min %.4f max %.4f)\n", (unsigned long long)*(uint64_t *)(ram + FAST), all[nr / 2], all[0], all[nr - 1]);
        return 0;
    }
    hv_vm_config_t cfg = hv_vm_config_create();
    HV(hv_vm_config_set_ipa_granule(cfg, g4k ? HV_IPA_GRANULE_4KB : HV_IPA_GRANULE_16KB));
    HV(hv_vm_create(cfg)); HV(hv_vm_map(ram, BASE, RAM, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    HV(hv_vcpu_create(&v, &ex, NULL));
    if (map16k) {   // T0SZ 28 (36-bit VA), start at level 2: L2 table at 0x4000, one L3 table at 0x8000 (2048 x 16 KiB = 32 MiB)
        memset(ram + 0x1000, 0, 0xb000);
        uint64_t *M2 = (void *)(ram + 0x4000), *M3 = (void *)(ram + 0x8000);
        M2[BASE >> 25] = (BASE + 0x8000) | 3;
        for (unsigned j = 0; j < RAM / 16384; j++) M3[j] = (BASE + j * 16384u) | 3 | AF | ISH | (el0 && j ? 1u << 6 : 0);
    }
    uint64_t tcr = map16k ? 28 | (1u << 8) | (1u << 10) | (3u << 12) | (2u << 14) | (1u << 23) | (UINT64_C(2) << 32) : 16 | (1u << 8) | (1u << 10) | (3u << 12) | (1u << 23) | (UINT64_C(2) << 32);   // T0SZ 16, 4K, WB, ISH, EPD1, 40-bit IPS
    HV(hv_vcpu_set_reg(v, HV_REG_PC, BASE)); HV(hv_vcpu_set_reg(v, HV_REG_CPSR, 0x3c5));
    HV(hv_vcpu_set_reg(v, HV_REG_X10, 0xff)); HV(hv_vcpu_set_reg(v, HV_REG_X11, BASE + (map16k ? 0x4000 : 0x1000))); HV(hv_vcpu_set_reg(v, HV_REG_X12, tcr));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SCTLR_EL1, 0x30d00980)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_ACTLR_EL1, tso ? 2 : 0));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_CNTV_CTL_EL0, 0)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_VBAR_EL1, BASE + 0x800));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SP_EL0, BASE + STACK + 0x10000)); HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SP_EL1, BASE + STACK + 0x10000));
    run_to(1);
    uint64_t actlr, sctlr, el; HV(hv_vcpu_get_reg(v, HV_REG_X9, &actlr)); HV(hv_vcpu_get_reg(v, HV_REG_X10, &sctlr)); HV(hv_vcpu_get_reg(v, HV_REG_X11, &el));
    if ((actlr & 2) != (tso ? 2u : 0u) || (sctlr & 0x1005) != 0x1005 || el != 4) die("boot readback (actlr, sctlr)", actlr, sctlr);
    printf("tso=%d map=%s ipa=%s el=%d ", tso, argv[3], g4k ? "4k" : "16k", el0 ? 0 : 1);
    double all[64]; int nr = 0; if (reps > 64) reps = 64;
    for (int rep = 0; rep < reps + 1; rep++) {          // rep 0: warm-up, discarded
        double t[2];
        for (int k = 1; k <= 2; k++) {
            for (int r = 0; r < 31; r++) HV(hv_vcpu_set_reg(v, (hv_reg_t)(HV_REG_X0 + r), 0));
            HV(hv_vcpu_set_reg(v, (hv_reg_t)(HV_REG_X0 + 28), frame_va));
            for (int i = 0; i < nreg; i++) {
                const char *s = rs[i].v; uint64_t val;
                if (!strncmp(s, "buf+", 4)) val = BASE + BUF + strtoull(s + 4, 0, 0);
                else if (!strncmp(s, "stack+", 6)) val = BASE + STACK + strtoull(s + 6, 0, 0);
                else val = strtoull(s, 0, 0);
                HV(hv_vcpu_set_reg(v, (hv_reg_t)(HV_REG_X0 + SRA[rs[i].r]), val));
            }
            HV(hv_vcpu_set_reg(v, (hv_reg_t)(HV_REG_X0 + SRA[ctr]), n * k));
            memcpy(ram + BUF, orig, bufbytes);   // every run starts from the same data (outside the timed span)
            HV(hv_vcpu_set_reg(v, HV_REG_PC, BASE + IMG + entry)); HV(hv_vcpu_set_reg(v, HV_REG_CPSR, el0 ? 0x3c0 : 0x3c5));
            double t0 = now(); run_to(2); t[k - 1] = now() - t0;
            if (el0) {   // the exit really came from EL0: SPSR_EL1.M = EL0t, ESR_EL1.EC = 0 (undefined hvc)
                uint64_t spsr, esr; HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_SPSR_EL1, &spsr)); HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_ESR_EL1, &esr));
                if ((spsr & 15) != 0 || (esr >> 26) != 0) die("EL0 exit (spsr, esr)", spsr, esr);
            }
            uint64_t c; HV(hv_vcpu_get_reg(v, (hv_reg_t)(HV_REG_X0 + SRA[ctr]), &c)); if (c) die("counter not zero", c, 0);
        }
        if (rep) all[nr++] = (t[1] - t[0]) / n * 1e9;
    }
    for (int i = 0; i < nr; i++) for (int j = i + 1; j < nr; j++) if (all[j] < all[i]) { double x = all[i]; all[i] = all[j]; all[j] = x; }
    printf("regs");   // the 16 guest GPRs (rax..r15) after the last run (2N iterations)
    for (int i = 0; i < 16; i++) { uint64_t g; HV(hv_vcpu_get_reg(v, (hv_reg_t)(HV_REG_X0 + SRA[i]), &g)); printf(" %llx", (unsigned long long)g); }
    printf(" fast=%llu ns/iter %.4f (min %.4f max %.4f)\n", (unsigned long long)*(uint64_t *)(ram + FAST), all[nr / 2], all[0], all[nr - 1]);
    return 0;
}
