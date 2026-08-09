/*
 * plugin_count.c -- minimal QEMU TCG plugin: count executed guest
 * instructions, translation blocks, translations and guest memory accesses.
 *
 * Everything uses the inline (scoreboard) API so the counting itself costs a
 * single host add per event and does not distort the shape of the workload
 * much. It still slows the run down, so the counts -- not the times -- from a
 * plugin run are what should be used.
 *
 * Build (macOS/arm64, Homebrew QEMU 11):
 *   cc -O2 -shared -fPIC -o plugin_count.so plugin_count.c \
 *      -I/opt/homebrew/include $(pkg-config --cflags glib-2.0) \
 *      -undefined dynamic_lookup
 * Use:
 *   qemu-system-x86_64 -plugin ./plugin_count.so -d plugin ...
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <glib.h>
#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

typedef struct {
    uint64_t insns;
    uint64_t tbs;
    uint64_t mem;
} Counters;

static struct qemu_plugin_scoreboard *sb;
static uint64_t translations; /* not per-vcpu; translation is serialized enough */

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
            insn, QEMU_PLUGIN_MEM_RW,
            QEMU_PLUGIN_INLINE_ADD_U64,
            qemu_plugin_scoreboard_u64_in_struct(sb, Counters, mem), 1);
    }
}

static void plugin_exit(qemu_plugin_id_t id, void *p)
{
    uint64_t insns = 0, tbs = 0, mem = 0;
    for (int i = 0; i < qemu_plugin_num_vcpus(); i++) {
        Counters *c = qemu_plugin_scoreboard_find(sb, i);
        insns += c->insns;
        tbs += c->tbs;
        mem += c->mem;
    }
    g_autoptr(GString) s = g_string_new(NULL);
    g_string_append_printf(s, "PLUGINCOUNT guest_insns=%" PRIu64
                              " guest_tb_exec=%" PRIu64
                              " guest_mem_ops=%" PRIu64
                              " tb_translations=%" PRIu64
                              " insns_per_tb=%.2f mem_per_insn=%.4f\n",
                           insns, tbs, mem, translations,
                           tbs ? (double)insns / tbs : 0.0,
                           insns ? (double)mem / insns : 0.0);
    qemu_plugin_outs(s->str);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    sb = qemu_plugin_scoreboard_new(sizeof(Counters));
    qemu_plugin_register_vcpu_tb_trans_cb(id, vcpu_tb_trans);
    qemu_plugin_register_atexit_cb(id, plugin_exit, NULL);
    return 0;
}
