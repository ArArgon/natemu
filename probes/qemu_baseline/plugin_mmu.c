/*
 * plugin_mmu.c -- QEMU TCG plugin used as the measuring instrument for
 * docs/research/26-guest-rates-and-footprint.md.
 *
 * QEMU is a MEASURING INSTRUMENT here, not the design. What this plugin
 * observes is the *guest's architectural behaviour*: which guest-virtual pages
 * it touches, under which CR3, in which order; which physical frames it uses as
 * page tables and how often it writes to them; how often it loads CR3 or
 * executes INVLPG; and how many exceptions it takes and where they vector to.
 * All of those are properties of the guest, not of QEMU, and therefore transfer
 * to any x86-64 execution engine.
 *
 * On top of that observation it runs a SHADOW-MMU MODEL: for each guest address
 * space (identified by the PML4 physical frame in CR3) it maintains the set of
 * guest-virtual pages that would currently have a valid shadow stage-1
 * descriptor, and counts the faults our design would take:
 *
 *   fill_xlat  - translation fault: first touch of a page whose shadow PTE is
 *                invalid. Costs 146 ns (doc 16 item 4).
 *   fill_perm  - permission fault: first WRITE to a page whose shadow PTE is
 *                present but read-only, i.e. the x86 Dirty-bit / write-protect
 *                event. Costs 28 ns + fixup (doc 16 items 1, 17).
 *   ptwrite    - store landing in a frame currently used as a page table.
 *
 * Three shadow-retention policies are modelled simultaneously, because the
 * answer depends entirely on which one the design picks:
 *   POLICY A "keep"   : per-CR3 shadow tables, unbounded, never dropped.
 *   POLICY B "flush"  : one shadow table, wiped on every CR3 load.
 *   POLICY C "asid256": per-CR3 shadow tables, at most 256 live (the host's
 *                       real ASID budget, ID_AA64MMFR0_EL1.ASIDBits=0), LRU.
 *
 * Build:
 *   cc -O2 -shared -fPIC -o bin/plugin_mmu.so plugin_mmu.c \
 *      -I/opt/homebrew/include $(pkg-config --cflags glib-2.0) \
 *      -undefined dynamic_lookup
 * Use:
 *   qemu-system-x86_64 -plugin ./bin/plugin_mmu.so,out=/path/snap.txt ...
 *
 * SMP: this plugin keeps global unlocked state and is only valid at -smp 1
 * with tcg thread=single. The driver enforces that.
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <glib.h>
#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

/* ------------------------------------------------------------------ u64 set */
/* Open-addressing linear-probe set of uint64_t. Key 0 is reserved as EMPTY, so
 * every key is stored as key+1 internally. */
typedef struct {
    uint64_t *slot;
    size_t cap;      /* power of two */
    size_t n;
} U64Set;

static void set_init(U64Set *s, size_t cap)
{
    s->cap = cap; s->n = 0;
    s->slot = g_new0(uint64_t, cap);
}
static void set_free(U64Set *s)
{
    g_free(s->slot); s->slot = NULL; s->cap = 0; s->n = 0;
}
static inline uint64_t mix(uint64_t x)
{
    x ^= x >> 33; x *= 0xff51afd7ed558ccdULL;
    x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL;
    x ^= x >> 33; return x;
}
static void set_grow(U64Set *s);
/* returns 1 if newly inserted, 0 if already present */
static int set_add(U64Set *s, uint64_t key)
{
    uint64_t k = key + 1;
    size_t m = s->cap - 1, i = mix(k) & m;
    while (s->slot[i]) {
        if (s->slot[i] == k) return 0;
        i = (i + 1) & m;
    }
    s->slot[i] = k;
    if (++s->n * 10 > s->cap * 7) set_grow(s);
    return 1;
}
static int set_has(const U64Set *s, uint64_t key)
{
    uint64_t k = key + 1;
    size_t m = s->cap - 1, i = mix(k) & m;
    while (s->slot[i]) {
        if (s->slot[i] == k) return 1;
        i = (i + 1) & m;
    }
    return 0;
}
static void set_del(U64Set *s, uint64_t key)
{
    /* backward-shift deletion */
    uint64_t k = key + 1;
    size_t m = s->cap - 1, i = mix(k) & m, j;
    while (s->slot[i]) {
        if (s->slot[i] == k) break;
        i = (i + 1) & m;
    }
    if (!s->slot[i]) return;
    s->slot[i] = 0; s->n--;
    j = (i + 1) & m;
    while (s->slot[j]) {
        uint64_t kk = s->slot[j];
        size_t home = mix(kk) & m;
        /* can kk move back to i? */
        size_t dj = (j - home) & m, di = (i - home) & m;
        if (di <= dj) { s->slot[i] = kk; s->slot[j] = 0; i = j; }
        j = (j + 1) & m;
    }
}
static void set_grow(U64Set *s)
{
    size_t oc = s->cap;
    uint64_t *os = s->slot;
    s->cap = oc * 2; s->n = 0;
    s->slot = g_new0(uint64_t, s->cap);
    for (size_t i = 0; i < oc; i++)
        if (os[i]) set_add(s, os[i] - 1);
    g_free(os);
}

/* --------------------------------------------------------- address space (AS) */
typedef struct {
    uint64_t cr3;        /* PML4 physical frame << 12 */
    U64Set present;      /* guest-virtual pages with a valid shadow PTE */
    U64Set writable;     /* subset of present that is shadow-writable */
    uint64_t last_use;   /* LRU stamp */
    uint64_t fills;      /* translation faults charged to this AS */
    uint64_t activations;
    uint64_t peak_present;
} AS;

static AS *as_new(uint64_t cr3)
{
    AS *a = g_new0(AS, 1);
    a->cr3 = cr3;
    set_init(&a->present, 1024);
    set_init(&a->writable, 1024);
    return a;
}
static void as_free(void *p)
{
    AS *a = p;
    set_free(&a->present); set_free(&a->writable); g_free(a);
}

