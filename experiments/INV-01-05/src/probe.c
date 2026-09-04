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

enum { M_TRACK = 0, M_TRAP = 1 };
/* Per-thread: a trapping probe on this thread must not change how a fault on
 * another thread is handled. M_TRACK is the zero-initialized default. */
static _Thread_local volatile int hmode = M_TRACK;

#define MAXREC 8192
static _Atomic size_t rec_n;
static struct { uintptr_t addr; int signo; int code; int tid; } rec[MAXREC];

/* Identity of the thread whose handler runs, assigned before any fault and read
 * from thread-local storage inside the handler. Without it the per-thread
 * attribution check is satisfied by construction. */
static _Thread_local int t_id;

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
		rec[i].tid = t_id;
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

struct tctx { size_t lo, hi, faults_seen; uint64_t *rt; size_t n; int id; };
static atomic_int start_gate;

static void *tfault(void *arg) {
	struct tctx *c = arg;
	t_id = c->id;
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
	struct tctx a = {0, PER, 0, rt0, PER, 1};
	struct tctx b = {region_pages / 2, region_pages / 2 + PER, 0, rt1, PER, 2};
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
	size_t matched = 0, mismatched = 0, no_id = 0;
	for (size_t i = 0; i < got && i < MAXREC; i++) {
		uintptr_t off = rec[i].addr - (uintptr_t)region;
		if (rec[i].addr < (uintptr_t)region || off >= region_pages * PG) continue;
		inside++;
		size_t page = off / PG;
		int in_range_of = (page >= a.lo && page < a.hi) ? 1
		                : (page >= b.lo && page < b.hi) ? 2 : 0;
		if (in_range_of == 1) in_a++;
		if (in_range_of == 2) in_b++;
		/* The control that can actually fail: the address must lie in the
		 * range of the thread whose handler recorded it. */
		if (rec[i].tid == 0) no_id++;
		else if (rec[i].tid == in_range_of) matched++;
		else mismatched++;
	}
	printf("  \"concurrent_tracking\": {\n");
	printf("    \"threads\": 2,\n");
	printf("    \"pages_written_per_thread\": %d,\n", PER);
	printf("    \"faults_recorded\": %zu,\n", got);
	printf("    \"fault_addresses_inside_region\": %zu,\n", inside);
	printf("    \"in_thread_a_range\": %zu,\n", in_a);
	printf("    \"in_thread_b_range\": %zu,\n", in_b);
	printf("    \"identity_matches_range\": %zu,\n", matched);
	printf("    \"identity_mismatches_range\": %zu,\n", mismatched);
	printf("    \"faults_without_thread_identity\": %zu,\n", no_id);
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
	printf("    \"write_to_unmapped\": {\"faulted\": %s, \"signal\": %d, \"si_code\": %d,\n",
	       b_ok ? "false" : "true", b_sig, b_code);
	printf("      \"caveat\": \"the address was obtained by munmap of a prior "
	       "mapping; a non-fault here may mean the range was remapped by another "
	       "allocation rather than that the platform did not fault\"}\n");
	printf("  },\n");
	munmap(m, PG);
}

/* ---- software dirty-bitmap alternative --------------------------------- */

static uint8_t sb_bitmap[1 << 20];

static uint64_t run_plain(volatile char *r, size_t span, int n) {
	uint64_t t0 = mach_absolute_time();
	for (int i = 0; i < n; i++) r[((size_t)i * 4096) % span] = (char)i;
	return mach_absolute_time() - t0;
}

static uint64_t run_marked(volatile char *r, size_t span, int n) {
	uint64_t t0 = mach_absolute_time();
	for (int i = 0; i < n; i++) {
		size_t off = ((size_t)i * 4096) % span;
		r[off] = (char)i;
		size_t g = off / 4096;
		sb_bitmap[g >> 3] |= (uint8_t)(1u << (g & 7));
	}
	return mach_absolute_time() - t0;
}

static int cmp_dbl(const void *a, const void *b) {
	double x = *(const double *)a, y = *(const double *)b;
	return x < y ? -1 : x > y ? 1 : 0;
}

/* Reports two whole-loop per-store figures and their paired marginal
 * difference. It asserts no bitmap cost: the marginal difference between a
 * cache-missing store and the same store plus a bitmap update is below what
 * this design resolves, and the record says so rather than attributing the
 * difference to the bitmap. A powered per-store measurement is owed to
 * Q-04-01. */
static volatile size_t sb_sink;

/* Index arithmetic only, no store to the region: the baseline against which the
 * bitmap update alone is measured. */
static uint64_t run_index_only(size_t span, int n) {
	size_t acc = 0;
	uint64_t t0 = mach_absolute_time();
	for (int i = 0; i < n; i++) acc += ((size_t)i * 4096) % span / 4096;
	uint64_t dt = mach_absolute_time() - t0;
	sb_sink = acc;
	return dt;
}

static uint64_t run_bitmap_only(size_t span, int n) {
	size_t acc = 0;
	uint64_t t0 = mach_absolute_time();
	for (int i = 0; i < n; i++) {
		size_t g = ((size_t)i * 4096) % span / 4096;
		sb_bitmap[g >> 3] |= (uint8_t)(1u << (g & 7));
		acc += g;
	}
	uint64_t dt = mach_absolute_time() - t0;
	sb_sink = acc;
	return dt;
}

