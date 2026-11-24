// repmicro: the cost of REP MOVS copies (docs/restructure/bulk-rep-plan.md section 8, the bulk REP plan's kill-test).
//
//     repmicro RUN SUMSFILE     the timed rows (init's bench=repmicro; RUN 1 or 2): this build's rows only
//     repmicro --sums           every row's checksum (both builds' rows) from a byte-by-byte copy, untimed (build.sh pins
//                               them in init)
//
// Built twice from this file: 64-bit (`repmicro`, the 64-bit rows) and with -m32 (`repmicro32`, the 32-bit rows). Each row
// is one REP MOVS of a fixed size between fixed buffer offsets, repeated for about 50 ms, three rounds; it prints
//     NATEMU-REPMICRO run=R bits=B row=NAME bytes=N iters=I ns_med=M ns_min=A ns_max=Z sum=HEX
// (ns per copy: the median, least and most of the three rounds), then writes `NAME=HEX ...` (one line) to SUMSFILE. A row's
// checksum is FNV-1a 64 over its destination and 64 bytes either side after one copy from freshly filled buffers; --sums
// computes the same with a forward byte-by-byte copy, the SDM's element order for every row here (DF is 0; the overlap
// row's destination is above its source), so the image's check compares the guest's REP results with that model.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

enum op { MOVSB, MOVSQ, MOVSQ_TAIL, MOVSD };          // MOVSQ_TAIL: rep movsq of n/8 quadwords, then rep movsb of n%8 bytes
struct row { const char *name; int bits; enum op op; uint32_t bytes, src_off, dst_off; int same_buffer; };

// Offsets are from each buffer's start (page-aligned, with a guard page before it); same_buffer: dst inside src's buffer.
static const struct row ROWS[] = {
    // 64-bit, hot: pointers page-aligned
    {"movsb-16", 64, MOVSB, 16, 0, 0, 0}, {"movsb-64", 64, MOVSB, 64, 0, 0, 0}, {"movsb-128", 64, MOVSB, 128, 0, 0, 0},
    {"movsb-512", 64, MOVSB, 512, 0, 0, 0}, {"movsb-4096", 64, MOVSB, 4096, 0, 0, 0},
    {"movsb-65536", 64, MOVSB, 65536, 0, 0, 0},
    {"movsq-64", 64, MOVSQ, 64, 0, 0, 0}, {"movsq-100", 64, MOVSQ_TAIL, 100, 0, 0, 0},
    {"movsq-128", 64, MOVSQ, 128, 0, 0, 0}, {"movsq-512", 64, MOVSQ, 512, 0, 0, 0}, {"movsq-4096", 64, MOVSQ, 4096, 0, 0, 0},
    // 64-bit, short (under one 16-byte round)
    {"movsb-1", 64, MOVSB, 1, 0, 0, 0}, {"movsb-3", 64, MOVSB, 3, 0, 0, 0}, {"movsb-7", 64, MOVSB, 7, 0, 0, 0},
    {"movsb-15", 64, MOVSB, 15, 0, 0, 0}, {"movsq-8", 64, MOVSQ, 8, 0, 0, 0},
    // 64-bit, declined or cut by the rooms: source 4 mod 8; destination 8 above the source (the overlap skip); the source
    // at page offset 0x808 (the two page ends differ)
    {"movsq-4096-src4", 64, MOVSQ, 4096, 4, 0, 0}, {"movsb-4096-overlap8", 64, MOVSB, 4096, 0, 8, 1},
    {"movsq-4096-src808", 64, MOVSQ, 4096, 0x808, 0, 0},
    // 32-bit (the -m32 build): 1, 2 and 3 doublewords; 4 KiB with both pointers 8-aligned and with both 4 mod 8
    {"movsd-4", 32, MOVSD, 4, 0, 0, 0}, {"movsd-8", 32, MOVSD, 8, 0, 0, 0}, {"movsd-12", 32, MOVSD, 12, 0, 0, 0},
    {"movsd-4096", 32, MOVSD, 4096, 0, 0, 0}, {"movsd-4096-off4", 32, MOVSD, 4096, 4, 4, 0},
};
#define NROWS (sizeof ROWS / sizeof ROWS[0])
#define BUF (65536 + 3 * 4096)                    // the largest copy, its offsets and the guard bytes after it
#define GUARD 64

static uint8_t *src_buf, *dst_buf;

static uint64_t now_ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000u + (uint64_t)t.tv_nsec;
}

static void fill(void)
{
    for (uint32_t i = 0; i < BUF; i++) { src_buf[i] = (uint8_t)(i * 131u + 7u) ^ (uint8_t)(i >> 8); dst_buf[i] = (uint8_t)~i; }
}

static uint8_t *src_of(const struct row *r) { return src_buf + r->src_off; }
static uint8_t *dst_of(const struct row *r) { return (r->same_buffer ? src_buf : dst_buf) + r->dst_off; }