/* ------------------------------------------------------------------ counters */
static struct {
    uint64_t insns, tb_exec, tb_trans;
    uint64_t ld, st;
    uint64_t cr3_write, cr3_write_noflush, cr3_read, cr4_write;
    uint64_t invlpg, invlpg_addr_ok, invpcid, wbinvd, hltn;
    uint64_t excep, interrupt, hostcall;
    /* policy A: per-CR3 shadow tables kept forever */
    uint64_t fillA_xlat, fillA_perm;
    /* policy B: single shadow, flushed on every CR3 load */
    uint64_t fillB_xlat, fillB_perm;
    /* policy C: 256 live address spaces, LRU */
    uint64_t fillC_xlat, fillC_perm, asid_evict;
    /* page-table traffic */
    uint64_t ptwrite, ptwrite_l[5], ptwrite_unwalked, ptwrite_unwalked_zero;
    uint64_t ptwrite_narrow, ptwrite_noop, ptwrite_eff;
    uint64_t pt_frames_marked, walks, walk_entries;
    uint64_t shadow_inval_pages;
    /* footprint */
    uint64_t code_pages, phys_frames_touched;
} C;

/* Per-vCPU state. Everything else -- Aspaces, Cspaces, the policy-B shadow, ptframes,
 * the footprint sets -- is legitimately shared, because a shadow page table is a
 * translation of a guest page table and two vCPUs running the same process (same guest
 * CR3) share it. What is per-vCPU is only WHICH address space is currently active.
 * Getting this wrong was what confined the plugin to -smp 1: with one global cur_cr3
 * every vCPU's memory accesses were attributed to whichever vCPU wrote CR3 last.
 * Concurrency is still handled by -accel tcg,thread=single, which serialises all vCPUs
 * onto one host thread; this fixes the SEMANTICS, not a data race. */
#define MAX_VCPU 64
static uint64_t cur_cr3_v[MAX_VCPU];
static AS *Acur_v[MAX_VCPU];
static AS *Ccur_v[MAX_VCPU];
static unsigned n_vcpu = 1;

/* policy B state (single flat shadow) */
static U64Set Bpresent, Bwritable;
/* policy C state */
static GHashTable *Cspaces;         /* cr3 -> AS* */
static uint64_t Cstamp;
#define ASID_BUDGET 256

/* policy A state */
static GHashTable *Aspaces;

/* footprint sets */
static U64Set codepages;            /* (cr3frame<<36)|vpage of executed code */
static U64Set codepages_phys;       /* physical frames containing translated code */
static U64Set physframes;           /* distinct guest physical frames touched */
static U64Set shadow_l1, shadow_l2, shadow_l3;  /* (cr3frame<<28)|(va>>21 / >>30 / >>39) */

/* page-table frame map: frame -> level (1..4) */
static GHashTable *ptframes;        /* key = frame (phys>>12), value = PTNode* */

/* Stores landing in a frame that is not (yet) a known page table are invisible to
 * the ptwrite counter, because pt_find() misses and the store handler returns. That
 * is not a rare corner: Linux allocates a page, populates it with PTEs, and only
 * then links it into the hierarchy, so for fork+exec -- the workload doc 20's whole
 * argument rests on -- most PTE stores happen BEFORE the frame is walkable. This
 * table accumulates a per-frame count of those stores; when the frame is later
 * promoted to a PTNode the accumulated count is credited to ptwrite_unwalked, which
 * bounds the undercount instead of hiding it. ptwrite remains the lower bound and
 * ptwrite + ptwrite_unwalked the upper bound. */
static GHashTable *prewrites;       /* key = frame, value = (uint64_t) store count */

/* The value packs two 32-bit counts: high half = stores of a present-looking PTE,
 * low half = stores of zero. They must be separated, because they are semantically
 * different events. A fresh page-table page is zeroed by clear_page() before it is
 * populated -- 512 zero stores per level -- and measurement shows that zeroing
 * dominates: boot logs ~1964 pre-promotion stores per address space, which is 3.8x
 * 512, i.e. about one clear_page for each of the four paging levels. A shadow design
 * that write-protects guest page tables traps on those zero stores; a lazy design
 * (doc 20) ignores them entirely. Folding the two together would overstate the PTE
 * update rate by more than an order of magnitude. */
static void prewrite_bump(uint64_t frame, int is_zero)
{
    if (!prewrites) return;
    gpointer k = GSIZE_TO_POINTER((gsize)frame);
    gsize v = GPOINTER_TO_SIZE(g_hash_table_lookup(prewrites, k));
    uint64_t zero = v & 0xffffffffULL, pte = v >> 32;
    if (is_zero) zero++; else pte++;
    g_hash_table_insert(prewrites, k, GSIZE_TO_POINTER((gsize)((pte << 32) | zero)));
}

/* Credit (and clear) a frame's pre-promotion stores. Called when the frame becomes a
 * PTNode. Clearing matters: a frame that is freed and recycled must not carry its
 * previous life's stores into the next one. */
static void prewrite_take(uint64_t frame, uint64_t *pte, uint64_t *zero)
{
    *pte = *zero = 0;
    if (!prewrites) return;
    gpointer k = GSIZE_TO_POINTER((gsize)frame);
    gpointer v = g_hash_table_lookup(prewrites, k);
    if (!v) return;
    g_hash_table_remove(prewrites, k);
    gsize x = GPOINTER_TO_SIZE(v);
    *zero = x & 0xffffffffULL; *pte = x >> 32;
}

/* cur_cr3 is now per-vCPU: see cur_cr3_v[] above. */
static uint64_t pt_nodes_live, pt_nodes_peak, pt_read_fail;
static uint64_t hw_check_n, hw_check_bad;
static int narrow_dbg;
static GHashTable *rootset;
static uint64_t root_retire, root_stale;

/* ------------------------------------------------------------------ output */
static FILE *outf;
static uint64_t t0_us;
static uint64_t snap_every_us = 250000;
static uint64_t next_snap_us;
static int enable_pt = 1, enable_shadow = 1;
/* Upper bound on guest physical addresses, used only by the PTE-shaped heuristic in
 * the store handler. runmmu.py runs the guest with -m 3072, so 4 GiB is a safe cap;
 * it only has to be tight enough to reject most data values, not to be exact. */
static int ram_mb = 4096;

static uint64_t now_us(void)
{
    struct timeval tv; gettimeofday(&tv, NULL);
    return (uint64_t)tv.tv_sec * 1000000ULL + tv.tv_usec;
}

static void sync_counts(void);

