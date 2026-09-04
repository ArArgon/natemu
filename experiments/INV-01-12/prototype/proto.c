/*
 * NatEmu research prototype — INV-01-12 (question Q-01-12, kill assumption KA-11).
 *
 * Integrated mechanism prototype: dynamic binary translation of a documented
 * x86-64 subset to arm64 in a MAP_JIT region, a software MMU walking guest
 * 4-level page tables, precise and restartable exceptions through a guest IDT,
 * timer and IPI interrupt delivery, and two vCPUs executing concurrently over
 * one shared guest-physical memory.
 *
 * This is prototype code. It tests a mechanism and is discarded after Track 01.
 * It is not an emulator, not a design, and produces no performance result.
 * Deliberate simplifications are listed in ../README.md.
 *
 * Build: ./build.sh    Run: ./run.sh    Checker: src/check.py
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <stdint.h>
#include <stddef.h>
#include <setjmp.h>
#include <pthread.h>
#include <unistd.h>
#include <time.h>
#include <sys/mman.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <libkern/OSCacheControl.h>
#include <stdatomic.h>

#include "guest_blob.h"

/* ------------------------------------------------------------------ config */

#define GUEST_RAM_SIZE   (16u << 20)
#define GUEST_LOAD_GPA   0x00100000ull
#define STACK0_TOP       0x00080000ull
#define STACK1_TOP       0x00090000ull
#define LAPIC_BASE       0xFEE00000ull
#define LAPIC_SIZE       0x1000ull
#define NVCPU            2
#define JIT_BYTES        (8u << 20)
#define TB_SLOTS         16384
#define TLB_SLOTS        512
#define MAX_BLOCK_INSNS  64
#define VEC_TIMER        0x20
#define VEC_IPI          0x21
#define TIMER_PERIOD_NS  1000000ull          /* 1 kHz */
/* Per-vCPU interrupt-stack-table entry 1. Real x86 reads IST pointers from
 * the TSS; this prototype fixes them per vCPU (declared simplification), so a
 * fault on the guest stack can be delivered without re-faulting on the frame. */
#define IST1_TOP(id)     (0x00070000ull + 0x1000ull * (uint64_t)(id) + 0x1000ull)
#define DEFAULT_TIMEOUT  180                 /* seconds; matches the frozen method value */

static int inj_skip_restart, inj_no_barrier, inj_nonatomic, inj_drop_ipi, inj_stale_cache;
static int inj_push_order;
static int opt_timeout = DEFAULT_TIMEOUT;

/* ------------------------------------------------------------- guest state */

enum { CC_NONE = 0, CC_ADD, CC_SUB, CC_LOGIC, CC_INC, CC_DEC, CC_FLAGS };

struct tlbe { uint64_t tag; uint8_t *host; uint64_t prot; }; /* prot: 1 read, 3 read+write */
struct tbe { uint64_t gpa; void *code; };

typedef struct cpu {
    /* --- fields addressed by generated code (offsets must stay under 32760) */
    uint64_t regs[16];       /* RAX RCX RDX RBX RSP RBP RSI RDI R8..R15 */
    uint64_t rip;
    uint64_t cur_rip;        /* first byte of the instruction being executed */
    uint64_t cur_next;       /* first byte after it */
    uint64_t cc_op, cc_a, cc_b, cc_res, cc_cf;
    uint64_t iflag;
    uint64_t tmp, tmp2;      /* value stashes across a helper call */
    uint64_t mmio_scratch;
    uint64_t insns, blocks, translations, tlb_fills, smc_flushes, faults_raised;
    uint64_t halted;
    uint64_t cr0, cr2, cr3;
    uint64_t idt_base, idt_limit;

    /* --- host-only */
    int id;
    uint8_t *jit_base, *jit_ptr, *jit_end;
    struct tbe *tb;
    struct tlbe *tlb;
    uint8_t *code_page;
    jmp_buf fault_jmp;
    int fault_vec, has_err;
    uint64_t fault_err;
    uint64_t mark_begin, mark_end;     /* host clock: overlap evidence */
    uint64_t cpu_begin_ns, cpu_end_ns;  /* per-thread CPU time across the phase */
    uint64_t hlt_wakes, irq_delivered;
    int in_delivery, nested_fault;
    _Atomic int halted_clean;
    int finished;
    _Atomic int has_abort;
    char abort_buf[192];
    _Atomic uint32_t ipi_vector;
    _Atomic uint32_t irr;              /* bit0 timer, bit1 IPI */
    _Atomic uint32_t isr;              /* vector in service, 0 = none */
} cpu_t;

static uint8_t *ram;
static cpu_t cpus[NVCPU];
static _Atomic int g_stop;
static _Atomic uint64_t dev_timer_posted, dev_ipi_sent, dev_ipi_dropped;
static _Atomic uint32_t dev_timer_enabled;
static _Atomic int dev_timer_stop;
static _Atomic uint32_t dev_timer_vector = VEC_TIMER;
static mach_timebase_info_data_t tb_info;

static uint64_t now_ns(void)
{
    return mach_absolute_time() * tb_info.numer / tb_info.denom;
}

/* CPU time consumed by the calling thread, used to distinguish two threads
 * running in parallel from one core time-slicing between them. */
static uint64_t thread_cpu_ns(void)
{
    mach_port_t self = mach_thread_self();
    thread_basic_info_data_t info;
    mach_msg_type_number_t count = THREAD_BASIC_INFO_COUNT;
    uint64_t ns = 0;
    if (thread_info(self, THREAD_BASIC_INFO, (thread_info_t)&info, &count) == KERN_SUCCESS)
        ns = ((uint64_t)info.user_time.seconds + (uint64_t)info.system_time.seconds) * 1000000000ull
           + ((uint64_t)info.user_time.microseconds + (uint64_t)info.system_time.microseconds) * 1000ull;
    mach_port_deallocate(mach_task_self(), self);
    return ns;
}

static void vabort(cpu_t *c, const char *fmt, ...)
{
    if (!atomic_load(&c->has_abort)) {
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(c->abort_buf, sizeof c->abort_buf, fmt, ap);
        va_end(ap);
        atomic_store(&c->has_abort, 1);
    }
    atomic_store(&g_stop, 1);
}

/* --------------------------------------------------------------- arm64 emit */

enum { X0 = 0, X1, X2, X3, X4, X5, X6, X16 = 16, X19 = 19, X30 = 30, SP_R = 31, XZR = 31 };

typedef struct { cpu_t *c; uint32_t *p, *end; } emit_t;

static void e(emit_t *m, uint32_t insn)
{
    if (m->p >= m->end) { vabort(m->c, "JIT region exhausted"); return; }
    *m->p++ = insn;
}

#define A_MOVZ(d, imm, hw) (0xD2800000u | ((uint32_t)(hw) << 21) | ((uint32_t)(imm) << 5) | (uint32_t)(d))
#define A_MOVK(d, imm, hw) (0xF2800000u | ((uint32_t)(hw) << 21) | ((uint32_t)(imm) << 5) | (uint32_t)(d))
#define A_ADDr(d, n, m_)  (0x8B000000u | ((uint32_t)(m_) << 16) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_ADDrs(d, n, m_, s) (0x8B000000u | ((uint32_t)(m_) << 16) | ((uint32_t)(s) << 10) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_SUBr(d, n, m_)  (0xCB000000u | ((uint32_t)(m_) << 16) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_ANDr(d, n, m_)  (0x8A000000u | ((uint32_t)(m_) << 16) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_ORRr(d, n, m_)  (0xAA000000u | ((uint32_t)(m_) << 16) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_EORr(d, n, m_)  (0xCA000000u | ((uint32_t)(m_) << 16) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_ORRw(d, n, m_)  (0x2A000000u | ((uint32_t)(m_) << 16) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_ADDi(d, n, i)   (0x91000000u | ((uint32_t)(i) << 10) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_SUBi(d, n, i)   (0xD1000000u | ((uint32_t)(i) << 10) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_MOVr(d, m_)     A_ORRr(d, XZR, m_)
#define A_LSLV(d, n, m_)  (0x9AC02000u | ((uint32_t)(m_) << 16) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_LSRV(d, n, m_)  (0x9AC02400u | ((uint32_t)(m_) << 16) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_ASRV(d, n, m_)  (0x9AC02800u | ((uint32_t)(m_) << 16) | ((uint32_t)(n) << 5) | (uint32_t)(d))
#define A_BLR(n)          (0xD63F0000u | ((uint32_t)(n) << 5))
#define A_RET             (0xD65F03C0u)
#define A_CBZ(t, off)     (0xB4000000u | (((uint32_t)(off) & 0x7FFFFu) << 5) | (uint32_t)(t))
#define A_B(off)          (0x14000000u | ((uint32_t)(off) & 0x3FFFFFFu))
#define A_DMBISH          (0xD5033BBFu)
#define A_YIELD           (0xD503203Fu)

