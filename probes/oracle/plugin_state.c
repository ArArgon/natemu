/*
 * plugin_state.c -- QEMU TCG plugin: an instruction-count-addressable
 * architectural-state hash chain, plus a windowed full-state trace.
 *
 * This is the high-throughput half of the correctness oracle in
 * docs/research/25-correctness-oracle.md.  Single-stepping over the gdbstub
 * gives ~1.9 k instructions/s of lockstep, which is 6+ days for a Linux boot.
 * This plugin instead makes every emulator emit a *checkpoint file*:
 *
 *      (icount, rolling_hash) every 2^k instructions
 *
 * Two such files are compared with bisect.py, which finds the first differing
 * checkpoint -- i.e. the 2^k-instruction window containing the first
 * divergence -- and then each side is re-run with --window to dump full
 * per-instruction state for just that window.  Total trace bytes are
 * O(insns / 2^k) + O(window), not O(insns).
 *
 * Modes (comma-separated plugin args):
 *   mode=pc      hash only the program counter        (control-flow divergence)
 *   mode=regs    hash PC + 16 GPRs + RFLAGS           (default; full state)
 *   ckpt=N       emit a checkpoint every 2^N insns    (default 16)
 *   out=PATH     checkpoint file                      (default /tmp/state.ckpt)
 *   wlo=N,whi=N  also dump per-instruction state for icount in [N,N)
 *   wout=PATH    window dump file                     (default out + ".win")
 *   inject=N     flip a bit in the hash at icount N   (self-test only)
 *
 * Build (macOS/arm64, Homebrew QEMU 11):
 *   cc -O2 -shared -fPIC -o plugin_state.so plugin_state.c \
 *      -I/opt/homebrew/include $(pkg-config --cflags glib-2.0) \
 *      -undefined dynamic_lookup
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <glib.h>
#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

/* ------------------------------------------------------------------ config */
static int mode_regs = 1;
static int ckpt_shift = 16;
static uint64_t ckpt_mask;
static uint64_t inject_at = 0;
static uint64_t wlo = 0, whi = 0;
static char *out_path, *win_path;

/* --------------------------------------------------------------- per-vcpu */
/* Single-vCPU by design: a differential oracle must be deterministic, and a
 * multi-threaded TCG guest is not.  Run with -smp 1.  Any vCPU > 0 is ignored
 * and reported at exit so the omission is never silent. */
#define NREG 18
static const char *want[NREG] = {
    "rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp",
    "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15",
    "rip", "eflags",
};
static struct qemu_plugin_register *reg[NREG];
static GByteArray *rbuf;
static int have_regs;

static uint64_t icount;
static uint64_t h = 0xcbf29ce484222325ULL; /* FNV-1a offset basis */
static uint64_t skipped_other_vcpu;
static FILE *fckpt, *fwin;

static inline void mix(uint64_t v)
{
    h ^= v;
    h *= 0x100000001b3ULL;
    h ^= h >> 29;
}

static void vcpu_insn(unsigned int vcpu_index, void *udata)
{
    uint64_t pc = (uint64_t)(uintptr_t)udata;
    uint64_t v[NREG];

    if (vcpu_index != 0) {
        skipped_other_vcpu++;
        return;
    }

    mix(pc);
    /* Register reads are the expensive part.  Do them when the *hash* needs
     * them (mode=regs) or when this instruction is inside the dump window --
     * so a stage-2 window re-run costs the same as the cheap stage-1 pass. */
    int in_win = fwin && (icount + 1) >= wlo && (icount + 1) < whi;
    int need = (mode_regs || in_win) && have_regs;
    if (need) {
        for (int i = 0; i < NREG; i++) {
            g_byte_array_set_size(rbuf, 0);
            if (reg[i] && qemu_plugin_read_register(reg[i], rbuf) && rbuf->len) {
                uint64_t x = 0;
                memcpy(&x, rbuf->data, rbuf->len > 8 ? 8 : rbuf->len);
                v[i] = x;
            } else {
                v[i] = 0;
            }
            if (mode_regs) {
                mix(v[i]);
            }
        }
    }

    icount++;
    if (inject_at && icount == inject_at) {
        h ^= 1ULL << 17;
    }

    if (fwin && icount >= wlo && icount < whi) {
        fprintf(fwin, "%" PRIu64 " %016" PRIx64, icount, pc);
        if (need) {
            for (int i = 0; i < NREG; i++) {
                fprintf(fwin, " %s=%" PRIx64, want[i], v[i]);
            }
        }
        fprintf(fwin, "\n");
    }

    if ((icount & ckpt_mask) == 0) {
        fprintf(fckpt, "%" PRIu64 " %016" PRIx64 "\n", icount, h);
    }
}