static void emit(const char *tag)
{
    sync_counts();
    uint64_t t = now_us();
    fprintf(outf,
        "SNAP tag=%s t_epoch_us=%" PRIu64 " t_rel_s=%.6f"
        " insns=%" PRIu64 " tb_exec=%" PRIu64 " tb_trans=%" PRIu64
        " ld=%" PRIu64 " st=%" PRIu64
        " cr3w=%" PRIu64 " cr3w_noflush=%" PRIu64 " cr3r=%" PRIu64 " cr4w=%" PRIu64
        " invlpg=%" PRIu64 " invlpg_addr_ok=%" PRIu64 " invpcid=%" PRIu64
        " wbinvd=%" PRIu64 " hlt=%" PRIu64
        " excep=%" PRIu64 " intr=%" PRIu64
        " fillA_xlat=%" PRIu64 " fillA_perm=%" PRIu64
        " fillB_xlat=%" PRIu64 " fillB_perm=%" PRIu64
        " fillC_xlat=%" PRIu64 " fillC_perm=%" PRIu64 " asid_evict=%" PRIu64
        " ptwrite=%" PRIu64 " ptw1=%" PRIu64 " ptw2=%" PRIu64 " ptw3=%" PRIu64 " ptw4=%" PRIu64
        " ptwrite_unwalked=%" PRIu64 " ptwrite_unwalked_zero=%" PRIu64
        " ptw_narrow=%" PRIu64
        " ptw_noop=%" PRIu64 " ptw_eff=%" PRIu64 " pt_nodes_live=%" PRIu64
        " pt_nodes_peak=%" PRIu64 " pt_read_fail=%" PRIu64
        " hwchk=%" PRIu64 " hwchk_bad=%" PRIu64
        " root_retire=%" PRIu64 " root_stale=%" PRIu64 " roots=%u"
        " pt_frames=%u walks=%" PRIu64
        " shadow_inval=%" PRIu64
        " as_live_A=%u as_live_C=%u"
        " codepages=%zu physframes=%zu"
        " n_vcpu=%u shl1=%zu shl2=%zu shl3=%zu\n",
        tag, t, (t - t0_us) / 1e6,
        C.insns, C.tb_exec, C.tb_trans, C.ld, C.st,
        C.cr3_write, C.cr3_write_noflush, C.cr3_read, C.cr4_write,
        C.invlpg, C.invlpg_addr_ok, C.invpcid, C.wbinvd, C.hltn,
        C.excep, C.interrupt,
        C.fillA_xlat, C.fillA_perm, C.fillB_xlat, C.fillB_perm,
        C.fillC_xlat, C.fillC_perm, C.asid_evict,
        C.ptwrite, C.ptwrite_l[1], C.ptwrite_l[2], C.ptwrite_l[3], C.ptwrite_l[4],
        C.ptwrite_unwalked, C.ptwrite_unwalked_zero,
        C.ptwrite_narrow, C.ptwrite_noop, C.ptwrite_eff,
        pt_nodes_live, pt_nodes_peak, pt_read_fail, hw_check_n, hw_check_bad,
        root_retire, root_stale, rootset ? g_hash_table_size(rootset) : 0,
        ptframes ? g_hash_table_size(ptframes) : 0, C.walks,
        C.shadow_inval_pages,
        Aspaces ? g_hash_table_size(Aspaces) : 0,
        Cspaces ? g_hash_table_size(Cspaces) : 0,
        codepages.n, physframes.n,
        n_vcpu, shadow_l1.n, shadow_l2.n, shadow_l3.n);
    fflush(outf);
}

/* ------------------------------------------------------- register plumbing */
#define NGPR 16
static const char *gpr_name[NGPR] = {
    "rax","rcx","rdx","rbx","rsp","rbp","rsi","rdi",
    "r8","r9","r10","r11","r12","r13","r14","r15"
};
/* Register handles are PER-VCPU. Capturing them once from vCPU 0 and then calling
 * qemu_plugin_read_register() while another vCPU is current corrupted the guest: at
 * -smp 2 the kernel took a not-present supervisor write fault and panicked with
 * "Kernel stack is corrupted". That, not the shadow model, is what confined this
 * plugin to -smp 1. */
static struct qemu_plugin_register *gpr_handle[MAX_VCPU][NGPR];
static GByteArray *regbuf;

static int gpr_index(const char *nm)
{
    for (int i = 0; i < NGPR; i++) if (!strcmp(gpr_name[i], nm)) return i;
    return -1;
}
static int read_named(struct qemu_plugin_register *h, uint64_t *v)
{
    if (!h) return 0;
    g_byte_array_set_size(regbuf, 0);
    if (!qemu_plugin_read_register(h, regbuf)) return 0;
    if (regbuf->len < 8) return 0;
    memcpy(v, regbuf->data, 8);
    return 1;
}
static int read_gpr(unsigned vcpu, int idx, uint64_t *v)
{
    if (vcpu >= MAX_VCPU || idx < 0 || idx >= NGPR || !gpr_handle[vcpu][idx]) return 0;
    g_byte_array_set_size(regbuf, 0);
    if (!qemu_plugin_read_register(gpr_handle[vcpu][idx], regbuf)) return 0;
    if (regbuf->len < 8) return 0;
    memcpy(v, regbuf->data, 8);
    return 1;
}

/* ---------------------------------------------------------- page-table walk */
/*
 * A physical frame is "a page table" only while some live parent entry points
 * at it. Linux allocates and frees page-table pages constantly (every fork
 * builds a tree, every exit tears one down), so a monotonically-growing set of
 * "frames that were ever a page table" overcounts PT writes by more than an
 * order of magnitude -- freed frames get reused as ordinary data and every
 * store to them is then miscounted.
 *
 * So each live table is mirrored: we keep our own copy of its 512 entries and
 * a refcount of the parent entries pointing at it (kernel PUD/PMD tables are
 * shared by every process's PGD, so a plain tree would be wrong too). A store
 * that clears a parent entry drops the child's refcount and, at zero, retires
 * the subtree.
 */
#define PA_MASK 0x000ffffffffff000ULL
/* Present, not a 1 GiB/2 MiB leaf, and with the reserved bits 62:52 clear.
 * The reserved-bit test is what stops a garbage 64-bit value that happens to
 * have bit 0 set from being followed into a fictitious page table. */
#define IS_TABLE_ENTRY(v, lvl) \
    (((v) & 1) && !(((lvl) <= 3) && ((v) & (1ULL << 7))) \
     && (((v) & 0x7ff0000000000000ULL) == 0))
