// probe_mmu_tso.c — SoftMMU + TSO microbenchmarks on M2 Pro/Max
// Reproduce 01 §6.3 numbers: plain LDR vs LDAPR/STLR, softMMU 9-insn, and TSO ablation
// Compile: clang -O2 -arch arm64 -o /tmp/probe_mmu_tso /tmp/probe_mmu_tso.c
// Run: /tmp/probe_mmu_tso
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <time.h>
#include <pthread.h>
#include <mach/mach_time.h>

static uint64_t now_ns(void){
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return (uint64_t)ts.tv_sec*1000000000ULL + ts.tv_nsec;
}
// Pin to P-core best effort: set QoS
static void pin_perf(void){
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
}

// L1 buffer: 64KB, 16 lines
#define L1_BUF_SIZE (64*1024)
static uint64_t *l1_buf;
// TLB mock for softmmu
struct Entry { uint64_t cmp; int64_t addend; };
struct Desc  { uint64_t mask; struct Entry *table; };
static struct Desc desc;
static struct Entry *entries;
static uint64_t guest_base = 0x400000000ULL; // fake guest VA
static const size_t NUM_ENTRIES = 64; // enough for 64KB /4K =16 pages
static const uint64_t MY_PAGE_MASK = 0xFFF; // page offset mask alternative
// Use index mask for table: (NUM_ENTRIES-1)*16 is max offset into entries
// QEMU uses mask that already encodes shift; we use simple: idx = (addr>>12) & (NUM_ENTRIES-1)

// Helpers to build TLB entries for L1 region
static void build_tlb(void){
    entries = aligned_alloc(64, NUM_ENTRIES*sizeof(struct Entry));
    memset(entries, 0, NUM_ENTRIES*sizeof(struct Entry));
    // mask for index: low bits of page number
    // We'll usemask = (NUM_ENTRIES-1) << 4 ? but AND+ADD needs entry size scaling
    // Simplify: mask = (NUM_ENTRIES-1) * 16  ??? No need precise, just set mask to extract index*16
    // For our sequence: LDP mask,table ; AND tmp, addr, LSR trick -> we emulate via AND + ADD with shift
    // Let's set mask = (NUM_ENTRIES-1) << 12  then (addr & mask)>>? Too complex.
    // Easier: set mask = (NUM_ENTRIES-1) and do: lsr + and ? Instead our asm will use:
    // and x6, x0, x4  where x4 = 0x3F0 ? Not.
    // For benchmark we want realistic dependent chain: LDP, AND (with LSR), ADD, LDR, LDR, CMP, B.NE
    // We'll use exact QEMU shape:
    //   LDP x4,x5,[x10]         // mask, table
    //   AND x6, x4, x0, LSR #9  // PAGE_BITS-ENTRY_BITS = 12-3=9 for 512-entry table? but we use 64 entries => 6 bits => shift 6
    // So with NUM_ENTRIES=64 => ENTRY_BITS=6 => shift = 12-6=6
    // Then ADD x5,x5,x6
    // That yields byte offset into table (since entry size 16, need *16 but AND already scaled? Actually mask is index*entry_size)
    // So mask should be (NUM_ENTRIES-1)*16 shifted? Let's just set mask = (NUM_ENTRIES-1)*16 = 0x3F0
    // Then AND x6, mask, x0 LSR # (12-4) ??? Let's keep simple: mask = 0x3F0, operation = AND x6, x4, x0, LSR #8  => (addr>>8) & 0x3F0 = page_index*16
    // Because addr>>12 = page index, *16 = <<4, so (addr>>8) & 0x3F0 = ((addr>>12)&0x3F)<<4
    // That's equivalent to (page_index & 63)*16
    // So we can encode as: AND x6, x4, x0, LSR #8  with x4=0x3F0
    // Good.

    // But for simplicity we set mask = 0x3F0 and use that exact shift in asm.
    desc.mask = 0x3F0ULL; // (64-1)*16
    desc.table = entries;
    // Fill entries: for each page that covers guest_base .. guest_base+L1_BUF_SIZE
    for(size_t i=0;i<NUM_ENTRIES;i++){
        uint64_t gva_page = guest_base + i*4096;
        // Does this page overlap l1_buf region? l1_buf is 64KB =16 pages starting at guest_base
        if(i < L1_BUF_SIZE/4096){
            uint64_t host_page = (uint64_t)(uintptr_t)l1_buf + i*4096;
            int64_t addend = (int64_t)(host_page - gva_page);
            // comparator: masked addr bits for page base: use same encoding as QEMU compare
            // QEMU: AND tmp2, addr_adj, #compare_mask ; CMP tmp0, tmp2
            // comparator is the page-aligned guest addr masked? We'll store gva_page & ~PAGE_MASK & mask_transform?
            // For our simplified sequence, we just store gva_page aligned to compare directly with x6? Let's store gva_page & ~0xFFF ?
            // Actually our AND for comparator will be different; we can bypass complexity: make comparator = gva_page & ~0xFFF? But CMP will compare tmp0 (loaded cmp) with tmp2 = addr & ~0xFFF ??? Let's make compare just check page base equality via: comparator = gva_page, and our asm will do AND tmp2, addr, #~0xFFF then CMP. For benchmark we can keep comparator as page base and our asm masks addr to page base before compare.
            // Simpler: store gva_page as comparator, and in asm do: and x6_masked, x0, #~0xFFF ; cmp x6_loaded, x6_masked
            // Let's do that.
            entries[i].cmp = gva_page; // page base
            entries[i].addend = addend;
        } else {
            entries[i].cmp = 0xdeadbeefdeadbeefULL;
            entries[i].addend = 0;
        }
    }
}