static uint32_t a_ldr(int t, int n, unsigned off, int size)
{
    static const uint32_t b[9] = { 0, 0x39400000u, 0x79400000u, 0, 0xB9400000u, 0, 0, 0, 0xF9400000u };
    return b[size] | ((off / (unsigned)size) << 10) | ((unsigned)n << 5) | (unsigned)t;
}
static uint32_t a_str(int t, int n, unsigned off, int size)
{
    static const uint32_t b[9] = { 0, 0x39000000u, 0x79000000u, 0, 0xB9000000u, 0, 0, 0, 0xF9000000u };
    return b[size] | ((off / (unsigned)size) << 10) | ((unsigned)n << 5) | (unsigned)t;
}
static uint32_t a_ldar(int t, int n, int size)
{
    static const uint32_t b[9] = { 0, 0x08DFFC00u, 0x48DFFC00u, 0, 0x88DFFC00u, 0, 0, 0, 0xC8DFFC00u };
    return b[size] | ((unsigned)n << 5) | (unsigned)t;
}
static uint32_t a_stlr(int t, int n, int size)
{
    static const uint32_t b[9] = { 0, 0x089FFC00u, 0x489FFC00u, 0, 0x889FFC00u, 0, 0, 0, 0xC89FFC00u };
    return b[size] | ((unsigned)n << 5) | (unsigned)t;
}
static uint32_t a_ldaddal(int s, int t, int n, int size)
{ return (size == 8 ? 0xF8E00000u : 0xB8E00000u) | ((unsigned)s << 16) | ((unsigned)n << 5) | (unsigned)t; }
static uint32_t a_swpal(int s, int t, int n, int size)
{ return (size == 8 ? 0xF8E08000u : 0xB8E08000u) | ((unsigned)s << 16) | ((unsigned)n << 5) | (unsigned)t; }
static uint32_t a_casal(int s, int t, int n, int size)
{ return (size == 8 ? 0xC8E0FC00u : 0x88E0FC00u) | ((unsigned)s << 16) | ((unsigned)n << 5) | (unsigned)t; }

static void emit_imm(emit_t *m, int rd, uint64_t v)
{
    int first = 1;
    for (int hw = 0; hw < 4; hw++) {
        uint32_t h = (uint32_t)((v >> (16 * hw)) & 0xFFFF);
        if (h == 0) continue;
        e(m, first ? A_MOVZ(rd, h, hw) : A_MOVK(rd, h, hw));
        first = 0;
    }
    if (first) e(m, A_MOVZ(rd, 0, 0));
}

#define CPUOFF(f) ((unsigned)offsetof(cpu_t, f))
#define REGOFF(r) ((unsigned)(offsetof(cpu_t, regs) + 8u * (unsigned)(r)))

static void emit_ld_cpu(emit_t *m, int rd, unsigned off) { e(m, a_ldr(rd, X19, off, 8)); }
static void emit_st_cpu(emit_t *m, int rs, unsigned off) { e(m, a_str(rs, X19, off, 8)); }
static void emit_call(emit_t *m, void *fn) { emit_imm(m, X16, (uint64_t)(uintptr_t)fn); e(m, A_BLR(X16)); }

/* ------------------------------------------------------------ software MMU */

#define PF_P 1u
#define PF_W 2u
#define PF_I 16u

static void raise_exc(cpu_t *c, int vec, int has_err, uint64_t err)
{
    if (c->in_delivery) {
        /* A fault while delivering a fault. Declared stop: never a livelock. */
        c->nested_fault = 1;
        vabort(c, "nested fault: vector %d raised while delivering vector %d", vec, c->fault_vec);
        longjmp(c->fault_jmp, 2);
    }
    c->fault_vec = vec;
    c->has_err = has_err;
    c->fault_err = err;
    c->faults_raised++;
    longjmp(c->fault_jmp, 1);
}

static inline int gpa_is_ram(uint64_t gpa) { return gpa < GUEST_RAM_SIZE; }
static inline int gpa_is_lapic(uint64_t gpa) { return gpa >= LAPIC_BASE && gpa < LAPIC_BASE + LAPIC_SIZE; }

/* Walk the guest page tables. Raises #PF (vector 14) with a Linux-compatible
 * error code and CR2 when the translation fails. Sets A on every level and D
 * on the leaf for a write. */
static uint64_t walk(cpu_t *c, uint64_t va, int write, int fetch)
{
    if (!(c->cr0 & (1ull << 31))) return va;              /* paging disabled */
    uint64_t tbl = c->cr3 & ~0xFFFull;
    for (int level = 3; level >= 0; level--) {
        if (!gpa_is_ram(tbl)) { vabort(c, "page table outside RAM: 0x%llx", tbl); longjmp(c->fault_jmp, 2); }
        uint64_t idx = (va >> (12 + 9 * level)) & 511;
        _Atomic uint64_t *slot = (_Atomic uint64_t *)(void *)(ram + tbl + idx * 8);
        uint64_t ent = atomic_load_explicit(slot, memory_order_acquire);
        if (!(ent & 1)) {
            c->cr2 = va;
            raise_exc(c, 14, 1, (write ? PF_W : 0u) | (fetch ? PF_I : 0u));
        }
        if (write && !(ent & 2)) {
            c->cr2 = va;
            raise_exc(c, 14, 1, PF_P | PF_W);
        }
        /* x86 performs accessed/dirty updates as locked read-modify-writes; the
         * two vCPUs share one page-table tree, so a plain store would lose a
         * concurrent PTE repair from the peer's #PF handler. */
        uint64_t setbits = (1ull << 5) | ((level == 0 && write) ? (1ull << 6) : 0ull);
        if ((ent & setbits) != setbits)
            atomic_fetch_or_explicit(slot, setbits, memory_order_relaxed);
        uint64_t next = ent & 0x000FFFFFFFFFF000ull;
        if (level == 0) return next | (va & 0xFFFull);
        tbl = next;
    }
    return 0;
}

static uint8_t *tlb_lookup_ex(cpu_t *c, uint64_t va, int write, int fetch, int *is_ram)
{
    struct tlbe *t = &c->tlb[(va >> 12) & (TLB_SLOTS - 1)];
    uint64_t tag = (va & ~0xFFFull) | 1;
    if (t->tag == tag && (!write || t->prot == 3)) { *is_ram = 1; return t->host + (va & 0xFFF); }
    uint64_t gpa = walk(c, va, write, fetch);
    if (gpa_is_ram(gpa)) {
        c->tlb_fills++;
        t->tag = tag;
        t->host = ram + (gpa & ~0xFFFull);
        t->prot = write ? 3 : 1;
        *is_ram = 1;
        return ram + gpa;
    }
    *is_ram = 0;
    return (uint8_t *)(uintptr_t)gpa;
}

static uint8_t *tlb_lookup(cpu_t *c, uint64_t va, int write, int *is_ram)
{
    return tlb_lookup_ex(c, va, write, 0, is_ram);
}

static void tlb_flush_page(cpu_t *c, uint64_t va) { c->tlb[(va >> 12) & (TLB_SLOTS - 1)].tag = 0; }
static void tlb_flush_all(cpu_t *c) { for (int i = 0; i < TLB_SLOTS; i++) c->tlb[i].tag = 0; }

/* ------------------------------------------------------------- LAPIC model */

static void lapic_write(cpu_t *c, uint64_t off, uint32_t val)
{
    switch (off) {
    case 0x0B0:
        atomic_store(&c->isr, 0);
        break;
    case 0x300: {                                   /* ICR: (target << 24) | vector */
        int target = (int)((val >> 24) & 0xFF);
        uint32_t vec = val & 0xFF;
        if (target < 0 || target >= NVCPU) { vabort(c, "IPI to unknown vCPU %d", target); return; }
        uint64_t n = atomic_fetch_add(&dev_ipi_sent, 1) + 1;
        if (inj_drop_ipi && (n % 3) == 0) { atomic_fetch_add(&dev_ipi_dropped, 1); break; }
        atomic_store(&cpus[target].ipi_vector, vec);
        atomic_fetch_or(&cpus[target].irr, 2u);
        break;
    }
    case 0x320: atomic_store(&dev_timer_vector, val & 0xFF); break;
    case 0x380: atomic_store(&dev_timer_enabled, val ? 1u : 0u); break;
    default: break;
    }
}

static uint32_t lapic_read(cpu_t *c, uint64_t off)
{
    switch (off) {
    case 0x020: return (uint32_t)c->id << 24;
    case 0x380: return atomic_load(&dev_timer_enabled);
    default: return 0;
    }
}

/* --------------------------------------------------- helpers called by code */

static void smc_check(cpu_t *c, uint64_t gpa)
{
    uint64_t page = gpa >> 12;
    if (!c->code_page[page] || inj_stale_cache) return;
    for (int i = 0; i < TB_SLOTS; i++)
        if (c->tb[i].code && (c->tb[i].gpa >> 12) == page) { c->tb[i].code = NULL; c->tb[i].gpa = 0; }
    c->code_page[page] = 0;
    c->smc_flushes++;
}