static GByteArray *pagebuf;
static int dbg_walks;
static int dbg_max;

typedef struct {
    int level;          /* 1 = PT (leaf table), 4 = PML4 */
    int refs;
    uint64_t frame;
    uint64_t va_base;
    uint64_t ents[512]; /* our mirror of the table's contents */
    uint64_t wr, wr_narrow, wr_eff;
} PTNode;

static void pt_ref(uint64_t frame, int level, uint64_t va_base, int *budget);
static void pt_unref(uint64_t frame, int *budget);

static PTNode *pt_find(uint64_t frame)
{
    return g_hash_table_lookup(ptframes, &frame);
}
static int pt_level(uint64_t frame)
{
    PTNode *n = pt_find(frame);
    return n ? n->level : 0;
}

static void pt_ref(uint64_t frame, int level, uint64_t va_base, int *budget)
{
    if (level < 1 || level > 4 || !frame || *budget <= 0) return;
    PTNode *n = pt_find(frame);
    if (n) { n->refs++; return; }
    (*budget)--;

    uint64_t ents[512];
    memset(ents, 0, sizeof ents);
    if (level >= 2) {
        /* A frame whose physical read fails is not RAM and cannot be a page
         * table; refusing to create the node keeps bogus frames from absorbing
         * stores and being reported as page-table writes. */
        g_byte_array_set_size(pagebuf, 0);
        int rc = qemu_plugin_read_memory_hwaddr(frame << 12, pagebuf, 4096);
        if (rc != QEMU_PLUGIN_HWADDR_OPERATION_OK || pagebuf->len < 4096) {
            pt_read_fail++;
            return;
        }
        memcpy(ents, pagebuf->data, 4096);
    }
    n = g_new0(PTNode, 1);
    n->level = level; n->refs = 1; n->frame = frame; n->va_base = va_base;
    memcpy(n->ents, ents, sizeof ents);
    uint64_t *k = g_new(uint64_t, 1); *k = frame;
    g_hash_table_insert(ptframes, k, n);
    {
        uint64_t upte, uzero;
        prewrite_take(frame, &upte, &uzero);
        C.ptwrite_unwalked += upte;
        C.ptwrite_unwalked_zero += uzero;
    }
    C.pt_frames_marked++;
    pt_nodes_live++;
    if (pt_nodes_live > pt_nodes_peak) pt_nodes_peak = pt_nodes_live;
    if (level == 1) return;

    int shift = 12 + 9 * (level - 1);
    for (int i = 0; i < 512; i++) {
        uint64_t v = ents[i];
        C.walk_entries++;
        if (!IS_TABLE_ENTRY(v, level)) continue;
        uint64_t nb = va_base + ((uint64_t)i << shift);
        if (level == 4 && i >= 256) nb |= 0xffff000000000000ULL;
        pt_ref((v & PA_MASK) >> 12, level - 1, nb, budget);
        if (*budget <= 0) return;
    }
}

static void pt_unref(uint64_t frame, int *budget)
{
    if (!frame || *budget <= 0) return;
    PTNode *n = pt_find(frame);
    if (!n) return;
    if (--n->refs > 0) return;
    (*budget)--;
    if (n->level >= 2) {
        for (int i = 0; i < 512; i++) {
            uint64_t v = n->ents[i];
            if (!IS_TABLE_ENTRY(v, n->level)) continue;
            pt_unref((v & PA_MASK) >> 12, budget);
            if (*budget <= 0) break;
        }
    }
    pt_nodes_live--;
    g_hash_table_remove(ptframes, &frame);   /* frees the node */
}

/* Roots: one reference per CR3 value we have seen loaded. The guest never tells
 * us that a process exited, so roots are retired LRU-style once more than
 * ROOT_BUDGET of them are live. */
/*
 * Roots. The guest never tells us a process exited, so a PML4 frame we rooted
 * can be freed and reused as ordinary memory behind our back -- and then every
 * store to it would be miscounted as a page-table write. Two defences:
 *   (1) a small activation-LRU, so a PGD that stops being used is retired;
 *   (2) re-validation on every activation -- a live x86-64 Linux PGD always has
 *       a few dozen present kernel-half entries, so a frame that no longer does
 *       is not a PGD any more.
 */
#define ROOT_BUDGET 256
#define ROOT_IDLE_LOADS 1024   /* CR3 loads before an unused root is retired */
static GQueue *rootq;                 /* frames, most-recent at head */

static GHashTable *root_lastuse;      /* frame -> CR3-load ordinal, as a value */

static int root_looks_like_pgd(uint64_t frame)
{
    g_byte_array_set_size(pagebuf, 0);
    if (qemu_plugin_read_memory_hwaddr(frame << 12, pagebuf, 4096)
        != QEMU_PLUGIN_HWADDR_OPERATION_OK || pagebuf->len < 4096) return 0;
    uint64_t *e = (uint64_t *)pagebuf->data;
    int hi = 0;
    for (int i = 256; i < 512; i++) if (IS_TABLE_ENTRY(e[i], 4)) hi++;
    return hi >= 8;
}

static void root_drop(uint64_t frame)
{
    GList *l = g_hash_table_lookup(rootset, &frame);
    if (l) g_queue_delete_link(rootq, l);
    g_hash_table_remove(rootset, &frame);
    g_hash_table_remove(root_lastuse, &frame);
    int b = 400000;
    pt_unref(frame, &b);
    root_retire++;
}

/* Force-retire a frame that has demonstrably stopped being a page table. */
static void root_drop(uint64_t frame);
static void pt_retire(uint64_t frame)
{
    PTNode *n = pt_find(frame);
    if (!n) return;
    if (n->level == 4 && g_hash_table_contains(rootset, &frame)) {
        root_drop(frame);
        return;
    }
    n->refs = 1;
    int b = 400000;
    pt_unref(frame, &b);
}

/* Retire roots that have not been activated recently. A process's PGD frame is
 * freed at exit and reused as ordinary memory, and the page allocator does not
 * zero it -- so the "does it still look like a PGD?" test keeps passing on a
 * dead PGD and the frame silently absorbs unrelated stores. Recency is the only
 * signal we have: a live address space gets its CR3 loaded, a dead one never
 * does again. */
