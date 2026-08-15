/*
 * plugin_rates.c -- QEMU TCG plugin measuring the guest events a shadow-MMU
 * design cares about, for docs/research/26.
 *
 * What it counts, per run, for an x86-64 system guest:
 *
 *   guest_insns, guest_tb_exec, guest_mem_ops   (same as qemu_baseline's
 *                                                plugin_count.c, for cross-check)
 *   cr3_writes      -- MOV to CR3, i.e. address-space switches / TLB flushes
 *   invlpg, invpcid -- explicit guest TLB invalidations
 *   distinct_cr3    -- number of distinct CR3 values ever observed
 *
 * and, the point of the exercise, two bounds on how many times OUR shadow
 * page-table walker would miss:
 *
 *   U_hi_all   sum over CR3 epochs of |distinct 4 KiB pages touched in epoch|
 *              == every CR3 write throws away the whole shadow tree.
 *              Strict upper bound on shadow misses for a per-CR3 shadow with
 *              no cross-switch retention.
 *
 *   U_hi_user  same, but pages in the kernel half of the canonical address
 *              space are charged only once globally (they live in the shared
 *              kernel subtree that every address space maps to the same
 *              tables; see doc 19 on shared page tables).  This is the upper
 *              bound for the design we would actually build.
 *
 *   U_lo       |distinct (CR3, user page)| + |distinct kernel page|
 *              == shadow trees are cached per address space forever and never
 *              invalidated.  Lower bound on shadow misses (you must fault at
 *              least once per (address space, page) pair to fill it).
 *
 * A page is charged when it is touched by a data access (mem callback) or when
 * a TB whose first byte lies in it is executed (instruction fetch).  A TB that
 * spans a page boundary charges only its first page, so code-page counts are a
 * slight underestimate; cross-page TBs are 0% of TBs per `info jit`.
 *
 * Single-vCPU only (-smp 1, tcg thread=single): no locking is done.
 *
 * Build: ./build_plugins.sh
 * Use:   qemu-system-x86_64 -plugin ./bin/plugin_rates.so -d plugin -D out.txt
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <glib.h>
#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

/* ------------------------------------------------------------------ hashset */
/* open-addressed, linear probe, power-of-two, 0 is a legal key (flagged) */
typedef struct {
    uint64_t *k;
    size_t cap;      /* power of two */
    size_t n;        /* live entries, excluding the zero key */
    int has_zero;
} Set;

static void set_init(Set *s, size_t cap)
{
    s->cap = cap;
    s->k = calloc(cap, sizeof(uint64_t));
    if (!s->k) { fprintf(stderr, "plugin_rates: OOM\n"); abort(); }
    s->n = 0;
    s->has_zero = 0;
}

static void set_clear(Set *s)
{
    memset(s->k, 0, s->cap * sizeof(uint64_t));
    s->n = 0;
    s->has_zero = 0;
}

static inline uint64_t mix64(uint64_t x)
{
    x ^= x >> 33; x *= 0xff51afd7ed558ccdULL;
    x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL;
    x ^= x >> 33;
    return x;
}

static int set_insert(Set *s, uint64_t key);

static void set_grow(Set *s)
{
    uint64_t *old = s->k;
    size_t oldcap = s->cap;
    s->cap *= 2;
    s->k = calloc(s->cap, sizeof(uint64_t));
    if (!s->k) { fprintf(stderr, "plugin_rates: OOM (grow to %zu)\n", s->cap); abort(); }
    s->n = 0;
    for (size_t i = 0; i < oldcap; i++) {
        if (old[i]) { set_insert(s, old[i]); }
    }
    free(old);
}

/* returns 1 if newly inserted, 0 if already present */
static int set_insert(Set *s, uint64_t key)
{
    if (key == 0) {
        if (s->has_zero) return 0;
        s->has_zero = 1;
        return 1;
    }
    if ((s->n + 1) * 10 >= s->cap * 7) {
        set_grow(s);
    }
    size_t m = s->cap - 1;
    size_t i = mix64(key) & m;
    while (s->k[i]) {
        if (s->k[i] == key) return 0;
        i = (i + 1) & m;
    }
    s->k[i] = key;
    s->n++;
    return 1;
}

static size_t set_count(const Set *s) { return s->n + (size_t)s->has_zero; }

/* ------------------------------------------------------------------ state */
static struct qemu_plugin_register *cr3_handle;
static GByteArray *regbuf;

static Set epoch;      /* pages touched in the current CR3 epoch  */
static Set glob;       /* (cr3,user page) and (0,kernel page) ever */
static Set kglob;      /* distinct kernel pages ever              */
static Set cr3set;     /* distinct CR3 values                     */

