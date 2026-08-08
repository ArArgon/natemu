// G-1, properly closed.
//
// The original probe (probe_g1_tgran_tso.c) read ID_AA64MMFR0_EL1.TGran4 and inferred that a
// 4 KB stage-1 regime works. It never built a page table, never set SCTLR_EL1.M, and never
// translated an address. This probe does the experiment instead.
//
//   stage1  Guest EL1 builds a real 4-level, 4 KB-granule stage-1 table with TCR_EL1.TG0=4K and
//           T0SZ=16 (48-bit VA), enables the MMU, and:
//             (a) translates a load/store at VA 0x0000_4000_0000_0000 -- a VA that is only
//                 reachable with T0SZ=16, so success also proves the 48-bit VA claim (09 N-01);
//             (b) maps two ADJACENT 4 KB pages that live inside ONE 16 KB host page with
//                 DIFFERENT permissions (RW and RO) and shows the RO one faults on write.
//           (b) is the test that can actually fail, and the one the whole shadow-paging design
//           depends on.
//
//   stage2  Same sub-16K-page question one level down: with HV_IPA_GRANULE_4KB, map two adjacent
//           4 KB IPAs backed by the same 16 KB host page with different stage-2 permissions and
//           show the RO one traps. The old probe only checked that hv_vm_map() RETURNED SUCCESS;
//           a successful map call is not evidence of enforcement.
//
//   ids     Corrected decode of ID_AA64MMFR0/1_EL1 as read from guest EL1 (fixes the TGran16 and
//           PARange decode bugs in the original probe, and reports ASIDBits / HAFDBS / PAN / XNX
//           which were never logged).
//
// Build: see build.sh (needs com.apple.security.hypervisor, ad-hoc signing is enough).

#include <Hypervisor/Hypervisor.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#define RAM_IPA   0x40000000ULL
#define RAM_SIZE  (2ULL << 20)

// Offsets inside guest RAM.
#define OFF_CODE   0x00000
#define OFF_VEC    0x02000   // 2 KB aligned, as VBAR_EL1 requires
#define OFF_L0     0x04000
#define OFF_L1_ID  0x05000
#define OFF_L2_ID  0x06000
#define OFF_L1_T   0x07000
#define OFF_L2_T   0x08000
#define OFF_L3_T   0x09000
// DATA_A and DATA_B are two adjacent 4 KB pages inside ONE 16 KB host page (0x10000 is
// 16 KB-aligned, so 0x10000..0x13fff is a single host page). This is the whole point.
#define OFF_DATA_A 0x10000
#define OFF_DATA_B 0x11000

// A VA that needs all four levels AND needs T0SZ=16. With T0SZ=25 (39-bit VA) this address is
// out of range and would take an address-size fault instead.
#define VA_A 0x0000400000000000ULL
#define VA_B 0x0000400000001000ULL

#define MAGIC_A 0xAAAA0000AAAA0001ULL
#define MAGIC_B 0xBBBB0000BBBB0001ULL
#define STOREV  0x5A5A5A5A5A5A5A5AULL

