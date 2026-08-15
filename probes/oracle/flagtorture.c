/*
 * flagtorture.c -- a flag-exhaustive x86-64 differential test vector generator.
 *
 * Purpose (docs/research/25-correctness-oracle.md, question B): EFLAGS is the
 * highest-value differential-testing target in this design, because the flag
 * scheme borrowed from FEX never computes PF or AF at definition time (PF is a
 * raw result byte resolved by an XOR cascade at read time; AF is stored as
 * src1^src2 and resolved against that same byte; CF polarity is tracked
 * symbolically and un-inverted only on the way out).  Each of those is a place
 * where a flag that is almost never read can be silently wrong for months.
 *
 * This program executes real x86-64 flag-writing instructions over a controlled
 * operand corpus and folds (op, width, src1, src2, cf_in, result, flags) into a
 * per-group FNV-1a hash.  Two independent x86-64 implementations that agree on
 * every group hash agree on every one of those cases.
 *
 * Capture uses SETO + LAHF only: no PUSHFQ, so no stack traffic and no red-zone
 * interaction, and the captured set is exactly the six arithmetic flags
 * {CF,PF,AF,ZF,SF,OF} that the FEX-style scheme defers.
 *
 * Corpus:
 *   width 8      EXHAUSTIVE   256 x 256 x {cf=0,1}   = 131072 cases per op
 *   width 16/32/64  STRUCTURED  N x N x {cf=0,1}     (N = --corpus, default 256)
 *
 * The 8-bit arm is exhaustive *and* it is the arm that matters most: PF is a
 * function of the low 8 bits of the result alone, and AF is a function of the
 * low 4 bits of each source plus carry-in.  So the 8-bit exhaustive sweep is a
 * complete decision-procedure for PF and AF at every width, for every op whose
 * low byte is computed the same way at every width (all of ADD/ADC/SUB/SBB/
 * AND/OR/XOR/CMP/TEST/INC/DEC/NEG).  The wider arms exist for CF/OF/SF/ZF,
 * which do depend on the top bits, and are therefore structured rather than
 * exhaustive.
 *
 * Build:
 *   zig cc -target x86_64-macos       -O2 -o flagtorture.macos-x64 flagtorture.c
 *   zig cc -target x86_64-linux-musl  -O2 -static -o flagtorture.linux-x64 flagtorture.c
 * Run:
 *   ./flagtorture                 # group hashes + a grand total
 *   ./flagtorture --dump ADD.8    # every case in one group, for bisecting a mismatch
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#define OFBIT 11
/* CF(0) PF(2) AF(4) ZF(6) SF(7) */
#define LAHF_MASK 0xD5u

static uint64_t h;
static inline void mix(uint64_t v) { h ^= v; h *= 0x100000001b3ULL; h ^= h >> 29; }

static char dump_group[64];
static int dumping;

/* Architecturally-defined flag masks (SDM Vol.2, per-instruction "Flags
 * Affected").  Bits outside the mask are UNDEFINED and MUST NOT be compared:
 * two shipping x86-64 implementations disagree on them (see doc 25 sec 3). */
#define F_CF (1u<<0)
#define F_PF (1u<<2)
#define F_AF (1u<<4)
#define F_ZF (1u<<6)
#define F_SF (1u<<7)
#define F_OF (1u<<11)
#define M_ALL   (F_CF|F_PF|F_AF|F_ZF|F_SF|F_OF)
#define M_LOGIC (M_ALL & ~F_AF)                 /* AND/OR/XOR/TEST: AF undefined */
#define M_SHIFT(c) ((c)==1 ? (M_ALL & ~F_AF) : (M_ALL & ~F_AF & ~F_OF))
#define M_ROT(c)   ((c)==1 ? (F_CF|F_OF) : F_CF) /* rotates: only CF (+OF at c==1) */
#define M_BSF   (F_ZF)
#define M_BT    (F_CF)

static unsigned g_mask = M_ALL;
static int strict;   /* --strict: compare undefined bits too */
static int g_bsf;    /* BSF/BSR: the DESTINATION is undefined when src==0 */