static void root_sweep(void)
{
    GList *stale = NULL;
    GHashTableIter it; gpointer k, v;
    g_hash_table_iter_init(&it, root_lastuse);
    while (g_hash_table_iter_next(&it, &k, &v)) {
        if (C.cr3_write - GPOINTER_TO_SIZE(v) > ROOT_IDLE_LOADS)
            stale = g_list_prepend(stale, GSIZE_TO_POINTER(*(uint64_t *)k));
    }
    for (GList *q = stale; q; q = q->next) {
        uint64_t f = GPOINTER_TO_SIZE(q->data);
        g_hash_table_remove(root_lastuse, &f);
        root_drop(f);
    }
    g_list_free(stale);
}

static void root_touch(uint64_t frame)
{
    uint64_t *k = g_new(uint64_t, 1); *k = frame;
    g_hash_table_replace(root_lastuse, k, GSIZE_TO_POINTER(C.cr3_write));
}

static void walk_cr3(uint64_t cr3)
{
    uint64_t frame = (cr3 & PA_MASK) >> 12;
    if (!frame) return;
    if ((C.cr3_write & 0xff) == 0) root_sweep();
    GList *l = g_hash_table_lookup(rootset, &frame);
    if (l) {
        if (root_looks_like_pgd(frame)) {          /* still live: touch the LRU */
            g_queue_unlink(rootq, l);
            g_queue_push_head_link(rootq, l);
            root_touch(frame);
            return;
        }
        root_stale++;
        root_drop(frame);                          /* recycled behind our back */
        return;
    }
    if (!root_looks_like_pgd(frame)) return;
    int budget = 400000;
    C.walks++;
    pt_ref(frame, 4, 0, &budget);
    g_queue_push_head(rootq, GSIZE_TO_POINTER(frame));
    uint64_t *k = g_new(uint64_t, 1); *k = frame;
    g_hash_table_insert(rootset, k, g_queue_peek_head_link(rootq));
    root_touch(frame);
    while (g_queue_get_length(rootq) > ROOT_BUDGET)
        root_drop(GPOINTER_TO_SIZE(g_queue_peek_tail(rootq)));
}

/* ------------------------------------------------------- shadow-MMU model */
/* key packing: address spaces are per-AS objects so the key is just the page */
#define VPG(v) ((v) >> 12)

static AS *as_get(GHashTable *t, uint64_t cr3)
{
    AS *a = g_hash_table_lookup(t, &cr3);
    if (!a) {
        a = as_new(cr3);
        uint64_t *k = g_new(uint64_t, 1); *k = cr3;
        g_hash_table_insert(t, k, a);
    }
    return a;
}

static void evict_lru(void)
{
    GHashTableIter it; gpointer k, v;
    AS *victim = NULL; uint64_t best = UINT64_MAX; gpointer vkey = NULL;
    g_hash_table_iter_init(&it, Cspaces);
    while (g_hash_table_iter_next(&it, &k, &v)) {
        AS *a = v;
        int in_use = 0;
        for (unsigned q = 0; q < n_vcpu; q++) if (a == Ccur_v[q]) in_use = 1;
        if (in_use) continue;
        if (a->last_use < best) { best = a->last_use; victim = a; vkey = k; }
    }
    if (victim) { g_hash_table_remove(Cspaces, vkey); C.asid_evict++; }
}

static void set_cr3(unsigned vcpu, uint64_t cr3)
{
    if (vcpu >= MAX_VCPU) return;
    uint64_t c3 = cr3 & PA_MASK;
    cur_cr3_v[vcpu] = c3;
    if (vcpu >= n_vcpu) n_vcpu = vcpu + 1;
    if (enable_shadow) {
        Acur_v[vcpu] = as_get(Aspaces, c3);
        Acur_v[vcpu]->last_use = ++Cstamp; Acur_v[vcpu]->activations++;
        /* policy B: whole shadow dies. One flat shadow shared by all vCPUs, so any
         * vCPU's CR3 load wipes it -- which is exactly what policy B means. */
        set_free(&Bpresent); set_init(&Bpresent, 1024);
        set_free(&Bwritable); set_init(&Bwritable, 1024);
        /* policy C: LRU over 256 live spaces */
        Ccur_v[vcpu] = as_get(Cspaces, c3);
        Ccur_v[vcpu]->last_use = Cstamp;
        while (g_hash_table_size(Cspaces) > ASID_BUDGET) evict_lru();
    }
    if (enable_pt) walk_cr3(c3);
}

/* touch: is_write, is_exec */
static inline void touch(unsigned vcpu, uint64_t vaddr, int is_write)
{
    if (!enable_shadow || vcpu >= MAX_VCPU) return;
    AS *Acur = Acur_v[vcpu], *Ccur = Ccur_v[vcpu];
    uint64_t cur_cr3 = cur_cr3_v[vcpu];
    uint64_t p = VPG(vaddr);
    /* policy A */
    if (Acur) {
        if (set_add(&Acur->present, p)) { C.fillA_xlat++; Acur->fills++; }
        if (is_write && set_add(&Acur->writable, p)) C.fillA_perm++;
        if (Acur->present.n > Acur->peak_present) Acur->peak_present = Acur->present.n;
    }
    /* policy B */
    if (set_add(&Bpresent, p)) C.fillB_xlat++;
    if (is_write && set_add(&Bwritable, p)) C.fillB_perm++;
    /* policy C */
    if (Ccur) {
        if (set_add(&Ccur->present, p)) C.fillC_xlat++;
        if (is_write && set_add(&Ccur->writable, p)) C.fillC_perm++;
    }
    /* shadow table footprint: how many distinct L1/L2/L3 shadow tables exist */
    uint64_t c = cur_cr3 >> 12;
    set_add(&shadow_l1, (c << 36) ^ (vaddr >> 21));
    set_add(&shadow_l2, (c << 36) ^ (vaddr >> 30));
    set_add(&shadow_l3, (c << 36) ^ (vaddr >> 39));
}

static void shadow_invalidate(unsigned vcpu, uint64_t vaddr)
{
    if (vcpu >= MAX_VCPU) return;
    AS *Acur = Acur_v[vcpu], *Ccur = Ccur_v[vcpu];
    uint64_t p = VPG(vaddr);
    if (Acur) { set_del(&Acur->present, p); set_del(&Acur->writable, p); }
    set_del(&Bpresent, p); set_del(&Bwritable, p);
    if (Ccur) { set_del(&Ccur->present, p); set_del(&Ccur->writable, p); }
    C.shadow_inval_pages++;
}

