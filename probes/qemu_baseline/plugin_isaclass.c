/*
 * plugin_isaclass.c -- QEMU TCG plugin: classify EXECUTED guest x86
 * instructions into ISA families, split kernel/user, for doc 24
 * (boot path & legacy modes).
 *
 * Motivation: doc 24 asks "does a Linux x86-64 boot to a shell touch x87 at
 * all?", "how much legacy segmentation is there?", "how many descriptor-table
 * instructions?".  Those are measurable, not matters of opinion.
 *
 * Classification is done ONCE per translated instruction, using the
 * disassembly text (qemu_plugin_insn_disas).  Counting is an inline add, so
 * the hot path costs one host add per event.  The instruction TOTAL is
 * accumulated at TB granularity (one add per TB) to keep overhead sane.
 *
 * Syntax-independence: QEMU's i386 disassembler emits AT&T ("fldl 8(%rbp)",
 * "pxor %xmm0,%xmm0").  Rather than matching a fixed mnemonic list with all
 * its AT&T size suffixes, we classify structurally:
 *   - operand contains "zmm"/"ymm"   -> wide vector
 *   - operand contains "xmm"         -> SSE (or AVX128 if mnemonic starts 'v')
 *   - operand contains bare "mm"     -> MMX
 *   - mnemonic starts with 'f'       -> x87   (deny-list: fxsave/fxrstor/femms)
 *   - "%fs:"/"%gs:" appears          -> segment-override memory access
 * This is robust to capstone-vs-builtin disas differences.
 *
 * Build (macOS/arm64, Homebrew QEMU 11):
 *   cc -O2 -shared -fPIC -o bin/plugin_isaclass.so plugin_isaclass.c \
 *      -I/opt/homebrew/include $(pkg-config --cflags glib-2.0) \
 *      -undefined dynamic_lookup
 * Use:
 *   qemu-system-x86_64 -plugin ./bin/plugin_isaclass.so -d plugin -D out.txt ...
 *
 * READ-ONLY analysis tool: it observes, it does not change guest behaviour.
 */
#include <inttypes.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <glib.h>
#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

enum {
    B_TOTAL = 0,     /* every guest instruction (TB-granularity add)        */
    B_X87,           /* x87 FPU proper                                       */
    B_X87_ARITH,     /* subset of B_X87 that is real arithmetic, not ctl     */
    B_FXSAVE,        /* fxsave/fxrstor/xsave*/
    B_XCR,           /* xgetbv/xsetbv                                        */
    B_MMX,           /* MMX register ops incl. emms/femms                    */
    B_SSE_FP,        /* SSE/SSE2 scalar+packed float (non-VEX)               */
    B_SSE_INT,       /* SSE integer / movdqa / pxor-xmm etc (non-VEX)        */
    B_AVX128,        /* VEX-encoded, xmm-width                               */
    B_AVX256,        /* ymm/zmm width                                        */
    B_SEGOVR,        /* instruction with an %fs:/%gs: segment override       */
    B_SEGMOV,        /* mov to/from a segment register, lar/lsl/verr/verw    */
    B_DTABLE,        /* lgdt/lidt/lldt/ltr/sgdt/sidt/sldt/str                */
    B_FARJMP,        /* lcall/ljmp/lret/retf/iret                            */
    B_CR,            /* mov to/from CR or DR                                 */
    B_MSR,           /* rdmsr/wrmsr                                          */
    B_CPUID,         /* cpuid                                                */
    B_IOPORT,        /* in/out/ins/outs                                      */
    B_INVLPG,        /* invlpg/invpcid                                       */
    B_SWAPGS,        /* swapgs                                               */
    B_FSGSBASE,      /* rd/wr fs/gs base                                     */
    B_SYSCALL,       /* syscall/sysret/sysenter/sysexit/int                  */
    B_CR0_W,         /* mov -> CR0  (PE/PG/WP/EM/TS/MP toggles)              */
    B_CR3_W,         /* mov -> CR3  (address-space switch / TLB flush)       */
    B_CR4_W,         /* mov -> CR4  (PAE/PSE/PGE/LA57/SMEP/SMAP/FSGSBASE)    */
    B_CR_R,          /* mov CRn ->  (read)                                   */
    /* Address-region buckets.  Under QEMU's `-kernel`, SeaBIOS + the
     * linuxboot option ROM still run first, in 16-bit real mode, at
     * addresses below 1 MB.  The kernel's own real-mode setup code also
     * lives below 1 MB.  Everything from code32_start (0x100000) upward is
     * 32-bit protected mode or long mode.  So vaddr<1MB is a sound proxy for
     * "legacy-mode boot code we would refuse to implement", and it lets this
     * measurement separate the firmware boot from the direct-kernel boot. */
    B_VA_LO1M,       /* vaddr < 0x100000        (real mode / BIOS / setup)   */
    B_VA_LO4G,       /* 0x100000 <= vaddr < 4G  (decompressor, early kernel) */
    B_VA_MID,        /* 4G <= vaddr < kernel half                            */
    B_VA_HIGH,       /* vaddr >= 0xffff800000000000 (kernel virtual)         */
    NB
};