static void record(const char *op, int w, uint64_t a, uint64_t b, int cf,
                   uint64_t res, unsigned fl)
{
    if (!strict) { fl &= g_mask; if (g_bsf && b == 0) res = 0; }
    mix((uint64_t)(uintptr_t)op[0] * 1315423911u + w * 7 + cf);
    mix(a); mix(b); mix(res); mix(fl);
    if (dumping) {
        printf("%s.%d %016llx %016llx cf=%d -> res=%016llx fl=%04x "
               "CF=%d PF=%d AF=%d ZF=%d SF=%d OF=%d\n",
               op, w, (unsigned long long)a, (unsigned long long)b, cf,
               (unsigned long long)res, fl,
               (fl >> 0) & 1, (fl >> 2) & 1, (fl >> 4) & 1,
               (fl >> 6) & 1, (fl >> 7) & 1, (fl >> OFBIT) & 1);
    }
}

/* ---- the capture primitive -------------------------------------------- */
/* MN: mnemonic with size suffix.  WM: operand modifier b/w/k/q.
 * Sets CF from `cf`, runs "MN src, dst", captures OF via SETO and the low five
 * arithmetic flags via LAHF. */
#define OP2(MN, WM, dst, src, cf, outfl)                                   \
    __asm__ volatile (                                                     \
        "cmpb $1, %[cfin]\n\t"          /* CF = 1 iff cfin == 0 ... */      \
        "cmc\n\t"                       /* ... so complement: CF = cfin */  \
        MN " %" WM "[s], %" WM "[d]\n\t"                                    \
        "seto %%al\n\t"                                                     \
        "lahf\n\t"                                                          \
        "movzbl %%ah, %%ecx\n\t"                                            \
        "movzbl %%al, %%edx\n\t"                                            \
        "andl $0xd5, %%ecx\n\t"                                             \
        "shll $11, %%edx\n\t"                                               \
        "orl %%edx, %%ecx\n\t"                                              \
        "movl %%ecx, %[f]"                                                  \
        : [d] "+r" (dst), [f] "=&r" (outfl)                                 \
        : [s] "r" (src), [cfin] "r" ((uint8_t)(cf))                         \
        : "cc", "rax", "rcx", "rdx")

#define OP1(MN, WM, dst, cf, outfl)                                        \
    __asm__ volatile (                                                     \
        "cmpb $1, %[cfin]\n\t"                                              \
        "cmc\n\t"                                                           \
        MN " %" WM "[d]\n\t"                                                \
        "seto %%al\n\t"                                                     \
        "lahf\n\t"                                                          \
        "movzbl %%ah, %%ecx\n\t"                                            \
        "movzbl %%al, %%edx\n\t"                                            \
        "andl $0xd5, %%ecx\n\t"                                             \
        "shll $11, %%edx\n\t"                                               \
        "orl %%edx, %%ecx\n\t"                                              \
        "movl %%ecx, %[f]"                                                  \
        : [d] "+r" (dst), [f] "=&r" (outfl)                                 \
        : [cfin] "r" ((uint8_t)(cf))                                        \
        : "cc", "rax", "rcx", "rdx")

/* shift by CL */
#define OPSH(MN, WM, dst, cnt, cf, outfl)                                  \
    __asm__ volatile (                                                     \
        "movb %[c], %%cl\n\t"                                               \
        "cmpb $1, %[cfin]\n\t"                                              \
        "cmc\n\t"                                                           \
        MN " %%cl, %" WM "[d]\n\t"                                          \
        "seto %%al\n\t"                                                     \
        "lahf\n\t"                                                          \
        "movzbl %%ah, %%edx\n\t"                                            \
        "andl $0xd5, %%edx\n\t"                                             \
        "movzbl %%al, %%ecx\n\t"                                            \
        "shll $11, %%ecx\n\t"                                               \
        "orl %%ecx, %%edx\n\t"                                              \
        "movl %%edx, %[f]"                                                  \
        : [d] "+r" (dst), [f] "=&r" (outfl)                                 \
        : [c] "r" ((uint8_t)(cnt)), [cfin] "r" ((uint8_t)(cf))              \
        : "cc", "rax", "rcx", "rdx")

/* ---- operand corpora --------------------------------------------------- */
static uint64_t corpus[4096];
static int ncorpus;

