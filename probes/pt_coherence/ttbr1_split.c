// ttbr1_split.c -- does the ARM TTBR0/TTBR1 split land on x86's canonical boundary,
// and does a kernel-half mapping survive a TTBR0 (== guest CR3) swap untouched?
//
// `09` and `12` assert that with TCR_EL1.T0SZ = T1SZ = 17 the ARM VA split lands exactly on
// x86-64's canonical boundary at bit 47:
//     TTBR0  0x0000_0000_0000_0000 .. 0x0000_7fff_ffff_ffff   (x86 user half)
//     TTBR1  0xffff_8000_0000_0000 .. 0xffff_ffff_ffff_ffff   (x86 kernel half)
// and that guest-kernel mappings could therefore live in TTBR1 and survive guest CR3 switches
// without being rebuilt.  `probes/g1_mmu4k` used T0SZ=16 with EPD1=1 -- TTBR1 was never enabled
// and this claim has never been executed.  This probe executes it.
//
// Modes
// -----
//   func   Build a real T0SZ=T1SZ=17 pair of trees.  Run guest EL1 code, its exception vectors,
//          its data and its scratch block ENTIRELY out of TTBR1 at real Linux kernel VAs
//          (0xffff_8880_.... direct map, 0xffff_ffff_81.. text).  Then swap TTBR0 three ways --
//          to a different populated tree, to an all-zero root, and 64 times in a row with fresh
//          ASIDs -- and check that (a) every kernel-half access still works and returns the same
//          data, (b) the user-half VA that only the first tree mapped now faults, (c) restoring
//          the first tree brings it back.  No TLBI is issued anywhere after the initial one.
//
//   time   Cost of that.  Three 12 MiB pointer-chase working sets over distinct physical pages:
//          one in TTBR1 marked global (nG=0), one in TTBR1 marked non-global (nG=1), one in
//          TTBR0.  For each: chase warm, then chase with a fresh-ASID TTBR0 write inside the
//          timed window.  Also TLBI ASIDE1IS and TLBI VMALLE1IS against the global set.  The
//          question is how much TLB refill a guest CR3 switch costs the kernel half.
//
//   both   func then time.
//
// Build: ../build.sh pt_coherence/ttbr1_split.c
// Run:   ./bin/ttbr1_split both

#include <Hypervisor/Hypervisor.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#define RAM_IPA   0x40000000ULL
#define RAM_SIZE  (64ULL << 20)

// ---- guest physical (IPA offset) layout --------------------------------------------------------
#define OFF_CODE     0x00000ULL   // 4 pages of code+vectors, aliased into TTBR1 as "kernel text"
#define OFF_VEC      0x02000ULL   // inside the same 4-page code window
#define OFF_PARAM    0x04000ULL   // scratch/param block, mapped ONLY in TTBR1
#define OFF_DATA_KD  0x05000ULL   // kernel-half data page #1
#define OFF_DATA_KT  0x06000ULL   // kernel-half data page #2
#define OFF_DATA_LOW 0x07000ULL   // user-half data page
#define OFF_TABLES   0x10000ULL   // page-table arena
#define OFF_CHASE    0x400000ULL  // 3 x 12 MiB pointer-chase regions start here

// ---- guest virtual addresses -------------------------------------------------------------------
// Kernel half.  These are the *actual* addresses x86-64 Linux uses, so a pass here is a
// statement about the real guest, not about an arbitrary high address.
#define VA_KTEXT_BASE 0xffffffff81000000ULL   // Linux kernel text base -> our code+vectors
#define VA_KDIRECT    0xffff888000001000ULL   // Linux direct map base  -> data page #1
#define VA_KTEXT_DATA 0xffff888000002000ULL
#define VA_PARAM      0xffff888000010000ULL
#define VA_CHASE_HI_G  0xffff888040000000ULL  // kernel half, global (nG=0)
#define VA_CHASE_HI_NG 0xffff888080000000ULL  // kernel half, non-global (nG=1)
// User half.
#define VA_LOW         0x0000200000000000ULL
#define VA_CHASE_LO    0x0000400000000000ULL

#define MAGIC 0x5150000051500001ULL

// chase geometry
#define CHASE_PAGES 3072                 // 12 MiB, == the measured 4 KiB TLB reach (16 §4)
#define CHASE_BYTES ((uint64_t)CHASE_PAGES * 4096)
#define TIME_ROUNDS 15

// ---- stage-1 descriptor bits -------------------------------------------------------------------
#define D_TABLE  (3ULL << 0)
#define D_BLOCK  (1ULL << 0)
#define D_ATTR0  (0ULL << 2)
#define D_AP_RW  (0ULL << 6)
#define D_SH_IS  (3ULL << 8)
#define D_nG     (1ULL << 11)
#define D_AF     (1ULL << 10)
#define D_PXN    (1ULL << 53)
#define D_UXN    (1ULL << 54)

#define ATTR_DATA (D_AF | D_SH_IS | D_AP_RW | D_ATTR0 | D_UXN | D_PXN)
#define ATTR_CODE (D_AF | D_SH_IS | D_AP_RW | D_ATTR0 | D_UXN)   /* PXN=0: EL1 may execute */