static const char *bname[NB] = {
    "total", "x87", "x87_arith", "fxsave_grp", "xcr", "mmx",
    "sse_fp", "sse_int", "avx128", "avx256",
    "seg_override", "seg_movlar", "descr_table", "far_ctrlflow",
    "mov_cr_dr", "msr", "cpuid", "ioport", "invlpg", "swapgs",
    "fsgsbase", "syscall_int",
    "cr0_write", "cr3_write", "cr4_write", "cr_read",
    "va_below_1M", "va_1M_to_4G", "va_4G_to_kern", "va_kernel_half",
};

/* static (translated-once) instruction counts per address region */
static uint64_t static_by_region[4];

#define MAXF 192
typedef struct {
    uint64_t b[NB][2];    /* [bucket][0=user,1=kernel]  */
    uint64_t fx[MAXF][2]; /* per-x87-mnemonic           */
} Counters;

static struct qemu_plugin_scoreboard *sb;
static GHashTable *fmap;              /* mnemonic -> slot+1 */
static char *fnames[MAXF];
static int nfnames;
static uint64_t static_insns, static_x87, translations;

static qemu_plugin_u64 sb_at(size_t off)
{
    return (qemu_plugin_u64) { sb, off };
}
static qemu_plugin_u64 bucket_u64(int b, int k)
{
    return sb_at(offsetof(Counters, b) + ((size_t)b * 2 + k) * sizeof(uint64_t));
}
static qemu_plugin_u64 fx_u64(int i, int k)
{
    return sb_at(offsetof(Counters, fx) + ((size_t)i * 2 + k) * sizeof(uint64_t));
}

/* ------------------------------------------------------------------ helpers */

/* Does the disassembly mention a bare MMX register (mm0..mm7) not preceded
 * by x/y/z?  Returns 0 none, 1 mmx, 2 xmm, 3 ymm/zmm (widest wins). */
static int vecwidth(const char *s)
{
    int best = 0;
    for (const char *p = s; (p = strstr(p, "mm")) != NULL; p++) {
        /* require a digit after "mm" so we don't match "emms"/"comment" */
        if (!isdigit((unsigned char)p[2])) {
            continue;
        }
        char prev = (p == s) ? '\0' : p[-1];
        int w = (prev == 'y' || prev == 'z') ? 3 : (prev == 'x') ? 2 : 1;
        if (w > best) {
            best = w;
        }
    }
    return best;
}

static int has(const char *s, const char *n) { return strstr(s, n) != NULL; }

/* Whole-register match: "%es" must not match "%esi"/"%esp".  Requires the
 * character following the needle to not be alphanumeric. */
static int has_reg(const char *s, const char *n)
{
    size_t l = strlen(n);
    for (const char *p = s; (p = strstr(p, n)) != NULL; p++) {
        if (!isalnum((unsigned char)p[l])) {
            return 1;
        }
    }
    return 0;
}

static int mn_is(const char *m, const char *n) { return strcmp(m, n) == 0; }

/* AT&T size suffixes we strip when asking "is this mnemonic X?" */
static int mn_pre(const char *m, const char *p)
{
    return strncmp(m, p, strlen(p)) == 0;
}