/* Guest accesses must be naturally aligned and must not cross a guest page.
 * Both are declared simplifications (see README): the emitted LDAR/STLR and LSE
 * atomics are defined only within a 16-byte granule on this host, so anything
 * else is converted into a visible #GP failure rather than left undefined. */
static void access_check(cpu_t *c, uint64_t va, uint64_t size)
{
    if ((va & (size - 1)) || ((va & 0xFFF) + size) > 0x1000) raise_exc(c, 13, 1, 0);
}

/* Store path. Returns a host pointer for the inline release store, or NULL
 * when the helper has already performed the access (MMIO). */
static uint8_t *hst(cpu_t *c, uint64_t va, uint64_t val, uint64_t size)
{
    c->tmp = val;
    access_check(c, va, size);
    int is_ram;
    uint8_t *p = tlb_lookup(c, va, 1, &is_ram);
    if (!is_ram) {
        uint64_t gpa = (uint64_t)(uintptr_t)p;
        if (gpa_is_lapic(gpa)) { lapic_write(c, gpa - LAPIC_BASE, (uint32_t)val); return NULL; }
        vabort(c, "store to unbacked guest-physical 0x%llx", gpa);
        longjmp(c->fault_jmp, 2);
    }
    smc_check(c, (uint64_t)(p - ram));
    return p;
}

/* Load path. Returns a host pointer the generated code reads with LDAR. */
static uint8_t *hld(cpu_t *c, uint64_t va, uint64_t size)
{
    access_check(c, va, size);
    int is_ram;
    uint8_t *p = tlb_lookup(c, va, 0, &is_ram);
    if (!is_ram) {
        uint64_t gpa = (uint64_t)(uintptr_t)p;
        if (gpa_is_lapic(gpa)) {
            c->mmio_scratch = lapic_read(c, gpa - LAPIC_BASE);
            return (uint8_t *)&c->mmio_scratch;
        }
        vabort(c, "load from unbacked guest-physical 0x%llx", gpa);
        longjmp(c->fault_jmp, 2);
    }
    return p;
}

/* Read-modify-write path: RAM only, write permission resolved before any
 * effect of the instruction, which is what makes the restart exactly-once. */
static uint8_t *hrmw(cpu_t *c, uint64_t va, uint64_t size)
{
    access_check(c, va, size);
    int is_ram;
    uint8_t *p = tlb_lookup(c, va, 1, &is_ram);
    if (!is_ram) { vabort(c, "read-modify-write on MMIO 0x%llx", va); longjmp(c->fault_jmp, 2); }
    smc_check(c, (uint64_t)(p - ram));
    return p;
}

static uint64_t heval_cc(cpu_t *c, uint64_t cc)
{
    uint64_t a = c->cc_a, b = c->cc_b, r = c->cc_res;
    int zf = 0, sf = 0, cf = 0, of = 0;
    switch (c->cc_op) {
    case CC_ADD: cf = r < a; of = (int)((~(a ^ b) & (a ^ r)) >> 63); break;
    case CC_SUB: cf = a < b; of = (int)(((a ^ b) & (a ^ r)) >> 63); break;
    case CC_INC: cf = (int)c->cc_cf; of = (int)((~(a ^ b) & (a ^ r)) >> 63); break;
    case CC_DEC: cf = (int)c->cc_cf; of = (int)(((a ^ b) & (a ^ r)) >> 63); break;
    case CC_FLAGS:
        zf = (int)((r >> 6) & 1); sf = (int)((r >> 7) & 1);
        cf = (int)(r & 1); of = (int)((r >> 11) & 1);
        goto have;
    default: break;
    }
    zf = (r == 0); sf = (int)(r >> 63);
have:
    switch (cc) {
    case 0x0: return (uint64_t)of;                case 0x1: return (uint64_t)!of;
    case 0x2: return (uint64_t)cf;                case 0x3: return (uint64_t)!cf;
    case 0x4: return (uint64_t)zf;                case 0x5: return (uint64_t)!zf;
    case 0x6: return (uint64_t)(cf || zf);        case 0x7: return (uint64_t)!(cf || zf);
    case 0x8: return (uint64_t)sf;                case 0x9: return (uint64_t)!sf;
    case 0xC: return (uint64_t)(sf != of);        case 0xD: return (uint64_t)(sf == of);
    case 0xE: return (uint64_t)(zf || (sf != of));
    case 0xF: return (uint64_t)(!zf && (sf == of));
    default: vabort(c, "unsupported condition code 0x%llx", cc); longjmp(c->fault_jmp, 2);
    }
}

static uint64_t compose_rflags(cpu_t *c)
{
    uint64_t f = 0x2ull | (c->iflag ? 0x200ull : 0ull);
    if (heval_cc(c, 0x4)) f |= 0x40;
    if (heval_cc(c, 0x8)) f |= 0x80;
    if (heval_cc(c, 0x2)) f |= 0x01;
    if (heval_cc(c, 0x0)) f |= 0x800;
    return f;
}

static void hfreeze_cf(cpu_t *c) { c->cc_cf = heval_cc(c, 0x2); }
static void hhlt(cpu_t *c) { c->halted = 1; }
static void hinvlpg(cpu_t *c, uint64_t va) { tlb_flush_page(c, va); }
static void hwrite_cr(cpu_t *c, uint64_t which, uint64_t val)
{
    if (which == 0) { c->cr0 = val; tlb_flush_all(c); }
    else if (which == 3) { c->cr3 = val; tlb_flush_all(c); }
    else vabort(c, "write to unsupported control register %llu", which);
}
static uint64_t hread_cr(cpu_t *c, uint64_t which)
{
    return which == 0 ? c->cr0 : which == 2 ? c->cr2 : which == 3 ? c->cr3 : 0;
}
static void hlidt(cpu_t *c, uint64_t va)
{
    int r;
    uint8_t *p = tlb_lookup(c, va, 0, &r);
    if (!r) { vabort(c, "lidt operand is not RAM"); longjmp(c->fault_jmp, 2); }
    c->idt_limit = (uint64_t)p[0] | ((uint64_t)p[1] << 8);
    memcpy(&c->idt_base, p + 2, 8);
}
static void hud2(cpu_t *c) { raise_exc(c, 6, 0, 0); }
static void hint3(cpu_t *c) { raise_exc(c, 3, 0, 0); }
static void hout(cpu_t *c, uint64_t port, uint64_t val)
{
    (void)val;
    switch (port) {
    case 0x10: c->cpu_begin_ns = thread_cpu_ns(); c->mark_begin = now_ns(); break;
    case 0x11: c->mark_end = now_ns(); c->cpu_end_ns = thread_cpu_ns(); break;
    case 0x13: c->finished = 1; break;
    default: vabort(c, "out to unknown port 0x%llx", port); break;
    }
}

/* ------------------------------------------------- exception and interrupt */

static void gpush(cpu_t *c, uint64_t v)
{
    c->regs[4] -= 8;
    int r;
    uint8_t *p = tlb_lookup(c, c->regs[4], 1, &r);
    if (!r) { vabort(c, "guest stack is not RAM"); return; }
    memcpy(p, &v, 8);
}
static uint64_t gpop(cpu_t *c)
{
    int r;
    uint8_t *p = tlb_lookup(c, c->regs[4], 0, &r);
    if (!r) { vabort(c, "guest stack is not RAM"); return 0; }
    uint64_t v;
    memcpy(&v, p, 8);
    c->regs[4] += 8;
    return v;
}

/* Push the x86-64 interrupt frame (SS, RSP, RFLAGS, CS, RIP [, error code])
 * and enter the guest handler with IF cleared, as an interrupt gate does. */
static void deliver(cpu_t *c, int vec, int has_err, uint64_t err, uint64_t resume)
{
    if ((uint64_t)vec * 16 + 15 > c->idt_limit) { vabort(c, "vector %d beyond IDT limit", vec); return; }
    int r;
    uint8_t *g = tlb_lookup(c, c->idt_base + (uint64_t)vec * 16, 0, &r);
    if (!r) { vabort(c, "IDT is not RAM"); return; }
    uint64_t lo, hi;
    memcpy(&lo, g, 8);
    memcpy(&hi, g + 8, 8);
    if (!(lo & (1ull << 47))) { vabort(c, "IDT gate %d not present", vec); return; }
    uint64_t off = (lo & 0xFFFFull) | (((lo >> 48) & 0xFFFFull) << 16) | ((hi & 0xFFFFFFFFull) << 32);
    if (off == 0) { vabort(c, "IDT gate %d has a null handler", vec); return; }
    uint64_t rflags = compose_rflags(c);
    uint64_t rsp_before = c->regs[4];
    if ((lo >> 32) & 7) c->regs[4] = IST1_TOP(c->id);    /* gate names an IST slot */
    c->regs[4] &= ~0xFull;                               /* Intel SDM Vol. 3A 7.14.2 */
    c->in_delivery = 1;
    gpush(c, 0x10);
    gpush(c, rsp_before);
    gpush(c, rflags);
    gpush(c, 0x08);
    gpush(c, resume);
    if (has_err) gpush(c, err);
    c->in_delivery = 0;
    c->iflag = 0;
    c->rip = off;
}