// --- Benchmark primitives ---

// Plain 8x LDR throughput L1
static uint64_t bench_plain_ldr_L1(uint64_t iters){
    uint64_t *b = l1_buf;
    // 8 pointers spaced 1KB apart to stay in L1 but distinct lines
    uint64_t *p0=b+0, *p1=b+128, *p2=b+256, *p3=b+384, *p4=b+512, *p5=b+640, *p6=b+768, *p7=b+896;
    uint64_t sum=0;
    uint64_t start=now_ns();
    for(uint64_t i=0;i<iters;i++){
        uint64_t v0,v1,v2,v3,v4,v5,v6,v7;
        __asm__ volatile(
            "ldr %0, [%8]\n\t"
            "ldr %1, [%9]\n\t"
            "ldr %2, [%10]\n\t"
            "ldr %3, [%11]\n\t"
            "ldr %4, [%12]\n\t"
            "ldr %5, [%13]\n\t"
            "ldr %6, [%14]\n\t"
            "ldr %7, [%15]\n\t"
            : "=&r"(v0),"=&r"(v1),"=&r"(v2),"=&r"(v3),"=&r"(v4),"=&r"(v5),"=&r"(v6),"=&r"(v7)
            : "r"(p0),"r"(p1),"r"(p2),"r"(p3),"r"(p4),"r"(p5),"r"(p6),"r"(p7)
            : "memory"
        );
        sum+=v0+v1+v2+v3+v4+v5+v6+v7;
        // prevent dead store elimination
        __asm__ volatile(""::"r"(sum):"memory");
    }
    uint64_t end=now_ns();
    // use sum to avoid optimize away
    volatile uint64_t sink=sum; (void)sink;
    return end-start;
}
static uint64_t bench_ldapr_L1(uint64_t iters){
    uint64_t *b = l1_buf;
    uint64_t *p0=b+0, *p1=b+128, *p2=b+256, *p3=b+384, *p4=b+512, *p5=b+640, *p6=b+768, *p7=b+896;
    uint64_t sum=0;
    uint64_t start=now_ns();
    for(uint64_t i=0;i<iters;i++){
        uint64_t v0,v1,v2,v3,v4,v5,v6,v7;
        __asm__ volatile(
            "ldapr %0, [%8]\n\t"
            "ldapr %1, [%9]\n\t"
            "ldapr %2, [%10]\n\t"
            "ldapr %3, [%11]\n\t"
            "ldapr %4, [%12]\n\t"
            "ldapr %5, [%13]\n\t"
            "ldapr %6, [%14]\n\t"
            "ldapr %7, [%15]\n\t"
            : "=&r"(v0),"=&r"(v1),"=&r"(v2),"=&r"(v3),"=&r"(v4),"=&r"(v5),"=&r"(v6),"=&r"(v7)
            : "r"(p0),"r"(p1),"r"(p2),"r"(p3),"r"(p4),"r"(p5),"r"(p6),"r"(p7)
            : "memory"
        );
        sum+=v0+v1+v2+v3+v4+v5+v6+v7;
        __asm__ volatile(""::"r"(sum):"memory");
    }
    volatile uint64_t sink=sum; (void)sink;
    return now_ns()-start;
}
static uint64_t bench_ldar_L1(uint64_t iters){
    uint64_t *b = l1_buf;
    uint64_t *p0=b+0, *p1=b+128, *p2=b+256, *p3=b+384, *p4=b+512, *p5=b+640, *p6=b+768, *p7=b+896;
    uint64_t sum=0;
    uint64_t start=now_ns();
    for(uint64_t i=0;i<iters;i++){
        uint64_t v0,v1,v2,v3,v4,v5,v6,v7;
        __asm__ volatile(
            "ldar %0, [%8]\n\t"
            "ldar %1, [%9]\n\t"
            "ldar %2, [%10]\n\t"
            "ldar %3, [%11]\n\t"
            "ldar %4, [%12]\n\t"
            "ldar %5, [%13]\n\t"
            "ldar %6, [%14]\n\t"
            "ldar %7, [%15]\n\t"
            : "=&r"(v0),"=&r"(v1),"=&r"(v2),"=&r"(v3),"=&r"(v4),"=&r"(v5),"=&r"(v6),"=&r"(v7)
            : "r"(p0),"r"(p1),"r"(p2),"r"(p3),"r"(p4),"r"(p5),"r"(p6),"r"(p7)
            : "memory"
        );
        sum+=v0+v1+v2+v3+v4+v5+v6+v7;
        __asm__ volatile(""::"r"(sum):"memory");
    }
    volatile uint64_t sink=sum; (void)sink;
    return now_ns()-start;
}
static uint64_t bench_stlr_L1(uint64_t iters){
    uint64_t *b = l1_buf;
    uint64_t *p0=b+0, *p1=b+128, *p2=b+256, *p3=b+384, *p4=b+512, *p5=b+640, *p6=b+768, *p7=b+896;
    uint64_t v=0x12345678;
    uint64_t start=now_ns();
    for(uint64_t i=0;i<iters;i++){
        __asm__ volatile(
            "stlr %0, [%1]\n\t"
            "stlr %0, [%2]\n\t"
            "stlr %0, [%3]\n\t"
            "stlr %0, [%4]\n\t"
            "stlr %0, [%5]\n\t"
            "stlr %0, [%6]\n\t"
            "stlr %0, [%7]\n\t"
            "stlr %0, [%8]\n\t"
            :: "r"(v),"r"(p0),"r"(p1),"r"(p2),"r"(p3),"r"(p4),"r"(p5),"r"(p6),"r"(p7) : "memory"
        );
    }
    uint64_t end=now_ns();
    // prevent optimize
    __asm__ volatile(""::"r"(v):"memory");
    return end-start;
}
static uint64_t bench_str_L1(uint64_t iters){
    uint64_t *b = l1_buf;
    uint64_t *p0=b+0, *p1=b+128, *p2=b+256, *p3=b+384, *p4=b+512, *p5=b+640, *p6=b+768, *p7=b+896;
    uint64_t v=0x12345678;
    uint64_t start=now_ns();
    for(uint64_t i=0;i<iters;i++){
        __asm__ volatile(
            "str %0, [%1]\n\t"
            "str %0, [%2]\n\t"
            "str %0, [%3]\n\t"
            "str %0, [%4]\n\t"
            "str %0, [%5]\n\t"
            "str %0, [%6]\n\t"
            "str %0, [%7]\n\t"
            "str %0, [%8]\n\t"
            :: "r"(v),"r"(p0),"r"(p1),"r"(p2),"r"(p3),"r"(p4),"r"(p5),"r"(p6),"r"(p7) : "memory"
        );
    }
    return now_ns()-start;
}