/* x87 control/state ops (as opposed to arithmetic).  Matched on the mnemonic
 * with any AT&T suffix still attached, so use prefix matching. */
static int x87_is_control(const char *m)
{
    static const char *ctl[] = {
        "fldcw", "fnstcw", "fstcw", "fnstsw", "fstsw", "fninit", "finit",
        "fnclex", "fclex", "fnsave", "fsave", "frstor", "fldenv", "fnstenv",
        "fstenv", "ffree", "fincstp", "fdecstp", "fnop", "fwait", "fdisi",
        "feni", "fsetpm", NULL
    };
    for (int i = 0; ctl[i]; i++) {
        if (mn_pre(m, ctl[i])) {
            return 1;
        }
    }
    return 0;
}

static void add(struct qemu_plugin_insn *insn, int b, int k)
{
    qemu_plugin_register_vcpu_insn_exec_inline_per_vcpu(
        insn, QEMU_PLUGIN_INLINE_ADD_U64, bucket_u64(b, k), 1);
}

static void vcpu_tb_trans(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    size_t n = qemu_plugin_tb_n_insns(tb);
    translations++;
    static_insns += n;

    for (size_t i = 0; i < n; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        uint64_t va = qemu_plugin_insn_vaddr(insn);
        /* x86-64 kernel half: bit 63 set.  Also treat 32-bit-era early-boot
         * low addresses as "kernel" is wrong, so: anything with bit63 set is
         * kernel, everything else user-or-early-boot.  Reported as such. */
        int k = (va >> 63) & 1;

        /* TOTAL is per-instruction here too so that the kernel/user split is
         * exact; one inline add per insn is acceptable for this experiment. */
        add(insn, B_TOTAL, k);

        int region = (va < 0x100000ULL)      ? 0
                   : (va < 0x100000000ULL)   ? 1
                   : (va < 0xffff800000000000ULL) ? 2 : 3;
        static_by_region[region]++;
        add(insn, B_VA_LO1M + region, k);

        char *d = qemu_plugin_insn_disas(insn);
        if (!d) {
            continue;
        }
        /* mnemonic = first token, lowercased */
        char mn[32];
        size_t j = 0;
        const char *p = d;
        while (*p == ' ') p++;
        while (*p && !isspace((unsigned char)*p) && j < sizeof(mn) - 1) {
            mn[j++] = tolower((unsigned char)*p++);
        }
        mn[j] = 0;

        int w = vecwidth(d);
        int vex = (mn[0] == 'v' && j > 3 && !mn_pre(mn, "verr") &&
                   !mn_pre(mn, "verw") && !mn_pre(mn, "vm"));

        /* ---- x87 --------------------------------------------------------- */
        if (mn[0] == 'f' && !mn_pre(mn, "fxsave") && !mn_pre(mn, "fxrstor") &&
            !mn_is(mn, "femms")) {
            add(insn, B_X87, k);
            static_x87++;
            if (!x87_is_control(mn)) {
                add(insn, B_X87_ARITH, k);
            }
            gpointer v = g_hash_table_lookup(fmap, mn);
            int slot;
            if (v) {
                slot = GPOINTER_TO_INT(v) - 1;
            } else if (nfnames < MAXF) {
                slot = nfnames++;
                fnames[slot] = g_strdup(mn);
                g_hash_table_insert(fmap, fnames[slot],
                                    GINT_TO_POINTER(slot + 1));
            } else {
                slot = -1;
            }
            if (slot >= 0) {
                qemu_plugin_register_vcpu_insn_exec_inline_per_vcpu(
                    insn, QEMU_PLUGIN_INLINE_ADD_U64, fx_u64(slot, k), 1);
            }
        } else if (mn_pre(mn, "fxsave") || mn_pre(mn, "fxrstor") ||
                   mn_pre(mn, "xsave") || mn_pre(mn, "xrstor")) {
            add(insn, B_FXSAVE, k);
        } else if (mn_is(mn, "xgetbv") || mn_is(mn, "xsetbv")) {
            add(insn, B_XCR, k);
        } else if (w == 3) {
            add(insn, B_AVX256, k);
        } else if (w == 2) {
            add(insn, vex ? B_AVX128 : (
                    /* FP-ish? scalar/packed float mnemonics end ss/sd/ps/pd,
                     * or start with cvt, or are (u)comis* */
                    (j >= 2 && ((mn[j-2] == 's' && (mn[j-1] == 's' || mn[j-1] == 'd')) ||
                                (mn[j-2] == 'p' && (mn[j-1] == 's' || mn[j-1] == 'd')))) ||
                    mn_pre(mn, "cvt") || has(mn, "comis") || mn_pre(mn, "rcp") ||
                    mn_pre(mn, "rsqrt")) ? B_SSE_FP : B_SSE_INT, k);
        } else if (w == 1 || mn_is(mn, "emms") || mn_is(mn, "femms")) {
            add(insn, B_MMX, k);
        }

        /* ---- system / legacy surface (orthogonal, may double-count) ------- */
        if (has(d, "%fs:") || has(d, "%gs:")) {
            add(insn, B_SEGOVR, k);
        }
        if (mn_pre(mn, "lgdt") || mn_pre(mn, "lidt") || mn_pre(mn, "lldt") ||
            mn_pre(mn, "ltr")  || mn_pre(mn, "sgdt") || mn_pre(mn, "sidt") ||
            mn_pre(mn, "sldt") || mn_pre(mn, "str")) {
            add(insn, B_DTABLE, k);
        }
        if (mn_pre(mn, "lar") || mn_pre(mn, "lsl") || mn_pre(mn, "verr") ||
            mn_pre(mn, "verw") ||
            (mn_pre(mn, "mov") && (has_reg(d, "%cs") || has_reg(d, "%ds") ||
                                   has_reg(d, "%es") || has_reg(d, "%ss") ||
                                   has(d, "%fs,") || has(d, "%gs,") ||
                                   has(d, ",%fs") || has(d, ",%gs")))) {
            add(insn, B_SEGMOV, k);
        }
        if (mn_pre(mn, "lcall") || mn_pre(mn, "ljmp") || mn_pre(mn, "lret") ||
            mn_pre(mn, "iret") || mn_is(mn, "retf")) {
            add(insn, B_FARJMP, k);
        }
        if (mn_pre(mn, "mov") && (has(d, "%cr") || has(d, "%db") || has(d, "%dr"))) {
            add(insn, B_CR, k);
            /* AT&T: destination is the last operand, so a trailing "%crN"
             * is a WRITE and a leading "%crN," is a READ. */
            size_t dl = strlen(d);
            if (dl >= 4 && !strcmp(d + dl - 4, "%cr0")) {
                add(insn, B_CR0_W, k);
            } else if (dl >= 4 && !strcmp(d + dl - 4, "%cr3")) {
                add(insn, B_CR3_W, k);
            } else if (dl >= 4 && !strcmp(d + dl - 4, "%cr4")) {
                add(insn, B_CR4_W, k);
            } else if (has(d, "%cr0,") || has(d, "%cr2,") || has(d, "%cr3,") ||
                       has(d, "%cr4,") || has(d, "%cr8,")) {
                add(insn, B_CR_R, k);
            }
        }
        if (mn_is(mn, "rdmsr") || mn_is(mn, "wrmsr")) {
            add(insn, B_MSR, k);
        }
        if (mn_is(mn, "cpuid")) {
            add(insn, B_CPUID, k);
        }
        if (mn_pre(mn, "in") && (mn_is(mn, "in") || mn_is(mn, "inb") ||
                                 mn_is(mn, "inw") || mn_is(mn, "inl") ||
                                 mn_pre(mn, "ins"))) {
            add(insn, B_IOPORT, k);
        } else if (mn_pre(mn, "out")) {
            add(insn, B_IOPORT, k);
        }
        if (mn_pre(mn, "invlpg") || mn_pre(mn, "invpcid")) {
            add(insn, B_INVLPG, k);
        }
        if (mn_is(mn, "swapgs")) {
            add(insn, B_SWAPGS, k);
        }
        if (mn_pre(mn, "rdfsbase") || mn_pre(mn, "rdgsbase") ||
            mn_pre(mn, "wrfsbase") || mn_pre(mn, "wrgsbase")) {
            add(insn, B_FSGSBASE, k);
        }
        if (mn_pre(mn, "syscall") || mn_pre(mn, "sysret") ||
            mn_pre(mn, "sysenter") || mn_pre(mn, "sysexit") ||
            mn_is(mn, "int") || mn_pre(mn, "int$") || mn_is(mn, "int3")) {
            add(insn, B_SYSCALL, k);
        }
        g_free(d);
    }
}

