/*
 * indirect.c: the computed-exit census (docs/restructure/el0-sampler-plan.md, chunk 2), a QEMU TCG plugin built by the
 * framework against its plugin QEMU's header (testkit/artifacts.py census_plugin).
 *
 *     qemu-system-x86_64 ... -plugin libindirect.so,out=FILE.json[,ctl=FILE][,first=NAME][,mark=HEX:NAME]
 *
 * Counted, decoded from each translated instruction's bytes (legacy prefixes 26 2E 36 3E 64 65 66 67 F0 F2 F3, then
 * one REX byte when it is not the instruction's last byte):
 *   - indirect call FF /2 and indirect jmp FF /4 (register and memory forms; 3E there is the CET notrack prefix), and
 *     near ret C3 and C2 iw: per site, executions, up to 4 targets with counts and an overflow count;
 *   - direct call E8: counted, and pushed on the return-stack model;
 *   - far call FF /3, far jmp FF /5, far ret CB and CA iw: counted only (no sites, not in the return model);
 *   - IRET, SYSCALL, SYSRET, INT and direct far transfers (9A, EA) are not counted.
 * Every one of these ends its TCG block, so the target is the start of the next block the vCPU executes (a TB-exec
 * callback, conditional on a branch being pending), or, when an interrupt comes first, the interrupted PC the
 * discontinuity callback gives; an interrupt whose PC is the branch's own is the branch faulting (it is dropped and
 * runs again after the handler). Exception events are counted only: QEMU also reports each delivery as an interrupt.
 *
 * PCs are the vCPU's run-time addresses. x86 system mode uses PC-relative blocks (CF_PCREL): one translation serves
 * every virtual mapping of its physical page, so the address seen at translation can be another process's. A callback
 * reads RIP (the PC of an instruction at or before it in the same block, which the translator keeps exact up to a whole
 * number of pages) and adds that page delta; before paging is on (CR0.PG, firmware and boot code), the translation's
 * address is used.
 *
 * User and kernel are told apart by the address half (bit 63: Linux's kernel addresses); a user site is keyed by its
 * address space too, CR3 with its PCID and PTI bits (11-12) cleared, read through the register API after the vCPU ran
 * kernel code (only the kernel writes CR3), and at every user branch until paging is on. The return-stack model is one
 * stack per vCPU and depth (8, 16, 32): a call pushes its return address (dropping the oldest when full), a ret pops
 * and compares; a pop of an empty stack is a miss of its own kind ("empty").
 *
 * Segments: the counts are kept per segment. The first is named `first` (default "boot"); each line appended to the
 * `ctl` file (polled every 20 ms) starts a new segment of that name, and so does, once, the first execution of the
 * block that starts at the address `mark` gives (a test program's entry: tests/bench/plugins/census_check.asm). A vCPU
 * moves to the new segment at its next callback, which a timer interrupt bounds. Instructions are counted per block
 * executed, by the block's address half
 * (a block left early by an exception still counts whole). The JSON is written at exit (to FILE.tmp, then renamed).
 */
#include <glib.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

enum kind { K_CALL, K_JMP, K_RET, K_DCALL, K_FARCALL, K_FARJMP, K_FARRET };
static const char *KIND[] = {"call", "jmp", "ret"};
enum { F_MEM = 1, F_REX = 2, F_OPSIZE = 4, F_NOTRACK = 8, F_SEG = 16, F_REP = 32, F_IMM = 64, F_LOCKP = 128 };

struct insn_info { uint64_t tb_vaddr, vaddr, phys; uint8_t len, kind, flags; };

#define NT 4
struct site {
    uint64_t asid, pc, phys, execs, overflow;
    uint64_t tgt[NT], cnt[NT];
    uint8_t kind, flags, mode, ntgt, used;
};
struct table { struct site *s; size_t cap, n; };

#define NDEPTH 3
static const int DEPTHS[NDEPTH] = {8, 16, 32};
struct ras { uint64_t buf[32]; int top, count, depth; };

struct stats {      /* [mode]: 0 user, 1 kernel */
    uint64_t direct_calls[2], far_call[2], far_jmp[2], far_ret[2];
    uint64_t hit[NDEPTH][2], miss[NDEPTH][2], empty[NDEPTH][2];
    uint64_t interrupts, exceptions, dropped, unresolved, wide;
};