// Emulation-shaped: 2 loads + 1 store + ALU per guest op, 4 guest ops per iter (split to avoid >30 operands)
// We run two asm blocks of 4 ops each to keep operand count <30
static uint64_t bench_emul_plain_L1(uint64_t iters){
    uint64_t *b = l1_buf;
    uint64_t *l0a=b+0, *l0b=b+1, *s0=b+64;
    uint64_t *l1a=b+128, *l1b=b+129, *s1=b+192;
    uint64_t *l2a=b+256, *l2b=b+257, *s2=b+320;
    uint64_t *l3a=b+384, *l3b=b+385, *s3=b+448;
    uint64_t start=now_ns();
    for(uint64_t i=0;i<iters;i++){
        uint64_t v0,v1,v2,v3,w0,w1,w2,w3;
        __asm__ volatile(
            "ldr %0, [%8]\n\t ldr %4, [%9]\n\t add %4, %0, %4\n\t str %4, [%10]\n\t"
            "ldr %1, [%11]\n\t ldr %5, [%12]\n\t add %5, %1, %5\n\t str %5, [%13]\n\t"
            "ldr %2, [%14]\n\t ldr %6, [%15]\n\t add %6, %2, %6\n\t str %6, [%16]\n\t"
            "ldr %3, [%17]\n\t ldr %7, [%18]\n\t add %7, %3, %7\n\t str %7, [%19]\n\t"
            : "=&r"(v0),"=&r"(v1),"=&r"(v2),"=&r"(v3),"=&r"(w0),"=&r"(w1),"=&r"(w2),"=&r"(w3)
            : "r"(l0a),"r"(l0b),"r"(s0),"r"(l1a),"r"(l1b),"r"(s1),"r"(l2a),"r"(l2b),"r"(s2),"r"(l3a),"r"(l3b),"r"(s3)
            : "memory"
        );
        __asm__ volatile(""::"r"(v0),"r"(w0):"memory");
    }
    return now_ns()-start;
}
static uint64_t bench_emul_tso_L1(uint64_t iters){
    uint64_t *b = l1_buf;
    uint64_t *l0a=b+0, *l0b=b+1, *s0=b+64;
    uint64_t *l1a=b+128, *l1b=b+129, *s1=b+192;
    uint64_t *l2a=b+256, *l2b=b+257, *s2=b+320;
    uint64_t *l3a=b+384, *l3b=b+385, *s3=b+448;
    uint64_t start=now_ns();
    for(uint64_t i=0;i<iters;i++){
        uint64_t v0,v1,v2,v3,w0,w1,w2,w3;
        __asm__ volatile(
            "ldapr %0, [%8]\n\t ldapr %4, [%9]\n\t add %4, %0, %4\n\t stlr %4, [%10]\n\t"
            "ldapr %1, [%11]\n\t ldapr %5, [%12]\n\t add %5, %1, %5\n\t stlr %5, [%13]\n\t"
            "ldapr %2, [%14]\n\t ldapr %6, [%15]\n\t add %6, %2, %6\n\t stlr %6, [%16]\n\t"
            "ldapr %3, [%17]\n\t ldapr %7, [%18]\n\t add %7, %3, %7\n\t stlr %7, [%19]\n\t"
            : "=&r"(v0),"=&r"(v1),"=&r"(v2),"=&r"(v3),"=&r"(w0),"=&r"(w1),"=&r"(w2),"=&r"(w3)
            : "r"(l0a),"r"(l0b),"r"(s0),"r"(l1a),"r"(l1b),"r"(s1),"r"(l2a),"r"(l2b),"r"(s2),"r"(l3a),"r"(l3b),"r"(s3)
            : "memory"
        );
        __asm__ volatile(""::"r"(v0),"r"(w0):"memory");
    }
    return now_ns()-start;
}

