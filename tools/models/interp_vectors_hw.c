// Writes crates/abi/tests/interp_vectors.txt: the boot-time interpreter's ALU, INC/DEC, shift, bit-test and XADD forms as an x86
// processor executes them, over generated operands and input flags, for the executor core's test (crates/abi/src/interp/tests.rs,
// `matches_the_hardware`). The flags the SDM leaves undefined are written as the processor left them; the test masks them. On any
// x86-64 Linux machine (the operands and flags are generated from a fixed seed, so another processor differs only in those flags):
//   cc -O1 -mno-red-zone -o interp_vectors_hw tools/models/interp_vectors_hw.c && ./interp_vectors_hw > crates/abi/tests/interp_vectors.txt
#include <cpuid.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define FLAGS 0x8d5u /* CF PF AF ZF SF OF */
typedef uint32_t (*fn)(uint32_t a, uint32_t *b, uint64_t *flags);

// Each form on `a` (the destination, in a register) and `b` (the source; CL for the shifts; XADD writes it), with RFLAGS set to
// `*flags` before and read back after (popfq/pushfq: build with -mno-red-zone).
#define BIN(name, insn, w)                                                                                                     \
    static uint32_t name(uint32_t a, uint32_t *bp, uint64_t *f) {                                                            \
        uint64_t fl = *f;                                                                                                    \
        __asm__ volatile("push %[fl]\n\tpopfq\n\t" insn " %" w "[b], %" w "[a]\n\tpushfq\n\tpop %[fl]"                       \
                         : [a] "+r"(a), [fl] "+r"(fl)                                                                        \
                         : [b] "r"(*bp)                                                                                      \
                         : "cc", "memory");                                                                                  \
        *f = fl;                                                                                                             \
        return a;                                                                                                            \
    }
#define XADD(name, w)                                                                                                          \
    static uint32_t name(uint32_t a, uint32_t *bp, uint64_t *f) {                                                            \
        uint64_t fl = *f;                                                                                                    \
        uint32_t b = *bp;                                                                                                    \
        __asm__ volatile("push %[fl]\n\tpopfq\n\txadd %" w "[b], %" w "[a]\n\tpushfq\n\tpop %[fl]"                           \
                         : [a] "+r"(a), [b] "+r"(b), [fl] "+r"(fl)                                                           \
                         :                                                                                                   \
                         : "cc", "memory");                                                                                  \
        *f = fl;                                                                                                             \
        *bp = b;                                                                                                             \
        return a;                                                                                                            \
    }
#define UN(name, insn, w)                                                                                                      \
    static uint32_t name(uint32_t a, uint32_t *bp, uint64_t *f) {                                                            \
        uint64_t fl = *f;                                                                                                    \
        (void)bp;                                                                                                            \
        __asm__ volatile("push %[fl]\n\tpopfq\n\t" insn " %" w "[a]\n\tpushfq\n\tpop %[fl]"                                  \
                         : [a] "+r"(a), [fl] "+r"(fl)                                                                        \
                         :                                                                                                   \
                         : "cc", "memory");                                                                                  \
        *f = fl;                                                                                                             \
        return a;                                                                                                            \
    }
#define SH(name, insn, w)                                                                                                      \
    static uint32_t name(uint32_t a, uint32_t *bp, uint64_t *f) {                                                            \
        uint64_t fl = *f;                                                                                                    \
        __asm__ volatile("push %[fl]\n\tpopfq\n\t" insn " %%cl, %" w "[a]\n\tpushfq\n\tpop %[fl]"                            \
                         : [a] "+r"(a), [fl] "+r"(fl)                                                                        \
                         : "c"(*bp)                                                                                          \
                         : "cc", "memory");                                                                                  \
        *f = fl;                                                                                                             \
        return a;                                                                                                            \
    }
