/* Independent native model of the five benchmark bodies: final GPRs and exact dynamic instruction count.
 * usage: model BENCH N R0..R15 (hex initial GPRs, order rax rcx rdx rbx rsp rbp rsi rdi r8..r15) [BUFFER_FILE for mixed]
 * prints 16 final GPRs (hex) then the instruction count (decimal), one per line. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
enum { RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI, R8, R9, R10, R11, R12, R13, R14, R15 };
static uint64_t r[16];
static uint64_t ld(const uint8_t *m, uint64_t o) { uint64_t v; memcpy(&v, m + o, 8); return v; }
static void st(uint8_t *m, uint64_t o, uint64_t v) { memcpy(m + o, &v, 8); }
int main(int argc, char **argv) {
    if (argc < 19) return 2;
    const char *b = argv[1]; uint64_t n = strtoull(argv[2], 0, 0), insns = 0;
    for (int i = 0; i < 16; i++) r[i] = strtoull(argv[3 + i], 0, 16);
    r[RCX] = n;
    if (!strcmp(b, "int")) {
        for (uint64_t c = n; c; c--) {
            r[RAX] += r[RBX]; r[RDX] = r[RAX] << 13; r[RAX] ^= r[RDX]; r[RDX] = r[RAX] >> 7; r[RAX] ^= r[RDX]; r[RAX] *= r[RSI];
        }
        r[RCX] = 0; insns = 10 * n;
    } else if (!strncmp(b, "mem", 3)) {
        uint64_t stride = strtoull(b + 3, 0, 10); uint8_t *m = calloc(1, 1 << 20); uint64_t c = n;
        for (; c; c--) {
            uint64_t a = ld(m, r[RSI]) + c; st(m, r[RSI], a); r[RAX] = a; r[R8] += a; r[RSI] = (r[RSI] + stride) & 0xFFFFF;
        }
        r[RCX] = 0; insns = 8 * n;
    } else if (!strcmp(b, "call")) {
        for (uint64_t c = n; c; c--) { r[RDI] = c; r[RAX] = r[RDI] * 3 + r[RSI]; r[R8] += r[RAX]; }
        r[RCX] = 0; insns = 1 + 11 * n;      /* rbx restored by the leaf; rdi holds the last counter value (1); rsp balanced */
    } else if (!strcmp(b, "branchy")) {
        for (uint64_t c = n; c; c--) {
            r[RAX] = r[RAX] * r[RSI] + r[RDI]; r[RDX] = r[RAX] >> 40; insns += 4;
            insns += 2;                       /* test, jz */
            if (r[RDX] & 1) { r[R8] += r[RDX]; insns += 2; } else { r[R9] ^= r[RDX]; insns += 1; }
            insns += 2;                       /* test, jnz */
            if (!(r[RDX] & 6)) { r[R10]++; insns += 1; }
            insns += 2;                       /* dec, jnz */
        }
        r[RCX] = 0;
    } else if (!strcmp(b, "mixed")) {
        if (argc < 20) return 2;
        uint8_t *m = malloc(65536); FILE *f = fopen(argv[19], "rb"); if (!f || fread(m, 1, 65536, f) != 65536) return 3; fclose(f);
        for (uint64_t c = n; c; c--) {
            uint64_t o = r[RDI], a = ld(m, o); r[RAX] = a; r[R8] += a; r[RDX] = m[o + 1]; r[R9] = r[R9] * 31 ^ r[RDX];
            r[R10] = a >> 17; r[R8] ^= r[R10]; st(m, o, r[R8]); r[RDI] = (o + 8) & 0xfff8;
        }
        r[RCX] = 0; insns = 13 * n;
    } else return 2;
    for (int i = 0; i < 16; i++) printf("%016llx\n", (unsigned long long)r[i]);
    printf("%llu\n", (unsigned long long)insns);
    return 0;
}