// ---------------------------------------------------------------------------------------------
// Guest payloads. Written as real assembly so clang encodes them; the host memcpy's the bytes
// into guest RAM. Position-independent: everything comes in through registers.
// ---------------------------------------------------------------------------------------------
__asm__(
".section __TEXT,__text\n"
".p2align 2\n"

// ---- stage-1 payload -------------------------------------------------------------------------
// x0=MAIR  x1=TCR  x2=TTBR0  x3=VBAR  x4=VA_A  x5=VA_B  x6=store value
".globl _g1_s1_start\n"
"_g1_s1_start:\n"
"    msr  mair_el1, x0\n"
"    msr  tcr_el1,  x1\n"
"    msr  ttbr0_el1, x2\n"
"    msr  vbar_el1, x3\n"
"    isb\n"
"    tlbi vmalle1\n"
"    dsb  nsh\n"
"    isb\n"
"    mrs  x7, sctlr_el1\n"
"    mov  x8, #0x1005\n"          // M | C | I
"    orr  x7, x7, x8\n"
"    msr  sctlr_el1, x7\n"
"    isb\n"
// MMU is on from here. Every access below is translated by our own 4 KB tables.
"    ldr  x10, [x4]\n"            // read RW page
"    str  x6,  [x4]\n"            // write RW page -- must succeed
"    ldr  x11, [x4]\n"            // read back
"    ldr  x12, [x5]\n"            // read RO page -- reads are allowed
"    mrs  x13, sctlr_el1\n"
"    hvc  #0\n"                   // checkpoint: report x10..x13
"    str  x6,  [x5]\n"            // write RO page -- MUST fault (4 KB granularity inside 16 KB)
"    hvc  #1\n"                   // only reached if no fault occurred == FAIL
".globl _g1_s1_end\n"
"_g1_s1_end:\n"

// ---- exception handler (replicated into every vector slot) ------------------------------------
".globl _g1_vec_start\n"
"_g1_vec_start:\n"
"    mrs  x20, esr_el1\n"
"    mrs  x21, far_el1\n"
"    mrs  x22, elr_el1\n"
"    hvc  #2\n"
".globl _g1_vec_end\n"
"_g1_vec_end:\n"

// ---- stage-2 payload -------------------------------------------------------------------------
// MMU off; VA==IPA. x0 = IPA of the read-only 4 KB page, x1 = value to store.
".globl _g1_s2_start\n"
"_g1_s2_start:\n"
"    ldr  x10, [x0]\n"            // read stage-2 RO page -- allowed
"    hvc  #0\n"                   // checkpoint
"    str  x1,  [x0]\n"            // write it -- MUST trap to the host
"    hvc  #1\n"                   // only reached if no trap == FAIL
".globl _g1_s2_end\n"
"_g1_s2_end:\n"

// ---- id-register payload ----------------------------------------------------------------------
".globl _g1_ids_start\n"
"_g1_ids_start:\n"
"    mrs  x0, id_aa64mmfr0_el1\n"
"    mrs  x1, id_aa64mmfr1_el1\n"
"    mrs  x2, id_aa64mmfr2_el1\n"
"    mrs  x3, midr_el1\n"
"    mrs  x4, aidr_el1\n"
"    hvc  #0\n"
".globl _g1_ids_end\n"
"_g1_ids_end:\n"
);

extern const uint8_t g1_s1_start[], g1_s1_end[];
extern const uint8_t g1_vec_start[], g1_vec_end[];
extern const uint8_t g1_s2_start[], g1_s2_end[];
extern const uint8_t g1_ids_start[], g1_ids_end[];

// ---------------------------------------------------------------------------------------------

static int failures = 0;
static void ok(bool cond, const char *what) {
    printf("  [%s] %s\n", cond ? "PASS" : "FAIL", what);
    if (!cond) failures++;
}
#define HV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) { \
    fprintf(stderr, "  %s -> 0x%x at %d\n", #e, _r, __LINE__); exit(2);} } while (0)

// Stage-1 descriptor attributes (4 KB granule, VMSAv8-64).
#define D_VALID   (1ULL << 0)
#define D_TABLE   (3ULL << 0)   // table descriptor at L0..L2, page descriptor at L3
#define D_BLOCK   (1ULL << 0)   // block descriptor at L1/L2
#define D_ATTR0   (0ULL << 2)   // AttrIndx = 0 -> MAIR attr0
#define D_AP_RW   (0ULL << 6)   // EL1 RW, EL0 none
#define D_AP_RO   (2ULL << 6)   // EL1 RO, EL0 none
#define D_SH_IS   (3ULL << 8)   // inner shareable
#define D_AF      (1ULL << 10)
#define D_UXN     (1ULL << 54)

// TCR_EL1: T0SZ=16 (48-bit VA), TG0=0b00 (4 KB), IRGN0/ORGN0=WB, SH0=IS,
//          T1SZ=16, EPD1=1 (TTBR1 unused), TG1=0b10 (4 KB), IPS=0b010 (40-bit PA).
#define TCR_VAL (16ULL | (1ULL<<8) | (1ULL<<10) | (3ULL<<12) | (0ULL<<14) \
                | (16ULL<<16) | (1ULL<<23) | (2ULL<<30) | (2ULL<<32))
