/* INV-01-05 guest-memory write-tracking and invalidation-cost probe.
 * Emits one JSON object on stdout.
 *
 * --negative injects a defect in the fault handler (it records a null faulting
 * address instead of si_addr) that src/check.py must report as FAIL: a
 * tracking mechanism that cannot name the modified page is unsound.
 */
#include <errno.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <setjmp.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static int negative = 0;
static size_t PG;

/* ---- timing ------------------------------------------------------------ */
static double tb_num = 1.0, tb_den = 1.0;
static void timebase_init(void) {
	mach_timebase_info_data_t tb; mach_timebase_info(&tb);
	tb_num = tb.numer; tb_den = tb.denom;
}
static double ns(uint64_t t) { return (double)t * tb_num / tb_den; }
static int cmp_u64(const void *a, const void *b) {
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
	return x < y ? -1 : x > y ? 1 : 0;
}
static void stats(const char *name, uint64_t *v, size_t n, const char *tail) {
	if (n == 0) { printf("    \"%s\": null%s\n", name, tail); return; }
	qsort(v, n, sizeof *v, cmp_u64);
	double sum = 0; for (size_t i = 0; i < n; i++) sum += ns(v[i]);
	printf("    \"%s\": {\"n\": %zu, \"min_ns\": %.1f, \"median_ns\": %.1f, "
	       "\"p99_ns\": %.1f, \"max_ns\": %.1f, \"mean_ns\": %.1f}%s\n",
	       name, n, ns(v[0]), ns(v[n / 2]), ns(v[(size_t)(n * 0.99)]),
	       ns(v[n - 1]), sum / (double)n, tail);
}

/* ---- fault handling ---------------------------------------------------- */

enum { M_TRACK, M_TRAP };
static volatile int hmode = M_TRACK;

#define MAXREC 8192
static _Atomic size_t rec_n;
static struct { uintptr_t addr; int signo; int code; } rec[MAXREC];

static _Thread_local sigjmp_buf tjb;
static _Thread_local volatile sig_atomic_t t_signo, t_code;
static _Thread_local volatile uintptr_t t_addr;

static void onfault(int s, siginfo_t *si, void *uc) {
	(void)uc;
	uintptr_t a = si ? (uintptr_t)si->si_addr : 0;
	if (hmode == M_TRAP) {
		t_signo = s; t_code = si ? si->si_code : 0; t_addr = a;
		siglongjmp(tjb, 1);
	}
	size_t i = atomic_fetch_add(&rec_n, 1);
	if (i < MAXREC) {
		rec[i].addr = negative ? 0 : a;    /* injected defect when --negative */
		rec[i].signo = s;
		rec[i].code = si ? si->si_code : 0;
	}
	/* Unprotect the containing host page and let the faulting store retry. */
	void *pg = (void *)(a & ~(uintptr_t)(PG - 1));
	mprotect(pg, PG, PROT_READ | PROT_WRITE);
}

static void install(void) {
	struct sigaction sa; memset(&sa, 0, sizeof sa);
	sa.sa_sigaction = onfault; sa.sa_flags = SA_SIGINFO;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGSEGV, &sa, NULL);
	sigaction(SIGBUS, &sa, NULL);
}

/* ---- single-thread write tracking -------------------------------------- */

static char *region;
static size_t region_pages;