static void hiretq(cpu_t *c)
{
    uint64_t rip = gpop(c), cs = gpop(c), fl = gpop(c), rsp = gpop(c), ss = gpop(c);
    (void)cs; (void)ss;
    c->rip = rip;
    c->regs[4] = rsp;
    c->iflag = (fl >> 9) & 1;
    c->cc_op = CC_FLAGS;
    c->cc_res = fl;
}

/* --------------------------------------------------------------- x86 decode */

typedef struct {
    uint64_t rip, next;
    int rex, w, opsize, lock, rep;
    int mod, reg, rm;
    int mem, base, index, scale, riprel;
    int64_t disp;
} insn_t;

typedef struct { emit_t m; cpu_t *c; insn_t in; int ended; } tr_t;

static void ld_greg(tr_t *t, int rd, int g) { emit_ld_cpu(&t->m, rd, REGOFF(g)); }
static void st_greg(tr_t *t, int rs, int g) { emit_st_cpu(&t->m, rs, REGOFF(g)); }

static void emit_zext(tr_t *t, int rd, int size)
{
    if (size == 8) return;
    if (size == 4) { e(&t->m, A_ORRw(rd, XZR, rd)); return; }
    emit_imm(&t->m, X6, size == 2 ? 0xFFFFu : 0xFFu);
    e(&t->m, A_ANDr(rd, rd, X6));
}

static void emit_addr(tr_t *t, insn_t *in)
{
    if (in->riprel) { emit_imm(&t->m, X1, in->next + (uint64_t)in->disp); return; }
    if (in->base >= 0) ld_greg(t, X1, in->base); else emit_imm(&t->m, X1, 0);
    if (in->index >= 0) { ld_greg(t, X2, in->index); e(&t->m, A_ADDrs(X1, X1, X2, in->scale)); }
    if (in->disp) { emit_imm(&t->m, X3, (uint64_t)in->disp); e(&t->m, A_ADDr(X1, X1, X3)); }
}

static void emit_track_rip(tr_t *t, insn_t *in)
{
    emit_imm(&t->m, X0, in->rip);
    emit_st_cpu(&t->m, X0, CPUOFF(cur_rip));
    emit_imm(&t->m, X0, in->next);
    emit_st_cpu(&t->m, X0, CPUOFF(cur_next));
}

/* X1 = address, X2 = value */
static void emit_store_mem(tr_t *t, int size)
{
    e(&t->m, A_MOVr(X0, X19));
    emit_imm(&t->m, X3, (uint64_t)size);
    emit_call(&t->m, (void *)hst);
    emit_ld_cpu(&t->m, X2, CPUOFF(tmp));
    e(&t->m, A_CBZ(X0, 2));
    e(&t->m, inj_no_barrier ? a_str(X2, X0, 0, size) : a_stlr(X2, X0, size));
}

/* X1 = address -> X1 = value (zero-extended) */
static void emit_load_mem(tr_t *t, int size)
{
    e(&t->m, A_MOVr(X0, X19));
    emit_imm(&t->m, X2, (uint64_t)size);
    emit_call(&t->m, (void *)hld);
    e(&t->m, inj_no_barrier ? a_ldr(X1, X0, 0, size) : a_ldar(X1, X0, size));
}

/* Push the 64-bit value in cpu->tmp2. The store is emitted *before* the RSP
 * commit, so a faulting push leaves no guest-visible effect and the restarted
 * instruction decrements RSP exactly once. --inject-push-order restores the
 * defective ordering. */
static void emit_push_tmp2(tr_t *t)
{
    emit_t *m = &t->m;
    ld_greg(t, X4, 4);
    e(m, A_SUBi(X4, X4, 8));
    if (inj_push_order) st_greg(t, X4, 4);
    e(m, A_MOVr(X1, X4));
    emit_ld_cpu(m, X2, CPUOFF(tmp2));
    emit_store_mem(t, 8);
    if (!inj_push_order) {
        ld_greg(t, X4, 4);                                /* X4 is caller-saved */
        e(m, A_SUBi(X4, X4, 8));
        st_greg(t, X4, 4);
    }
}

static void emit_flags(tr_t *t, int op, int ra, int rb, int rres)
{
    emit_st_cpu(&t->m, ra, CPUOFF(cc_a));
    emit_st_cpu(&t->m, rb, CPUOFF(cc_b));
    emit_st_cpu(&t->m, rres, CPUOFF(cc_res));
    emit_imm(&t->m, X6, (uint64_t)op);
    emit_st_cpu(&t->m, X6, CPUOFF(cc_op));
}

static void emit_end_block(tr_t *t, uint64_t rip)
{
    emit_imm(&t->m, X0, rip);
    emit_st_cpu(&t->m, X0, CPUOFF(rip));
    t->ended = 1;
}

static void emit_jcc(tr_t *t, int cc, uint64_t tgt, uint64_t fall)
{
    emit_t *m = &t->m;
    e(m, A_MOVr(X0, X19));
    emit_imm(m, X1, (uint64_t)cc);
    emit_call(m, (void *)heval_cc);
    uint32_t *site = m->p;
    e(m, A_CBZ(X0, 0));
    emit_imm(m, X1, tgt);
    emit_st_cpu(m, X1, CPUOFF(rip));
    uint32_t *site2 = m->p;
    e(m, A_B(0));
    if (site < m->end) *site = A_CBZ(X0, (int)(m->p - site));
    emit_imm(m, X1, fall);
    emit_st_cpu(m, X1, CPUOFF(rip));
    if (site2 < m->end) *site2 = A_B((int)(m->p - site2));
    t->ended = 1;
}

static uint64_t rd_imm(const uint8_t *p, int n)
{
    uint64_t v = 0;
    for (int i = 0; i < n; i++) v |= (uint64_t)p[i] << (8 * i);
    return v;
}
static int64_t rd_simm(const uint8_t *p, int n)
{
    uint64_t v = rd_imm(p, n);
    if (n == 1) return (int8_t)v;
    if (n == 2) return (int16_t)v;
    if (n == 4) return (int32_t)v;
    return (int64_t)v;
}

static int decode_modrm(insn_t *in, const uint8_t *p)
{
    int n = 1;
    uint8_t b = p[0];
    in->mod = b >> 6;
    in->reg = ((b >> 3) & 7) | ((in->rex & 4) ? 8 : 0);
    in->rm = b & 7;
    in->base = in->index = -1;
    in->scale = 0;
    in->disp = 0;
    in->riprel = 0;
    if (in->mod == 3) { in->mem = 0; in->rm |= (in->rex & 1) ? 8 : 0; return n; }
    in->mem = 1;
    if (in->rm == 4) {
        uint8_t s = p[n++];
        in->scale = s >> 6;
        int idx = ((s >> 3) & 7) | ((in->rex & 2) ? 8 : 0);
        in->index = (idx == 4) ? -1 : idx;
        if ((s & 7) == 5 && in->mod == 0) { in->base = -1; in->disp = rd_simm(p + n, 4); n += 4; }
        else in->base = (s & 7) | ((in->rex & 1) ? 8 : 0);
    } else if (in->rm == 5 && in->mod == 0) {
        in->riprel = 1;
        in->disp = rd_simm(p + n, 4);
        n += 4;
    } else {
        in->base = in->rm | ((in->rex & 1) ? 8 : 0);
    }
    if (in->mod == 1) { in->disp += rd_simm(p + n, 1); n += 1; }
    else if (in->mod == 2) { in->disp += rd_simm(p + n, 4); n += 4; }
    return n;
}

/* ---------------------------------------------------------- the translator */