#define MAIR_VAL 0xFFULL        // attr0 = Normal, Inner/Outer WB RW-Allocate

static uint64_t esr_ec(uint64_t esr) { return (esr >> 26) & 0x3f; }
static const char *dfsc_str(uint64_t esr) {
    switch (esr & 0x3f) {
        case 0x04: case 0x05: case 0x06: case 0x07: return "translation fault";
        case 0x08: case 0x09: case 0x0a: case 0x0b: return "access flag fault";
        case 0x0c: case 0x0d: case 0x0e: case 0x0f: return "PERMISSION FAULT";
        case 0x00: case 0x01: case 0x02: case 0x03: return "address size fault";
        default: return "other";
    }
}

// ---------------------------------------------------------------------------------------------
static int run_stage1(void) {
    printf("\n=== stage-1: real 4 KB-granule page tables at guest EL1 ===\n");

    HV(hv_vm_create(NULL));

    uint8_t *mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE,
                        MAP_ANON | MAP_PRIVATE, -1, 0);
    if (mem == MAP_FAILED) { perror("mmap"); return 2; }
    memset(mem, 0, RAM_SIZE);
    HV(hv_vm_map(mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));

    // ---- code ----
    memcpy(mem + OFF_CODE, g1_s1_start, (size_t)(g1_s1_end - g1_s1_start));
    // Replicate the handler into all 16 vector slots (0x000..0x780, stride 0x80).
    size_t vlen = (size_t)(g1_vec_end - g1_vec_start);
    for (int i = 0; i < 16; i++) memcpy(mem + OFF_VEC + i * 0x80, g1_vec_start, vlen);

    // ---- data ----
    *(uint64_t *)(mem + OFF_DATA_A) = MAGIC_A;
    *(uint64_t *)(mem + OFF_DATA_B) = MAGIC_B;

    // ---- page tables ----
    uint64_t *l0 = (uint64_t *)(mem + OFF_L0);
    uint64_t *l1_id = (uint64_t *)(mem + OFF_L1_ID), *l2_id = (uint64_t *)(mem + OFF_L2_ID);
    uint64_t *l1_t = (uint64_t *)(mem + OFF_L1_T), *l2_t = (uint64_t *)(mem + OFF_L2_T);
    uint64_t *l3_t = (uint64_t *)(mem + OFF_L3_T);

    // Identity map of guest RAM so the code can keep executing once the MMU comes on.
    // VA 0x40000000 -> L0[0], L1[1], L2[0] as a 2 MB block.
    l0[0]    = (RAM_IPA + OFF_L1_ID) | D_TABLE;
    l1_id[1] = (RAM_IPA + OFF_L2_ID) | D_TABLE;
    l2_id[0] = RAM_IPA | D_BLOCK | D_AF | D_SH_IS | D_AP_RW | D_ATTR0;  // executable: PXN=0

    // Test mapping. VA_A/VA_B -> L0[128], L1[0], L2[0], L3[0..1].
    l0[128] = (RAM_IPA + OFF_L1_T) | D_TABLE;
    l1_t[0] = (RAM_IPA + OFF_L2_T) | D_TABLE;
    l2_t[0] = (RAM_IPA + OFF_L3_T) | D_TABLE;
    l3_t[0] = (RAM_IPA + OFF_DATA_A) | D_TABLE | D_AF | D_SH_IS | D_AP_RW | D_ATTR0 | D_UXN;
    l3_t[1] = (RAM_IPA + OFF_DATA_B) | D_TABLE | D_AF | D_SH_IS | D_AP_RO | D_ATTR0 | D_UXN;

    printf("  DATA_A ipa=0x%llx  DATA_B ipa=0x%llx  (same 16 KB host page: %s)\n",
           RAM_IPA + OFF_DATA_A, RAM_IPA + OFF_DATA_B,
           ((OFF_DATA_A & ~0x3fffULL) == (OFF_DATA_B & ~0x3fffULL)) ? "YES" : "NO");
    printf("  VA_A=0x%llx VA_B=0x%llx  TCR_EL1=0x%llx (TG0=4K, T0SZ=16)\n",
           VA_A, VA_B, (unsigned long long)TCR_VAL);

    hv_vcpu_t vcpu; hv_vcpu_exit_t *vexit;
    HV(hv_vcpu_create(&vcpu, &vexit, NULL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_PC,  RAM_IPA + OFF_CODE));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5));  // EL1h, DAIF masked
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X0,  MAIR_VAL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X1,  TCR_VAL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X2,  RAM_IPA + OFF_L0));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X3,  RAM_IPA + OFF_VEC));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X4,  VA_A));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X5,  VA_B));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X6,  STOREV));

    // ---- checkpoint 1: MMU on, translations working ----
    HV(hv_vcpu_run(vcpu));
    uint64_t sy = vexit->exception.syndrome;
    if (vexit->reason != HV_EXIT_REASON_EXCEPTION || esr_ec(sy) != 0x16) {
        printf("  UNEXPECTED exit reason=%u ESR=0x%llx EC=0x%llx\n",
               vexit->reason, (unsigned long long)sy, (unsigned long long)esr_ec(sy));
        printf("  [FAIL] guest did not reach checkpoint 1 -- MMU enable or translation failed\n");
        return 1;
    }
    uint64_t imm, x10, x11, x12, x13, pc;
    imm = sy & 0xffff;
    HV(hv_vcpu_get_reg(vcpu, HV_REG_X10, &x10));
    HV(hv_vcpu_get_reg(vcpu, HV_REG_X11, &x11));
    HV(hv_vcpu_get_reg(vcpu, HV_REG_X12, &x12));
    HV(hv_vcpu_get_reg(vcpu, HV_REG_X13, &x13));
    printf("\n  -- checkpoint %llu: SCTLR_EL1=0x%llx (M=%llu C=%llu I=%llu)\n",
           (unsigned long long)imm, (unsigned long long)x13,
           (unsigned long long)(x13 & 1), (unsigned long long)((x13 >> 2) & 1),
           (unsigned long long)((x13 >> 12) & 1));
    ok(imm == 0, "reached checkpoint 1 (MMU enabled, code still executing)");
    ok((x13 & 1) == 1, "SCTLR_EL1.M == 1 -- stage-1 MMU actually ON");
    ok(x10 == MAGIC_A, "load through 4 KB stage-1 translation at 48-bit VA returned correct data");
    ok(x11 == STOREV,  "store through 4 KB stage-1 translation to RW page took effect");
    ok(*(uint64_t *)(mem + OFF_DATA_A) == STOREV, "store landed at the right host address");
    ok(x12 == MAGIC_B, "read of the adjacent RO 4 KB page returned correct data");

    // ---- the decisive test: write to the RO page sharing a 16 KB host page with the RW one ----
    HV(hv_vcpu_run(vcpu));
    sy = vexit->exception.syndrome;
    if (vexit->reason == HV_EXIT_REASON_EXCEPTION && esr_ec(sy) == 0x16 && (sy & 0xffff) == 1) {
        printf("\n  -- guest reached hvc #1: the store to the RO page was NOT faulted\n");
        ok(false, "4 KB-granular permission enforcement inside one 16 KB host page");
        printf("         => stage-1 permissions are NOT enforced at 4 KB here. This would\n"
               "            invalidate the 1:1 shadow-paging design.\n");
    } else if (vexit->reason == HV_EXIT_REASON_EXCEPTION && esr_ec(sy) == 0x16 &&
               (sy & 0xffff) == 2) {
        uint64_t esr, far, elr;
        HV(hv_vcpu_get_reg(vcpu, HV_REG_X20, &esr));
        HV(hv_vcpu_get_reg(vcpu, HV_REG_X21, &far));
        HV(hv_vcpu_get_reg(vcpu, HV_REG_X22, &elr));
        printf("\n  -- guest took an exception: ESR_EL1=0x%llx EC=0x%llx (%s) FAR_EL1=0x%llx ELR=0x%llx\n",
               (unsigned long long)esr, (unsigned long long)esr_ec(esr),
               dfsc_str(esr), (unsigned long long)far, (unsigned long long)elr);
        bool is_data_abort = (esr_ec(esr) == 0x25);           // data abort, current EL
        bool is_perm       = ((esr & 0x3c) == 0x0c);          // DFSC 0b1100xx
        bool is_write      = ((esr >> 6) & 1);                // WnR
        ok(is_data_abort, "exception is a data abort from current EL (EC=0x25)");
        ok(is_perm,       "fault status is PERMISSION fault (not translation/access-flag)");
        ok(is_write,      "fault was on a write (ESR.WnR=1)");
        ok(far == VA_B,   "FAR_EL1 == the RO page's VA");
        ok(*(uint64_t *)(mem + OFF_DATA_B) == MAGIC_B, "RO page contents unmodified");
        printf("\n  ==> 4 KB stage-1 permissions ARE enforced independently for two 4 KB pages\n"
               "      inside a single 16 KB host page.\n");
    } else {
        printf("\n  UNEXPECTED exit reason=%u ESR=0x%llx EC=0x%llx\n", vexit->reason,
               (unsigned long long)sy, (unsigned long long)esr_ec(sy));
        HV(hv_vcpu_get_reg(vcpu, HV_REG_PC, &pc));
        printf("  PC=0x%llx\n", (unsigned long long)pc);
        ok(false, "decisive permission test produced a recognizable result");
    }

    hv_vcpu_destroy(vcpu);
    hv_vm_unmap(RAM_IPA, RAM_SIZE);
    hv_vm_destroy();
    return failures ? 1 : 0;
}