static uint64_t c_mem;
static uint64_t c_cr3w, c_invlpg, c_invpcid, c_cr3_insn_exec;
static uint64_t u_hi_all, u_hi_user;
static uint64_t epoch_pages_user;   /* user pages in the current epoch */
static uint64_t epochs;
static uint64_t new_from_mem, new_from_code;

static uint64_t last_mem_page = ~0ULL, last_code_page = ~0ULL;
static uint64_t last_cr3_seen;

/* canonical-hole test: x86-64 kernel half has bit 47 set (and 63) */
static inline int is_kernel_page(uint64_t page)
{
    return (page >> (47 - 12)) != 0;   /* vaddr bit 47 */
}

static inline void touch(uint64_t page, int from_code)
{
    if (set_insert(&epoch, page)) {
        if (!is_kernel_page(page)) {
            epoch_pages_user++;
        }
        if (from_code) new_from_code++; else new_from_mem++;
    }
}

static void close_epoch(uint64_t cr3)
{
    uint64_t pfn = cr3 & 0x000ffffffffff000ULL;   /* strip PCID / flags */
    set_insert(&cr3set, pfn);
    u_hi_all  += set_count(&epoch);
    u_hi_user += epoch_pages_user;

    if (epoch.has_zero) {
        set_insert(&glob, mix64(pfn) ^ 0);
    }
    for (size_t i = 0; i < epoch.cap; i++) {
        uint64_t p = epoch.k[i];
        if (!p) continue;
        if (is_kernel_page(p)) {
            set_insert(&kglob, p);
        } else {
            set_insert(&glob, mix64(pfn) ^ (p * 0x9e3779b97f4a7c15ULL));
        }
    }
    set_clear(&epoch);
    epoch_pages_user = 0;
    last_mem_page = ~0ULL;
    last_code_page = ~0ULL;
    epochs++;
}

/* ------------------------------------------------------------------ cbs */
static void cb_mem(unsigned int vcpu, qemu_plugin_meminfo_t info,
                   uint64_t vaddr, void *ud)
{
    c_mem++;
    uint64_t page = vaddr >> 12;
    if (page == last_mem_page) return;
    last_mem_page = page;
    touch(page, 0);
}

static void cb_tb(unsigned int vcpu, void *ud)
{
    uint64_t page = (uint64_t)(uintptr_t)ud;
    if (page == last_code_page) return;
    last_code_page = page;
    touch(page, 1);
}

static uint64_t read_cr3(void)
{
    if (!cr3_handle) return 0;
    g_byte_array_set_size(regbuf, 0);
    int n = qemu_plugin_read_register(cr3_handle, regbuf);
    uint64_t v = 0;
    for (int i = 0; i < n && i < 8; i++) {
        v |= (uint64_t)regbuf->data[i] << (8 * i);
    }
    return v;
}

/* fires BEFORE the MOV to CR3 executes, so the CR3 we read is the one that was
 * live for the epoch that is ending -- exactly what we want to attribute it to */
static void cb_cr3w(unsigned int vcpu, void *ud)
{
    uint64_t v = read_cr3();
    c_cr3_insn_exec++;
    last_cr3_seen = v;
    close_epoch(v);
}

static void cb_invlpg(unsigned int vcpu, void *ud)  { c_invlpg++; }
static void cb_invpcid(unsigned int vcpu, void *ud) { c_invpcid++; }

/* ------------------------------------------------------------------ trans */
static GString *seen_cr3_disas;

static void vcpu_tb_trans(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    size_t n = qemu_plugin_tb_n_insns(tb);

    qemu_plugin_register_vcpu_tb_exec_cb(tb, cb_tb, QEMU_PLUGIN_CB_NO_REGS,
                                         (void *)(uintptr_t)(qemu_plugin_tb_vaddr(tb) >> 12));

    for (size_t i = 0; i < n; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        char *d = qemu_plugin_insn_disas(insn);
        if (d) {
            if (strstr(d, "invlpg")) {
                qemu_plugin_register_vcpu_insn_exec_cb(insn, cb_invlpg,
                                                       QEMU_PLUGIN_CB_NO_REGS, NULL);
            } else if (strstr(d, "invpcid")) {
                qemu_plugin_register_vcpu_insn_exec_cb(insn, cb_invpcid,
                                                       QEMU_PLUGIN_CB_NO_REGS, NULL);
            } else {
                char *p = strstr(d, "cr3");
                if (p) {
                    /* AT&T: "mov %rax,%cr3" is a write (cr3 is the last operand);
                     * "mov %cr3,%rax" is a read. Distinguish by position. */
                    size_t len = strlen(d);
                    while (len && (d[len-1] == ' ' || d[len-1] == '\n')) len--;
                    int is_write = ((size_t)(p - d) + 3 >= len);
                    if (is_write) {
                        c_cr3w++;   /* static count of translated write sites */
                        if (seen_cr3_disas->len < 4000) {
                            g_string_append_printf(seen_cr3_disas, "  CR3W-SITE %s\n", d);
                        }
                        qemu_plugin_register_vcpu_insn_exec_cb(insn, cb_cr3w,
                                                               QEMU_PLUGIN_CB_R_REGS, NULL);
                    } else if (seen_cr3_disas->len < 4000) {
                        g_string_append_printf(seen_cr3_disas, "  CR3R-SITE %s\n", d);
                    }
                }
            }
            g_free(d);
        }
    }
}

