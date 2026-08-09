/*
 * plugin_inv.c -- QEMU TCG plugin: count x86-64 TLB-invalidation-class
 * instructions, control-register accesses, halts, and totals.
 *
 * Derived from probes/qemu_baseline/plugin_count.c (same build recipe).
 *
 * All hot counting is done with the inline (scoreboard) API so the counting
 * itself is a single host add per event.  Classification happens once, at
 * translate time, from the disassembly text.
 *
 * Build (macOS/arm64, Homebrew QEMU 11):
 *   cc -O2 -shared -fPIC -o plugin_inv.so plugin_inv.c \
 *      -I/opt/homebrew/include $(pkg-config --cflags glib-2.0) \
 *      -undefined dynamic_lookup
 * Use:
 *   qemu-system-x86_64 -plugin ./plugin_inv.so -d plugin ...
 *
 * Args:  -plugin ./plugin_inv.so,hist=on   (dump per-disas histogram)
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <glib.h>
#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

/* Order must match FIELDS[] below. */
typedef struct {
    uint64_t insns;
    uint64_t tbs;
    uint64_t mem_r;
    uint64_t mem_w;
    uint64_t invlpg;
    uint64_t invlpga;
    uint64_t invpcid;
    uint64_t vmxinv;     /* invept / invvpid */
    uint64_t cr3_w;
    uint64_t cr3_r;
    uint64_t cr4_w;
    uint64_t cr4_r;
    uint64_t cr0_w;
    uint64_t cr0_r;
    uint64_t cr_other;
    uint64_t wbinvd;
    uint64_t invd;
    uint64_t hlt;
    uint64_t iret;
    uint64_t swapgs;
} Counters;

#define OFF(f) offsetof(Counters, f)

static const struct { const char *name; size_t off; } FIELDS[] = {
    {"insns",   OFF(insns)},   {"tbs",     OFF(tbs)},
    {"mem_r",   OFF(mem_r)},   {"mem_w",   OFF(mem_w)},
    {"invlpg",  OFF(invlpg)},  {"invlpga", OFF(invlpga)},
    {"invpcid", OFF(invpcid)}, {"vmxinv",  OFF(vmxinv)},
    {"cr3_w",   OFF(cr3_w)},   {"cr3_r",   OFF(cr3_r)},
    {"cr4_w",   OFF(cr4_w)},   {"cr4_r",   OFF(cr4_r)},
    {"cr0_w",   OFF(cr0_w)},   {"cr0_r",   OFF(cr0_r)},
    {"cr_other",OFF(cr_other)},{"wbinvd",  OFF(wbinvd)},
    {"invd",    OFF(invd)},    {"hlt",     OFF(hlt)},
    {"iret",    OFF(iret)},    {"swapgs",  OFF(swapgs)},
};
#define NFIELDS (sizeof(FIELDS)/sizeof(FIELDS[0]))

static struct qemu_plugin_scoreboard *sb;
static uint64_t translations;
static bool want_hist = true;
static GMutex hist_lock;
static GHashTable *hist;      /* disas string -> count of translations */
static GHashTable *unmatched; /* "cr"/"inv" disas we did NOT classify */

static void bump(GHashTable *h, const char *s)
{
    gpointer v = g_hash_table_lookup(h, s);
    if (v) {
        g_hash_table_replace(h, g_strdup(s), GUINT_TO_POINTER(GPOINTER_TO_UINT(v) + 1));
    } else {
        g_hash_table_insert(h, g_strdup(s), GUINT_TO_POINTER(1));
    }
}

/*
 * Is this a write to control register `crN`?
 * QEMU x86 disassembly (capstone / built-in) is AT&T: dest is the LAST operand,
 * i.e. after the last comma:  "mov %rax, %cr3"  = write
 *                             "mov %cr3, %rax"  = read
 * We verify this empirically via the histogram dump.
 */
static int cr_is_write(const char *d, const char *crpos)
{
    const char *comma = strrchr(d, ',');
    if (!comma) {
        return -1;             /* no operands? unclassifiable */
    }
    return crpos > comma;
}

/* Returns offset of the counter field to bump, or (size_t)-1 for none. */
static size_t classify(const char *d, int *is_mem_insn)
{
    const char *p;

    *is_mem_insn = 0;

    if (strstr(d, "invlpga")) return OFF(invlpga);
    if (strstr(d, "invlpg"))  return OFF(invlpg);
    if (strstr(d, "invpcid")) return OFF(invpcid);
    if (strstr(d, "invept") || strstr(d, "invvpid")) return OFF(vmxinv);
    if (strstr(d, "wbinvd") || strstr(d, "wbnoinvd")) return OFF(wbinvd);
    if (strstr(d, "invd"))    return OFF(invd);
    if (strstr(d, "hlt"))     return OFF(hlt);
    if (strstr(d, "iret"))    return OFF(iret);
    if (strstr(d, "swapgs"))  return OFF(swapgs);

    p = strstr(d, "cr3");
    if (p) { int w = cr_is_write(d, p); return w < 0 ? OFF(cr_other) : (w ? OFF(cr3_w) : OFF(cr3_r)); }
    p = strstr(d, "cr4");
    if (p) { int w = cr_is_write(d, p); return w < 0 ? OFF(cr_other) : (w ? OFF(cr4_w) : OFF(cr4_r)); }
    p = strstr(d, "cr0");
    if (p) { int w = cr_is_write(d, p); return w < 0 ? OFF(cr_other) : (w ? OFF(cr0_w) : OFF(cr0_r)); }
    p = strstr(d, "%cr");
    if (p) return OFF(cr_other);
    if (strstr(d, "lmsw") || strstr(d, "smsw")) return OFF(cr_other);

    return (size_t)-1;
}