static void emit_software_bitmap(void) {
	enum { N = 1 << 20, REPS = 8, WARMUP = 2 };
	volatile char *r = region;
	size_t span = region_pages * PG;
	uint64_t plain[REPS], marked[REPS];
	double paired[REPS];

	for (int w = 0; w < WARMUP; w++) {
		run_plain(r, span, N);
		run_marked(r, span, N);
	}
	for (int pass = 0; pass < REPS; pass++) {
		if (pass & 1) {
			marked[pass] = run_marked(r, span, N);
			plain[pass] = run_plain(r, span, N);
		} else {
			plain[pass] = run_plain(r, span, N);
			marked[pass] = run_marked(r, span, N);
		}
		paired[pass] = (ns(marked[pass]) - ns(plain[pass])) / N;
	}
	/* Isolated pair: the bitmap update against index arithmetic alone. The
	 * whole-loop pair above cannot yield a bitmap cost, because its baseline
	 * is a cache-missing store whose retirement the extra work perturbs; this
	 * pair removes that store from both sides. */
	uint64_t base[REPS], bits[REPS];
	double ipaired[REPS];
	for (int w = 0; w < WARMUP; w++) { run_index_only(span, N); run_bitmap_only(span, N); }
	for (int pass = 0; pass < REPS; pass++) {
		if (pass & 1) {
			bits[pass] = run_bitmap_only(span, N);
			base[pass] = run_index_only(span, N);
		} else {
			base[pass] = run_index_only(span, N);
			bits[pass] = run_bitmap_only(span, N);
		}
		ipaired[pass] = (ns(bits[pass]) - ns(base[pass])) / N;
	}
	int ipos = 0, ineg = 0;
	for (int i = 0; i < REPS; i++) { if (ipaired[i] > 0) ipos++; if (ipaired[i] < 0) ineg++; }
	qsort(ipaired, REPS, sizeof *ipaired, cmp_dbl);
	qsort(base, REPS, sizeof *base, cmp_u64);
	qsort(bits, REPS, sizeof *bits, cmp_u64);
	double base_spread = (ns(base[REPS - 1]) - ns(base[0])) / N;
	double bits_spread = (ns(bits[REPS - 1]) - ns(bits[0])) / N;
	double icrit = (base_spread + bits_spread) / 2.0;
	double imed = ipaired[REPS / 2];
	int iconsistent = (ipos == REPS) || (ineg == REPS);
	int iresolvable = iconsistent && (imed > icrit || imed < -icrit);

	qsort(paired, REPS, sizeof *paired, cmp_dbl);
	qsort(plain, REPS, sizeof *plain, cmp_u64);
	qsort(marked, REPS, sizeof *marked, cmp_u64);
	double pm = ns(plain[REPS / 2]) / N, mm = ns(marked[REPS / 2]) / N;

	printf("  \"software_dirty_bitmap\": {\n");
	printf("    \"stores_per_repetition\": %d,\n", N);
	printf("    \"repetitions\": %d,\n", REPS);
	printf("    \"warmup_passes_per_variant\": %d,\n", WARMUP);
	printf("    \"order_alternated_by_repetition\": true,\n");
	printf("    \"guest_page_granularity\": 4096,\n");
	printf("    \"bitmap_cost_asserted\": false,\n");
	printf("    \"whole_loop\": {\n");
	printf("      \"plain_ns_per_store\": %.4f,\n", pm);
	printf("      \"marked_ns_per_store\": %.4f,\n", mm);
	printf("      \"paired_marginal_median_ns_per_store\": %.4f,\n", paired[REPS / 2]);
	printf("      \"interpretable_as_bitmap_cost\": false,\n");
	printf("      \"why\": \"the baseline is a cache-missing store whose "
	       "retirement the added work perturbs; the two loops differ in more "
	       "than the bitmap update\"\n");
	printf("    },\n");
	printf("    \"isolated_bitmap_update\": {\n");
	printf("      \"index_only_ns_per_op\": %.4f,\n", ns(base[REPS / 2]) / N);
	printf("      \"index_plus_bitmap_ns_per_op\": %.4f,\n", ns(bits[REPS / 2]) / N);
	printf("      \"paired_marginal_median_ns_per_op\": %.4f,\n", imed);
	printf("      \"paired_marginal_min_ns_per_op\": %.4f,\n", ipaired[0]);
	printf("      \"paired_marginal_max_ns_per_op\": %.4f,\n", ipaired[REPS - 1]);
	printf("      \"paired_sign_consistent\": %s,\n", iconsistent ? "true" : "false");
	printf("      \"resolution_criterion_ns_per_op\": %.4f,\n", icrit);
	printf("      \"marginal_resolvable\": %s\n", iresolvable ? "true" : "false");
	printf("    }\n");
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
	{
		mach_timebase_info_data_t tb; mach_timebase_info(&tb);
		printf("  \"mach_timebase_info\": {\"numer\": %u, \"denom\": %u, "
		       "\"tick_ns\": %.6f},\n", tb.numer, tb.denom,
		       (double)tb.numer / (double)tb.denom);
	}
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