// TCR_EL1: the whole point of this probe.
//   T0SZ=17 -> TTBR0 covers VA[46:0]  (0 .. 0x7fff_ffff_ffff)
//   T1SZ=17 -> TTBR1 covers VA[63:47]==all-ones (0xffff_8000_0000_0000 .. )
//   TG0=0b00 (4 KB), TG1=0b10 (4 KB), EPD1=0 (TTBR1 ENABLED -- g1_mmu4k had EPD1=1),
//   A1=0 (ASID comes from TTBR0_EL1), IPS=0b010 (40-bit PA), AS=0 (8-bit ASID).
#define TCR_VAL ( 17ULL                                                      \
                | (1ULL << 8) | (1ULL << 10) | (3ULL << 12) | (0ULL << 14)   \
                | (17ULL << 16) | (0ULL << 22) | (0ULL << 23)                \
                | (1ULL << 24) | (1ULL << 26) | (3ULL << 28) | (2ULL << 30)  \
                | (2ULL << 32) )
#define MAIR_VAL 0xFFULL

// ------------------------------------------------------------------------------------------------
// Guest payload.  Runs at EL1.  After `br x5` every instruction fetch, every vector entry and
// every data access in `func` mode goes through TTBR1.
// ------------------------------------------------------------------------------------------------
__asm__(
".section __TEXT,__text\n"
".p2align 2\n"

// x0=MAIR x1=TCR x2=TTBR0 x3=TTBR1 x4=VBAR(high) x5=high entry x6=param VA(high) x7=mode
".globl _pt_start\n"
"_pt_start:\n"
"    msr  mair_el1, x0\n"
"    msr  tcr_el1,  x1\n"
"    msr  ttbr0_el1, x2\n"
"    msr  ttbr1_el1, x3\n"
"    msr  vbar_el1, x4\n"
"    isb\n"
"    tlbi vmalle1\n"
"    dsb  nsh\n"
"    isb\n"
"    mrs  x9, sctlr_el1\n"
"    mov  x10, #0x1005\n"           // M | C | I
"    orr  x9, x9, x10\n"
"    msr  sctlr_el1, x9\n"
"    isb\n"
"    ic   iallu\n"                  // host rewrote guest code; drop stale I-lines (16 §1)
"    dsb  nsh\n"
"    isb\n"
// MMU on.  Leave the identity mapping behind and continue executing from TTBR1.
"    br   x5\n"

".globl _pt_high\n"
"_pt_high:\n"
"    mov  x28, x6\n"                // param block -- a TTBR1 VA from here on
"    mov  x19, #0\n"                // fault counter
"    mov  x20, #256\n"              // fault limit
"    mrs  x9, cntfrq_el0\n"
"    str  x9, [x28, #0xc18]\n"
"    mov  x9, #1\n"
"    str  x9, [x28, #0xc20]\n"      // witness: we are executing from a TTBR1 VA
"    cmp  x7, #0\n"
"    b.eq Lfunc\n"
"    cmp  x7, #1\n"
"    b.eq Ltime\n"
"    hvc  #9\n"

// ---------------- Ltimeit: x7=body x9=rounds x11=result cursor -----------------------------------
"Ltimeit:\n"
"    mov  x13, x30\n"
"    mov  x14, x9\n"
"1:  isb\n"
"    mrs  x16, cntvct_el0\n"
"    blr  x7\n"
"    isb\n"
"    mrs  x17, cntvct_el0\n"
"    sub  x17, x17, x16\n"
"    str  x17, [x11], #8\n"
"    subs x14, x14, #1\n"
"    b.ne 1b\n"
"    mov  x30, x13\n"
"    ret\n"

// ================= mode 0: functional ============================================================
"Lfunc:\n"
"    ldr  x0, [x28, #0x000]\n"      // VA_KDIRECT
"    ldr  x1, [x28, #0x008]\n"      // VA_KTEXT_DATA
"    ldr  x2, [x28, #0x010]\n"      // VA_LOW
"    ldr  x6, [x28, #0x030]\n"      // magic
"    mrs  x9, tcr_el1\n"
"    str  x9, [x28, #0x170]\n"
"    mrs  x9, ttbr1_el1\n"
"    str  x9, [x28, #0x178]\n"
"    mrs  x9, ttbr0_el1\n"
"    str  x9, [x28, #0x190]\n"
// T1 -- write and read back both kernel-half data pages
"    str  x6, [x0]\n"
"    ldr  x9, [x0]\n"
"    str  x9, [x28, #0x100]\n"
"    add  x10, x6, #1\n"
"    str  x10, [x1]\n"
"    ldr  x9, [x1]\n"
"    str  x9, [x28, #0x108]\n"
// T2 -- write and read back the user-half data page (mapped only in tree A)
"    add  x11, x6, #2\n"
"    str  x11, [x2]\n"
"    ldr  x9, [x2]\n"
"    str  x9, [x28, #0x110]\n"
"    str  x19, [x28, #0x168]\n"     // faults so far -- must be 0
// T3 -- swap TTBR0 to tree B (identity map, but no VA_LOW).  No TLBI.
"    ldr  x12, [x28, #0x020]\n"
"    msr  ttbr0_el1, x12\n"
"    isb\n"
"    ldr  x9, [x0]\n"
"    str  x9, [x28, #0x118]\n"
"    ldr  x9, [x1]\n"
"    str  x9, [x28, #0x120]\n"
"    mov  x13, x19\n"
"    mov  x9, #0\n"
"    ldr  x9, [x2]\n"               // must fault
"    sub  x14, x19, x13\n"
"    str  x14, [x28, #0x128]\n"
"    ldr  x9, [x28, #0xc00]\n"
"    str  x9, [x28, #0x130]\n"
"    ldr  x9, [x28, #0xc08]\n"
"    str  x9, [x28, #0x138]\n"
// T4 -- swap TTBR0 to an ALL-ZERO root.  Nothing at all is mapped in the user half now.
"    ldr  x12, [x28, #0x028]\n"
"    msr  ttbr0_el1, x12\n"
"    isb\n"
"    ldr  x9, [x0]\n"
"    str  x9, [x28, #0x140]\n"
"    ldr  x9, [x1]\n"
"    str  x9, [x28, #0x141+7]\n"    // 0x148
"    mov  x13, x19\n"
"    ldr  x9, [x2]\n"               // must fault
"    sub  x14, x19, x13\n"
"    str  x14, [x28, #0x150]\n"
// ...and call a subroutine that lives at another TTBR1 VA, while TTBR0 maps nothing.
"    ldr  x15, [x28, #0x038]\n"
"    blr  x15\n"
// T5 -- 64 back-to-back TTBR0 swaps with fresh ASIDs, kernel-half access between each
"    ldr  x12, [x28, #0x018]\n"
"    and  x22, x12, #0x0000ffffffffffff\n"
"    mov  x23, #0\n"
"Lswap:\n"
"    add  x23, x23, #1\n"
"    and  x24, x23, #0x3f\n"
"    add  x24, x24, #4\n"
"    lsl  x24, x24, #48\n"
"    orr  x24, x24, x22\n"
"    msr  ttbr0_el1, x24\n"
"    isb\n"
"    ldr  x9, [x0]\n"
"    ldr  x10, [x1]\n"
"    cmp  x23, #64\n"
"    b.lo Lswap\n"
"    str  x9,  [x28, #0x180]\n"
"    str  x10, [x28, #0x188]\n"
"    str  x19, [x28, #0x18c+4]\n"   // 0x190? -- no: use 0x198
// T6 -- restore tree A; the user-half page must come back with its data intact
"    msr  ttbr0_el1, x12\n"
"    isb\n"
"    ldr  x9, [x2]\n"
"    str  x9, [x28, #0x158]\n"
"    str  x19, [x28, #0x160]\n"
"    hvc  #0\n"

// subroutine at a TTBR1 VA, called while TTBR0 is the zero root
"Lsub:\n"
"    ldr  x22, [x28, #0x000]\n"
"    ldr  x23, [x22]\n"
"    add  x23, x23, #0x100\n"
"    str  x23, [x28, #0x148+8]\n"   // 0x150 is taken; use 0x1a0 below instead
"    ret\n"

// ================= mode 1: timing ================================================================
"Ltime:\n"
"    ldr  x18, [x28, #0x018]\n"     // nodes per pass
"    ldr  x9,  [x28, #0x020]\n"     // rounds
"    ldr  x22, [x28, #0x028]\n"     // TTBR0 root (no ASID)
"    ldr  x5,  [x28, #0x030]\n"     // ASID counter seed
"    add  x11, x28, #0x200\n"
// --- arm 0/1: TTBR1 global set
"    ldr  x10, [x28, #0x000]\n"
"    bl   Lwarm\n"
"    adr  x7, Lb_chase\n"
"    bl   Ltimeit\n"
"    adr  x7, Lb_chase_asid\n"
"    bl   Ltimeit\n"
// --- arm 2/3: TTBR1 non-global set
"    ldr  x10, [x28, #0x008]\n"
"    bl   Lwarm\n"
"    adr  x7, Lb_chase\n"
"    bl   Ltimeit\n"
"    adr  x7, Lb_chase_asid\n"
"    bl   Ltimeit\n"
// --- arm 4/5: TTBR0 set
"    ldr  x10, [x28, #0x010]\n"
"    bl   Lwarm\n"
"    adr  x7, Lb_chase\n"
"    bl   Ltimeit\n"
"    adr  x7, Lb_chase_asid\n"
"    bl   Ltimeit\n"
// --- arm 6: TTBR1 global set + TLBI ASIDE1IS (must NOT evict global entries)
"    ldr  x10, [x28, #0x000]\n"
"    bl   Lwarm\n"
"    adr  x7, Lb_chase_aside\n"
"    bl   Ltimeit\n"
// --- arm 7: TTBR1 global set + TLBI VMALLE1IS (must evict everything)
"    bl   Lwarm\n"
"    adr  x7, Lb_chase_vmalle\n"
"    bl   Ltimeit\n"
"    str  x19, [x28, #0xc00+0x30]\n"
"    hvc  #0\n"

"Lwarm:\n"
"    mov  x13, x30\n"
"    mov  x0, x10\n"
"    mov  x15, x18\n"
"1:  ldr  x0, [x0]\n"
"    subs x15, x15, #1\n"
"    b.ne 1b\n"
"    mov  x0, x10\n"
"    mov  x15, x18\n"
"2:  ldr  x0, [x0]\n"
"    subs x15, x15, #1\n"
"    b.ne 2b\n"
"    mov  x30, x13\n"
"    ret\n"

"Lb_chase:\n"
"    mov  x0, x10\n"
"    mov  x15, x18\n"
"1:  ldr  x0, [x0]\n"
"    subs x15, x15, #1\n"
"    b.ne 1b\n"
"    ret\n"

"Lb_chase_asid:\n"
"    add  x5, x5, #1\n"
"    and  x6, x5, #0xff\n"
"    lsl  x6, x6, #48\n"
"    orr  x6, x6, x22\n"
"    msr  ttbr0_el1, x6\n"
"    isb\n"
"    mov  x0, x10\n"
"    mov  x15, x18\n"
"1:  ldr  x0, [x0]\n"
"    subs x15, x15, #1\n"
"    b.ne 1b\n"
"    ret\n"

"Lb_chase_aside:\n"
"    mrs  x6, ttbr0_el1\n"
"    and  x6, x6, #0xffff000000000000\n"
"    dsb  ishst\n"
"    tlbi aside1is, x6\n"
"    dsb  ish\n"
"    isb\n"
"    mov  x0, x10\n"
"    mov  x15, x18\n"
"1:  ldr  x0, [x0]\n"
"    subs x15, x15, #1\n"
"    b.ne 1b\n"
"    ret\n"

"Lb_chase_vmalle:\n"
"    dsb  ishst\n"
"    tlbi vmalle1is\n"
"    dsb  ish\n"
"    isb\n"
"    mov  x0, x10\n"
"    mov  x15, x18\n"
"1:  ldr  x0, [x0]\n"
"    subs x15, x15, #1\n"
"    b.ne 1b\n"
"    ret\n"

// ---------------- exception handler (replicated into all 16 vector slots) -------------------------
".globl _pt_vec\n"
"_pt_vec:\n"
"    mrs  x25, esr_el1\n"
"    str  x25, [x28, #0xc00]\n"
"    mrs  x26, far_el1\n"
"    str  x26, [x28, #0xc08]\n"
"    add  x19, x19, #1\n"
"    cmp  x19, x20\n"
"    b.hi 9f\n"
"    mrs  x27, elr_el1\n"
"    add  x27, x27, #4\n"
"    msr  elr_el1, x27\n"
"    eret\n"
"9:  hvc  #9\n"
".globl _pt_vec_end\n"
"_pt_vec_end:\n"
".globl _pt_end\n"
"_pt_end:\n"
);