static void vcpu_tb_trans(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    size_t n = qemu_plugin_tb_n_insns(tb);
    translations++;

    qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(
        tb, QEMU_PLUGIN_INLINE_ADD_U64,
        qemu_plugin_scoreboard_u64_in_struct(sb, Counters, tbs), 1);
    qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(
        tb, QEMU_PLUGIN_INLINE_ADD_U64,
        qemu_plugin_scoreboard_u64_in_struct(sb, Counters, insns), n);

    for (size_t i = 0; i < n; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);

        qemu_plugin_register_vcpu_mem_inline_per_vcpu(
            insn, QEMU_PLUGIN_MEM_R, QEMU_PLUGIN_INLINE_ADD_U64,
            qemu_plugin_scoreboard_u64_in_struct(sb, Counters, mem_r), 1);
        qemu_plugin_register_vcpu_mem_inline_per_vcpu(
            insn, QEMU_PLUGIN_MEM_W, QEMU_PLUGIN_INLINE_ADD_U64,
            qemu_plugin_scoreboard_u64_in_struct(sb, Counters, mem_w), 1);

        char *d = qemu_plugin_insn_disas(insn);
        if (!d) {
            continue;
        }
        int dummy;
        size_t off = classify(d, &dummy);
        int interesting = (strstr(d, "cr") != NULL) || (strstr(d, "inv") != NULL);

        if (off != (size_t)-1) {
            qemu_plugin_u64 slot = { sb, off };
            qemu_plugin_register_vcpu_insn_exec_inline_per_vcpu(
                insn, QEMU_PLUGIN_INLINE_ADD_U64, slot, 1);
        }
        if (want_hist && interesting) {
            g_mutex_lock(&hist_lock);
            bump(hist, d);
            if (off == (size_t)-1) {
                bump(unmatched, d);
            }
            g_mutex_unlock(&hist_lock);
        }
        g_free(d);
    }
}

static void dump_hist(GString *s, const char *label, GHashTable *h)
{
    GList *keys = g_hash_table_get_keys(h);
    keys = g_list_sort(keys, (GCompareFunc)g_strcmp0);
    g_string_append_printf(s, "%s n_distinct=%u\n", label, g_hash_table_size(h));
    for (GList *l = keys; l; l = l->next) {
        g_string_append_printf(s, "%s  %6u  %s\n", label,
                               GPOINTER_TO_UINT(g_hash_table_lookup(h, l->data)),
                               (char *)l->data);
    }
    g_list_free(keys);
}

static void plugin_exit(qemu_plugin_id_t id, void *p)
{
    uint64_t tot[NFIELDS];
    memset(tot, 0, sizeof(tot));
    int nv = qemu_plugin_num_vcpus();
    for (int i = 0; i < nv; i++) {
        char *c = (char *)qemu_plugin_scoreboard_find(sb, i);
        for (size_t f = 0; f < NFIELDS; f++) {
            tot[f] += *(uint64_t *)(c + FIELDS[f].off);
        }
    }

    g_autoptr(GString) s = g_string_new(NULL);
    g_string_append_printf(s, "PLUGININV vcpus=%d tb_translations=%" PRIu64, nv, translations);
    for (size_t f = 0; f < NFIELDS; f++) {
        g_string_append_printf(s, " %s=%" PRIu64, FIELDS[f].name, tot[f]);
    }
    g_string_append(s, "\n");

    /* per-vcpu breakdown, useful for the SMP shootdown question */
    for (int i = 0; i < nv; i++) {
        char *c = (char *)qemu_plugin_scoreboard_find(sb, i);
        g_string_append_printf(s, "PLUGININVCPU %d", i);
        for (size_t f = 0; f < NFIELDS; f++) {
            g_string_append_printf(s, " %s=%" PRIu64, FIELDS[f].name,
                                   *(uint64_t *)(c + FIELDS[f].off));
        }
        g_string_append(s, "\n");
    }

    if (want_hist) {
        dump_hist(s, "PLUGINHIST", hist);
        dump_hist(s, "PLUGINUNMATCHED", unmatched);
    }
    qemu_plugin_outs(s->str);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    for (int i = 0; i < argc; i++) {
        if (!strcmp(argv[i], "hist=off")) {
            want_hist = false;
        }
    }
    hist = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    unmatched = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    sb = qemu_plugin_scoreboard_new(sizeof(Counters));
    qemu_plugin_register_vcpu_tb_trans_cb(id, vcpu_tb_trans);
    qemu_plugin_register_atexit_cb(id, plugin_exit, NULL);
    return 0;
}