static void emit_tracking(void) {
	enum { N = 2000 };
	static uint64_t rt[N];
	size_t n = N < (int)region_pages ? N : region_pages;
	atomic_store(&rec_n, 0);
	mprotect(region, region_pages * PG, PROT_READ);
	for (size_t i = 0; i < n; i++) {
		volatile char *p = region + i * PG;
		uint64_t t0 = mach_absolute_time();
		*p = 1;                                   /* faults, handler resumes */
		rt[i] = mach_absolute_time() - t0;
	}
	size_t got = atomic_load(&rec_n);
	size_t inside = 0;
	for (size_t i = 0; i < got && i < MAXREC; i++) {
		uintptr_t a = rec[i].addr;
		if (a >= (uintptr_t)region && a < (uintptr_t)region + region_pages * PG)
			inside++;
	}
	printf("  \"write_tracking\": {\n");
	printf("    \"pages_written\": %zu,\n", n);
	printf("    \"faults_recorded\": %zu,\n", got);
	printf("    \"fault_addresses_inside_region\": %zu,\n", inside);
	printf("    \"first_signal\": %d,\n", got ? rec[0].signo : 0);
	printf("    \"first_si_code\": %d,\n", got ? rec[0].code : 0);
	stats("fault_round_trip", rt, n, "");
	printf("  },\n");
	mprotect(region, region_pages * PG, PROT_READ | PROT_WRITE);
}

/* ---- mprotect cost ----------------------------------------------------- */

static void emit_mprotect_cost(void) {
	enum { N = 5000 };
	static uint64_t c[N];
	for (int i = 0; i < N; i++) {
		uint64_t t0 = mach_absolute_time();
		mprotect(region, PG, (i & 1) ? PROT_READ : (PROT_READ | PROT_WRITE));
		c[i] = mach_absolute_time() - t0;
	}
	mprotect(region, PG, PROT_READ | PROT_WRITE);
	printf("  \"mprotect_cost\": {\n");
	printf("    \"range_bytes\": %zu,\n", PG);
	stats("per_call", c, N, "");
	printf("  },\n");
}

/* ---- concurrent faulting ----------------------------------------------- */

struct tctx { size_t lo, hi, faults_seen; uint64_t *rt; size_t n; };
static atomic_int start_gate;

static void *tfault(void *arg) {
	struct tctx *c = arg;
	while (!atomic_load(&start_gate)) ;
	for (size_t i = 0; i < c->n; i++) {
		volatile char *p = region + (c->lo + i) * PG;
		uint64_t t0 = mach_absolute_time();
		*p = 1;
		c->rt[i] = mach_absolute_time() - t0;
	}
	return NULL;
}

static void emit_concurrent(void) {
	enum { PER = 500 };
	static uint64_t rt0[PER], rt1[PER];
	struct tctx a = {0, PER, 0, rt0, PER};
	struct tctx b = {region_pages / 2, region_pages / 2 + PER, 0, rt1, PER};
	if (region_pages / 2 + PER > region_pages) { printf("  \"concurrent\": null,\n"); return; }
	atomic_store(&rec_n, 0);
	atomic_store(&start_gate, 0);
	mprotect(region, region_pages * PG, PROT_READ);
	pthread_t t0, t1;
	pthread_create(&t0, NULL, tfault, &a);
	pthread_create(&t1, NULL, tfault, &b);
	atomic_store(&start_gate, 1);
	pthread_join(t0, NULL); pthread_join(t1, NULL);
	size_t got = atomic_load(&rec_n), inside = 0, in_a = 0, in_b = 0;
	for (size_t i = 0; i < got && i < MAXREC; i++) {
		uintptr_t off = rec[i].addr - (uintptr_t)region;
		if (rec[i].addr < (uintptr_t)region || off >= region_pages * PG) continue;
		inside++;
		size_t page = off / PG;
		if (page >= a.lo && page < a.hi) in_a++;
		if (page >= b.lo && page < b.hi) in_b++;
	}
	printf("  \"concurrent_tracking\": {\n");
	printf("    \"threads\": 2,\n");
	printf("    \"pages_written_per_thread\": %d,\n", PER);
	printf("    \"faults_recorded\": %zu,\n", got);
	printf("    \"fault_addresses_inside_region\": %zu,\n", inside);
	printf("    \"in_thread_a_range\": %zu,\n", in_a);
	printf("    \"in_thread_b_range\": %zu,\n", in_b);
	stats("fault_round_trip_thread_a", rt0, PER, ",");
	stats("fault_round_trip_thread_b", rt1, PER, "");
	printf("  },\n");
	mprotect(region, region_pages * PG, PROT_READ | PROT_WRITE);
}