static int translate_insn(tr_t *t, uint64_t rip, const uint8_t *p, uint64_t avail)
{
#define in (t->in)
    memset(&in, 0, sizeof in);
    in.rip = rip;
    in.opsize = 4;
    emit_t *m = &t->m;
    int n = 0;
    for (;;) {
        if ((uint64_t)n >= avail) return -1;
        uint8_t b = p[n];
        if (b == 0xF0) { in.lock = 1; n++; }
        else if (b == 0x66) { in.opsize = 2; n++; }
        else if (b == 0xF3) { in.rep = 1; n++; }
        else if (b == 0xF2) { n++; }
        else if (b >= 0x40 && b <= 0x4F) { in.rex = b & 0xF; if (b & 8) in.w = 1; n++; }
        else break;
    }
    if (in.w) in.opsize = 8;
    uint8_t op = p[n++];
    /* LOCK is honoured only on the forms emitted as a single atomic instruction
     * (0F C1 xadd, 0F B1 cmpxchg); 87 xchg is atomic without the prefix. Any
     * other LOCK is rejected rather than silently emitted non-atomically. */
    if (in.lock && !(op == 0x0F && ((uint64_t)n < avail) && (p[n] == 0xC1 || p[n] == 0xB1))
        && op != 0x87) return -1;

    if (op == 0x0F) {
        uint8_t op2 = p[n++];
        if (op2 >= 0x80 && op2 <= 0x8F) {
            int64_t rel = rd_simm(p + n, 4); n += 4;
            in.next = rip + n;
            emit_jcc(t, op2 & 0xF, in.next + (uint64_t)rel, in.next);
            return 0;
        }
        switch (op2) {
        case 0x0B:
            in.next = rip + n;
            emit_track_rip(t, &in);
            e(m, A_MOVr(X0, X19));
            emit_call(m, (void *)hud2);
            emit_end_block(t, in.next);
            return 0;
        case 0xAE:
            if ((uint64_t)n >= avail || p[n] != 0xF0) return -1;
            n++;
            in.next = rip + n;
            e(m, A_DMBISH);
            return 0;
        case 0x01:
            n += decode_modrm(&in, p + n);
            in.next = rip + n;
            if (!in.mem) return -1;
            emit_track_rip(t, &in);
            emit_addr(t, &in);
            e(m, A_MOVr(X0, X19));
            if ((in.reg & 7) == 3) emit_call(m, (void *)hlidt);
            else if ((in.reg & 7) == 7) emit_call(m, (void *)hinvlpg);
            else return -1;
            return 0;
        case 0x20:
            n += decode_modrm(&in, p + n);
            in.next = rip + n;
            if (in.mem) return -1;
            e(m, A_MOVr(X0, X19));
            emit_imm(m, X1, (uint64_t)(in.reg & 7));
            emit_call(m, (void *)hread_cr);
            st_greg(t, X0, in.rm);
            return 0;
        case 0x22:
            n += decode_modrm(&in, p + n);
            in.next = rip + n;
            if (in.mem) return -1;
            ld_greg(t, X2, in.rm);
            e(m, A_MOVr(X0, X19));
            emit_imm(m, X1, (uint64_t)(in.reg & 7));
            emit_call(m, (void *)hwrite_cr);
            emit_end_block(t, in.next);
            return 0;
        case 0xB6: case 0xB7: {
            int ssz = (op2 == 0xB6) ? 1 : 2;
            n += decode_modrm(&in, p + n);
            in.next = rip + n;
            if (in.mem) { emit_track_rip(t, &in); emit_addr(t, &in); emit_load_mem(t, ssz); }
            else { ld_greg(t, X1, in.rm); emit_zext(t, X1, ssz); }
            st_greg(t, X1, in.reg);
            return 0;
        }
        case 0xC1:                                        /* xadd r/m, r */
            n += decode_modrm(&in, p + n);
            in.next = rip + n;
            if (!in.mem || in.opsize != 8) return -1;   /* 64-bit only: see README */
            emit_track_rip(t, &in);
            emit_addr(t, &in);
            e(m, A_MOVr(X0, X19));
            emit_imm(m, X2, (uint64_t)in.opsize);
            emit_call(m, (void *)hrmw);
            ld_greg(t, X1, in.reg);
            if (inj_nonatomic) {
                e(m, a_ldr(X2, X0, 0, in.opsize));
                e(m, A_ADDr(X3, X2, X1));
                e(m, a_str(X3, X0, 0, in.opsize));
            } else {
                e(m, a_ldaddal(X1, X2, X0, in.opsize));
                e(m, A_ADDr(X3, X2, X1));
            }
            st_greg(t, X2, in.reg);
            emit_flags(t, CC_ADD, X2, X1, X3);
            return 0;
        case 0xB1:                                        /* cmpxchg r/m, r */
            n += decode_modrm(&in, p + n);
            in.next = rip + n;
            if (!in.mem || in.opsize != 8) return -1;   /* 64-bit only: see README */
            emit_track_rip(t, &in);
            emit_addr(t, &in);
            e(m, A_MOVr(X0, X19));
            emit_imm(m, X2, (uint64_t)in.opsize);
            emit_call(m, (void *)hrmw);
            ld_greg(t, X1, 0);
            ld_greg(t, X2, in.reg);
            e(m, A_MOVr(X3, X1));
            e(m, a_casal(X1, X2, X0, in.opsize));
            st_greg(t, X1, 0);
            e(m, A_SUBr(X4, X3, X1));
            emit_flags(t, CC_SUB, X3, X1, X4);
            return 0;
        default: return -1;
        }
    }

    if (op >= 0x50 && op <= 0x57) {                        /* push r64 */
        int g = (op & 7) | ((in.rex & 1) ? 8 : 0);
        in.next = rip + n;
        emit_track_rip(t, &in);
        ld_greg(t, X2, g);
        emit_st_cpu(m, X2, CPUOFF(tmp2));
        emit_push_tmp2(t);
        return 0;
    }
    if (op >= 0x58 && op <= 0x5F) {                        /* pop r64 */
        int g = (op & 7) | ((in.rex & 1) ? 8 : 0);
        in.next = rip + n;
        emit_track_rip(t, &in);
        ld_greg(t, X1, 4);
        emit_load_mem(t, 8);
        emit_st_cpu(m, X1, CPUOFF(tmp2));
        ld_greg(t, X4, 4);
        e(m, A_ADDi(X4, X4, 8));
        st_greg(t, X4, 4);
        emit_ld_cpu(m, X1, CPUOFF(tmp2));
        st_greg(t, X1, g);
        return 0;
    }
    if (op >= 0xB8 && op <= 0xBF) {                        /* mov r, imm */
        int g = (op & 7) | ((in.rex & 1) ? 8 : 0);
        uint64_t v;
        if (in.w) { v = rd_imm(p + n, 8); n += 8; } else { v = rd_imm(p + n, 4); n += 4; }
        in.next = rip + n;
        emit_imm(m, X1, v);
        st_greg(t, X1, g);
        return 0;
    }
    if (op >= 0x70 && op <= 0x7F) {                        /* jcc rel8 */
        int64_t rel = rd_simm(p + n, 1); n += 1;
        in.next = rip + n;
        emit_jcc(t, op & 0xF, in.next + (uint64_t)rel, in.next);
        return 0;
    }

    /* ALU group, register/memory forms: 00..03, 08..0B, ... 38..3B */
    if (op < 0x40 && (op & 7) <= 3) {
        int low = op & 7;
        {
            int aop = (op >> 3) & 7;
            if (aop == 2 || aop == 3) return -1;
            int to_rm = (low == 0 || low == 1);
            int byte = (low == 0 || low == 2);
            n += decode_modrm(&in, p + n);
            in.next = rip + n;
            int size = byte ? 1 : in.opsize;
            if (size != 8) return -1;
            int is_cmp = (aop == 7);
            int have_ptr = 0;
            if (!in.mem) {
                ld_greg(t, X1, to_rm ? in.rm : in.reg);
                ld_greg(t, X2, to_rm ? in.reg : in.rm);
            } else {
                emit_track_rip(t, &in);
                emit_addr(t, &in);
                if (to_rm) {
                    if (is_cmp) { emit_load_mem(t, 8); ld_greg(t, X2, in.reg); }
                    else {
                        e(m, A_MOVr(X0, X19));
                        emit_imm(m, X2, 8);
                        emit_call(m, (void *)hrmw);
                        e(m, A_MOVr(X5, X0));
                        e(m, inj_no_barrier ? a_ldr(X1, X5, 0, 8) : a_ldar(X1, X5, 8));
                        ld_greg(t, X2, in.reg);
                        have_ptr = 1;
                    }
                } else { emit_load_mem(t, 8); e(m, A_MOVr(X2, X1)); ld_greg(t, X1, in.reg); }
            }
            int ccop = CC_LOGIC;
            switch (aop) {
            case 0: e(m, A_ADDr(X3, X1, X2)); ccop = CC_ADD; break;
            case 1: e(m, A_ORRr(X3, X1, X2)); break;
            case 4: e(m, A_ANDr(X3, X1, X2)); break;
            case 5: case 7: e(m, A_SUBr(X3, X1, X2)); ccop = CC_SUB; break;
            case 6: e(m, A_EORr(X3, X1, X2)); break;
            default: return -1;
            }
            emit_flags(t, ccop, X1, X2, X3);
            if (!is_cmp) {
                if (!in.mem) st_greg(t, X3, to_rm ? in.rm : in.reg);
                else if (have_ptr) { e(m, A_MOVr(X2, X3)); e(m, inj_no_barrier ? a_str(X2, X5, 0, 8) : a_stlr(X2, X5, 8)); }
                else st_greg(t, X3, in.reg);
            }
            return 0;
        }
    }

    switch (op) {
    case 0x88: case 0x89: case 0x8A: case 0x8B: {
        int byte = (op == 0x88 || op == 0x8A);
        int to_rm = (op == 0x88 || op == 0x89);
        int size = byte ? 1 : in.opsize;
        n += decode_modrm(&in, p + n);
        in.next = rip + n;
        if (to_rm) {
            if (in.mem) {
                emit_track_rip(t, &in);
                emit_addr(t, &in);
                ld_greg(t, X2, in.reg);
                emit_zext(t, X2, size);
                emit_store_mem(t, size);
            } else {
                ld_greg(t, X1, in.reg);
                emit_zext(t, X1, size);
                st_greg(t, X1, in.rm);
            }
        } else {
            if (in.mem) { emit_track_rip(t, &in); emit_addr(t, &in); emit_load_mem(t, size); }
            else { ld_greg(t, X1, in.rm); emit_zext(t, X1, size); }
            st_greg(t, X1, in.reg);
        }
        return 0;
    }
    case 0x8D:
        n += decode_modrm(&in, p + n);
        in.next = rip + n;
        if (!in.mem) return -1;
        emit_addr(t, &in);
        st_greg(t, X1, in.reg);
        return 0;
    case 0xC6: case 0xC7: {
        int size = (op == 0xC6) ? 1 : in.opsize;
        n += decode_modrm(&in, p + n);
        if ((in.reg & 7) != 0) return -1;
        int64_t v = rd_simm(p + n, (op == 0xC6) ? 1 : 4);
        n += (op == 0xC6) ? 1 : 4;
        in.next = rip + n;
        if (in.mem) {
            emit_track_rip(t, &in);
            emit_addr(t, &in);
            emit_imm(m, X2, (uint64_t)v);
            emit_zext(t, X2, size);
            emit_store_mem(t, size);
        } else {
            emit_imm(m, X1, (uint64_t)v);
            emit_zext(t, X1, size);
            st_greg(t, X1, in.rm);
        }
        return 0;
    }
    case 0x81: case 0x83: {
        n += decode_modrm(&in, p + n);
        int aop = in.reg & 7;
        int64_t v = rd_simm(p + n, (op == 0x81) ? 4 : 1);
        n += (op == 0x81) ? 4 : 1;
        in.next = rip + n;
        if (in.opsize != 8 || aop == 2 || aop == 3) return -1;
        int is_cmp = (aop == 7), have_ptr = 0;
        if (in.mem) {
            emit_track_rip(t, &in);
            emit_addr(t, &in);
            if (is_cmp) emit_load_mem(t, 8);
            else {
                e(m, A_MOVr(X0, X19));
                emit_imm(m, X2, 8);
                emit_call(m, (void *)hrmw);
                e(m, A_MOVr(X5, X0));
                e(m, inj_no_barrier ? a_ldr(X1, X5, 0, 8) : a_ldar(X1, X5, 8));
                have_ptr = 1;
            }
        } else ld_greg(t, X1, in.rm);
        emit_imm(m, X2, (uint64_t)v);
        int ccop = CC_LOGIC;
        switch (aop) {
        case 0: e(m, A_ADDr(X3, X1, X2)); ccop = CC_ADD; break;
        case 1: e(m, A_ORRr(X3, X1, X2)); break;
        case 4: e(m, A_ANDr(X3, X1, X2)); break;
        case 5: case 7: e(m, A_SUBr(X3, X1, X2)); ccop = CC_SUB; break;
        case 6: e(m, A_EORr(X3, X1, X2)); break;
        default: return -1;
        }
        emit_flags(t, ccop, X1, X2, X3);
        if (!is_cmp) {
            if (have_ptr) { e(m, A_MOVr(X2, X3)); e(m, inj_no_barrier ? a_str(X2, X5, 0, 8) : a_stlr(X2, X5, 8)); }
            else if (!in.mem) st_greg(t, X3, in.rm);
            else return -1;
        }
        return 0;
    }
    case 0x85: {
        n += decode_modrm(&in, p + n);
        in.next = rip + n;
        if (in.opsize != 8) return -1;
        if (in.mem) { emit_track_rip(t, &in); emit_addr(t, &in); emit_load_mem(t, 8); }
        else ld_greg(t, X1, in.rm);
        ld_greg(t, X2, in.reg);
        e(m, A_ANDr(X3, X1, X2));
        emit_flags(t, CC_LOGIC, X1, X2, X3);
        return 0;
    }
    case 0xC1: case 0xD3: {
        n += decode_modrm(&in, p + n);
        int sop = in.reg & 7;
        int64_t v = 0;
        if (op == 0xC1) { v = rd_simm(p + n, 1); n += 1; }
        in.next = rip + n;
        if (in.mem || in.opsize != 8) return -1;
        ld_greg(t, X1, in.rm);
        if (op == 0xC1) emit_imm(m, X2, (uint64_t)(v & 63));
        else { ld_greg(t, X2, 1); emit_imm(m, X3, 63); e(m, A_ANDr(X2, X2, X3)); }
        if (sop == 4) e(m, A_LSLV(X3, X1, X2));
        else if (sop == 5) e(m, A_LSRV(X3, X1, X2));
        else if (sop == 7) e(m, A_ASRV(X3, X1, X2));
        else return -1;
        st_greg(t, X3, in.rm);
        return 0;                                          /* flags unchanged: see README */
    }
    case 0xF7: {
        n += decode_modrm(&in, p + n);
        if ((in.reg & 7) != 0) return -1;
        int64_t v = rd_simm(p + n, 4); n += 4;
        in.next = rip + n;
        if (in.opsize != 8) return -1;
        if (in.mem) { emit_track_rip(t, &in); emit_addr(t, &in); emit_load_mem(t, 8); }
        else ld_greg(t, X1, in.rm);
        emit_imm(m, X2, (uint64_t)v);
        e(m, A_ANDr(X3, X1, X2));
        emit_flags(t, CC_LOGIC, X1, X2, X3);
        return 0;
    }
    case 0xFF: {
        n += decode_modrm(&in, p + n);
        int sub = in.reg & 7;
        in.next = rip + n;
        if (sub == 0 || sub == 1) {
            if (in.mem || in.opsize != 8) return -1;
            e(m, A_MOVr(X0, X19));
            emit_call(m, (void *)hfreeze_cf);
            ld_greg(t, X1, in.rm);
            emit_imm(m, X2, 1);
            if (sub == 0) e(m, A_ADDr(X3, X1, X2)); else e(m, A_SUBr(X3, X1, X2));
            st_greg(t, X3, in.rm);
            emit_flags(t, sub == 0 ? CC_INC : CC_DEC, X1, X2, X3);
            return 0;
        }
        if (sub == 2 || sub == 4) {                        /* call/jmp r/m64 */
            if (in.mem) { emit_track_rip(t, &in); emit_addr(t, &in); emit_load_mem(t, 8); }
            else ld_greg(t, X1, in.rm);
            emit_st_cpu(m, X1, CPUOFF(tmp));
            if (sub == 2) {
                emit_track_rip(t, &in);
                emit_imm(m, X2, in.next);
                emit_st_cpu(m, X2, CPUOFF(tmp2));
                emit_push_tmp2(t);
            }
            emit_ld_cpu(m, X1, CPUOFF(tmp));
            emit_st_cpu(m, X1, CPUOFF(rip));
            t->ended = 1;
            return 0;
        }
        if (sub == 6) {                                    /* push r/m64 */
            if (in.mem) { emit_track_rip(t, &in); emit_addr(t, &in); emit_load_mem(t, 8); }
            else ld_greg(t, X1, in.rm);
            emit_st_cpu(m, X1, CPUOFF(tmp2));
            emit_track_rip(t, &in);
            emit_push_tmp2(t);
            return 0;
        }
        return -1;
    }
    case 0x87: {                                           /* xchg r/m, r */
        n += decode_modrm(&in, p + n);
        in.next = rip + n;
        if (!in.mem || in.opsize != 8) return -1;
        emit_track_rip(t, &in);
        emit_addr(t, &in);
        e(m, A_MOVr(X0, X19));
        emit_imm(m, X2, 8);
        emit_call(m, (void *)hrmw);
        ld_greg(t, X1, in.reg);
        e(m, a_swpal(X1, X2, X0, 8));
        st_greg(t, X2, in.reg);
        return 0;
    }
    case 0xE8: {
        int64_t rel = rd_simm(p + n, 4); n += 4;
        in.next = rip + n;
        emit_track_rip(t, &in);
        emit_imm(m, X2, in.next);
        emit_st_cpu(m, X2, CPUOFF(tmp2));
        emit_push_tmp2(t);
        emit_end_block(t, in.next + (uint64_t)rel);
        return 0;
    }
    case 0xE9: case 0xEB: {
        int w = (op == 0xE9) ? 4 : 1;
        int64_t rel = rd_simm(p + n, w); n += w;
        in.next = rip + n;
        emit_end_block(t, in.next + (uint64_t)rel);
        return 0;
    }
    case 0xC3:
        in.next = rip + n;
        emit_track_rip(t, &in);
        ld_greg(t, X1, 4);
        emit_load_mem(t, 8);
        emit_st_cpu(m, X1, CPUOFF(rip));
        ld_greg(t, X4, 4);
        e(m, A_ADDi(X4, X4, 8));
        st_greg(t, X4, 4);
        t->ended = 1;
        return 0;
    case 0xCF:
        in.next = rip + n;
        emit_track_rip(t, &in);
        e(m, A_MOVr(X0, X19));
        emit_call(m, (void *)hiretq);
        t->ended = 1;
        return 0;
    case 0xF4:
        in.next = rip + n;
        e(m, A_MOVr(X0, X19));
        emit_call(m, (void *)hhlt);
        emit_end_block(t, in.next);
        return 0;
    case 0xFA: case 0xFB:
        in.next = rip + n;
        emit_imm(m, X1, op == 0xFB ? 1 : 0);
        emit_st_cpu(m, X1, CPUOFF(iflag));
        return 0;
    case 0x90:
        in.next = rip + n;
        if (in.rep) e(m, A_YIELD);
        return 0;
    case 0xCC:
        in.next = rip + n;
        emit_track_rip(t, &in);
        e(m, A_MOVr(X0, X19));
        emit_call(m, (void *)hint3);
        emit_end_block(t, in.next);
        return 0;
    case 0xEF:
        in.next = rip + n;
        e(m, A_MOVr(X0, X19));
        ld_greg(t, X1, 2);
        emit_imm(m, X3, 0xFFFF);
        e(m, A_ANDr(X1, X1, X3));
        ld_greg(t, X2, 0);
        emit_call(m, (void *)hout);
        return 0;
    default:
        return -1;
    }
#undef in
}