struct segment { int index; struct table t; struct stats st; uint64_t insns[2]; struct segment *next; };

struct vcpu {       /* the scoreboard's element; all but `pending` and `insns` only touched by the vCPU's own thread */
    uint64_t pending;
    uint64_t insns[2];
    uint64_t p_pc;
    const struct insn_info *p_info;
    uint64_t asid;
    int p_mode, asid_valid, paging, seg, initialized;
    uint64_t seg_base[2];
    struct table t;
    struct stats st;
    struct segment *done;
    struct ras ras[NDEPTH];
    GByteArray *buf;
};

static struct qemu_plugin_scoreboard *sb;
static qemu_plugin_u64 pending_entry, insn_entry[2];
static struct qemu_plugin_register *reg_rip, *reg_cr0, *reg_cr3;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static const char *out_path, *ctl_path;
#define MAXSEG 256
static char *seg_names[MAXSEG];
static atomic_int g_seg;
static int registers_found;
static uint64_t mark_pc;
static const char *mark_name;
static atomic_int mark_fired;

static void new_segment(const char *name)
{
    pthread_mutex_lock(&lock);
    int g = atomic_load(&g_seg);
    if (g + 1 < MAXSEG) {
        seg_names[g + 1] = strdup(name);
        atomic_store_explicit(&g_seg, g + 1, memory_order_release);
    }
    pthread_mutex_unlock(&lock);
}

/* --- the site table: open addressing on (asid, pc, kind) --- */

static uint64_t mix(uint64_t x)
{
    x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL; x ^= x >> 33;
    return x;
}

static void table_init(struct table *t) { t->cap = 1024; t->n = 0; t->s = calloc(t->cap, sizeof *t->s); }

static struct site *table_get(struct table *t, uint64_t asid, uint64_t pc, int kind, bool create);

static void table_grow(struct table *t)
{
    struct table old = *t;
    t->cap *= 2; t->n = 0; t->s = calloc(t->cap, sizeof *t->s);
    for (size_t i = 0; i < old.cap; i++) {
        if (!old.s[i].used) continue;
        struct site *n = table_get(t, old.s[i].asid, old.s[i].pc, old.s[i].kind, true);
        *n = old.s[i];
    }
    free(old.s);
}

static struct site *table_get(struct table *t, uint64_t asid, uint64_t pc, int kind, bool create)
{
    if (create && 2 * (t->n + 1) > t->cap) table_grow(t);
    size_t m = t->cap - 1, i = mix(pc ^ (asid * 0x9e3779b97f4a7c15ULL) ^ ((uint64_t)kind << 61)) & m;
    for (;; i = (i + 1) & m) {
        struct site *s = &t->s[i];
        if (!s->used) {
            if (!create) return NULL;
            memset(s, 0, sizeof *s);
            s->used = 1; s->asid = asid; s->pc = pc; s->kind = kind; t->n++;
            return s;
        }
        if (s->pc == pc && s->asid == asid && s->kind == kind) return s;
    }
}

static void site_target(struct site *s, uint64_t target, uint64_t count)
{
    for (int i = 0; i < s->ntgt; i++)
        if (s->tgt[i] == target) { s->cnt[i] += count; return; }
    if (s->ntgt < NT) { s->tgt[s->ntgt] = target; s->cnt[s->ntgt++] = count; return; }
    s->overflow += count;
}

/* --- registers --- */

static uint64_t rd(struct vcpu *v, struct qemu_plugin_register *r)
{
    g_byte_array_set_size(v->buf, 0);
    if (!r || !qemu_plugin_read_register(r, v->buf)) return 0;
    uint64_t x = 0;
    for (guint i = 0; i < v->buf->len && i < 8; i++) x |= (uint64_t)v->buf->data[i] << (8 * i);
    return x;
}

/* The run-time address of `vaddr`, an instruction of the block translated at `tb_vaddr` (see the header). */
static uint64_t runtime(struct vcpu *v, uint64_t tb_vaddr, uint64_t vaddr)
{
    if (!v->paging) {
        if (!(rd(v, reg_cr0) & 0x80000000ULL)) return vaddr;
        v->paging = 1;
    }
    if (vaddr - tb_vaddr >= 4096) v->st.wide++;
    return vaddr + ((rd(v, reg_rip) - tb_vaddr) & ~0xfffULL);
}