/* separate scoreboard-based counters so the hot counts stay cheap */
typedef struct { uint64_t insns; uint64_t tbs; } Cnt;
static struct qemu_plugin_scoreboard *sb;

static void vcpu_tb_trans2(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    size_t n = qemu_plugin_tb_n_insns(tb);
    qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(
        tb, QEMU_PLUGIN_INLINE_ADD_U64,
        qemu_plugin_scoreboard_u64_in_struct(sb, Cnt, tbs), 1);
    qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(
        tb, QEMU_PLUGIN_INLINE_ADD_U64,
        qemu_plugin_scoreboard_u64_in_struct(sb, Cnt, insns), n);
    for (size_t i = 0; i < n; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        qemu_plugin_register_vcpu_mem_cb(insn, cb_mem, QEMU_PLUGIN_CB_NO_REGS,
                                         QEMU_PLUGIN_MEM_RW, NULL);
    }
    vcpu_tb_trans(id, tb);
}

static void vcpu_init(qemu_plugin_id_t id, unsigned int vcpu_index)
{
    if (cr3_handle) return;
    GArray *regs = qemu_plugin_get_registers();
    if (!regs) return;
    for (guint i = 0; i < regs->len; i++) {
        qemu_plugin_reg_descriptor *dsc =
            &g_array_index(regs, qemu_plugin_reg_descriptor, i);
        if (dsc->name && !strcmp(dsc->name, "cr3")) {
            cr3_handle = dsc->handle;
            break;
        }
    }
}

static void plugin_exit(qemu_plugin_id_t id, void *p)
{
    /* Fold the final, unterminated epoch in. qemu_plugin_read_register() is
     * illegal here (no current_cpu at atexit), so attribute it to the last CR3
     * we saw; the tail epoch is the shutdown path and is tiny. */
    close_epoch(last_cr3_seen);

    uint64_t insns = 0, tbs = 0;
    for (int i = 0; i < qemu_plugin_num_vcpus(); i++) {
        Cnt *c = qemu_plugin_scoreboard_find(sb, i);
        insns += c->insns;
        tbs += c->tbs;
    }
    uint64_t u_lo = (uint64_t)set_count(&glob) + (uint64_t)set_count(&kglob);

    g_autoptr(GString) s = g_string_new(NULL);
    g_string_append_printf(s,
        "RATES guest_insns=%" PRIu64
        " guest_tb_exec=%" PRIu64
        " guest_mem_ops=%" PRIu64
        " cr3_write_sites=%" PRIu64
        " cr3_writes_exec=%" PRIu64
        " invlpg=%" PRIu64
        " invpcid=%" PRIu64
        " distinct_cr3=%zu"
        " epochs=%" PRIu64
        " U_hi_all=%" PRIu64
        " U_hi_user=%" PRIu64
        " U_lo=%" PRIu64
        " U_lo_user=%zu"
        " U_lo_kern=%zu"
        " new_from_mem=%" PRIu64
        " new_from_code=%" PRIu64
        "\n",
        insns, tbs, c_mem, c_cr3w, c_cr3_insn_exec, c_invlpg, c_invpcid,
        set_count(&cr3set), epochs, u_hi_all, u_hi_user, u_lo,
        set_count(&glob), set_count(&kglob), new_from_mem, new_from_code);
    g_string_append(s, seen_cr3_disas->str);
    qemu_plugin_outs(s->str);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    set_init(&epoch, 1 << 16);
    set_init(&glob,  1 << 20);
    set_init(&kglob, 1 << 16);
    set_init(&cr3set, 1 << 12);
    regbuf = g_byte_array_new();
    seen_cr3_disas = g_string_new(NULL);
    sb = qemu_plugin_scoreboard_new(sizeof(Cnt));
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init);
    qemu_plugin_register_vcpu_tb_trans_cb(id, vcpu_tb_trans2);
    qemu_plugin_register_atexit_cb(id, plugin_exit, NULL);
    return 0;
}