/* Copy up to 16 bytes (the x86 maximum instruction length) starting at `va`
 * into `buf`, translating the next guest page when the instruction would span
 * a page boundary. Every decoder read is therefore inside a bounded buffer, and
 * a not-present continuation page raises an instruction-fetch #PF rather than
 * silently decoding whatever follows in host memory. */
static uint64_t code_window(cpu_t *c, uint64_t va, uint8_t *buf)
{
    int r;
    uint8_t *p = tlb_lookup_ex(c, va, 0, 1, &r);
    if (!r) { vabort(c, "instruction fetch from MMIO 0x%llx", va); longjmp(c->fault_jmp, 2); }
    uint64_t here = 0x1000 - (va & 0xFFF);
    uint64_t n = here < 16 ? here : 16;
    memcpy(buf, p, n);
    if (n < 16) {
        uint8_t *q = tlb_lookup_ex(c, (va + n) & ~0xFFFull, 0, 1, &r);
        if (r) { memcpy(buf + n, q, 16 - n); n = 16; }
    }
    return n;
}

static void *translate_block(cpu_t *c, uint64_t va)
{
    pthread_jit_write_protect_np(0);
    tr_t t;
    memset(&t, 0, sizeof t);
    t.c = c;
    t.m.c = c;
    t.m.p = (uint32_t *)(void *)c->jit_ptr;
    t.m.end = (uint32_t *)(void *)c->jit_end;
    emit_t *m = &t.m;
    uint32_t *start = m->p;

    e(m, A_SUBi(SP_R, SP_R, 32));
    e(m, a_str(X30, SP_R, 0, 8));
    e(m, a_str(X19, SP_R, 8, 8));
    e(m, A_MOVr(X19, X0));

    uint64_t rip = va;
    int count = 0;
    while (!t.ended && count < MAX_BLOCK_INSNS && !atomic_load(&c->has_abort)) {
        uint8_t win[16];
        uint64_t avail = code_window(c, rip, win);
        if (translate_insn(&t, rip, win, avail) < 0) {
            pthread_jit_write_protect_np(1);
            vabort(c, "unsupported instruction at guest RIP 0x%llx: %02x %02x %02x %02x",
                   rip, win[0], avail > 1 ? win[1] : 0, avail > 2 ? win[2] : 0,
                   avail > 3 ? win[3] : 0);
            return NULL;
        }
        rip = t.in.next;
        count++;
    }
    if (!t.ended) emit_end_block(&t, rip);

    emit_ld_cpu(m, X0, CPUOFF(insns));
    emit_imm(m, X1, (uint64_t)count);
    e(m, A_ADDr(X0, X0, X1));
    emit_st_cpu(m, X0, CPUOFF(insns));
    e(m, a_ldr(X30, SP_R, 0, 8));
    e(m, a_ldr(X19, SP_R, 8, 8));
    e(m, A_ADDi(SP_R, SP_R, 32));
    e(m, A_RET);

    pthread_jit_write_protect_np(1);
    if (atomic_load(&c->has_abort)) return NULL;
    sys_icache_invalidate(start, (size_t)((uint8_t *)m->p - (uint8_t *)start));
    c->jit_ptr = (uint8_t *)m->p;
    c->translations++;
    return start;
}