static void copy(const struct row *r)
{
    void *d = dst_of(r);
    const void *s = src_of(r);
    unsigned long n;
    switch (r->op) {
    case MOVSB: n = r->bytes; __asm__ volatile("rep movsb" : "+D"(d), "+S"(s), "+c"(n) : : "memory"); break;
    case MOVSD: n = r->bytes / 4; __asm__ volatile("rep movsl" : "+D"(d), "+S"(s), "+c"(n) : : "memory"); break;
#if defined(__x86_64__)
    case MOVSQ: n = r->bytes / 8; __asm__ volatile("rep movsq" : "+D"(d), "+S"(s), "+c"(n) : : "memory"); break;
    case MOVSQ_TAIL:
        n = r->bytes / 8; __asm__ volatile("rep movsq" : "+D"(d), "+S"(s), "+c"(n) : : "memory");
        n = r->bytes % 8; __asm__ volatile("rep movsb" : "+D"(d), "+S"(s), "+c"(n) : : "memory");
        break;
#else
    default: abort();
#endif
    }
}

static void model(const struct row *r)          // forward, one byte at a time
{
    volatile uint8_t *d = dst_of(r);
    const volatile uint8_t *s = src_of(r);
    for (uint32_t i = 0; i < r->bytes; i++) d[i] = s[i];
}

static uint64_t checksum(const struct row *r)
{
    const uint8_t *d = dst_of(r) - GUARD;
    uint64_t h = 0xcbf29ce484222325ull;
    for (uint32_t i = 0; i < r->bytes + 2 * GUARD; i++) { h ^= d[i]; h *= 0x100000001b3ull; }
    return h;
}

static uint64_t row_sum(const struct row *r, int use_model)
{
    fill();
    if (use_model) model(r); else copy(r);
    return checksum(r);
}

static uint64_t time_iters(const struct row *r, uint64_t iters)
{
    uint64_t t0 = now_ns();
    for (uint64_t i = 0; i < iters; i++) copy(r);
    return now_ns() - t0;
}

static int cmp(const void *a, const void *b)
{
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y;
}

int main(int argc, char **argv)
{
    int sums = argc == 2 && !strcmp(argv[1], "--sums");
    if (!sums && argc != 3) { fprintf(stderr, "usage: repmicro RUN SUMSFILE | repmicro --sums\n"); return 2; }
    src_buf = (uint8_t *)aligned_alloc(4096, BUF + 2 * 4096) + 4096;     // a page before each buffer for the guard bytes
    dst_buf = (uint8_t *)aligned_alloc(4096, BUF + 2 * 4096) + 4096;
    if (src_buf == (uint8_t *)4096 || dst_buf == (uint8_t *)4096) { perror("repmicro: buffers"); return 2; }
    memset(src_buf - 4096, 0, BUF + 2 * 4096); memset(dst_buf - 4096, 0, BUF + 2 * 4096);
    int bits = (int)(8 * sizeof(void *));
    char line[4096] = "";
    size_t at = 0;
    for (unsigned k = 0; k < NROWS; k++) {
        const struct row *r = &ROWS[k];
        if (!sums && r->bits != bits) continue;
        if (sums) {
            at += (size_t)snprintf(line + at, sizeof line - at, "%s%s=%016llx", at ? " " : "", r->name,
                                   (unsigned long long)row_sum(r, 1));
            continue;
        }
        fill();
        uint64_t iters = 1;                        // calibrate: about 5 ms, then 50 ms a round
        for (;;) { uint64_t t = time_iters(r, iters); if (t >= 5000000 || iters >= (1ull << 40)) { iters = iters * 50000000 / (t ? t : 1); break; } iters *= 2; }
        if (!iters) iters = 1;
        double ns[3];
        for (int round = 0; round < 3; round++) ns[round] = (double)time_iters(r, iters) / (double)iters;
        qsort(ns, 3, sizeof ns[0], cmp);
        uint64_t h = row_sum(r, 0);
        printf("NATEMU-REPMICRO run=%s bits=%d row=%s bytes=%u iters=%llu ns_med=%.2f ns_min=%.2f ns_max=%.2f sum=%016llx\n",
               argv[1], bits, r->name, r->bytes, (unsigned long long)iters, ns[1], ns[0], ns[2], (unsigned long long)h);
        fflush(stdout);
        at += (size_t)snprintf(line + at, sizeof line - at, "%s%s=%016llx", at ? " " : "", r->name, (unsigned long long)h);
    }
    if (sums) { printf("%s\n", line); return 0; }
    FILE *fo = fopen(argv[2], "w");
    if (!fo || fprintf(fo, "%s\n", line) < 0 || fclose(fo)) { perror("repmicro: sums file"); return 2; }
    return 0;
}
