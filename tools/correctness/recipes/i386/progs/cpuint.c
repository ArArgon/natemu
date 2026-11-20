/* cpuint: an integer workload with a fixed answer (hashing, sorting, 64-bit arithmetic), built for i686 and, as
   cpuint.64, for x86-64: the same program in both modes, timed by run-i386.sh's sec= field (the checkpoints'
   32-bit against 64-bit comparison). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int cmp(const void *a, const void *b) { unsigned x = *(const unsigned *)a, y = *(const unsigned *)b; return (x > y) - (x < y); }

int main(void)
{
    enum { N = 1 << 16 };
    static unsigned v[N]; static unsigned char buf[1 << 16];
    unsigned x = 2463534242u, h = 2166136261u; unsigned long long acc = 0;
    for (int round = 0; round < 40; round++) {
        for (int i = 0; i < N; i++) { x ^= x << 13; x ^= x >> 17; x ^= x << 5; v[i] = x; buf[i] = x >> 11; }
        qsort(v, N, sizeof v[0], cmp);
        for (int i = 0; i < N; i++) { h = (h ^ buf[i]) * 16777619u; acc += (unsigned long long)v[i] * (h | 1) / (i + 3); }
        memmove(buf + 1, buf, sizeof buf - 1);
        for (int i = 0; i < N; i += 7) acc ^= acc << 7 ^ v[(v[i] ^ h) % N];
    }
    printf("cpuint %08x %016llx\n", h, acc);
    return 0;
}