// ---------------------------------------------------------------------------------------------
static int run_stage2(void) {
    printf("\n=== stage-2: 4 KB IPA granule enforcement inside one 16 KB host page ===\n");

    hv_vm_config_t cfg = hv_vm_config_create();
    hv_return_t r = hv_vm_config_set_ipa_granule(cfg, HV_IPA_GRANULE_4KB);
    if (r != HV_SUCCESS) { printf("  [SKIP] 4 KB IPA granule unsupported (0x%x)\n", r); return 0; }
    HV(hv_vm_create(cfg));

    // ONE 16 KB host page, split into two 4 KB stage-2 mappings with different permissions.
    size_t hp = 16384;
    uint8_t *mem = mmap(NULL, hp, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (mem == MAP_FAILED) { perror("mmap"); return 2; }
    memset(mem, 0, hp);
    printf("  host page = %p (16 KB), split into 2 x 4 KB stage-2 mappings\n", (void *)mem);

    memcpy(mem, g1_s2_start, (size_t)(g1_s2_end - g1_s2_start));
    *(uint64_t *)(mem + 4096) = MAGIC_B;

    HV(hv_vm_map(mem,        RAM_IPA,          4096, HV_MEMORY_READ | HV_MEMORY_EXEC | HV_MEMORY_WRITE));
    HV(hv_vm_map(mem + 4096, RAM_IPA + 0x1000, 4096, HV_MEMORY_READ | HV_MEMORY_EXEC)); // no WRITE

    hv_vcpu_t vcpu; hv_vcpu_exit_t *vexit;
    HV(hv_vcpu_create(&vcpu, &vexit, NULL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_PC, RAM_IPA));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5));  // EL1h, DAIF masked
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X0, RAM_IPA + 0x1000));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_X1, STOREV));

    HV(hv_vcpu_run(vcpu));
    uint64_t sy = vexit->exception.syndrome;
    ok(vexit->reason == HV_EXIT_REASON_EXCEPTION && esr_ec(sy) == 0x16 && (sy & 0xffff) == 0,
       "guest read the read-only 4 KB stage-2 page");
    uint64_t x10; HV(hv_vcpu_get_reg(vcpu, HV_REG_X10, &x10));
    ok(x10 == MAGIC_B, "read returned the right data from host page offset 4096");

    HV(hv_vcpu_run(vcpu));
    sy = vexit->exception.syndrome;
    if (vexit->reason == HV_EXIT_REASON_EXCEPTION && esr_ec(sy) == 0x24) {
        printf("\n  -- stage-2 abort: ESR=0x%llx EC=0x24 (data abort, lower EL) IPA=0x%llx VA=0x%llx\n",
               (unsigned long long)sy,
               (unsigned long long)vexit->exception.physical_address,
               (unsigned long long)vexit->exception.virtual_address);
        ok(((sy & 0x3c) == 0x0c), "stage-2 fault status is PERMISSION fault");
        ok(((sy >> 6) & 1) == 1, "fault was on a write");
        ok(vexit->exception.physical_address == RAM_IPA + 0x1000, "faulting IPA is the RO 4 KB page");
        ok(*(uint64_t *)(mem + 4096) == MAGIC_B, "RO page contents unmodified");
        printf("\n  ==> stage-2 enforces 4 KB permissions inside one 16 KB host page.\n");
    } else if (esr_ec(sy) == 0x16 && (sy & 0xffff) == 1) {
        ok(false, "stage-2 4 KB permission enforcement (write to RO page was NOT trapped)");
    } else {
        printf("  UNEXPECTED exit reason=%u ESR=0x%llx EC=0x%llx\n", vexit->reason,
               (unsigned long long)sy, (unsigned long long)esr_ec(sy));
        ok(false, "stage-2 test produced a recognizable result");
    }

    hv_vcpu_destroy(vcpu);
    hv_vm_destroy();
    return failures ? 1 : 0;
}