#define ALL3(mac, name, insn) mac(name##8, insn, "b") mac(name##16, insn, "w") mac(name##32, insn, "k")
ALL3(BIN, add, "add")
ALL3(BIN, or, "or")
ALL3(BIN, adc, "adc")
ALL3(BIN, sbb, "sbb")
ALL3(BIN, and, "and")
ALL3(BIN, sub, "sub")
ALL3(BIN, xor, "xor")
ALL3(BIN, cmp, "cmp")
ALL3(BIN, test, "test")
ALL3(UN, inc, "inc")
ALL3(UN, dec, "dec")
ALL3(SH, shl, "shl")
ALL3(SH, shr, "shr")
ALL3(SH, sar, "sar")
BIN(bt16, "bt", "w") BIN(bt32, "bt", "k")
BIN(bts16, "bts", "w") BIN(bts32, "bts", "k")
BIN(btr16, "btr", "w") BIN(btr32, "btr", "k")
BIN(btc16, "btc", "w") BIN(btc32, "btc", "k")
XADD(xadd8, "b") XADD(xadd16, "w") XADD(xadd32, "k")

enum { BINARY, UNARY, SHIFT, BITS, EXCHANGE };
struct form {
    const char *name;
    int bits, kind;
    fn f;
};
#define E3(n, k) {#n, 8, k, n##8}, {#n, 16, k, n##16}, {#n, 32, k, n##32}
#define E2(n, k) {#n, 16, k, n##16}, {#n, 32, k, n##32}
static const struct form forms[] = {
    E3(add, BINARY), E3(or, BINARY),  E3(adc, BINARY), E3(sbb, BINARY), E3(and, BINARY), E3(sub, BINARY),
    E3(xor, BINARY), E3(cmp, BINARY), E3(test, BINARY), E3(inc, UNARY), E3(dec, UNARY),  E3(shl, SHIFT),
    E3(shr, SHIFT),  E3(sar, SHIFT),  E2(bt, BITS),     E2(bts, BITS),  E2(btr, BITS),   E2(btc, BITS),
    E3(xadd, EXCHANGE),
};

static uint64_t state = 20261122;
static uint32_t rnd(void) {
    state ^= state << 13;
    state ^= state >> 7;
    state ^= state << 17;
    return (uint32_t)(state >> 16);
}

int main(void) {
    unsigned brand[12];
    for (unsigned k = 0; k < 3; k++)
        __get_cpuid(0x80000002 + k, &brand[4 * k], &brand[4 * k + 1], &brand[4 * k + 2], &brand[4 * k + 3]);
    char name[49];
    memcpy(name, brand, 48);
    name[48] = 0;
    for (int k = 47; k >= 0 && (name[k] == ' ' || name[k] == 0); k--)
        name[k] = 0;
    printf("# generated by tools/models/interp_vectors_hw.c on %s: op bits a b flags result b_out flags_out (hex; flags CF PF AF "
           "ZF SF OF as RFLAGS bits)\n",
           name);
    for (unsigned k = 0; k < sizeof forms / sizeof forms[0]; k++) {
        const struct form *x = &forms[k];
        uint32_t m = x->bits == 32 ? 0xffffffffu : (1u << x->bits) - 1;
        uint32_t edge[8] = {0, 1, 0xf, 0x10, m >> 1, (m >> 1) + 1, m, m - 1};
        int cases = x->kind == BINARY ? 128 : x->kind == SHIFT ? 96 : 64;
        for (int c = 0; c < cases; c++) {
            uint32_t a = rnd() & m, b = rnd() & m;
            switch (x->kind) {
            case BINARY:
            case EXCHANGE:
                if (c < 64) a = edge[c / 8], b = edge[c % 8];
                break;
            case UNARY:
                if (c < 8) a = edge[c];
                b = 0;
                break;
            case SHIFT: // every count to 33 (past the 5-bit mask), then any byte
                if (c & 1) a = edge[c / 2 % 8];
                b = c < 34 ? (uint32_t)c : rnd() & 0xff;
                break;
            case BITS: // offsets 0..39, then any byte (the register form masks them to the width)
                b = c < 40 ? (uint32_t)c : rnd() & 0xff;
                break;
            }
            uint64_t in = 2 | (rnd() & FLAGS), f = in;
            uint32_t bo = b, r = x->f(a, &bo, &f);
            printf("%s %d %x %x %x %x %x %x\n", x->name, x->bits, a, b, (unsigned)in, r, bo, (unsigned)(f & FLAGS));
        }
    }
    return 0;
}