/* ------------------------------------------------------------- callbacks */
static void cb_mem(unsigned int vcpu, qemu_plugin_meminfo_t info,
                   uint64_t vaddr, void *ud)
{
    int is_st = qemu_plugin_mem_is_store(info);
    if (is_st) C.st++; else C.ld++;
    touch(vcpu, vaddr, is_st);

    if (!enable_pt) return;
    struct qemu_plugin_hwaddr *h = qemu_plugin_get_hwaddr(info, vaddr);
    if (!h || qemu_plugin_hwaddr_is_io(h)) return;
    uint64_t pa = qemu_plugin_hwaddr_phys_addr(h);
    uint64_t frame = pa >> 12;
    set_add(&physframes, frame);
    if (!is_st) return;

    PTNode *n = pt_find(frame);
    if (!n) {
        /* Not a known page table. Before paying for a hash insert on what is
         * usually just a data store, apply the cheapest filter that a PTE
         * population write passes and ordinary data mostly does not: an aligned
         * 64-bit store whose value is either zero (clear_page) or has P=1 with an
         * output address inside guest RAM and no reserved bits set. This is a
         * heuristic and it is reported as a bound, not as a measurement. */
        if ((pa & 7) == 0) {
            qemu_plugin_mem_value pv = qemu_plugin_mem_get_value(info);
            if (pv.type == QEMU_PLUGIN_MEM_VALUE_U64) {
                uint64_t v = pv.data.u64;
                uint64_t oa = v & 0x000ffffffffff000ULL;
                if (v == 0)
                    prewrite_bump(frame, 1);
                else if ((v & 1) && oa && oa < ((uint64_t)ram_mb << 20))
                    prewrite_bump(frame, 0);
            }
        }
        return;
    }
    int lvl = n->level;

    qemu_plugin_mem_value val = qemu_plugin_mem_get_value(info);
    if (val.type != QEMU_PLUGIN_MEM_VALUE_U64) {
        /* Page-table entries are only ever written with aligned 64-bit stores.
         * A sub-64-bit store to a frame we still believe is a page table means
         * the frame has been freed and is being reused -- in practice this is
         * clear_page() via `rep stosb` on a just-recycled PGD. Retiring the node
         * here is what keeps a dead page table from absorbing unrelated stores
         * and inflating the page-table write rate by 10x. */
        C.ptwrite_narrow++;
        pt_retire(frame);
        return;
    }
    C.ptwrite++;
    C.ptwrite_l[lvl]++;
    n->wr++;
    uint64_t nv = val.data.u64;
    int idx = (pa & 0xfff) >> 3;
    uint64_t ov = n->ents[idx];
    if (ov == nv) { C.ptwrite_noop++; return; }
    C.ptwrite_eff++;
    n->wr_eff++;
    if (hw_check_n < 64) {
        /* Validate that the physical address the plugin API hands us is the one
         * the guest actually stored to: re-read those 8 bytes by hwaddr and
         * compare. If this ever disagrees the whole PT-write measurement is
         * void, so it is checked rather than assumed. */
        g_byte_array_set_size(pagebuf, 0);
        if (qemu_plugin_read_memory_hwaddr(pa, pagebuf, 8)
            == QEMU_PLUGIN_HWADDR_OPERATION_OK && pagebuf->len >= 8) {
            uint64_t back; memcpy(&back, pagebuf->data, 8);
            hw_check_n++;
            if (back != nv) hw_check_bad++;
        }
    }

    int budget = 200000;
    if (lvl >= 2) {
        int shift = 12 + 9 * (lvl - 1);
        uint64_t nb = n->va_base + ((uint64_t)idx << shift);
        if (lvl == 4 && idx >= 256) nb |= 0xffff000000000000ULL;
        int old_tbl = IS_TABLE_ENTRY(ov, lvl);
        int new_tbl = IS_TABLE_ENTRY(nv, lvl);
        uint64_t of = (ov & PA_MASK) >> 12, nf = (nv & PA_MASK) >> 12;
        n->ents[idx] = nv;          /* update before recursing */
        if (old_tbl && !(new_tbl && nf == of)) pt_unref(of, &budget);
        if (new_tbl && !(old_tbl && nf == of)) pt_ref(nf, lvl - 1, nb, &budget);
    } else {
        n->ents[idx] = nv;
        /* a leaf PTE changed: the shadow entry for that VA is now stale and a
         * real design must invalidate it (and pay a TLBI if it was valid). */
        shadow_invalidate(vcpu, n->va_base + ((uint64_t)idx << 12));
    }
}

static void cb_tb_exec(unsigned int vcpu, void *ud)
{
    uint64_t pc = (uint64_t)(uintptr_t)ud;
    touch(vcpu, pc, 0);
    if ((C.tb_exec & 0xfffff) == 0) {
        uint64_t t = now_us();
        if (t >= next_snap_us) { emit("periodic"); next_snap_us = t + snap_every_us; }
    }
}

static void cb_cr3_write(unsigned int vcpu, void *ud)
{
    int reg = (int)(intptr_t)ud;
    uint64_t v = 0;
    C.cr3_write++;
    if (read_gpr(vcpu, reg, &v)) {
        if (v & (1ULL << 63)) C.cr3_write_noflush++;
        set_cr3(vcpu, v);
    }
}

static void cb_invlpg(unsigned int vcpu, void *ud)
{
    int reg = (int)(intptr_t)ud;
    uint64_t v = 0;
    C.invlpg++;
    if (reg >= 0 && read_gpr(vcpu, reg, &v)) { C.invlpg_addr_ok++; shadow_invalidate(vcpu, v); }
}

static void cb_simple(unsigned int vcpu, void *ud)
{
    uint64_t *p = ud; (*p)++;
}

static GHashTable *excep_by_pc;
static struct qemu_plugin_register *rip_handle[MAX_VCPU];
static int read_named(struct qemu_plugin_register *h, uint64_t *v);