static void build_corpus(int n, int width)
{
    static const uint64_t seeds[] = {
        0, 1, 2, 3, 7, 8, 0xf, 0x10, 0x7f, 0x80, 0x81, 0xff,
        0x100, 0x7fff, 0x8000, 0xffff, 0x10000, 0x7fffffff, 0x80000000u,
        0xffffffffu, 0x100000000ULL, 0x7fffffffffffffffULL,
        0x8000000000000000ULL, 0xffffffffffffffffULL,
        0x5555555555555555ULL, 0xaaaaaaaaaaaaaaaaULL, 0x0f0f0f0f0f0f0f0fULL,
    };
    uint64_t mask = width == 64 ? ~0ULL : ((1ULL << width) - 1);
    int i = 0;
    if (width == 8 && n >= 256) {           /* the 8-bit arm is genuinely exhaustive */
        for (; i < 256; i++) corpus[i] = i;
        ncorpus = 256;
        return;
    }
    for (size_t k = 0; k < sizeof(seeds) / sizeof(seeds[0]) && i < n; k++) {
        for (int d = -1; d <= 1 && i < n; d++) {
            corpus[i++] = (seeds[k] + d) & mask;
        }
    }
    uint64_t s = 0x243f6a8885a308d3ULL;   /* fixed seed: the corpus is a build artifact */
    while (i < n) {
        s ^= s << 13; s ^= s >> 7; s ^= s << 17;
        corpus[i++] = s & mask;
    }
    ncorpus = i;
}

static int want(const char *op, int w)
{
    if (!dump_group[0]) return 0;
    char buf[64];
    snprintf(buf, sizeof buf, "%s.%d", op, w);
    return !strcmp(buf, dump_group);
}

/* ---- the sweeps --------------------------------------------------------- */
#define SWEEP2M(NAME, MN, WM, CTYPE, W, USECF, MASK)                       \
    do {                                                                   \
        uint64_t g = h; h = 0xcbf29ce484222325ULL;                         \
        dumping = want(NAME, W); g_mask = (MASK); g_bsf = ((MASK) == M_BSF); \
        long cases = 0;                                                    \
        for (int i = 0; i < ncorpus; i++) {                                \
            for (int j = 0; j < ncorpus; j++) {                            \
                for (int cf = 0; cf <= (USECF); cf++) {                    \
                    uint64_t d = corpus[i], s = corpus[j];                 \
                    unsigned fl;                                           \
                    OP2(MN, WM, d, s, cf, fl);                             \
                    record(NAME, W, corpus[i], corpus[j], cf,              \
                           (CTYPE)d, fl);                                  \
                    cases++;                                               \
                }                                                          \
            }                                                              \
        }                                                                  \
        printf("  %-6s w=%-2d cases=%-9ld hash=%016llx\n", NAME, W, cases, \
               (unsigned long long)h);                                     \
        total_cases += cases; g ^= h; h = g; g_mask = M_ALL; g_bsf = 0;      \
    } while (0)
#define SWEEP2(NAME, MN, WM, CTYPE, W, USECF) \
    SWEEP2M(NAME, MN, WM, CTYPE, W, USECF, M_ALL)

#define SWEEP1(NAME, MN, WM, CTYPE, W)                                     \
    do {                                                                   \
        uint64_t g = h; h = 0xcbf29ce484222325ULL;                         \
        dumping = want(NAME, W);                                           \
        long cases = 0;                                                    \
        for (int i = 0; i < ncorpus; i++) {                                \
            for (int cf = 0; cf <= 1; cf++) {                              \
                uint64_t d = corpus[i]; unsigned fl;                       \
                OP1(MN, WM, d, cf, fl);                                    \
                record(NAME, W, corpus[i], 0, cf, (CTYPE)d, fl);           \
                cases++;                                                   \
            }                                                              \
        }                                                                  \
        printf("  %-6s w=%-2d cases=%-9ld hash=%016llx\n", NAME, W, cases, \
               (unsigned long long)h);                                     \
        total_cases += cases; g ^= h; h = g; g_mask = M_ALL;               \
    } while (0)