extern const uint8_t pt_start[], pt_end[], pt_high[], pt_vec[], pt_vec_end[];

// ------------------------------------------------------------------------------------------------
static int failures = 0;
static void ok(bool c, const char *w) { printf("  [%s] %s\n", c ? "PASS" : "FAIL", w); if (!c) failures++; }
#define HV(e) do { hv_return_t _r = (e); if (_r != HV_SUCCESS) { \
    fprintf(stderr, "  %s -> 0x%x at %d\n", #e, _r, __LINE__); exit(2);} } while (0)

static uint8_t *mem;
static uint64_t arena;          // bump allocator over the table arena, in IPA offsets
static uint64_t arena_top;

static uint64_t alloc_pt(void) {
    uint64_t o = arena; arena += 4096;
    if (arena > arena_top) { fprintf(stderr, "table arena exhausted\n"); exit(2); }
    memset(mem + o, 0, 4096);
    return o;
}
static uint64_t *T(uint64_t off) { return (uint64_t *)(mem + off); }

// 4 KB mapping into the tree rooted at IPA offset `root`.  T0SZ=T1SZ=17 => 8 index bits at L0.
static void map4k(uint64_t root, uint64_t va, uint64_t ipa, uint64_t attrs) {
    uint64_t i0 = (va >> 39) & 0xff, i1 = (va >> 30) & 0x1ff,
             i2 = (va >> 21) & 0x1ff, i3 = (va >> 12) & 0x1ff;
    uint64_t *l0 = T(root);
    if (!(l0[i0] & 1)) l0[i0] = (RAM_IPA + alloc_pt()) | D_TABLE;
    uint64_t *l1 = T((l0[i0] & 0x0000fffffffff000ULL) - RAM_IPA);
    if (!(l1[i1] & 1)) l1[i1] = (RAM_IPA + alloc_pt()) | D_TABLE;
    uint64_t *l2 = T((l1[i1] & 0x0000fffffffff000ULL) - RAM_IPA);
    if (!(l2[i2] & 1)) l2[i2] = (RAM_IPA + alloc_pt()) | D_TABLE;
    uint64_t *l3 = T((l2[i2] & 0x0000fffffffff000ULL) - RAM_IPA);
    l3[i3] = ipa | D_TABLE | attrs;   // at L3 the 0b11 encoding is "page"
}

// 2 MB block mapping (used only for the boot-time identity map of guest RAM)
static void map2m(uint64_t root, uint64_t va, uint64_t ipa, uint64_t attrs) {
    uint64_t i0 = (va >> 39) & 0xff, i1 = (va >> 30) & 0x1ff, i2 = (va >> 21) & 0x1ff;
    uint64_t *l0 = T(root);
    if (!(l0[i0] & 1)) l0[i0] = (RAM_IPA + alloc_pt()) | D_TABLE;
    uint64_t *l1 = T((l0[i0] & 0x0000fffffffff000ULL) - RAM_IPA);
    if (!(l1[i1] & 1)) l1[i1] = (RAM_IPA + alloc_pt()) | D_TABLE;
    uint64_t *l2 = T((l1[i1] & 0x0000fffffffff000ULL) - RAM_IPA);
    l2[i2] = ipa | D_BLOCK | attrs;
}

// deterministic shuffle
static uint64_t rng_s = 0x243f6a8885a308d3ULL;
static uint64_t rnd(void) { rng_s ^= rng_s << 13; rng_s ^= rng_s >> 7; rng_s ^= rng_s << 17; return rng_s; }

// Build a randomised pointer chase over `pages` 4 KiB pages at VA base `vb` / IPA offset `po`.
// One node per page at a rotating intra-page offset so nodes do not collide in cache sets.
// Returns the VA of the head node.
static uint64_t build_chase(uint64_t vb, uint64_t po, int pages) {
    int *perm = malloc(sizeof(int) * pages);
    for (int i = 0; i < pages; i++) perm[i] = i;
    for (int i = pages - 1; i > 0; i--) { int j = (int)(rnd() % (uint64_t)(i + 1)); int t = perm[i]; perm[i] = perm[j]; perm[j] = t; }
    uint64_t head = vb + (uint64_t)perm[0] * 4096 + (uint64_t)(perm[0] % 32) * 128;
    for (int k = 0; k < pages; k++) {
        int cur = perm[k], nxt = perm[(k + 1) % pages];
        uint64_t nva = vb + (uint64_t)nxt * 4096 + (uint64_t)(nxt % 32) * 128;
        *(uint64_t *)(mem + po + (uint64_t)cur * 4096 + (uint64_t)(cur % 32) * 128) = nva;
    }
    free(perm);
    return head;
}

static uint64_t esr_ec(uint64_t e) { return (e >> 26) & 0x3f; }
static const char *dfsc_str(uint64_t e) {
    switch (e & 0x3f) {
        case 0x04: case 0x05: case 0x06: case 0x07: return "translation fault";
        case 0x08: case 0x09: case 0x0a: case 0x0b: return "access flag fault";
        case 0x0c: case 0x0d: case 0x0e: case 0x0f: return "permission fault";
        case 0x00: case 0x01: case 0x02: case 0x03: return "address size fault";
        default: return "other";
    }
}

// ------------------------------------------------------------------------------------------------
struct vm {
    hv_vcpu_t vcpu;
    hv_vcpu_exit_t *vexit;
    uint64_t root_a, root_b, root_z, root_k;
    uint64_t param;     // IPA offset of the param page
};

static void build_common(struct vm *v) {
    arena = OFF_TABLES; arena_top = OFF_CHASE;
    v->root_a = alloc_pt();
    v->root_b = alloc_pt();
    v->root_z = alloc_pt();
    v->root_k = alloc_pt();
    v->param  = OFF_PARAM;

    // Boot identity map of guest RAM, in trees A and B.  Needed only until `br x5`.
    for (uint64_t o = 0; o < RAM_SIZE; o += (2ULL << 20)) {
        map2m(v->root_a, RAM_IPA + o, RAM_IPA + o, D_AF | D_SH_IS | D_AP_RW | D_ATTR0 | D_UXN);
        map2m(v->root_b, RAM_IPA + o, RAM_IPA + o, D_AF | D_SH_IS | D_AP_RW | D_ATTR0 | D_UXN);
    }
    // Kernel half: code+vectors at Linux's kernel-text base, executable at EL1.
    for (int i = 0; i < 4; i++)
        map4k(v->root_k, VA_KTEXT_BASE + (uint64_t)i * 4096, RAM_IPA + OFF_CODE + (uint64_t)i * 4096, ATTR_CODE);
    // Kernel half: param block and the two data pages, at Linux's direct-map base.
    map4k(v->root_k, VA_PARAM,      RAM_IPA + OFF_PARAM,    ATTR_DATA);
    map4k(v->root_k, VA_KDIRECT,    RAM_IPA + OFF_DATA_KD,  ATTR_DATA);
    map4k(v->root_k, VA_KTEXT_DATA, RAM_IPA + OFF_DATA_KT,  ATTR_DATA);
    // User half: one data page, mapped ONLY in tree A.
    map4k(v->root_a, VA_LOW,        RAM_IPA + OFF_DATA_LOW, ATTR_DATA);
}

static void load_code(void) {
    memcpy(mem + OFF_CODE, pt_start, (size_t)(pt_end - pt_start));
    size_t vlen = (size_t)(pt_vec_end - pt_vec);
    if (vlen > 0x80) { fprintf(stderr, "vector handler too long (%zu)\n", vlen); exit(2); }
    for (int i = 0; i < 16; i++) memcpy(mem + OFF_VEC + (size_t)i * 0x80, pt_vec, vlen);
}

// high (TTBR1) alias of a code label
static uint64_t hi(const uint8_t *label) {
    return VA_KTEXT_BASE + OFF_CODE + (uint64_t)(label - pt_start);
}

static int cmp_u64(const void *a, const void *b) {
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return x < y ? -1 : x > y ? 1 : 0;
}
static uint64_t median(uint64_t *v, int n) { qsort(v, (size_t)n, 8, cmp_u64); return v[n / 2]; }

// ------------------------------------------------------------------------------------------------
static int run_func(void) {
    printf("\n=== func: T0SZ=T1SZ=17, guest EL1 running entirely out of TTBR1 ===\n");
    struct vm v; memset(&v, 0, sizeof v);

    HV(hv_vm_create(NULL));
    mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (mem == MAP_FAILED) { perror("mmap"); return 2; }
    memset(mem, 0, RAM_SIZE);
    HV(hv_vm_map(mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));

    load_code();
    build_common(&v);

    uint64_t *p = (uint64_t *)(mem + v.param);
    p[0x000 / 8] = VA_KDIRECT;
    p[0x008 / 8] = VA_KTEXT_DATA;
    p[0x010 / 8] = VA_LOW;
    p[0x018 / 8] = (RAM_IPA + v.root_a) | (1ULL << 48);
    p[0x020 / 8] = (RAM_IPA + v.root_b) | (2ULL << 48);
    p[0x028 / 8] = (RAM_IPA + v.root_z) | (3ULL << 48);
    p[0x030 / 8] = MAGIC;
    p[0x038 / 8] = hi((const uint8_t *)0) + 0;  // patched below

    // address of Lsub: find it by symbol-free arithmetic -- we exported only a few labels, so
    // instead we store the high alias of the whole payload and let the guest use a fixed offset.
    // Simpler: export it.  (done via _pt_sub below)
    extern const uint8_t pt_sub[];
    p[0x038 / 8] = hi(pt_sub);

    printf("  TCR_EL1  = 0x%llx  (T0SZ=%llu T1SZ=%llu TG0=%llu TG1=%llu EPD1=%llu A1=%llu)\n",
           (unsigned long long)TCR_VAL, TCR_VAL & 0x3f, (TCR_VAL >> 16) & 0x3f,
           (TCR_VAL >> 14) & 3, (TCR_VAL >> 30) & 3, (TCR_VAL >> 23) & 1, (TCR_VAL >> 22) & 1);
    printf("  TTBR0 A  = 0x%llx (ASID 1)   TTBR0 B = 0x%llx (ASID 2)   TTBR0 zero = 0x%llx (ASID 3)\n",
           (unsigned long long)(RAM_IPA + v.root_a), (unsigned long long)(RAM_IPA + v.root_b),
           (unsigned long long)(RAM_IPA + v.root_z));
    printf("  TTBR1    = 0x%llx\n", (unsigned long long)(RAM_IPA + v.root_k));
    printf("  kernel text alias  0x%llx  entry 0x%llx  vectors 0x%llx\n",
           (unsigned long long)VA_KTEXT_BASE, (unsigned long long)hi(pt_high),
           (unsigned long long)(VA_KTEXT_BASE + OFF_VEC));
    printf("  kernel data        0x%llx / 0x%llx   param 0x%llx   user-half page 0x%llx\n",
           (unsigned long long)VA_KDIRECT, (unsigned long long)VA_KTEXT_DATA,
           (unsigned long long)VA_PARAM, (unsigned long long)VA_LOW);

    HV(hv_vcpu_create(&v.vcpu, &v.vexit, NULL));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_PC, RAM_IPA + OFF_CODE));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_CPSR, 0x3c5));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X0, MAIR_VAL));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X1, TCR_VAL));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X2, (RAM_IPA + v.root_a) | (1ULL << 48)));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X3, RAM_IPA + v.root_k));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X4, VA_KTEXT_BASE + OFF_VEC));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X5, hi(pt_high)));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X6, VA_PARAM));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X7, 0));

    HV(hv_vcpu_run(v.vcpu));
    uint64_t sy = v.vexit->exception.syndrome;
    if (v.vexit->reason != HV_EXIT_REASON_EXCEPTION || esr_ec(sy) != 0x16 || (sy & 0xffff) != 0) {
        uint64_t pc = 0; hv_vcpu_get_reg(v.vcpu, HV_REG_PC, &pc);
        printf("  UNEXPECTED exit reason=%u ESR=0x%llx EC=0x%llx imm=%llu PC=0x%llx\n",
               v.vexit->reason, (unsigned long long)sy, (unsigned long long)esr_ec(sy),
               (unsigned long long)(sy & 0xffff), (unsigned long long)pc);
        printf("    exception VA=0x%llx IPA=0x%llx\n",
               (unsigned long long)v.vexit->exception.virtual_address,
               (unsigned long long)v.vexit->exception.physical_address);
        ok(false, "guest reached the end of the functional sequence");
        goto done;
    }

    printf("\n  -- results read back from the TTBR1-mapped param block --\n");
    printf("     witness 'executing from TTBR1'      = %llu\n", (unsigned long long)p[0xc20 / 8]);
    printf("     TCR_EL1 readback                    = 0x%llx\n", (unsigned long long)p[0x170 / 8]);
    printf("     TTBR1_EL1 readback                  = 0x%llx\n", (unsigned long long)p[0x178 / 8]);
    printf("     TTBR0_EL1 readback (at entry)       = 0x%llx\n", (unsigned long long)p[0x190 / 8]);
    printf("     kernel-half read #1 / #2            = 0x%llx / 0x%llx\n",
           (unsigned long long)p[0x100 / 8], (unsigned long long)p[0x108 / 8]);
    printf("     user-half read                      = 0x%llx\n", (unsigned long long)p[0x110 / 8]);
    printf("     faults before any TTBR0 swap        = %llu\n", (unsigned long long)p[0x168 / 8]);
    printf("     after TTBR0 -> tree B: kernel reads = 0x%llx / 0x%llx, user faults = %llu\n",
           (unsigned long long)p[0x118 / 8], (unsigned long long)p[0x120 / 8],
           (unsigned long long)p[0x128 / 8]);
    printf("       ESR=0x%llx (%s) FAR=0x%llx\n", (unsigned long long)p[0x130 / 8],
           dfsc_str(p[0x130 / 8]), (unsigned long long)p[0x138 / 8]);
    printf("     after TTBR0 -> zero root: kernel    = 0x%llx / 0x%llx, user faults = %llu\n",
           (unsigned long long)p[0x140 / 8], (unsigned long long)p[0x148 / 8],
           (unsigned long long)p[0x150 / 8]);
    printf("     TTBR1 subroutine call under zero    = 0x%llx\n", (unsigned long long)p[0x1a0 / 8]);
    printf("     after 64 fresh-ASID TTBR0 swaps     = 0x%llx / 0x%llx\n",
           (unsigned long long)p[0x180 / 8], (unsigned long long)p[0x188 / 8]);
    printf("     after restoring tree A: user read   = 0x%llx\n", (unsigned long long)p[0x158 / 8]);
    printf("     total faults                        = %llu\n", (unsigned long long)p[0x160 / 8]);

    printf("\n");
    ok(p[0xc20 / 8] == 1, "guest executed instructions fetched through TTBR1 at 0xffffffff81xxxxxx");
    ok((p[0x170 / 8] & 0x3f) == 17 && ((p[0x170 / 8] >> 16) & 0x3f) == 17,
       "TCR_EL1 accepted T0SZ=T1SZ=17 (split at bit 47 == x86 canonical boundary)");
    ok(((p[0x170 / 8] >> 23) & 1) == 0, "TTBR1 is enabled (EPD1=0)");
    ok(p[0x100 / 8] == MAGIC, "store+load through TTBR1 at Linux direct-map VA 0xffff888000001000");
    ok(p[0x108 / 8] == MAGIC + 1, "store+load through TTBR1 at a second kernel-half VA");
    ok(*(uint64_t *)(mem + OFF_DATA_KD) == MAGIC, "kernel-half store landed at the right IPA");
    ok(p[0x110 / 8] == MAGIC + 2, "store+load through TTBR0 at user-half VA 0x200000000000");
    ok(p[0x168 / 8] == 0, "no faults before the first TTBR0 swap");
    ok(p[0x118 / 8] == MAGIC && p[0x120 / 8] == MAGIC + 1,
       "kernel half unchanged after TTBR0 swapped to a DIFFERENT tree (no TLBI issued)");
    ok(p[0x128 / 8] == 1, "user-half VA faulted exactly once under the new TTBR0");
    ok((p[0x130 / 8] & 0x3c) == 0x04 && p[0x138 / 8] == VA_LOW,
       "that fault was a translation fault with FAR == the user-half VA");
    ok(p[0x140 / 8] == MAGIC && p[0x148 / 8] == MAGIC + 1,
       "kernel half unchanged with TTBR0 pointing at an ALL-ZERO root");
    ok(p[0x150 / 8] == 1, "user-half VA faulted under the zero root");
    ok(p[0x1a0 / 8] == MAGIC + 0x100,
       "a subroutine call+return between two TTBR1 VAs works with TTBR0 mapping nothing");
    ok(p[0x180 / 8] == MAGIC && p[0x188 / 8] == MAGIC + 1,
       "kernel half survives 64 back-to-back TTBR0 writes with 64 different ASIDs");
    ok(p[0x158 / 8] == MAGIC + 2,
       "restoring the original TTBR0 restores the user-half mapping and its data");
    ok(p[0x160 / 8] == 2, "exactly two faults in the whole run, both the intended ones");