/* --- segments --- */

static void finish_segment(struct vcpu *v, int next)
{
    struct segment *s = calloc(1, sizeof *s);
    s->index = v->seg; s->t = v->t; s->st = v->st;
    for (int m = 0; m < 2; m++) { s->insns[m] = v->insns[m] - v->seg_base[m]; v->seg_base[m] = v->insns[m]; }
    s->next = v->done; v->done = s;
    table_init(&v->t); memset(&v->st, 0, sizeof v->st);
    v->seg = next;
}

static inline struct vcpu *vcpu_of(unsigned int idx)
{
    struct vcpu *v = qemu_plugin_scoreboard_find(sb, idx);
    int g = atomic_load_explicit(&g_seg, memory_order_acquire);
    if (v->seg != g) finish_segment(v, g);
    return v;
}

/* --- the return-stack model --- */

static void ras_push(struct vcpu *v, uint64_t ret)
{
    for (int d = 0; d < NDEPTH; d++) {
        struct ras *r = &v->ras[d];
        r->top = (r->top + 1) % r->depth; r->buf[r->top] = ret;
        if (r->count < r->depth) r->count++;
    }
}

static void ras_pop(struct vcpu *v, uint64_t target, int mode)
{
    for (int d = 0; d < NDEPTH; d++) {
        struct ras *r = &v->ras[d];
        if (!r->count) { v->st.empty[d][mode]++; continue; }
        uint64_t x = r->buf[r->top];
        r->top = (r->top + r->depth - 1) % r->depth; r->count--;
        if (x == target) v->st.hit[d][mode]++; else v->st.miss[d][mode]++;
    }
}

/* --- callbacks --- */

static void resolve(struct vcpu *v, uint64_t target)
{
    const struct insn_info *in = v->p_info;
    int mode = v->p_mode;
    uint64_t pc = v->p_pc;
    v->pending = 0;
    if (in->kind == K_DCALL) { v->st.direct_calls[mode]++; ras_push(v, pc + in->len); return; }
    struct site *s = table_get(&v->t, mode ? 0 : v->asid, pc, in->kind, true);
    if (!s->execs) { s->phys = in->phys; s->flags = in->flags; s->mode = mode; }
    s->execs++;
    site_target(s, target, 1);
    if (in->kind == K_CALL) ras_push(v, pc + in->len);
    else if (in->kind == K_RET) ras_pop(v, target, mode);
}

static void insn_exec(unsigned int idx, void *udata)
{
    struct vcpu *v = vcpu_of(idx);
    const struct insn_info *in = udata;
    if (v->pending) { v->st.unresolved++; v->pending = 0; }
    uint64_t pc = runtime(v, in->tb_vaddr, in->vaddr);
    int mode = (int)(pc >> 63);
    if (mode) v->asid_valid = 0;       /* kernel code ran: CR3 may change before user code runs again */
    switch (in->kind) {
    case K_FARCALL: v->st.far_call[mode]++; return;
    case K_FARJMP: v->st.far_jmp[mode]++; return;
    case K_FARRET: v->st.far_ret[mode]++; return;
    }
    if (!mode && !v->asid_valid) {     /* kept only once paging is on: boot code loads CR3 from low addresses */
        v->asid = reg_cr3 ? rd(v, reg_cr3) & ~0x1fffULL : 0;
        v->asid_valid = v->paging;
    }
    v->p_pc = pc; v->p_info = in; v->p_mode = mode; v->pending = 1;
}

static void tb_exec(unsigned int idx, void *udata)
{
    struct vcpu *v = vcpu_of(idx);
    if (!v->pending) return;
    resolve(v, v->paging ? rd(v, reg_rip) : (uint64_t)(uintptr_t)udata);
}

static void mark_exec(unsigned int idx, void *udata)
{
    if (!atomic_exchange(&mark_fired, 1)) { new_segment(mark_name); vcpu_of(idx); }
}

static void discon(unsigned int idx, enum qemu_plugin_discon_type type, uint64_t from, uint64_t to, void *udata)
{
    struct vcpu *v = vcpu_of(idx);
    if (type == QEMU_PLUGIN_DISCON_EXCEPTION) { v->st.exceptions++; return; }
    if (type != QEMU_PLUGIN_DISCON_INTERRUPT) return;
    v->st.interrupts++;
    if (!v->pending) return;
    if (from == v->p_pc) { v->st.dropped++; v->pending = 0; return; }   /* the branch itself faulted */
    resolve(v, from);
}