#define SWEEPSHM(NAME, MN, WM, CTYPE, W, MAXC, MASKEXPR)                   \
    do {                                                                   \
        uint64_t g = h; h = 0xcbf29ce484222325ULL;                         \
        dumping = want(NAME, W);                                           \
        long cases = 0;                                                    \
        for (int i = 0; i < ncorpus; i++) {                                \
            for (int c = 0; c <= (MAXC); c++) {                            \
                for (int cf = 0; cf <= 1; cf++) {                          \
                    uint64_t d = corpus[i]; unsigned fl;                   \
                    g_mask = (MASKEXPR);                                   \
                    OPSH(MN, WM, d, c, cf, fl);                            \
                    record(NAME, W, corpus[i], c, cf, (CTYPE)d, fl);       \
                    cases++;                                               \
                }                                                          \
            }                                                              \
        }                                                                  \
        printf("  %-6s w=%-2d cases=%-9ld hash=%016llx\n", NAME, W, cases, \
               (unsigned long long)h);                                     \
        total_cases += cases; g ^= h; h = g; g_mask = M_ALL;               \
    } while (0)
#define SWEEPSH(NAME, MN, WM, CTYPE, W, MAXC) \
    SWEEPSHM(NAME, MN, WM, CTYPE, W, MAXC, M_SHIFT(c))
#define SWEEPROT(NAME, MN, WM, CTYPE, W, MAXC) \
    SWEEPSHM(NAME, MN, WM, CTYPE, W, MAXC, M_ROT(c))

static long total_cases;