// SoftMMU fast path: 9-insn + final ldr, 1 op per sequence, 8 ops per iter independent
// We encode exactly QEMU's 9-insn shape with a TLB lookup before each guest load
// Sequence per load (guest addr in x0, desc in x10, result in x1):
//   ldp x4, x5, [x10]               // mask, table
//   and x6, x4, x0, lsr #8           // idx*16  (mask=0x3F0)
//   add x5, x5, x6
//   ldr x6, [x5]                     // comparator
//   ldr x7, [x5, #8]                 // addend
//   and x8, x0, #0xFFFFFFFFFFFFF000 // page base (bic)
//   cmp x6, x8
//   b.ne 1f  (but we ensure never taken, so no miss)
//   ldr x1, [x0, x7]                 // final: host addr = guest + addend
// For benchmark we do 8 independent such sequences
static uint64_t bench_softmmu_ldr_L1(uint64_t iters){
    // guest VAs for 8 independent lines, each on distinct page to get distinct TLB entry
    uint64_t g0 = guest_base + 0*4096;
    uint64_t g1 = guest_base + 1*4096;
    uint64_t g2 = guest_base + 2*4096;
    uint64_t g3 = guest_base + 3*4096;
    uint64_t g4 = guest_base + 4*4096;
    uint64_t g5 = guest_base + 5*4096;
    uint64_t g6 = guest_base + 6*4096;
    uint64_t g7 = guest_base + 7*4096;
    struct Desc *d = &desc;
    uint64_t sum=0;
    uint64_t start=now_ns();
    for(uint64_t i=0;i<iters;i++){
        uint64_t v0,v1,v2,v3,v4,v5,v6,v7;
        __asm__ volatile(
            // op0: guest g0
            "ldp x4, x5, [%8]\n\t"
            "and x6, x4, %9, lsr #8\n\t"
            "add x5, x5, x6\n\t"
            "ldr x6, [x5]\n\t"
            "ldr x7, [x5, #8]\n\t"
            "bic x9, %9, #0xfff\n\t"
            "cmp x6, x9\n\t"
            "b.ne 0f\n\t"
            "ldr %0, [%9, x7]\n\t"
            "0:\n\t"
            // op1
            "ldp x4, x5, [%8]\n\t"
            "and x6, x4, %10, lsr #8\n\t"
            "add x5, x5, x6\n\t"
            "ldr x6, [x5]\n\t"
            "ldr x7, [x5, #8]\n\t"
            "bic x9, %10, #0xfff\n\t"
            "cmp x6, x9\n\t"
            "b.ne 1f\n\t"
            "ldr %1, [%10, x7]\n\t"
            "1:\n\t"
            // op2
            "ldp x4, x5, [%8]\n\t"
            "and x6, x4, %11, lsr #8\n\t"
            "add x5, x5, x6\n\t"
            "ldr x6, [x5]\n\t"
            "ldr x7, [x5, #8]\n\t"
            "bic x9, %11, #0xfff\n\t"
            "cmp x6, x9\n\t"
            "b.ne 2f\n\t"
            "ldr %2, [%11, x7]\n\t"
            "2:\n\t"
            // op3
            "ldp x4, x5, [%8]\n\t"
            "and x6, x4, %12, lsr #8\n\t"
            "add x5, x5, x6\n\t"
            "ldr x6, [x5]\n\t"
            "ldr x7, [x5, #8]\n\t"
            "bic x9, %12, #0xfff\n\t"
            "cmp x6, x9\n\t"
            "b.ne 3f\n\t"
            "ldr %3, [%12, x7]\n\t"
            "3:\n\t"
            // op4
            "ldp x4, x5, [%8]\n\t"
            "and x6, x4, %13, lsr #8\n\t"
            "add x5, x5, x6\n\t"
            "ldr x6, [x5]\n\t"
            "ldr x7, [x5, #8]\n\t"
            "bic x9, %13, #0xfff\n\t"
            "cmp x6, x9\n\t"
            "b.ne 4f\n\t"
            "ldr %4, [%13, x7]\n\t"
            "4:\n\t"
            // op5
            "ldp x4, x5, [%8]\n\t"
            "and x6, x4, %14, lsr #8\n\t"
            "add x5, x5, x6\n\t"
            "ldr x6, [x5]\n\t"
            "ldr x7, [x5, #8]\n\t"
            "bic x9, %14, #0xfff\n\t"
            "cmp x6, x9\n\t"
            "b.ne 5f\n\t"
            "ldr %5, [%14, x7]\n\t"
            "5:\n\t"
            // op6
            "ldp x4, x5, [%8]\n\t"
            "and x6, x4, %15, lsr #8\n\t"
            "add x5, x5, x6\n\t"
            "ldr x6, [x5]\n\t"
            "ldr x7, [x5, #8]\n\t"
            "bic x9, %15, #0xfff\n\t"
            "cmp x6, x9\n\t"
            "b.ne 6f\n\t"
            "ldr %6, [%15, x7]\n\t"
            "6:\n\t"
            // op7
            "ldp x4, x5, [%8]\n\t"
            "and x6, x4, %16, lsr #8\n\t"
            "add x5, x5, x6\n\t"
            "ldr x6, [x5]\n\t"
            "ldr x7, [x5, #8]\n\t"
            "bic x9, %16, #0xfff\n\t"
            "cmp x6, x9\n\t"
            "b.ne 7f\n\t"
            "ldr %7, [%16, x7]\n\t"
            "7:\n\t"
            : "=&r"(v0),"=&r"(v1),"=&r"(v2),"=&r"(v3),"=&r"(v4),"=&r"(v5),"=&r"(v6),"=&r"(v7)
            : "r"(d), "r"(g0),"r"(g1),"r"(g2),"r"(g3),"r"(g4),"r"(g5),"r"(g6),"r"(g7)
            : "x4","x5","x6","x7","x9","memory"
        );
        sum+=v0+v1+v2+v3+v4+v5+v6+v7;
        __asm__ volatile(""::"r"(sum):"memory");
        // keep guest addrs constant to stay L1 resident; TLB entries already warm
        (void)sum;
    }
    volatile uint64_t sink=sum; (void)sink;
    return now_ns()-start;
}