/* ------------------------------------------------------------- code cache */

static void *tb_lookup(cpu_t *c, uint64_t gpa)
{
    uint64_t h = (gpa * 0x9E3779B97F4A7C15ull) >> 48;
    for (int i = 0; i < 64; i++) {
        struct tbe *s = &c->tb[(h + (uint64_t)i) & (TB_SLOTS - 1)];
        if (s->code && s->gpa == gpa) return s->code;
    }
    return NULL;
}
static void tb_insert(cpu_t *c, uint64_t gpa, void *code)
{
    uint64_t h = (gpa * 0x9E3779B97F4A7C15ull) >> 48;
    for (int i = 0; i < 64; i++) {
        struct tbe *s = &c->tb[(h + (uint64_t)i) & (TB_SLOTS - 1)];
        if (!s->code) { s->gpa = gpa; s->code = code; return; }
    }
    vabort(c, "code cache bucket full");
}

/* ------------------------------------------------------------- vCPU thread */

static void check_irq(cpu_t *c)
{
    if (!c->iflag) return;
    if (atomic_load(&c->isr)) return;
    uint32_t irr = atomic_load(&c->irr);
    if (!irr) return;
    int vec;
    if (irr & 1u) { atomic_fetch_and(&c->irr, ~1u); vec = (int)atomic_load(&dev_timer_vector); }
    else { atomic_fetch_and(&c->irr, ~2u); vec = (int)atomic_load(&c->ipi_vector); }
    atomic_store(&c->isr, (uint32_t)vec);
    if (c->halted) { c->halted = 0; c->hlt_wakes++; }
    c->irq_delivered++;
    deliver(c, vec, 0, 0, c->rip);
}

static void *vcpu_thread(void *arg)
{
    cpu_t *c = (cpu_t *)arg;
    pthread_jit_write_protect_np(1);
    for (;;) {
        if (atomic_load(&g_stop) || atomic_load(&c->has_abort)) break;
        int j = setjmp(c->fault_jmp);
        if (j == 0) {
            check_irq(c);
            if (c->halted) {
                if (!c->iflag) { atomic_store(&c->halted_clean, 1); break; }
                struct timespec ts = { 0, 100000 };
                nanosleep(&ts, NULL);
                continue;
            }
            uint64_t va = c->rip;
            c->cur_rip = va;
            uint64_t gpa = walk(c, va, 0, 1);
            void *code = tb_lookup(c, gpa);
            if (!code) {
                code = translate_block(c, va);
                if (!code) break;
                tb_insert(c, gpa, code);
                c->code_page[gpa >> 12] = 1;
            }
            c->blocks++;
            ((void (*)(cpu_t *))code)(c);
        } else if (j == 1) {
            pthread_jit_write_protect_np(1);
            uint64_t resume = inj_skip_restart ? c->cur_next : c->cur_rip;
            deliver(c, c->fault_vec, c->has_err, c->fault_err, resume);
        } else {
            pthread_jit_write_protect_np(1);
            break;
        }
    }
    return NULL;
}

static void *timer_thread(void *unused)
{
    (void)unused;
    uint64_t period = TIMER_PERIOD_NS * tb_info.denom / tb_info.numer;
    while (!atomic_load(&dev_timer_stop) && !atomic_load(&g_stop)) {
        if (atomic_load(&dev_timer_enabled)) {
            cpu_t *c = &cpus[0];
            /* post-on-EOI: at most one timer interrupt outstanding, so the
             * host-posted count and the guest-observed count are comparable
             * exactly, with no coalescing rule needed. */
            if (!(atomic_load(&c->irr) & 1u) && atomic_load(&c->isr) != atomic_load(&dev_timer_vector)) {
                atomic_fetch_or(&c->irr, 1u);
                atomic_fetch_add(&dev_timer_posted, 1);
            }
        }
        mach_wait_until(mach_absolute_time() + period);
    }
    return NULL;
}

/* ------------------------------------------------------------------- main */

static uint64_t gsym(const char *name)
{
    for (size_t i = 0; i < sizeof guest_syms / sizeof guest_syms[0]; i++)
        if (!strcmp(guest_syms[i].name, name)) return guest_syms[i].off;
    fprintf(stderr, "guest symbol not found: %s\n", name);
    exit(2);
}
static uint64_t gread(const char *name)
{
    uint64_t v;
    memcpy(&v, ram + GUEST_LOAD_GPA + gsym(name), 8);
    return v;
}
static uint64_t gread_at(uint64_t off)
{
    uint64_t v;
    memcpy(&v, ram + GUEST_LOAD_GPA + off, 8);
    return v;
}