// ---------------------------------------------------------------------------------------------
static const char *tg4(uint64_t v)  { return v == 0 ? "4 KB SUPPORTED" : v == 1 ? "4 KB SUPPORTED + LPA2" : v == 0xf ? "not supported" : "reserved"; }
static const char *tg16(uint64_t v) { return v == 0 ? "not supported" : v == 1 ? "16 KB SUPPORTED" : v == 2 ? "16 KB SUPPORTED + LPA2" : "reserved"; }
static const char *tg64(uint64_t v) { return v == 0 ? "64 KB SUPPORTED" : v == 0xf ? "not supported" : "reserved"; }
static const char *parange(uint64_t v) {
    static const char *t[] = {"32 bits / 4 GB", "36 bits / 64 GB", "40 bits / 1 TB", "42 bits / 4 TB",
                              "44 bits / 16 TB", "48 bits / 256 TB", "52 bits / 4 PB", "56 bits"};
    return v < 8 ? t[v] : "reserved";
}

static int run_ids(void) {
    printf("\n=== ID registers as seen from guest EL1 (corrected decode) ===\n");
    HV(hv_vm_create(NULL));
    uint8_t *mem = mmap(NULL, 16384, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    memset(mem, 0, 16384);
    memcpy(mem, g1_ids_start, (size_t)(g1_ids_end - g1_ids_start));
    HV(hv_vm_map(mem, RAM_IPA, 16384, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));

    hv_vcpu_t vcpu; hv_vcpu_exit_t *vexit;
    HV(hv_vcpu_create(&vcpu, &vexit, NULL));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_PC, RAM_IPA));
    HV(hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5));  // EL1h, DAIF masked

    size_t ilen = (size_t)(g1_ids_end - g1_ids_start);
    printf("  payload %zu bytes:", ilen);
    for (size_t i = 0; i < ilen / 4 && i < 8; i++)
        printf(" %08x", ((const uint32_t *)mem)[i]);
    printf("\n");

    HV(hv_vcpu_run(vcpu));
    printf("  exit reason=%u ESR=0x%llx EC=0x%llx FAR=0x%llx IPA=0x%llx\n",
           vexit->reason, (unsigned long long)vexit->exception.syndrome,
           (unsigned long long)esr_ec(vexit->exception.syndrome),
           (unsigned long long)vexit->exception.virtual_address,
           (unsigned long long)vexit->exception.physical_address);
    uint64_t dpc; HV(hv_vcpu_get_reg(vcpu, HV_REG_PC, &dpc));
    printf("  PC=0x%llx\n", (unsigned long long)dpc);

    uint64_t m0, m1, m2, midr, aidr;
    HV(hv_vcpu_get_reg(vcpu, HV_REG_X0, &m0));
    HV(hv_vcpu_get_reg(vcpu, HV_REG_X1, &m1));
    HV(hv_vcpu_get_reg(vcpu, HV_REG_X2, &m2));
    HV(hv_vcpu_get_reg(vcpu, HV_REG_X3, &midr));
    HV(hv_vcpu_get_reg(vcpu, HV_REG_X4, &aidr));

    printf("  ID_AA64MMFR0_EL1 = 0x%016llx\n", (unsigned long long)m0);
    printf("    TGran4  [31:28] = 0x%llx  %s\n", (m0 >> 28) & 0xf, tg4((m0 >> 28) & 0xf));
    printf("    TGran64 [27:24] = 0x%llx  %s\n", (m0 >> 24) & 0xf, tg64((m0 >> 24) & 0xf));
    printf("    TGran16 [23:20] = 0x%llx  %s   <-- old probe decoded this backwards\n",
           (m0 >> 20) & 0xf, tg16((m0 >> 20) & 0xf));
    printf("    ASIDBits [7:4]  = 0x%llx  %s   <-- never reported before\n",
           (m0 >> 4) & 0xf, ((m0 >> 4) & 0xf) == 2 ? "16-bit ASID (65536)" : "8-bit ASID (256)");
    printf("    PARange  [3:0]  = 0x%llx  %s   <-- old probe said '32 bits'\n",
           m0 & 0xf, parange(m0 & 0xf));
    printf("  ID_AA64MMFR1_EL1 = 0x%016llx\n", (unsigned long long)m1);
    printf("    HAFDBS  [3:0]   = 0x%llx  %s\n", m1 & 0xf,
           (m1 & 0xf) == 0 ? "no hardware Access/Dirty bit management" :
           (m1 & 0xf) == 1 ? "hardware Access flag only" : "hardware Access + Dirty");
    printf("    VMIDBits [7:4]  = 0x%llx  %s\n", (m1 >> 4) & 0xf,
           ((m1 >> 4) & 0xf) == 2 ? "16-bit VMID" : "8-bit VMID");
    printf("    PAN     [23:20] = 0x%llx  %s\n", (m1 >> 20) & 0xf,
           ((m1 >> 20) & 0xf) >= 2 ? "FEAT_PAN2 -- AT S1E1RP/S1E1WP available" : "no PAN2");
    printf("    XNX     [31:28] = 0x%llx  %s\n", (m1 >> 28) & 0xf,
           ((m1 >> 28) & 0xf) ? "FEAT_XNX -- distinct EL0/EL1 execute-never" : "no XNX");
    printf("  ID_AA64MMFR2_EL1 = 0x%016llx\n", (unsigned long long)m2);
    printf("    VARange [19:16] = 0x%llx  %s\n", (m2 >> 16) & 0xf,
           ((m2 >> 16) & 0xf) ? "52-bit VA (FEAT_LVA)" : "48-bit VA max");
    printf("  MIDR_EL1 = 0x%016llx  implementer=0x%llx part=0x%llx %s\n",
           (unsigned long long)midr, (midr >> 24) & 0xff, (midr >> 4) & 0xfff,
           (((midr >> 4) & 0xfff) == 0) ? "<-- synthetic/masked by HVF" : "");
    printf("  AIDR_EL1 = 0x%016llx  bit9 (TSO capable) = %llu\n",
           (unsigned long long)aidr, (aidr >> 9) & 1);

    hv_vcpu_destroy(vcpu);
    hv_vm_destroy();
    return 0;
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "stage1";
    int rc;
    if      (!strcmp(mode, "stage1")) rc = run_stage1();
    else if (!strcmp(mode, "stage2")) rc = run_stage2();
    else if (!strcmp(mode, "ids"))    rc = run_ids();
    else { fprintf(stderr, "usage: %s stage1|stage2|ids\n", argv[0]); return 2; }
    printf("\n  %s: %d failure(s)\n", mode, failures);
    return rc;
}