/* Decode one instruction's bytes; 0 when it is not counted. */
static int decode(const uint8_t *b, size_t len, uint8_t *kind, uint8_t *flags)
{
    size_t i = 0;
    *flags = 0;
    for (; i < len; i++) {
        uint8_t p = b[i];
        if (p == 0x66) *flags |= F_OPSIZE;
        else if (p == 0x3e) *flags |= F_NOTRACK;
        else if (p == 0x26 || p == 0x2e || p == 0x36 || p == 0x64 || p == 0x65) *flags |= F_SEG;
        else if (p == 0xf2 || p == 0xf3) *flags |= F_REP;
        else if (p == 0xf0) *flags |= F_LOCKP;
        else if (p == 0x67) continue;
        else break;
    }
    if (i < len && (b[i] & 0xf0) == 0x40 && i + 1 < len) { *flags |= F_REX; i++; }
    if (i >= len) return 0;
    uint8_t op = b[i];
    switch (op) {
    case 0xc3: *kind = K_RET; return 1;
    case 0xc2: *kind = K_RET; *flags |= F_IMM; return 1;
    case 0xcb: case 0xca: *kind = K_FARRET; return 1;
    case 0xe8: *kind = K_DCALL; return 1;
    case 0xff:
        if (i + 1 >= len) return 0;
        if ((b[i + 1] >> 6) != 3) *flags |= F_MEM;
        switch ((b[i + 1] >> 3) & 7) {
        case 2: *kind = K_CALL; return 1;
        case 3: *kind = K_FARCALL; return 1;
        case 4: *kind = K_JMP; return 1;
        case 5: *kind = K_FARJMP; return 1;
        }
    }
    return 0;
}

static void tb_trans(struct qemu_plugin_tb *tb, void *udata)
{
    size_t n = qemu_plugin_tb_n_insns(tb);
    uint64_t tbv = qemu_plugin_tb_vaddr(tb);
    qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(tb, QEMU_PLUGIN_INLINE_ADD_U64, insn_entry[tbv >> 63], n);
    qemu_plugin_register_vcpu_tb_exec_cond_cb(tb, tb_exec, QEMU_PLUGIN_CB_R_REGS, QEMU_PLUGIN_COND_NE, pending_entry, 0,
                                              (void *)(uintptr_t)tbv);
    if (mark_pc && tbv == mark_pc) qemu_plugin_register_vcpu_tb_exec_cb(tb, mark_exec, QEMU_PLUGIN_CB_NO_REGS, NULL);
    for (size_t i = 0; i < n; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        uint8_t b[16], kind, flags;
        size_t len = qemu_plugin_insn_data(insn, b, sizeof b);
        if (!decode(b, len, &kind, &flags)) continue;
        struct insn_info *in = malloc(sizeof *in);
        in->tb_vaddr = tbv; in->vaddr = qemu_plugin_insn_vaddr(insn);
        in->phys = (uint64_t)(uintptr_t)qemu_plugin_insn_haddr(insn);
        in->len = (uint8_t)qemu_plugin_insn_size(insn); in->kind = kind; in->flags = flags;
        qemu_plugin_register_vcpu_insn_exec_cb(insn, insn_exec, QEMU_PLUGIN_CB_R_REGS, in);
    }
}

static void vcpu_init(unsigned int idx, void *udata)
{
    pthread_mutex_lock(&lock);
    if (!registers_found) {
        GArray *regs = qemu_plugin_get_registers();
        for (guint i = 0; regs && i < regs->len; i++) {
            qemu_plugin_reg_descriptor *r = &g_array_index(regs, qemu_plugin_reg_descriptor, i);
            if (!strcmp(r->name, "rip")) reg_rip = r->handle;
            else if (!strcmp(r->name, "cr0")) reg_cr0 = r->handle;
            else if (!strcmp(r->name, "cr3")) reg_cr3 = r->handle;
        }
        if (regs) g_array_free(regs, true);
        registers_found = 1;
    }
    pthread_mutex_unlock(&lock);
    struct vcpu *v = qemu_plugin_scoreboard_find(sb, idx);
    v->buf = g_byte_array_new();
    table_init(&v->t);
    for (int d = 0; d < NDEPTH; d++) v->ras[d].depth = DEPTHS[d];
    v->seg = atomic_load(&g_seg);
    v->initialized = 1;
}