static void vcpu_tb_trans(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    size_t n = qemu_plugin_tb_n_insns(tb);
    for (size_t i = 0; i < n; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        uint64_t pc = qemu_plugin_insn_vaddr(insn);
        qemu_plugin_register_vcpu_insn_exec_cb(
            insn, vcpu_insn,
            QEMU_PLUGIN_CB_R_REGS,
            (void *)(uintptr_t)pc);
    }
}

static void vcpu_init(qemu_plugin_id_t id, unsigned int vcpu_index)
{
    if (vcpu_index != 0 || have_regs) {
        return;
    }
    GArray *regs = qemu_plugin_get_registers();
    if (!regs) {
        return;
    }
    for (guint i = 0; i < regs->len; i++) {
        qemu_plugin_reg_descriptor *d =
            &g_array_index(regs, qemu_plugin_reg_descriptor, i);
        for (int j = 0; j < NREG; j++) {
            if (!reg[j] && d->name && !strcmp(d->name, want[j])) {
                reg[j] = d->handle;
            }
        }
    }
    g_array_free(regs, TRUE);
    rbuf = g_byte_array_new();
    have_regs = 1;
}

static void plugin_exit(qemu_plugin_id_t id, void *p)
{
    fprintf(fckpt, "%" PRIu64 " %016" PRIx64 "\n", icount, h);
    fflush(fckpt);
    if (fwin) {
        fflush(fwin);
    }
    fprintf(stderr, "STATEHASH insns=%" PRIu64 " final_hash=%016" PRIx64
            " mode=%s ckpt_shift=%d skipped_other_vcpu=%" PRIu64 "\n",
            icount, h, mode_regs ? "regs" : "pc", ckpt_shift,
            skipped_other_vcpu);
}

QEMU_PLUGIN_EXPORT
int qemu_plugin_install(qemu_plugin_id_t id, const qemu_info_t *info,
                        int argc, char **argv)
{
    out_path = g_strdup("/tmp/state.ckpt");
    for (int i = 0; i < argc; i++) {
        char *a = argv[i];
        if (!strncmp(a, "mode=", 5)) {
            mode_regs = !strcmp(a + 5, "regs");
        } else if (!strncmp(a, "ckpt=", 5)) {
            ckpt_shift = atoi(a + 5);
        } else if (!strncmp(a, "out=", 4)) {
            g_free(out_path); out_path = g_strdup(a + 4);
        } else if (!strncmp(a, "wout=", 5)) {
            g_free(win_path); win_path = g_strdup(a + 5);
        } else if (!strncmp(a, "wlo=", 4)) {
            wlo = strtoull(a + 4, NULL, 0);
        } else if (!strncmp(a, "whi=", 4)) {
            whi = strtoull(a + 4, NULL, 0);
        } else if (!strncmp(a, "inject=", 7)) {
            inject_at = strtoull(a + 7, NULL, 0);
        }
    }
    ckpt_mask = (1ULL << ckpt_shift) - 1;
    fckpt = fopen(out_path, "w");
    if (!fckpt) {
        fprintf(stderr, "plugin_state: cannot open %s\n", out_path);
        return -1;
    }
    setvbuf(fckpt, NULL, _IOFBF, 1 << 20);
    if (whi > wlo) {
        if (!win_path) {
            win_path = g_strdup_printf("%s.win", out_path);
        }
        fwin = fopen(win_path, "w");
        setvbuf(fwin, NULL, _IOFBF, 1 << 20);
    }

    qemu_plugin_register_vcpu_init_cb(id, vcpu_init);
    qemu_plugin_register_vcpu_tb_trans_cb(id, vcpu_tb_trans);
    qemu_plugin_register_atexit_cb(id, plugin_exit, NULL);
    return 0;
}
