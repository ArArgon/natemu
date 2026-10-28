/* final GPRs of the mem loop (stride, mask): usage mm N STRIDE MASK -> rax r8 rsi (hex) */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(int c, char **v) {
    uint64_t n = strtoull(v[1], 0, 0), stride = strtoull(v[2], 0, 0), mask = strtoull(v[3], 0, 0), rsi = 0, r8 = 0, rax = 0;
    uint8_t *m = calloc(1, 4 << 20);
    for (uint64_t k = n; k; k--) { uint64_t a; memcpy(&a, m + rsi, 8); a += k; memcpy(m + rsi, &a, 8); rax = a; r8 += a; rsi = (rsi + stride) & mask; }
    printf("%llx %llx %llx\n", (unsigned long long)rax, (unsigned long long)r8, (unsigned long long)rsi);
}