// DRAM MLP: 512MB array, 8 independent loads, stride to defeat prefetch
static uint64_t bench_dram_plain(uint64_t iters, void *big, size_t bigsz){
    // 8 pointers each 64MB apart
    char *base = (char*)big;
    size_t stride = 4160; // prime-ish
    char *p0=base+0, *p1=base+64*1024*1024, *p2=base+128*1024*1024, *p3=base+192*1024*1024;
    char *p4=base+256*1024*1024, *p5=base+320*1024*1024, *p6=base+384*1024*1024, *p7=base+448*1024*1024;
    uint64_t sum=0;
    uint64_t start=now_ns();
    for(uint64_t i=0;i<iters;i++){
        uint64_t v0,v1,v2,v3,v4,v5,v6,v7;
        __asm__ volatile(
            "ldr %0, [%8]\n\t"
            "ldr %1, [%9]\n\t"
            "ldr %2, [%10]\n\t"
            "ldr %3, [%11]\n\t"
            "ldr %4, [%12]\n\t"
            "ldr %5, [%13]\n\t"
            "ldr %6, [%14]\n\t"
            "ldr %7, [%15]\n\t"
            : "=&r"(v0),"=&r"(v1),"=&r"(v2),"=&r"(v3),"=&r"(v4),"=&r"(v5),"=&r"(v6),"=&r"(v7)
            : "r"(p0),"r"(p1),"r"(p2),"r"(p3),"r"(p4),"r"(p5),"r"(p6),"r"(p7)
            : "memory"
        );
        sum+=v0+v1+v2+v3+v4+v5+v6+v7;
        // advance pointers by stride, wrap
        p0+=stride; if(p0>=base+64*1024*1024) p0=base+0;
        p1+=stride; if(p1>=base+128*1024*1024) p1=base+64*1024*1024;
        p2+=stride; if(p2>=base+192*1024*1024) p2=base+128*1024*1024;
        p3+=stride; if(p3>=base+256*1024*1024) p3=base+192*1024*1024;
        p4+=stride; if(p4>=base+320*1024*1024) p4=base+256*1024*1024;
        p5+=stride; if(p5>=base+384*1024*1024) p5=base+320*1024*1024;
        p6+=stride; if(p6>=base+448*1024*1024) p6=base+384*1024*1024;
        p7+=stride; if(p7>=base+512*1024*1024) p7=base+448*1024*1024;
        __asm__ volatile(""::"r"(sum):"memory");
    }
    volatile uint64_t sink=sum; (void)sink;
    return now_ns()-start;
}
static uint64_t bench_dram_ldapr(uint64_t iters, void *big, size_t bigsz){
    char *base = (char*)big;
    size_t stride = 4160;
    char *p0=base+0, *p1=base+64*1024*1024, *p2=base+128*1024*1024, *p3=base+192*1024*1024;
    char *p4=base+256*1024*1024, *p5=base+320*1024*1024, *p6=base+384*1024*1024, *p7=base+448*1024*1024;
    uint64_t sum=0;
    uint64_t start=now_ns();
    for(uint64_t i=0;i<iters;i++){
        uint64_t v0,v1,v2,v3,v4,v5,v6,v7;
        __asm__ volatile(
            "ldapr %0, [%8]\n\t"
            "ldapr %1, [%9]\n\t"
            "ldapr %2, [%10]\n\t"
            "ldapr %3, [%11]\n\t"
            "ldapr %4, [%12]\n\t"
            "ldapr %5, [%13]\n\t"
            "ldapr %6, [%14]\n\t"
            "ldapr %7, [%15]\n\t"
            : "=&r"(v0),"=&r"(v1),"=&r"(v2),"=&r"(v3),"=&r"(v4),"=&r"(v5),"=&r"(v6),"=&r"(v7)
            : "r"(p0),"r"(p1),"r"(p2),"r"(p3),"r"(p4),"r"(p5),"r"(p6),"r"(p7)
            : "memory"
        );
        sum+=v0+v1+v2+v3+v4+v5+v6+v7;
        p0+=stride; if(p0>=base+64*1024*1024) p0=base+0;
        p1+=stride; if(p1>=base+128*1024*1024) p1=base+64*1024*1024;
        p2+=stride; if(p2>=base+192*1024*1024) p2=base+128*1024*1024;
        p3+=stride; if(p3>=base+256*1024*1024) p3=base+192*1024*1024;
        p4+=stride; if(p4>=base+320*1024*1024) p4=base+256*1024*1024;
        p5+=stride; if(p5>=base+384*1024*1024) p5=base+320*1024*1024;
        p6+=stride; if(p6>=base+448*1024*1024) p6=base+384*1024*1024;
        p7+=stride; if(p7>=base+512*1024*1024) p7=base+448*1024*1024;
        __asm__ volatile(""::"r"(sum):"memory");
    }
    volatile uint64_t sink=sum; (void)sink;
    return now_ns()-start;
}