static void cb_discon(qemu_plugin_id_t id, unsigned int vcpu,
                      enum qemu_plugin_discon_type type,
                      uint64_t from_pc, uint64_t to_pc)
{
    if (type == QEMU_PLUGIN_DISCON_EXCEPTION) {
        C.excep++;
        /* The docs say the PC has already been updated to the trap vector when
         * this fires, so RIP is the IDT handler entry. That is how #PF gets
         * separated from every other exception -- QEMU does not hand the plugin
         * a vector number. */
        uint64_t rip = 0;
        if (vcpu < MAX_VCPU && rip_handle[vcpu] && read_named(rip_handle[vcpu], &rip)) to_pc = rip;
        /* bucket by handler PC: on x86-64 the IDT entry for #PF is a distinct
         * address, so this separates page faults from every other trap without
         * needing QEMU to tell us the vector number (it does not). */
        gpointer v;
        if (g_hash_table_lookup_extended(excep_by_pc, &to_pc, NULL, &v)) {
            g_hash_table_replace(excep_by_pc, g_memdup2(&to_pc, 8),
                                 GSIZE_TO_POINTER(GPOINTER_TO_SIZE(v) + 1));
        } else {
            g_hash_table_insert(excep_by_pc, g_memdup2(&to_pc, 8),
                                GSIZE_TO_POINTER(1));
        }
    } else if (type == QEMU_PLUGIN_DISCON_INTERRUPT) {
        C.interrupt++;
    } else {
        C.hostcall++;
    }
}