done:
    hv_vcpu_destroy(v.vcpu);
    hv_vm_unmap(RAM_IPA, RAM_SIZE);
    hv_vm_destroy();
    munmap(mem, RAM_SIZE);
    return failures ? 1 : 0;
}

// ------------------------------------------------------------------------------------------------
static int run_time(void) {
    printf("\n=== time: what a TTBR0 (guest CR3) swap costs the kernel half ===\n");
    struct vm v; memset(&v, 0, sizeof v);

    HV(hv_vm_create(NULL));
    mem = mmap(NULL, RAM_SIZE, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (mem == MAP_FAILED) { perror("mmap"); return 2; }
    memset(mem, 0, RAM_SIZE);
    HV(hv_vm_map(mem, RAM_IPA, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));

    load_code();
    build_common(&v);

    // Three disjoint 12 MiB physical regions, one per arm.
    uint64_t po_g  = OFF_CHASE;
    uint64_t po_ng = OFF_CHASE + CHASE_BYTES;
    uint64_t po_lo = OFF_CHASE + 2 * CHASE_BYTES;
    if (po_lo + CHASE_BYTES > RAM_SIZE) { fprintf(stderr, "RAM too small\n"); exit(2); }

    for (int i = 0; i < CHASE_PAGES; i++) {
        map4k(v.root_k, VA_CHASE_HI_G  + (uint64_t)i * 4096, RAM_IPA + po_g  + (uint64_t)i * 4096, ATTR_DATA);
        map4k(v.root_k, VA_CHASE_HI_NG + (uint64_t)i * 4096, RAM_IPA + po_ng + (uint64_t)i * 4096, ATTR_DATA | D_nG);
        map4k(v.root_a, VA_CHASE_LO    + (uint64_t)i * 4096, RAM_IPA + po_lo + (uint64_t)i * 4096, ATTR_DATA);
    }
    uint64_t head_g  = build_chase(VA_CHASE_HI_G,  po_g,  CHASE_PAGES);
    uint64_t head_ng = build_chase(VA_CHASE_HI_NG, po_ng, CHASE_PAGES);
    uint64_t head_lo = build_chase(VA_CHASE_LO,    po_lo, CHASE_PAGES);

    uint64_t *p = (uint64_t *)(mem + v.param);
    p[0x000 / 8] = head_g;
    p[0x008 / 8] = head_ng;
    p[0x010 / 8] = head_lo;
    p[0x018 / 8] = CHASE_PAGES;
    p[0x020 / 8] = TIME_ROUNDS;
    p[0x028 / 8] = RAM_IPA + v.root_a;
    p[0x030 / 8] = 16;      // ASID counter seed

    printf("  %d pages / arm (%llu MiB), %d rounds, three disjoint physical regions\n",
           CHASE_PAGES, (unsigned long long)(CHASE_BYTES >> 20), TIME_ROUNDS);
    printf("  arms: TTBR1 global (nG=0) | TTBR1 non-global (nG=1) | TTBR0\n");

    HV(hv_vcpu_create(&v.vcpu, &v.vexit, NULL));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_PC, RAM_IPA + OFF_CODE));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_CPSR, 0x3c5));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X0, MAIR_VAL));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X1, TCR_VAL));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X2, (RAM_IPA + v.root_a) | (1ULL << 48)));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X3, RAM_IPA + v.root_k));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X4, VA_KTEXT_BASE + OFF_VEC));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X5, hi(pt_high)));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X6, VA_PARAM));
    HV(hv_vcpu_set_reg(v.vcpu, HV_REG_X7, 1));

    HV(hv_vcpu_run(v.vcpu));
    uint64_t sy = v.vexit->exception.syndrome;
    if (v.vexit->reason != HV_EXIT_REASON_EXCEPTION || esr_ec(sy) != 0x16 || (sy & 0xffff) != 0) {
        uint64_t pc = 0; hv_vcpu_get_reg(v.vcpu, HV_REG_PC, &pc);
        printf("  UNEXPECTED exit reason=%u ESR=0x%llx EC=0x%llx imm=%llu PC=0x%llx FAR=0x%llx\n",
               v.vexit->reason, (unsigned long long)sy, (unsigned long long)esr_ec(sy),
               (unsigned long long)(sy & 0xffff), (unsigned long long)pc,
               (unsigned long long)v.vexit->exception.virtual_address);
        ok(false, "timing run completed");
        goto done;
    }

    double tick = 1e9 / (double)p[0xc18 / 8];   // ns per CNTVCT tick
    printf("  CNTFRQ_EL0 = %llu Hz (%.2f ns/tick), faults during timing = %llu\n",
           (unsigned long long)p[0xc18 / 8], tick, (unsigned long long)p[0xc30 / 8]);

    static const char *names[8] = {
        "TTBR1 global      chase, warm            ",
        "TTBR1 global      chase, fresh-ASID TTBR0",
        "TTBR1 non-global  chase, warm            ",
        "TTBR1 non-global  chase, fresh-ASID TTBR0",
        "TTBR0             chase, warm            ",
        "TTBR0             chase, fresh-ASID TTBR0",
        "TTBR1 global      chase, TLBI ASIDE1IS   ",
        "TTBR1 global      chase, TLBI VMALLE1IS  ",
    };
    double per[8];
    printf("\n  %-42s %12s %12s\n", "arm", "ns/access", "ns/round");
    for (int a = 0; a < 8; a++) {
        uint64_t r[TIME_ROUNDS];
        for (int i = 0; i < TIME_ROUNDS; i++) r[i] = p[(0x200 / 8) + a * TIME_ROUNDS + i];
        uint64_t m = median(r, TIME_ROUNDS);
        double ns = (double)m * tick;
        per[a] = ns / (double)CHASE_PAGES;
        printf("  %-42s %12.3f %12.1f\n", names[a], per[a], ns);
    }
    printf("\n  derived:\n");
    printf("    TTBR1 global, refill charged by a fresh-ASID TTBR0 write : %+8.3f ns/page\n",
           per[1] - per[0]);
    printf("    TTBR1 non-global, same                                   : %+8.3f ns/page\n",
           per[3] - per[2]);
    printf("    TTBR0,            same                                   : %+8.3f ns/page\n",
           per[5] - per[4]);
    printf("    TTBR1 global, after TLBI ASIDE1IS                        : %+8.3f ns/page\n",
           per[6] - per[0]);
    printf("    TTBR1 global, after TLBI VMALLE1IS                       : %+8.3f ns/page\n",
           per[7] - per[0]);
    printf("\n    whole-working-set cost of one CR3 switch, kernel half global     : %8.1f us\n",
           (per[1] - per[0]) * CHASE_PAGES / 1000.0);
    printf("    whole-working-set cost of one CR3 switch, kernel half non-global : %8.1f us\n",
           (per[3] - per[2]) * CHASE_PAGES / 1000.0);

done:
    hv_vcpu_destroy(v.vcpu);
    hv_vm_unmap(RAM_IPA, RAM_SIZE);
    hv_vm_destroy();
    munmap(mem, RAM_SIZE);
    return failures ? 1 : 0;
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "both";
    int rc = 0;
    if (!strcmp(mode, "func") || !strcmp(mode, "both")) rc |= run_func();
    if (!strcmp(mode, "time") || !strcmp(mode, "both")) rc |= run_time();
    printf("\n  %s: %d failure(s)\n", mode, failures);
    return rc;
}
