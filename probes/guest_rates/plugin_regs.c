/*
 * plugin_regs.c -- one-shot probe: dump the register list QEMU's plugin API
 * exposes for the target, so we know whether CR3 is readable from a plugin.
 *
 * Build: see build_plugins.sh
 * Use:   qemu-system-x86_64 -plugin ./plugin_regs.so -d plugin ...
 */
#include <inttypes.h>
#include <stdio.h>
#include <glib.h>
#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

static int done;

static void vcpu_init(qemu_plugin_id_t id, unsigned int vcpu_index)
{
    if (done++) {
        return;
    }
    g_autoptr(GString) s = g_string_new("REGLIST\n");
    GArray *regs = qemu_plugin_get_registers();
    if (!regs) {
        g_string_append(s, "  (null)\n");
    } else {
        for (guint i = 0; i < regs->len; i++) {
            qemu_plugin_reg_descriptor *d =
                &g_array_index(regs, qemu_plugin_reg_descriptor, i);
            g_string_append_printf(s, "  reg[%u] name=%s feature=%s\n",
                                   i, d->name, d->feature ? d->feature : "-");
        }
        g_string_append_printf(s, "REGLIST_COUNT %u\n", regs->len);
    }
    qemu_plugin_outs(s->str);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init);
    return 0;
}