/* --------------------------------------------------------------- translate */
/* Parse "%rax" style register out of a disassembly fragment. */
static int parse_reg_at(const char *p)
{
    if (*p != '%') return -1;
    p++;
    char nm[8]; int i = 0;
    while (i < 7 && ((*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9'))) nm[i++] = *p++;
    nm[i] = 0;
    return gpr_index(nm);
}

/* AT&T order: "mov SRC, DST". @p points at the operand; it is the destination
 * if a comma appears before it in the string. */
static int is_dst(const char *d, const char *p)
{
    for (const char *q = p; q > d; q--) if (*q == ',') return 1;
    return 0;
}

static void vcpu_tb_trans(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    size_t n = qemu_plugin_tb_n_insns(tb);
    C.tb_trans++;

    qemu_plugin_register_vcpu_tb_exec_cb(tb, cb_tb_exec, QEMU_PLUGIN_CB_NO_REGS,
        (void *)(uintptr_t)qemu_plugin_insn_vaddr(qemu_plugin_tb_get_insn(tb, 0)));

    for (size_t i = 0; i < n; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        uint64_t va = qemu_plugin_insn_vaddr(insn);
        /* Translation time has no vCPU context. codepages is a footprint
         * diagnostic only, so vCPU 0's CR3 is a good enough key; at -smp > 1 it
         * under-counts distinct (AS, page) pairs and is not used in any cost model. */
        set_add(&codepages, ((cur_cr3_v[0] >> 12) << 36) ^ (va >> 12));

        qemu_plugin_register_vcpu_mem_cb(insn, cb_mem, QEMU_PLUGIN_CB_NO_REGS,
                                         QEMU_PLUGIN_MEM_RW, NULL);

        char *d = qemu_plugin_insn_disas(insn);
        if (!d) continue;
        const char *p;
        if ((p = strstr(d, "%cr3")) != NULL && is_dst(d, p)) {
            /* "movq  %rbp, %cr3" -- source register is the operand before it */
            const char *q = p;
            while (q > d && *q != ',') q--;
            while (q > d && *q != '%') q--;
            int r = (*q == '%') ? parse_reg_at(q) : -1;
            qemu_plugin_register_vcpu_insn_exec_cb(insn, cb_cr3_write,
                QEMU_PLUGIN_CB_R_REGS, (void *)(intptr_t)r);
        } else if (strstr(d, "%cr3") != NULL) {
            qemu_plugin_register_vcpu_insn_exec_cb(insn, cb_simple,
                QEMU_PLUGIN_CB_NO_REGS, &C.cr3_read);
        } else if (!strncmp(d, "invlpg", 6)) {
            const char *q = strchr(d, '(');
            int r = q ? parse_reg_at(q + 1) : -1;
            qemu_plugin_register_vcpu_insn_exec_cb(insn, cb_invlpg,
                QEMU_PLUGIN_CB_R_REGS, (void *)(intptr_t)r);
        } else if (!strncmp(d, "invpcid", 7)) {
            qemu_plugin_register_vcpu_insn_exec_cb(insn, cb_simple,
                QEMU_PLUGIN_CB_NO_REGS, &C.invpcid);
        } else if ((p = strstr(d, "%cr4")) != NULL && is_dst(d, p)) {
            qemu_plugin_register_vcpu_insn_exec_cb(insn, cb_simple,
                QEMU_PLUGIN_CB_NO_REGS, &C.cr4_write);
        } else if (!strncmp(d, "wbinvd", 6)) {
            qemu_plugin_register_vcpu_insn_exec_cb(insn, cb_simple,
                QEMU_PLUGIN_CB_NO_REGS, &C.wbinvd);
        } else if (!strncmp(d, "hlt", 3)) {
            qemu_plugin_register_vcpu_insn_exec_cb(insn, cb_simple,
                QEMU_PLUGIN_CB_NO_REGS, &C.hltn);
        }
        g_free(d);
    }
}

/* real executed-instruction count via inline op on a scoreboard */
typedef struct { uint64_t insns; uint64_t tbs; } SB;
static struct qemu_plugin_scoreboard *sb;

static void vcpu_tb_trans2(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    size_t n = qemu_plugin_tb_n_insns(tb);
    qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(tb, QEMU_PLUGIN_INLINE_ADD_U64,
        qemu_plugin_scoreboard_u64_in_struct(sb, SB, insns), n);
    qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(tb, QEMU_PLUGIN_INLINE_ADD_U64,
        qemu_plugin_scoreboard_u64_in_struct(sb, SB, tbs), 1);
    vcpu_tb_trans(id, tb);
}

static void sync_counts(void)
{
    uint64_t insns = 0, tbs = 0;
    for (int i = 0; i < qemu_plugin_num_vcpus(); i++) {
        SB *s = qemu_plugin_scoreboard_find(sb, i);
        insns += s->insns; tbs += s->tbs;
    }
    C.insns = insns; C.tb_exec = tbs;
}

static void vcpu_init(qemu_plugin_id_t id, unsigned int vcpu)
{
    if (vcpu < MAX_VCPU && vcpu + 1 > n_vcpu) n_vcpu = vcpu + 1;
    GArray *regs = qemu_plugin_get_registers();
    if (!regs) return;
    fprintf(outf, "REGS n=%u:", regs->len);
    for (guint i = 0; i < regs->len; i++) {
        qemu_plugin_reg_descriptor *r =
            &g_array_index(regs, qemu_plugin_reg_descriptor, i);
        fprintf(outf, " %s", r->name);
        int idx = gpr_index(r->name);
        if (idx >= 0 && vcpu < MAX_VCPU) gpr_handle[vcpu][idx] = r->handle;
        if (!strcmp(r->name, "rip") && vcpu < MAX_VCPU) rip_handle[vcpu] = r->handle;
    }
    fprintf(outf, "\n"); fflush(outf);
    g_array_free(regs, TRUE);
}

static int ptnode_cmp(const void *a, const void *b)
{
    const PTNode *x = a, *y = b;
    return (x->wr < y->wr) - (x->wr > y->wr);
}

static void plugin_exit(qemu_plugin_id_t id, void *p)
{
    sync_counts();
    emit("final");
    /* per-exception-handler histogram, so #PF can be told from the rest */
    GHashTableIter it; gpointer k, v;
    g_hash_table_iter_init(&it, excep_by_pc);
    while (g_hash_table_iter_next(&it, &k, &v)) {
        fprintf(outf, "EXCEP_PC pc=0x%" PRIx64 " n=%zu\n",
                *(uint64_t *)k, GPOINTER_TO_SIZE(v));
    }
    /* per-address-space footprint, largest first */
    GList *l = g_hash_table_get_values(Aspaces);
    int i = 0;
    for (GList *q = l; q && i < 4000; q = q->next, i++) {
        AS *a = q->data;
        fprintf(outf, "AS cr3=0x%" PRIx64 " pages=%zu wpages=%zu fills=%" PRIu64
                      " activations=%" PRIu64 "\n",
                a->cr3, a->present.n, a->writable.n, a->fills, a->activations);
    }
    g_list_free(l);
    /* page-table frame level histogram */
    uint64_t lv[5] = {0};
    g_hash_table_iter_init(&it, ptframes);
    while (g_hash_table_iter_next(&it, &k, &v)) {
        int L = ((PTNode *)v)->level;
        if (L >= 1 && L <= 4) lv[L]++;
    }
    {
        GList *ln = g_hash_table_get_values(ptframes);
        GList *srt = g_list_sort(ln, (GCompareFunc)ptnode_cmp);
        int c = 0;
        for (GList *q = srt; q && c < 20; q = q->next, c++) {
            PTNode *nn = q->data;
            fprintf(outf, "PTTOP frame=0x%" PRIx64 " level=%d refs=%d"
                    " va_base=0x%" PRIx64 " wr=%" PRIu64 " narrow=%" PRIu64
                    " eff=%" PRIu64 "\n",
                    nn->frame, nn->level, nn->refs, nn->va_base,
                    nn->wr, nn->wr_narrow, nn->wr_eff);
        }
        g_list_free(srt);
    }
    fprintf(outf, "PTFRAMES l1=%" PRIu64 " l2=%" PRIu64 " l3=%" PRIu64
                  " l4=%" PRIu64 " total=%u walk_entries=%" PRIu64 "\n",
            lv[1], lv[2], lv[3], lv[4], g_hash_table_size(ptframes),
            C.walk_entries);
    fflush(outf);
    fclose(outf);
}

static guint u64_hash(gconstpointer p) { return (guint)mix(*(const uint64_t *)p); }
static gboolean u64_eq(gconstpointer a, gconstpointer b)
{ return *(const uint64_t *)a == *(const uint64_t *)b; }

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    const char *path = "/tmp/natemu-mmu.txt";
    for (int i = 0; i < argc; i++) {
        if (!strncmp(argv[i], "out=", 4)) path = argv[i] + 4;
        else if (!strncmp(argv[i], "pt=", 3)) enable_pt = atoi(argv[i] + 3);
        else if (!strncmp(argv[i], "shadow=", 7)) enable_shadow = atoi(argv[i] + 7);
        else if (!strncmp(argv[i], "snap_ms=", 8)) snap_every_us = atoll(argv[i] + 8) * 1000;
        else if (!strncmp(argv[i], "dbg=", 4)) dbg_max = atoi(argv[i] + 4);
    }
    outf = fopen(path, "w");
    if (!outf) outf = stderr;
    t0_us = now_us(); next_snap_us = t0_us;
    fprintf(outf, "PLUGIN plugin_mmu out=%s pt=%d shadow=%d t0_epoch_us=%" PRIu64 "\n",
            path, enable_pt, enable_shadow, t0_us);
    fflush(outf);

    regbuf = g_byte_array_new();
    pagebuf = g_byte_array_new();
    ptframes = g_hash_table_new_full(u64_hash, u64_eq, g_free, g_free);
    prewrites = g_hash_table_new(g_direct_hash, g_direct_equal);
    rootset  = g_hash_table_new_full(u64_hash, u64_eq, g_free, NULL);
    rootq    = g_queue_new();
    root_lastuse = g_hash_table_new_full(u64_hash, u64_eq, g_free, NULL);
    excep_by_pc = g_hash_table_new_full(u64_hash, u64_eq, g_free, NULL);
    Aspaces = g_hash_table_new_full(u64_hash, u64_eq, g_free, as_free);
    Cspaces = g_hash_table_new_full(u64_hash, u64_eq, g_free, as_free);
    set_init(&Bpresent, 1024); set_init(&Bwritable, 1024);
    set_init(&codepages, 4096); set_init(&codepages_phys, 4096);
    set_init(&physframes, 65536);
    set_init(&shadow_l1, 4096); set_init(&shadow_l2, 1024); set_init(&shadow_l3, 256);

    sb = qemu_plugin_scoreboard_new(sizeof(SB));
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init);
    qemu_plugin_register_vcpu_tb_trans_cb(id, vcpu_tb_trans2);
    qemu_plugin_register_vcpu_discon_cb(id, QEMU_PLUGIN_DISCON_ALL, cb_discon);
    qemu_plugin_register_atexit_cb(id, plugin_exit, NULL);
    return 0;
}