int main(int argc, char **argv)
{
    const char *out = NULL, *injection = "none";
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--inject-skip-restart")) { inj_skip_restart = 1; injection = "skip-restart"; }
        else if (!strcmp(argv[i], "--inject-no-barrier")) { inj_no_barrier = 1; injection = "no-barrier"; }
        else if (!strcmp(argv[i], "--inject-nonatomic-rmw")) { inj_nonatomic = 1; injection = "nonatomic-rmw"; }
        else if (!strcmp(argv[i], "--inject-drop-ipi")) { inj_drop_ipi = 1; injection = "drop-ipi"; }
        else if (!strcmp(argv[i], "--inject-stale-cache")) { inj_stale_cache = 1; injection = "stale-cache"; }
        else if (!strcmp(argv[i], "--inject-push-order")) { inj_push_order = 1; injection = "push-order"; }
        else if (!strcmp(argv[i], "--timeout") && i + 1 < argc) opt_timeout = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--out") && i + 1 < argc) out = argv[++i];
        else { fprintf(stderr, "unknown argument: %s\n", argv[i]); return 2; }
    }

    mach_timebase_info(&tb_info);

    ram = mmap(NULL, GUEST_RAM_SIZE, PROT_READ | PROT_WRITE,
               MAP_PRIVATE | MAP_ANON, -1, 0);
    if (ram == MAP_FAILED) { perror("mmap guest RAM"); return 2; }
    memcpy(ram + GUEST_LOAD_GPA, guest_blob, sizeof guest_blob);

    const char *jit_note = "MAP_JIT";
    for (int i = 0; i < NVCPU; i++) {
        cpu_t *c = &cpus[i];
        c->id = i;
        c->regs[7] = (uint64_t)i;                          /* RDI = vCPU id */
        c->regs[4] = i ? STACK1_TOP : STACK0_TOP;
        c->rip = GUEST_LOAD_GPA + gsym("_start");
        c->idt_limit = 0;
        c->jit_base = mmap(NULL, JIT_BYTES, PROT_READ | PROT_WRITE | PROT_EXEC,
                           MAP_PRIVATE | MAP_ANON | MAP_JIT, -1, 0);
        if (c->jit_base == MAP_FAILED) {
            jit_note = "MAP_JIT failed; retried without it";
            c->jit_base = mmap(NULL, JIT_BYTES, PROT_READ | PROT_WRITE | PROT_EXEC,
                               MAP_PRIVATE | MAP_ANON, -1, 0);
        }
        if (c->jit_base == MAP_FAILED) { perror("mmap JIT region"); return 2; }
        c->jit_ptr = c->jit_base;
        c->jit_end = c->jit_base + JIT_BYTES;
        c->tb = calloc(TB_SLOTS, sizeof *c->tb);
        c->tlb = calloc(TLB_SLOTS, sizeof *c->tlb);
        c->code_page = calloc(GUEST_RAM_SIZE >> 12, 1);
        if (!c->tb || !c->tlb || !c->code_page) { perror("calloc"); return 2; }
    }

    uint64_t t0 = now_ns();
    pthread_t th[NVCPU], tt;
    pthread_create(&tt, NULL, timer_thread, NULL);
    for (int i = 0; i < NVCPU; i++) pthread_create(&th[i], NULL, vcpu_thread, &cpus[i]);

    int timed_out = 0;
    uint64_t deadline = t0 + (uint64_t)opt_timeout * 1000000000ull;
    for (;;) {
        int live = 0;
        for (int i = 0; i < NVCPU; i++)
            if (!atomic_load(&cpus[i].halted_clean) && !atomic_load(&cpus[i].has_abort)) live = 1;
        if (!live || atomic_load(&g_stop)) break;
        if (now_ns() > deadline) { timed_out = 1; atomic_store(&g_stop, 1); break; }
        struct timespec ts = { 0, 5000000 };
        nanosleep(&ts, NULL);
    }
    for (int i = 0; i < NVCPU; i++) pthread_join(th[i], NULL);
    atomic_store(&dev_timer_stop, 1);
    pthread_join(tt, NULL);
    uint64_t wall = now_ns() - t0;

    uint64_t timer_pending =
        ((atomic_load(&cpus[0].irr) & 1u) ? 1u : 0u) +
        ((atomic_load(&cpus[0].isr) == atomic_load(&dev_timer_vector)) ? 1u : 0u);

    FILE *f = out ? fopen(out, "w") : stdout;
    if (!f) { perror("open output"); return 2; }
    fprintf(f, "{\n");
    fprintf(f, "  \"record\": \"INV-01-12\",\n");
    fprintf(f, "  \"question\": \"Q-01-12\",\n");
    fprintf(f, "  \"injection\": \"%s\",\n", injection);
    fprintf(f, "  \"jit_mapping\": \"%s\",\n", jit_note);
    fprintf(f, "  \"nvcpu\": %d,\n", NVCPU);
    fprintf(f, "  \"timer_period_ns\": %llu,\n", TIMER_PERIOD_NS);
    fprintf(f, "  \"timed_out\": %s,\n", timed_out ? "true" : "false");
    fprintf(f, "  \"wall_ns\": %llu,\n", wall);
    fprintf(f, "  \"abort\": ");
    if (atomic_load(&cpus[0].has_abort)) fprintf(f, "\"vcpu0: %s\",\n", cpus[0].abort_buf);
    else if (atomic_load(&cpus[1].has_abort)) fprintf(f, "\"vcpu1: %s\",\n", cpus[1].abort_buf);
    else fprintf(f, "null,\n");
    fprintf(f, "  \"host\": {\n");
    fprintf(f, "    \"timer_posted\": %llu,\n", atomic_load(&dev_timer_posted));
    fprintf(f, "    \"timer_pending_at_end\": %llu,\n", timer_pending);
    fprintf(f, "    \"ipi_sent\": %llu,\n", atomic_load(&dev_ipi_sent));
    fprintf(f, "    \"ipi_dropped\": %llu\n", atomic_load(&dev_ipi_dropped));
    fprintf(f, "  },\n");
    fprintf(f, "  \"vcpu\": [\n");
    for (int i = 0; i < NVCPU; i++) {
        cpu_t *c = &cpus[i];
        fprintf(f, "    {\"id\": %d, \"insns\": %llu, \"blocks\": %llu, \"translations\": %llu,"
                   " \"tlb_fills\": %llu, \"smc_flushes\": %llu, \"faults_raised\": %llu,"
                   " \"irq_delivered\": %llu, \"hlt_wakes\": %llu, \"halted_clean\": %s,"
                   " \"finished\": %s, \"nested_fault\": %s, \"mark_begin_ns\": %llu,"
                   " \"mark_end_ns\": %llu, \"cpu_ns\": %llu}%s\n",
                i, c->insns, c->blocks, c->translations, c->tlb_fills, c->smc_flushes,
                c->faults_raised, c->irq_delivered, c->hlt_wakes,
                atomic_load(&c->halted_clean) ? "true" : "false",
                c->finished ? "true" : "false", c->nested_fault ? "true" : "false",
                c->mark_begin, c->mark_end,
                c->cpu_end_ns > c->cpu_begin_ns ? c->cpu_end_ns - c->cpu_begin_ns : 0,
                i + 1 < NVCPU ? "," : "");
    }
    fprintf(f, "  ],\n");
    fprintf(f, "  \"guest\": {\n");
    static const char *names[] = {
        "counter", "mp_violations", "mp_timeouts", "pf_count", "ud_count",
        "gp_count", "timer_ticks", "timer_eois", "ipi_recv", "ipi_ack_timeouts",
        "hlt_wakeups", "cli_delta", "smc_before", "smc_after", "cas_ok",
        "cas_fail_rax", "test_a_val", "test_b_mem", "test_c_mem",
        "iters0", "iters1", "vcpu0_done", "vcpu1_done", "loop_k", "loop_n",
        "push_rsp_after", "push_val", "ad_a", "ad_b", "ad_c"
    };
    for (size_t i = 0; i < sizeof names / sizeof names[0]; i++)
        fprintf(f, "    \"%s\": %llu,\n", names[i], gread(names[i]));
    uint64_t flog = gsym("fault_log");
    fprintf(f, "    \"fault_log\": [\n");
    for (int i = 0; i < 4; i++)
        fprintf(f, "      {\"cr2\": %llu, \"err\": %llu, \"pushed_rip\": %llu, \"expected_rip\": %llu}%s\n",
                gread_at(flog + 32 * i), gread_at(flog + 32 * i + 8),
                gread_at(flog + 32 * i + 16), gread_at(flog + 32 * i + 24),
                i < 3 ? "," : "");
    fprintf(f, "    ]\n  }\n}\n");
    if (out) fclose(f);
    return (atomic_load(&cpus[0].has_abort) || atomic_load(&cpus[1].has_abort) || timed_out) ? 1 : 0;
}