static void run_width(int W, const char *wm, int n)
{
    build_corpus(n, W);
    printf("-- width %d, corpus %d values, %d x %d pairs\n", W, ncorpus, ncorpus, ncorpus);
    if (W == 8) {
        SWEEP2("ADD", "addb", "b", uint8_t, 8, 0);
        SWEEP2("ADC", "adcb", "b", uint8_t, 8, 1);
        SWEEP2("SUB", "subb", "b", uint8_t, 8, 0);
        SWEEP2("SBB", "sbbb", "b", uint8_t, 8, 1);
        SWEEP2("AND", "andb", "b", uint8_t, 8, 0);
        SWEEP2("OR",  "orb",  "b", uint8_t, 8, 0);
        SWEEP2("XOR", "xorb", "b", uint8_t, 8, 0);
        SWEEP2("CMP", "cmpb", "b", uint8_t, 8, 0);
        SWEEP2("TEST","testb","b", uint8_t, 8, 0);
        SWEEP1("INC", "incb", "b", uint8_t, 8);
        SWEEP1("DEC", "decb", "b", uint8_t, 8);
        SWEEP1("NEG", "negb", "b", uint8_t, 8);
        SWEEPSH("SHL", "shlb", "b", uint8_t, 8, 16);
        SWEEPSH("SHR", "shrb", "b", uint8_t, 8, 16);
        SWEEPSH("SAR", "sarb", "b", uint8_t, 8, 16);
        SWEEPROT("ROL", "rolb", "b", uint8_t, 8, 16);
        SWEEPROT("ROR", "rorb", "b", uint8_t, 8, 16);
        SWEEPROT("RCL", "rclb", "b", uint8_t, 8, 16);
        SWEEPROT("RCR", "rcrb", "b", uint8_t, 8, 16);
    } else if (W == 16) {
        SWEEP2("ADD", "addw", "w", uint16_t, 16, 0);
        SWEEP2("ADC", "adcw", "w", uint16_t, 16, 1);
        SWEEP2("SUB", "subw", "w", uint16_t, 16, 0);
        SWEEP2("SBB", "sbbw", "w", uint16_t, 16, 1);
        SWEEP2("AND", "andw", "w", uint16_t, 16, 0);
        SWEEP2("CMP", "cmpw", "w", uint16_t, 16, 0);
        SWEEP1("INC", "incw", "w", uint16_t, 16);
        SWEEP1("DEC", "decw", "w", uint16_t, 16);
        SWEEP1("NEG", "negw", "w", uint16_t, 16);
        SWEEPSH("SHL", "shlw", "w", uint16_t, 16, 33);
        SWEEPSH("SHR", "shrw", "w", uint16_t, 16, 33);
        SWEEPSH("SAR", "sarw", "w", uint16_t, 16, 33);
        SWEEPROT("ROL", "rolw", "w", uint16_t, 16, 33);
        SWEEPROT("RCR", "rcrw", "w", uint16_t, 16, 33);
    } else if (W == 32) {
        SWEEP2("ADD", "addl", "k", uint32_t, 32, 0);
        SWEEP2("ADC", "adcl", "k", uint32_t, 32, 1);
        SWEEP2("SUB", "subl", "k", uint32_t, 32, 0);
        SWEEP2("SBB", "sbbl", "k", uint32_t, 32, 1);
        SWEEP2("AND", "andl", "k", uint32_t, 32, 0);
        SWEEP2("CMP", "cmpl", "k", uint32_t, 32, 0);
        SWEEP2("TEST","testl","k", uint32_t, 32, 0);
        SWEEP1("INC", "incl", "k", uint32_t, 32);
        SWEEP1("DEC", "decl", "k", uint32_t, 32);
        SWEEP1("NEG", "negl", "k", uint32_t, 32);
        SWEEPSH("SHL", "shll", "k", uint32_t, 32, 65);
        SWEEPSH("SHR", "shrl", "k", uint32_t, 32, 65);
        SWEEPSH("SAR", "sarl", "k", uint32_t, 32, 65);
        SWEEPROT("ROL", "roll", "k", uint32_t, 32, 65);
        SWEEPROT("RCL", "rcll", "k", uint32_t, 32, 65);
    } else {
        SWEEP2("ADD", "addq", "q", uint64_t, 64, 0);
        SWEEP2("ADC", "adcq", "q", uint64_t, 64, 1);
        SWEEP2("SUB", "subq", "q", uint64_t, 64, 0);
        SWEEP2("SBB", "sbbq", "q", uint64_t, 64, 1);
        SWEEP2("AND", "andq", "q", uint64_t, 64, 0);
        SWEEP2("CMP", "cmpq", "q", uint64_t, 64, 0);
        SWEEP2("TEST","testq","q", uint64_t, 64, 0);
        SWEEP2M("BT",  "btq",  "q", uint64_t, 64, 0, M_BT);
        SWEEP2M("BTS", "btsq", "q", uint64_t, 64, 0, M_BT);
        SWEEP2M("BTR", "btrq", "q", uint64_t, 64, 0, M_BT);
        SWEEP2M("BTC", "btcq", "q", uint64_t, 64, 0, M_BT);
        SWEEP2M("BSF", "bsfq", "q", uint64_t, 64, 0, M_BSF);
        SWEEP2M("BSR", "bsrq", "q", uint64_t, 64, 0, M_BSF);
        SWEEP1("INC", "incq", "q", uint64_t, 64);
        SWEEP1("DEC", "decq", "q", uint64_t, 64);
        SWEEP1("NEG", "negq", "q", uint64_t, 64);
        SWEEPSH("SHL", "shlq", "q", uint64_t, 64, 65);
        SWEEPSH("SHR", "shrq", "q", uint64_t, 64, 65);
        SWEEPSH("SAR", "sarq", "q", uint64_t, 64, 65);
        SWEEPROT("ROL", "rolq", "q", uint64_t, 64, 65);
        SWEEPROT("RCR", "rcrq", "q", uint64_t, 64, 65);
    }
}

int main(int argc, char **argv)
{
    int n16 = 256;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--dump") && i + 1 < argc) {
            snprintf(dump_group, sizeof dump_group, "%s", argv[++i]);
        } else if (!strcmp(argv[i], "--strict")) {
            strict = 1;
        } else if (!strcmp(argv[i], "--corpus") && i + 1 < argc) {
            n16 = atoi(argv[++i]);
        }
    }
    h = 0xcbf29ce484222325ULL;
    printf("FLAGTORTURE v2 corpus=%d mode=%s\n", n16, strict ? "strict" : "defined-only");
    run_width(8,  "b", 256);      /* exhaustive */
    run_width(16, "w", n16);
    run_width(32, "k", n16);
    run_width(64, "q", n16);
    printf("FLAGTOTAL cases=%ld hash=%016llx\n", total_cases,
           (unsigned long long)h);
    return 0;
}
