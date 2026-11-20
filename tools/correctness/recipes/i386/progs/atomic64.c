/* atomic64: gcc's 64-bit atomic load and store on i686 (no SSE): FILD m64 then FISTP m64, 8 bytes moved through an x87
   register. Exact on hardware (a 64-bit significand); with x87 registers held as doubles, a value above 2^53 loses its
   low bits. CPython 3.13 for i386 has 27 such pairs (docs/restructure/m2-task0-findings.md). */
#include <stdio.h>

static long long values[] = { 1, -1, (1ll << 53) + 1, 0x7fffffffffffffffll, 0x123456789abcdef1ll, -0x7edcba9876543211ll };

int main(void)
{
    int bad = 0;
    for (unsigned i = 0; i < sizeof values / sizeof values[0]; i++) {
        long long got, copy;
        __atomic_store_n(&copy, __atomic_load_n(&values[i], __ATOMIC_SEQ_CST), __ATOMIC_SEQ_CST);
        got = __atomic_load_n(&copy, __ATOMIC_RELAXED);
        printf("%llx -> %llx\n", values[i], got); bad += got != values[i];
    }
    printf("changed %d\n", bad);
    return 0;
}