static void *poller(void *arg)
{
    long pos = 0;
    char line[512];
    for (;;) {
        struct timespec ts = {0, 20 * 1000 * 1000};
        nanosleep(&ts, NULL);
        FILE *f = fopen(ctl_path, "r");
        if (!f) continue;
        if (fseek(f, pos, SEEK_SET) == 0) {
            while (fgets(line, sizeof line, f)) {
                size_t l = strlen(line);
                if (!l || line[l - 1] != '\n') break;       /* a line still being written: read it next time */
                pos += (long)l;
                line[l - 1] = 0;
                new_segment(line);
            }
        }
        fclose(f);
    }
    return NULL;
}

/* --- output --- */

static void merge(struct table *into, const struct table *t)
{
    for (size_t i = 0; i < t->cap; i++) {
        const struct site *s = &t->s[i];
        if (!s->used) continue;
        struct site *m = table_get(into, s->asid, s->pc, s->kind, true);
        if (!m->execs) { m->phys = s->phys; m->flags = s->flags; m->mode = s->mode; }
        m->execs += s->execs; m->overflow += s->overflow;
        for (int k = 0; k < s->ntgt; k++) site_target(m, s->tgt[k], s->cnt[k]);
    }
}

static void add_stats(struct stats *a, const struct stats *b)
{
    uint64_t *x = (uint64_t *)a; const uint64_t *y = (const uint64_t *)b;
    for (size_t i = 0; i < sizeof *a / sizeof(uint64_t); i++) x[i] += y[i];
}

static void pair(FILE *f, const char *name, const uint64_t v[2], const char *sep)
{
    fprintf(f, "\"%s\": {\"user\": %" PRIu64 ", \"kernel\": %" PRIu64 "}%s", name, v[0], v[1], sep);
}