// dmb ish barrier cost (single per iteration for reference)
static uint64_t bench_dmb_ish_L1(uint64_t iters){
    uint64_t *b=l1_buf;
    uint64_t *p=b;
    uint64_t sum=0;
    uint64_t start=now_ns();
    for(uint64_t i=0;i<iters;i++){
        uint64_t v;
        __asm__ volatile("ldr %0, [%1]\n\t dmb ish\n\t" : "=&r"(v): "r"(p): "memory");
        sum+=v;
    }
    volatile uint64_t sink=sum; (void)sink;
    return now_ns()-start;
}

static double best_of_3(uint64_t (*fn)(uint64_t), uint64_t iters){
    uint64_t a=fn(iters), b=fn(iters), c=fn(iters);
    uint64_t m=a; if(b<m) m=b; if(c<m) m=c;
    return (double)m;
}
static double best_of_3_dram(uint64_t (*fn)(uint64_t, void*, size_t), uint64_t iters, void*big, size_t sz){
    uint64_t a=fn(iters,big,sz), b=fn(iters,big,sz), c=fn(iters,big,sz);
    uint64_t m=a; if(b<m) m=b; if(c<m) m=c;
    return (double)m;
}

int main(){
    pin_perf();
    printf("=== SoftMMU + TSO microbenchmarks — M2 Max, macOS 26.5.2 ===\n");
    printf("hw.pagesize=%d hw.ncpu=%d\n", getpagesize(), (int)sysconf(_SC_NPROCESSORS_ONLN));
    // alloc L1 buf
    if(posix_memalign((void**)&l1_buf, 16384, L1_BUF_SIZE)!=0){ perror("align"); return 1; }
    memset(l1_buf, 0xAB, L1_BUF_SIZE);
    for(size_t i=0;i<L1_BUF_SIZE/8;i++) l1_buf[i]=(uint64_t)i*0x9e3779b97f4a7c15ULL;
    build_tlb();
    // warm
    for(int i=0;i<100000;i++) __asm__ volatile("ldr x0, [%0]" :: "r"(l1_buf) : "x0","memory");

    const uint64_t ITERS_L1 = 2000000; // 2M *8 =16M ops (4 ops for emul)
    const uint64_t OPS_PER_ITER = 8;
    const uint64_t OPS_EMUL_PER_ITER = 4;

    printf("\n--- L1 throughput: 8 independent ops per iteration (emul 4 ops), best-of-3 ---\n");
    fflush(stdout);
    double t_ldr = best_of_3(bench_plain_ldr_L1, ITERS_L1);
    printf("t_ldr done\n"); fflush(stdout);
    double t_ldapr = best_of_3(bench_ldapr_L1, ITERS_L1);
    printf("t_ldapr done\n"); fflush(stdout);
    double t_ldar = best_of_3(bench_ldar_L1, ITERS_L1);
    printf("t_ldar done\n"); fflush(stdout);
    double t_str = best_of_3(bench_str_L1, ITERS_L1);
    printf("t_str done\n"); fflush(stdout);
    double t_stlr = best_of_3(bench_stlr_L1, ITERS_L1);
    printf("t_stlr done\n"); fflush(stdout);
    double t_soft = best_of_3(bench_softmmu_ldr_L1, ITERS_L1);
    printf("t_soft done\n"); fflush(stdout);
    double t_emul_plain = best_of_3(bench_emul_plain_L1, ITERS_L1);
    printf("t_emul_plain done\n"); fflush(stdout);
    double t_emul_tso = best_of_3(bench_emul_tso_L1, ITERS_L1);
    printf("t_emul_tso done\n"); fflush(stdout);

    uint64_t total_ops_ld = ITERS_L1*OPS_PER_ITER;
    double ns_ldr = t_ldr / total_ops_ld;
    double ns_ldapr = t_ldapr / total_ops_ld;
    double ns_ldar = t_ldar / total_ops_ld;
    double ns_str = t_str / total_ops_ld;
    double ns_stlr = t_stlr / total_ops_ld;
    double ns_soft = t_soft / total_ops_ld;
    // emul now 4 ops per iter
    double ratio_ldapr = t_ldapr / t_ldr;
    double ratio_ldar = t_ldar / t_ldr;
    double ratio_stlr = t_stlr / t_str;
    double ratio_soft_vs_ldr = ns_soft / ns_ldr;
    double ratio_soft_vs_ldapr = ns_soft / ns_ldapr;
    double ratio_emul_tso = t_emul_tso / t_emul_plain;

    printf("plain LDR      : %.1f ms total, %.4f ns/op, %.2f ops/ns  (1.00x)\n", t_ldr/1e6, ns_ldr, 1/ns_ldr);
    printf("LDAPR (RCpc)   : %.1f ms total, %.4f ns/op, ratio vs LDR %.2fx\n", t_ldapr/1e6, ns_ldapr, ratio_ldapr);
    printf("LDAR  (RCsc)   : %.1f ms total, %.4f ns/op, ratio vs LDR %.2fx  (LDAPR==LDAR? %s, diff %.1f%%)\n", t_ldar/1e6, ns_ldar, ratio_ldar, (ns_ldapr/ns_ldar>0.95 && ns_ldapr/ns_ldar<1.05)?"YES":"NO", (ns_ldapr/ns_ldar-1)*100);
    printf("plain STR      : %.1f ms total, %.4f ns/op\n", t_str/1e6, ns_str);
    printf("STLR           : %.1f ms total, %.4f ns/op, ratio vs STR %.2fx\n", t_stlr/1e6, ns_stlr, ratio_stlr);
    printf("SoftMMU LDR (9-insn QEMU shape + final LDR): %.1f ms, %.4f ns/op, ratio vs LDR %.2fx, vs LDAPR %.2fx\n", t_soft/1e6, ns_soft, ratio_soft_vs_ldr, ratio_soft_vs_ldapr);
    printf("Emul plain (2L+1S+ALU x4) : %.1f ms total, per-iter %.2f ns (%.2f ns/guest-op)\n", t_emul_plain/1e6, t_emul_plain/ITERS_L1, t_emul_plain/ITERS_L1/4);
    printf("Emul TSO   (LDAPR/STLR x4): %.1f ms total, per-iter %.2f ns, ratio TSO/plain %.2fx\n", t_emul_tso/1e6, t_emul_tso/ITERS_L1, ratio_emul_tso);
    printf("Expected 01 §6.3: LDR 0.1135 ns, LDAPR 0.2532 ns (2.23x), STLR 2.04x vs STR, emul TSO 3.43x (8 ops) -> ~similar for 4 ops\n");

    // DMB
    double t_dmb = best_of_3(bench_dmb_ish_L1, ITERS_L1*4); // scale to 4 to match emul iters
    printf("DMB ISH single load barrier: %.1f ms for %llu iters, %.3f ns/op\n", t_dmb/1e6, (unsigned long long)(ITERS_L1*4), t_dmb/(ITERS_L1*4));

    // DRAM
#if 0
    printf("\n--- DRAM MLP: 512MB, 8 independent loads, stride 4160, best-of-3 ---\n");
    fflush(stdout);
    size_t bigsz = 512*1024*1024;
    void *big = mmap(NULL, bigsz, PROT_READ|PROT_WRITE, MAP_ANON|MAP_PRIVATE, -1, 0);
    if(big==MAP_FAILED){ perror("mmap big"); return 1; }
    printf("touch 512MB...\n"); fflush(stdout);
    // touch to fault - madvise to avoid zeroing cost? use memset
    for(size_t off=0; off<bigsz; off+=16384) ((volatile char*)big)[off]=0x5A;
    printf("touched\n"); fflush(stdout);
    const uint64_t ITERS_DRAM = 200000; // 200k *8 =1.6M loads (lighter)
    double td_plain = best_of_3_dram(bench_dram_plain, ITERS_DRAM, big, bigsz);
    printf("dram plain done %.1f ms\n", td_plain/1e6); fflush(stdout);
    double td_ldapr = best_of_3_dram(bench_dram_ldapr, ITERS_DRAM, big, bigsz);
    printf("dram ldapr done %.1f ms\n", td_ldapr/1e6); fflush(stdout);
    double ns_d_plain = td_plain / (ITERS_DRAM*8);
    double ns_d_ldapr = td_ldapr / (ITERS_DRAM*8);
    printf("DRAM plain LDR : %.1f ms, %.3f ns/load, %.2f GB/s (8B per load)\n", td_plain/1e6, ns_d_plain, 8.0/ns_d_plain);
    printf("DRAM LDAPR     : %.1f ms, %.3f ns/load, ratio %.2fx (01 §6.3 expected 1.14x)\n", td_ldapr/1e6, ns_d_ldapr, td_ldapr/td_plain);
    munmap(big, bigsz);
#endif
    double td_plain=0, td_ldapr=0;

    printf("\n--- Interpretation ---\n");
    printf("SoftMMU 9-insn fast path ratio: %.2fx vs plain LDR on L1. Whole-program tax depends on memop density (35-45%% insns are mem).\n", ratio_soft_vs_ldr);
    printf("TSO tax L1 emul: %.2fx (plain vs LDAPR/STLR). HW TSO (EnTSO) would save this.\n", ratio_emul_tso);
    printf("DRAM MLP penalty LDAPR: %.2fx.\n", td_ldapr/td_plain);
    printf("LDAPR==LDAR on this chip? %s (%.3f vs %.3f ns)\n", (ns_ldapr/ns_ldar>0.95 && ns_ldapr/ns_ldar<1.05)?"YES - no RCpc benefit":"NO", ns_ldapr, ns_ldar);
    // TLB reach note
    printf("TLB reach @4K vs 16K: Firestorm L2 3072 entries => 12 MiB @4K vs 48 MiB @16K (06 §2.7)\n");
    return 0;
}