static int cmp_fx(const void *a, const void *b);
struct fxrow { const char *name; uint64_t u, s; };

static int cmp_fx(const void *a, const void *b)
{
    const struct fxrow *x = a, *y = b;
    uint64_t tx = x->u + x->s, ty = y->u + y->s;
    return (ty > tx) - (ty < tx);
}

static void plugin_exit(qemu_plugin_id_t id, void *p)
{
    uint64_t b[NB][2];
    memset(b, 0, sizeof(b));
    uint64_t fx[MAXF][2];
    memset(fx, 0, sizeof(fx));

    for (int v = 0; v < qemu_plugin_num_vcpus(); v++) {
        Counters *c = qemu_plugin_scoreboard_find(sb, v);
        for (int i = 0; i < NB; i++) {
            b[i][0] += c->b[i][0];
            b[i][1] += c->b[i][1];
        }
        for (int i = 0; i < nfnames; i++) {
            fx[i][0] += c->fx[i][0];
            fx[i][1] += c->fx[i][1];
        }
    }

    g_autoptr(GString) s = g_string_new(NULL);
    g_string_append_printf(s, "ISACLASS static_insns=%" PRIu64
                              " static_x87=%" PRIu64
                              " tb_translations=%" PRIu64
                              " static_lo1M=%" PRIu64 " static_1M_4G=%" PRIu64
                              " static_4G_kern=%" PRIu64 " static_kernhalf=%" PRIu64
                              "\n",
                           static_insns, static_x87, translations,
                           static_by_region[0], static_by_region[1],
                           static_by_region[2], static_by_region[3]);
    g_string_append_printf(s, "%-16s %16s %16s %16s %10s\n",
                           "bucket", "user", "kernel", "total", "pct_total");
    uint64_t tot = b[B_TOTAL][0] + b[B_TOTAL][1];
    for (int i = 0; i < NB; i++) {
        uint64_t t = b[i][0] + b[i][1];
        g_string_append_printf(s, "ISACLASS %-16s %16" PRIu64 " %16" PRIu64
                                  " %16" PRIu64 " %10.6f\n",
                               bname[i], b[i][0], b[i][1], t,
                               tot ? 100.0 * (double)t / (double)tot : 0.0);
    }

    struct fxrow rows[MAXF];
    int nr = 0;
    for (int i = 0; i < nfnames; i++) {
        if (fx[i][0] + fx[i][1]) {
            rows[nr].name = fnames[i];
            rows[nr].u = fx[i][0];
            rows[nr].s = fx[i][1];
            nr++;
        }
    }
    qsort(rows, nr, sizeof(rows[0]), cmp_fx);
    g_string_append_printf(s, "X87MNEM distinct_executed=%d "
                              "distinct_translated=%d\n", nr, nfnames);
    for (int i = 0; i < nr; i++) {
        g_string_append_printf(s, "X87MNEM %-12s user=%" PRIu64
                                  " kernel=%" PRIu64 " total=%" PRIu64 "\n",
                               rows[i].name, rows[i].u, rows[i].s,
                               rows[i].u + rows[i].s);
    }
    /* x87 mnemonics that were TRANSLATED but never EXECUTED are interesting
     * too: they are on cold paths the kernel/libc compiled in. */
    for (int i = 0; i < nfnames; i++) {
        if (!(fx[i][0] + fx[i][1])) {
            g_string_append_printf(s, "X87COLD %s\n", fnames[i]);
        }
    }
    qemu_plugin_outs(s->str);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    sb = qemu_plugin_scoreboard_new(sizeof(Counters));
    fmap = g_hash_table_new(g_str_hash, g_str_equal);
    qemu_plugin_register_vcpu_tb_trans_cb(id, vcpu_tb_trans);
    qemu_plugin_register_atexit_cb(id, plugin_exit, NULL);
    return 0;
}