static void at_exit(void *udata)
{
    int nv = qemu_plugin_num_vcpus(), nseg = atomic_load(&g_seg) + 1;
    for (int i = 0; i < nv; i++) {
        struct vcpu *v = qemu_plugin_scoreboard_find(sb, i);
        if (v->initialized) finish_segment(v, v->seg);
    }
    char *tmp = g_strdup_printf("%s.tmp", out_path);
    FILE *f = fopen(tmp, "w");
    if (!f) { fprintf(stderr, "indirect: cannot write %s\n", tmp); return; }
    fprintf(f, "{\"plugin\": \"indirect\", \"version\": 1, \"vcpus\": %d, \"depths\": [8, 16, 32],\n", nv);
    fprintf(f, " \"cr3\": \"%s\", \"rip\": \"%s\",\n", reg_cr3 ? "read" : "unavailable: no cr3 register",
            reg_rip && reg_cr0 ? "read" : "unavailable: translation addresses");
    fprintf(f, " \"sites_columns\": [\"kind\", \"mode\", \"asid\", \"pc\", \"phys\", \"flags\", \"execs\", \"overflow\", \"targets\"],\n");
    fprintf(f, " \"segments\": [\n");
    for (int g = 0; g < nseg; g++) {
        struct table t; table_init(&t);
        struct stats st; memset(&st, 0, sizeof st);
        uint64_t insns[2] = {0, 0};
        int seen = 0;
        for (int i = 0; i < nv; i++) {
            struct vcpu *v = qemu_plugin_scoreboard_find(sb, i);
            for (struct segment *s = v->initialized ? v->done : NULL; s; s = s->next) {
                if (s->index != g) continue;
                merge(&t, &s->t); add_stats(&st, &s->st); insns[0] += s->insns[0]; insns[1] += s->insns[1]; seen++;
            }
        }
        fprintf(f, "  {\"name\": \"%s\", \"vcpu_parts\": %d, ", seg_names[g] ? seg_names[g] : "?", seen);
        pair(f, "insns", insns, ", ");
        pair(f, "direct_calls", st.direct_calls, ", ");
        fprintf(f, "\"far\": {"); pair(f, "call", st.far_call, ", "); pair(f, "jmp", st.far_jmp, ", ");
        pair(f, "ret", st.far_ret, "}, ");
        fprintf(f, "\"ras\": {");
        for (int d = 0; d < NDEPTH; d++) {
            fprintf(f, "\"%d\": {", DEPTHS[d]);
            for (int m = 0; m < 2; m++)
                fprintf(f, "\"%s\": {\"hit\": %" PRIu64 ", \"miss\": %" PRIu64 ", \"empty\": %" PRIu64 "}%s",
                        m ? "kernel" : "user", st.hit[d][m], st.miss[d][m], st.empty[d][m], m ? "" : ", ");
            fprintf(f, "}%s", d + 1 < NDEPTH ? ", " : "");
        }
        fprintf(f, "},\n   \"events\": {\"interrupts\": %" PRIu64 ", \"exceptions\": %" PRIu64 ", \"dropped\": %" PRIu64
                ", \"unresolved\": %" PRIu64 ", \"wide\": %" PRIu64 "},\n   \"sites\": [",
                st.interrupts, st.exceptions, st.dropped, st.unresolved, st.wide);
        int first = 1;
        for (size_t i = 0; i < t.cap; i++) {
            const struct site *s = &t.s[i];
            if (!s->used) continue;
            fprintf(f, "%s\n    [\"%s\", \"%s\", %" PRIu64 ", %" PRIu64 ", %" PRIu64 ", %u, %" PRIu64 ", %" PRIu64 ", [",
                    first ? "" : ",", KIND[s->kind], s->mode ? "kernel" : "user", s->asid, s->pc, s->phys, s->flags,
                    s->execs, s->overflow);
            for (int k = 0; k < s->ntgt; k++)
                fprintf(f, "%s[%" PRIu64 ", %" PRIu64 "]", k ? ", " : "", s->tgt[k], s->cnt[k]);
            fprintf(f, "]]");
            first = 0;
        }
        fprintf(f, "]}%s\n", g + 1 < nseg ? "," : "");
        free(t.s);
    }
    fprintf(f, " ]}\n");
    if (fclose(f) == 0) rename(tmp, out_path);
    g_free(tmp);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id, const qemu_info_t *info, int argc, char **argv)
{
    seg_names[0] = "boot";
    for (int i = 0; i < argc; i++) {
        char *a = argv[i], *eq = strchr(a, '=');
        if (!eq) { fprintf(stderr, "indirect: argument %s is not NAME=VALUE\n", a); return -1; }
        *eq = 0;
        if (!strcmp(a, "out")) out_path = eq + 1;
        else if (!strcmp(a, "ctl")) ctl_path = eq + 1;
        else if (!strcmp(a, "first")) seg_names[0] = eq + 1;
        else if (!strcmp(a, "mark")) {
            char *colon = strchr(eq + 1, ':');
            if (!colon) { fprintf(stderr, "indirect: mark=HEX:NAME\n"); return -1; }
            *colon = 0; mark_pc = strtoull(eq + 1, NULL, 16); mark_name = colon + 1;
        }
        else { fprintf(stderr, "indirect: unknown argument %s\n", a); return -1; }
    }
    if (!out_path) { fprintf(stderr, "indirect: needs out=FILE\n"); return -1; }
    if (!info->system_emulation || strcmp(info->target_name, "x86_64")) {
        fprintf(stderr, "indirect: only for qemu-system-x86_64\n"); return -1;
    }
    sb = qemu_plugin_scoreboard_new(sizeof(struct vcpu));
    pending_entry = qemu_plugin_scoreboard_u64_in_struct(sb, struct vcpu, pending);
    insn_entry[0] = qemu_plugin_scoreboard_u64_in_struct(sb, struct vcpu, insns[0]);
    insn_entry[1] = qemu_plugin_scoreboard_u64_in_struct(sb, struct vcpu, insns[1]);
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init, NULL);
    qemu_plugin_register_vcpu_tb_trans_cb(id, tb_trans, NULL);
    qemu_plugin_register_vcpu_discon_cb(id, QEMU_PLUGIN_DISCON_INTERRUPT | QEMU_PLUGIN_DISCON_EXCEPTION, discon, NULL);
    qemu_plugin_register_atexit_cb(id, at_exit, NULL);
    if (ctl_path) {
        pthread_t t;
        if (pthread_create(&t, NULL, poller, NULL)) { fprintf(stderr, "indirect: no poller thread\n"); return -1; }
        pthread_detach(t);
    }
    return 0;
}