/* ---- granularity ------------------------------------------------------- */

static int writable_probe(volatile char *p) {
	hmode = M_TRAP;
	int ok;
	if (sigsetjmp(tjb, 1) == 0) { *p = 7; ok = 1; } else ok = 0;
	hmode = M_TRACK;
	return ok;
}

static void emit_granularity(void) {
	const size_t guest = 4096;
	char *p = mmap(NULL, PG * 2, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
	printf("  \"granularity\": {\n");
	printf("    \"host_page\": %zu,\n    \"guest_page\": %zu,\n", PG, guest);
	if (p == MAP_FAILED) { printf("    \"mapped\": false\n  },\n"); return; }
	memset(p, 0, PG * 2);
	errno = 0;
	int rc = mprotect(p + guest, guest, PROT_READ);
	int e = errno;
	printf("    \"mapped\": true,\n");
	printf("    \"sub_page_mprotect_rc\": %d,\n", rc);
	printf("    \"sub_page_mprotect_errno\": %d,\n", rc ? e : 0);
	printf("    \"sub_page_mprotect_errno_str\": \"%s\",\n", rc ? strerror(e) : "");
	/* host-page-aligned control: the same call at host-page granularity */
	errno = 0;
	int rc2 = mprotect(p, PG, PROT_READ);
	int e2 = errno;
	printf("    \"host_page_mprotect_rc\": %d,\n", rc2);
	printf("    \"host_page_mprotect_errno\": %d,\n", rc2 ? e2 : 0);
	printf("    \"guest_slot_writable_after_host_page_protect\": [");
	for (size_t i = 0; i < PG * 2 / guest; i++)
		printf("%s%s", i ? ", " : "", writable_probe(p + i * guest) ? "true" : "false");
	printf("]\n  },\n");
	munmap(p, PG * 2);
}

/* ---- SIGSEGV versus SIGBUS --------------------------------------------- */

static void emit_signal_classes(void) {
	char *m = mmap(NULL, PG, PROT_READ, MAP_PRIVATE | MAP_ANON, -1, 0);
	char *u = mmap(NULL, PG, PROT_NONE, MAP_PRIVATE | MAP_ANON, -1, 0);
	munmap(u, PG);                                   /* now unmapped */
	printf("  \"signal_classes\": {\n");
	hmode = M_TRAP;
	int a_ok = 1, b_ok = 1;
	int a_sig = 0, a_code = 0, b_sig = 0, b_code = 0;
	if (sigsetjmp(tjb, 1) == 0) { *(volatile char *)m = 1; }
	else { a_ok = 0; a_sig = t_signo; a_code = t_code; }
	if (sigsetjmp(tjb, 1) == 0) { *(volatile char *)u = 1; }
	else { b_ok = 0; b_sig = t_signo; b_code = t_code; }
	hmode = M_TRACK;
	printf("    \"write_to_mapped_read_only\": {\"faulted\": %s, \"signal\": %d, \"si_code\": %d},\n",
	       a_ok ? "false" : "true", a_sig, a_code);
	printf("    \"write_to_unmapped\": {\"faulted\": %s, \"signal\": %d, \"si_code\": %d}\n",
	       b_ok ? "false" : "true", b_sig, b_code);
	printf("  },\n");
	munmap(m, PG);
}

/* ---- software dirty-bitmap alternative --------------------------------- */

static uint8_t sb_bitmap[1 << 20];

static uint64_t run_plain(volatile char *r, size_t span, int n) {
	uint64_t t0 = mach_absolute_time();
	for (int i = 0; i < n; i++) r[(size_t)(i * 4096) % span] = (char)i;
	return mach_absolute_time() - t0;
}

static uint64_t run_marked(volatile char *r, size_t span, int n) {
	uint64_t t0 = mach_absolute_time();
	for (int i = 0; i < n; i++) {
		size_t off = (size_t)(i * 4096) % span;
		r[off] = (char)i;
		size_t g = off / 4096;
		sb_bitmap[g >> 3] |= (uint8_t)(1u << (g & 7));
	}
	return mach_absolute_time() - t0;
}

static void emit_software_bitmap(void) {
	enum { N = 1 << 20, REPS = 8 };
	volatile char *r = region;
	size_t span = region_pages * PG;
	uint64_t plain[REPS], marked[REPS];

	/* Warm-up passes are discarded so first-touch and cache-cold cost is not
	 * charged to whichever variant happens to run first, and the two variants
	 * swap positions on alternate repetitions so a position effect (clock ramp)
	 * cancels instead of biasing one variant. */
	run_plain(r, span, N);
	run_marked(r, span, N);
	for (int pass = 0; pass < REPS; pass++) {
		if (pass & 1) {
			marked[pass] = run_marked(r, span, N);
			plain[pass] = run_plain(r, span, N);
		} else {
			plain[pass] = run_plain(r, span, N);
			marked[pass] = run_marked(r, span, N);
		}
	}
	qsort(plain, REPS, sizeof *plain, cmp_u64);
	qsort(marked, REPS, sizeof *marked, cmp_u64);
	double pm = ns(plain[REPS / 2]) / N, mm = ns(marked[REPS / 2]) / N;
	/* Run-to-run spread of the unmodified loop bounds what this microbenchmark
	 * can resolve; a difference inside it is not a measurement of the bitmap. */
	double spread = (ns(plain[REPS - 1]) - ns(plain[0])) / N;
	double d = mm - pm;
	printf("  \"software_dirty_bitmap\": {\n");
	printf("    \"stores\": %d,\n", N);
	printf("    \"repetitions\": %d,\n", REPS);
	printf("    \"warmup_pass\": true,\n");
	printf("    \"order_alternated\": true,\n");
	printf("    \"guest_page_granularity\": 4096,\n");
	printf("    \"plain_store_ns_per_store\": %.3f,\n", pm);
	printf("    \"plain_store_ns_per_store_min\": %.3f,\n", ns(plain[0]) / N);
	printf("    \"store_plus_bitmap_ns_per_store\": %.3f,\n", mm);
	printf("    \"store_plus_bitmap_ns_per_store_min\": %.3f,\n", ns(marked[0]) / N);
	printf("    \"resolution_ns_per_store\": %.3f,\n", spread);
	printf("    \"overhead_ns_per_store\": %.3f,\n", d);
	printf("    \"overhead_resolvable\": %s\n", (d > spread || d < -spread) ? "true" : "false");
	printf("  }\n");
}

int main(int argc, char **argv) {
	for (int i = 1; i < argc; i++)
		if (strcmp(argv[i], "--negative") == 0) negative = 1;
	timebase_init();
	PG = (size_t)getpagesize();
	install();
	region_pages = 4096;                              /* 64 MiB at 16 KiB pages */
	region = mmap(NULL, region_pages * PG, PROT_READ | PROT_WRITE,
	              MAP_PRIVATE | MAP_ANON, -1, 0);
	printf("{\n");
	printf("  \"investigation\": \"INV-01-05\",\n");
	printf("  \"mode\": \"%s\",\n", negative ? "negative" : "normal");
	printf("  \"host_page_size\": %zu,\n", PG);
	if (region == MAP_FAILED) { printf("  \"region_mapped\": false\n}\n"); return 0; }
	printf("  \"region_mapped\": true,\n");
	printf("  \"region_pages\": %zu,\n", region_pages);
	memset(region, 0, region_pages * PG);             /* first-touch beforehand */
	emit_tracking();
	emit_mprotect_cost();
	emit_concurrent();
	emit_granularity();
	emit_signal_classes();
	emit_software_bitmap();
	printf("}\n");
	return 0;
}
