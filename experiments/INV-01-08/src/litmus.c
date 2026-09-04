/*
 * INV-01-08 arm64 memory-model, atomics-granularity, and cross-thread
 * coordination probe.
 *
 * Capability probe only. It corroborates architectural statements cited from
 * the Arm ARM (DDI 0487 M.c) and the Intel SDM Vol 3A; it does not establish
 * them, and non-observation of a relaxed outcome never proves prohibition.
 *
 * Writes one JSON object to --out (or stdout).
 * --negative runs the litmus threads sequentially, which destroys the
 * positive control (no relaxed outcome can be observed); the checker must
 * flag that FAIL.
 */
#include <mach/mach_init.h>
#include <mach/mach_time.h>
#include <mach/thread_act.h>
#include <mach/thread_policy.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <setjmp.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static int g_negative = 0;
static long g_iters = 1000000; /* litmus iterations per variant */
static long g_rt = 20000;      /* round-trip samples */
static struct mach_timebase_info g_tb;

static inline uint64_t now_ns(void) {
	return (uint64_t)((__uint128_t)mach_absolute_time() * g_tb.numer / g_tb.denom);
}
static int cmp_i64(const void *a, const void *b) {
	int64_t x = *(const int64_t *)a, y = *(const int64_t *)b;
	return (x > y) - (x < y);
}
static void set_qos(qos_class_t q) { (void)pthread_set_qos_class_self_np(q, 0); }
static const char *qos_name(qos_class_t q) {
	return q == QOS_CLASS_USER_INTERACTIVE ? "user-interactive" : "background";
}

/* ---------------- litmus harness ----------------
 * Two threads, one shared cache-line-separated pair of locations. Each
 * iteration is fenced by a sense-reversing spin barrier so the two threads
 * actually overlap; without that the test cannot expose weak behaviour and
 * the positive control is meaningless.
 */

#define CL 128
struct lit {
	_Alignas(CL) volatile uint64_t x;
	_Alignas(CL) volatile uint64_t y;
	_Alignas(CL) atomic_int bar;
	_Alignas(CL) atomic_int sense;
	volatile uint64_t r0, r1;
	long iters;
	int variant; /* 0 plain, 1 acquire/release, 2 dmb ish */
	int test;    /* 0 SB, 1 MP */
	int sequential;
	long relaxed, total;
	qos_class_t q;
};

static inline void barrier2(struct lit *L, int *local_sense) {
	*local_sense = !*local_sense;
	if (atomic_fetch_add(&L->bar, 1) == 1) {
		atomic_store(&L->bar, 0);
		atomic_store(&L->sense, *local_sense);
	} else {
		while (atomic_load(&L->sense) != *local_sense) __asm__ volatile("yield");
	}
}

/* Each body is inline asm so the compiler cannot reorder, fold, or elide the
 * accesses under test. Verify with llvm-objdump before any evidence run. */
#define ST_PLAIN(p, v) __asm__ volatile("str %1, [%0]" ::"r"(p), "r"((uint64_t)(v)) : "memory")
#define LD_PLAIN(p, d) __asm__ volatile("ldr %0, [%1]" : "=r"(d) : "r"(p) : "memory")
#define ST_REL(p, v)   __asm__ volatile("stlr %1, [%0]" ::"r"(p), "r"((uint64_t)(v)) : "memory")
#define LD_ACQ(p, d)   __asm__ volatile("ldar %0, [%1]" : "=r"(d) : "r"(p) : "memory")
#define DMB_ISH()      __asm__ volatile("dmb ish" ::: "memory")

static void lit_body(struct lit *L, int side) {
	uint64_t d;
	volatile uint64_t *a = L->test == 0 ? (side == 0 ? &L->x : &L->y)
	                                    : (side == 0 ? &L->x : &L->y);
	volatile uint64_t *b = L->test == 0 ? (side == 0 ? &L->y : &L->x) : NULL;
	if (L->test == 0) { /* SB: store a; load b */
		switch (L->variant) {
		case 0: ST_PLAIN(a, 1); LD_PLAIN(b, d); break;
		case 1: ST_REL(a, 1);   LD_ACQ(b, d);   break;
		default: ST_PLAIN(a, 1); DMB_ISH(); LD_PLAIN(b, d); break;
		}
		if (side == 0) L->r0 = d; else L->r1 = d;
	} else { /* MP: writer stores data then flag; reader loads flag then data */
		if (side == 0) {
			switch (L->variant) {
			case 0: ST_PLAIN(&L->x, 1); ST_PLAIN(&L->y, 1); break;
			case 1: ST_PLAIN(&L->x, 1); ST_REL(&L->y, 1); break;
			default: ST_PLAIN(&L->x, 1); DMB_ISH(); ST_PLAIN(&L->y, 1); break;
			}
		} else {
			uint64_t f, v;
			switch (L->variant) {
			case 0: LD_PLAIN(&L->y, f); LD_PLAIN(&L->x, v); break;
			case 1: LD_ACQ(&L->y, f);   LD_PLAIN(&L->x, v); break;
			default: LD_PLAIN(&L->y, f); DMB_ISH(); LD_PLAIN(&L->x, v); break;
			}
			L->r0 = f; L->r1 = v;
		}
	}
}

struct litarg { struct lit *L; int side; };

static void *lit_thread(void *p) {
	struct litarg *a = p;
	struct lit *L = a->L;
	set_qos(L->q);
	int ls = 0;
	for (long i = 0; i < L->iters; i++) {
		barrier2(L, &ls);          /* both sides ready, locations are zero */
		lit_body(L, a->side);
		barrier2(L, &ls);          /* both sides done */
		if (a->side == 0) {        /* side 0 scores and resets */
			int relaxed = L->test == 0 ? (L->r0 == 0 && L->r1 == 0)
			                           : (L->r0 == 1 && L->r1 == 0);
			L->relaxed += relaxed;
			L->total++;
			L->x = L->y = 0;
			L->r0 = L->r1 = 0;
		}
		barrier2(L, &ls);          /* reset is visible before the next body */
	}
	return NULL;
}

/* Negative mode: run both sides on one thread, one after the other. No
 * interleaving is possible, so no relaxed outcome can ever appear. */
static void lit_sequential(struct lit *L) {
	for (long i = 0; i < L->iters; i++) {
		lit_body(L, 0);
		lit_body(L, 1);
		int relaxed = L->test == 0 ? (L->r0 == 0 && L->r1 == 0)
		                           : (L->r0 == 1 && L->r1 == 0);
		L->relaxed += relaxed;
		L->total++;
		L->x = L->y = 0;
		L->r0 = L->r1 = 0;
	}
}

static void run_litmus(FILE *f, int test, int variant, qos_class_t q, int last) {
	static const char *tn[] = {"SB", "MP"};
	static const char *vn[] = {"plain-ldr-str", "ldar-stlr", "dmb-ish"};
	struct lit *L = aligned_alloc(CL, ((sizeof(struct lit) + CL - 1) / CL) * CL);
	memset(L, 0, sizeof *L);
	L->iters = g_iters; L->variant = variant; L->test = test; L->q = q;
	atomic_store(&L->bar, 0); atomic_store(&L->sense, 0);
	uint64_t t0 = now_ns();
	if (g_negative) {
		L->sequential = 1;
		lit_sequential(L);
	} else {
		pthread_t th[2];
		struct litarg a0 = {L, 0}, a1 = {L, 1};
		pthread_create(&th[0], NULL, lit_thread, &a0);
		pthread_create(&th[1], NULL, lit_thread, &a1);
		pthread_join(th[0], NULL);
		pthread_join(th[1], NULL);
	}
	fprintf(f,
	        "    {\"test\":\"%s\",\"variant\":\"%s\",\"qos\":\"%s\","
	        "\"sequential\":%s,\"iterations\":%ld,\"relaxed_observed\":%ld,"
	        "\"elapsed_ns\":%llu}%s\n",
	        tn[test], vn[variant], qos_name(q), L->sequential ? "true" : "false",
	        L->total, L->relaxed, (unsigned long long)(now_ns() - t0),
	        last ? "" : ",");
	free(L);
}

/* ---------------- LSE atomics contention ---------------- */

struct lse { _Alignas(CL) volatile uint64_t c; long iters; int use_cas; qos_class_t q; };

static void *lse_thread(void *p) {
	struct lse *S = p;
	set_qos(S->q);
	volatile uint64_t *a = &S->c;
	for (long i = 0; i < S->iters; i++) {
		if (S->use_cas) {
			uint64_t old, want;
			do {
				old = __atomic_load_n(a, __ATOMIC_RELAXED);
				want = old;
				__asm__ volatile("cas %0, %2, [%1]"
				                 : "+r"(want)
				                 : "r"(a), "r"(old + 1)
				                 : "memory");
			} while (want != old);
		} else {
			uint64_t one = 1, dst;
			__asm__ volatile("ldadd %2, %0, [%1]"
			                 : "=r"(dst)
			                 : "r"(a), "r"(one)
			                 : "memory");
			(void)dst;
		}
	}
	return NULL;
}

static void run_lse(FILE *f, int nthreads, int use_cas, qos_class_t q, int last) {
	struct lse *S = aligned_alloc(CL, ((sizeof *S + CL - 1) / CL) * CL);
	memset(S, 0, sizeof *S);
	S->iters = 1000000; S->use_cas = use_cas; S->q = q;
	pthread_t th[8];
	uint64_t t0 = now_ns();
	for (int i = 0; i < nthreads; i++) pthread_create(&th[i], NULL, lse_thread, S);
	for (int i = 0; i < nthreads; i++) pthread_join(th[i], NULL);
	uint64_t el = now_ns() - t0;
	long expected = (long)S->iters * nthreads;
	fprintf(f,
	        "    {\"op\":\"%s\",\"threads\":%d,\"qos\":\"%s\",\"ops\":%ld,"
	        "\"counter\":%llu,\"lost_updates\":%lld,\"elapsed_ns\":%llu,"
	        "\"ns_per_op\":%.2f}%s\n",
	        use_cas ? "cas" : "ldadd", nthreads, qos_name(q), expected,
	        (unsigned long long)S->c, (long long)(expected - (long)S->c),
	        (unsigned long long)el, (double)el / (double)expected,
	        last ? "" : ",");
	free(S);
}

/* ---------------- misaligned atomic probe ---------------- */

static sigjmp_buf g_jb;
static volatile sig_atomic_t g_sig = 0, g_code = 0, g_in_probe = 0;

static void fault_handler(int s, siginfo_t *si, void *uc) {
	(void)uc;
	if (!g_in_probe) _exit(90); /* never swallow a fault we did not provoke */
	g_sig = s;
	g_code = si ? si->si_code : 0;
	siglongjmp(g_jb, 1);
}

struct alignres { const char *what; long off; int faulted, sig, code; uint32_t val; };

static void probe_one(volatile uint8_t *base, long off, const char *what,
                      int kind, struct alignres *out) {
	/* kind 0: ldaddal (32-bit LSE atomic), 1: plain 32-bit ldr, 2: ldar */
	out->what = what; out->off = off; out->faulted = 0; out->sig = 0;
	out->code = 0; out->val = 0;
	g_sig = g_code = 0;
	volatile uint8_t *p = base + off;
	g_in_probe = 1;
	if (sigsetjmp(g_jb, 1) == 0) {
		uint32_t add = 1, dst = 0;
		switch (kind) {
		case 0: __asm__ volatile("ldaddal %w2, %w0, [%1]" : "=r"(dst) : "r"(p), "r"(add) : "memory"); break;
		case 1: __asm__ volatile("ldr %w0, [%1]" : "=r"(dst) : "r"(p) : "memory"); break;
		default: __asm__ volatile("ldar %w0, [%1]" : "=r"(dst) : "r"(p) : "memory"); break;
		}
		out->val = dst;
	} else {
		out->faulted = 1; out->sig = g_sig; out->code = g_code;
	}
	g_in_probe = 0;
}

/* fallback worker for the hardware-refused-atomic case */
static pthread_mutex_t g_glob = PTHREAD_MUTEX_INITIALIZER;
struct fbarg { volatile uint8_t *p; long iters; int striped; };
static void *fallback_thread(void *a) {
	struct fbarg *F = a;
	(void)F->striped;
	for (long i = 0; i < F->iters; i++) {
		pthread_mutex_t *m = &g_glob;
		pthread_mutex_lock(m);
		uint32_t v;
		memcpy(&v, (const void *)F->p, 4);
		v++;
		memcpy((void *)F->p, &v, 4);
		pthread_mutex_unlock(m);
	}
	return NULL;
}

static void run_alignment(FILE *f) {
	struct sigaction sa;
	memset(&sa, 0, sizeof sa);
	sa.sa_sigaction = fault_handler;
	sa.sa_flags = SA_SIGINFO | SA_NODEFER;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGBUS, &sa, NULL);
	sigaction(SIGSEGV, &sa, NULL);

	volatile uint8_t *buf = mmap(NULL, 65536, PROT_READ | PROT_WRITE,
	                             MAP_PRIVATE | MAP_ANON, -1, 0);
	memset((void *)buf, 0, 65536);
	struct alignres r[6];
	int n = 0;
	probe_one(buf, 64, "ldaddal aligned 4B (control)", 0, &r[n++]);
	probe_one(buf, 65, "ldaddal misaligned within 16B granule", 0, &r[n++]);
	probe_one(buf, 78, "ldaddal crossing 16-byte boundary", 0, &r[n++]);
	probe_one(buf, 126, "ldaddal crossing 64-byte cache line", 0, &r[n++]);
	probe_one(buf, 126, "plain ldr crossing 64-byte cache line", 1, &r[n++]);
	probe_one(buf, 126, "ldar crossing 64-byte cache line", 2, &r[n++]);
	fprintf(f, "  \"alignment\": [\n");
	for (int i = 0; i < n; i++)
		fprintf(f,
		        "    {\"case\":\"%s\",\"offset\":%ld,\"faulted\":%s,"
		        "\"signal\":%d,\"si_code\":%d}%s\n",
		        r[i].what, r[i].off, r[i].faulted ? "true" : "false", r[i].sig,
		        r[i].code, i + 1 == n ? "" : ",");
	fprintf(f, "  ],\n");

	/* Software fallback cost for a case the hardware refuses: 4 threads doing
	 * a read-modify-write on a misaligned address under a global lock, then
	 * under a 64-way striped lock. */
	/* Cost of the software fallback a hardware-refused atomic would need.
	 * One global lock: a single contended address cannot be helped by
	 * address striping, so striping is not measured here. If a fallback ever
	 * has to cover many addresses, per-address striping is the upgrade path
	 * and needs its own arm. */
	struct fbarg fb = {buf + 78, 250000, 0};
	uint64_t t0 = now_ns();
	pthread_t th[4];
	for (int i = 0; i < 4; i++) pthread_create(&th[i], NULL, fallback_thread, &fb);
	for (int i = 0; i < 4; i++) pthread_join(th[i], NULL);
	uint64_t el = now_ns() - t0;
	fprintf(f,
	        "  \"fallback_global_lock\": {\"threads\":4,\"ops\":%ld,"
	        "\"elapsed_ns\":%llu,\"ns_per_op\":%.2f},\n",
	        fb.iters * 4, (unsigned long long)el,
	        (double)el / (double)(fb.iters * 4));
	munmap((void *)buf, 65536);
}

/* ---------------- cross-thread invalidation round trip ---------------- */
/* The host analogue of a cross-vCPU TLB shootdown handshake: one thread bumps
 * an epoch and waits for the peer to acknowledge with a full barrier. */

struct rt { _Alignas(CL) atomic_ullong epoch; _Alignas(CL) atomic_ullong ack;
            atomic_int stop; qos_class_t q; };

static void *rt_peer(void *p) {
	struct rt *R = p;
	set_qos(R->q);
	unsigned long long seen = 0;
	while (!atomic_load_explicit(&R->stop, memory_order_relaxed)) {
		unsigned long long e = atomic_load_explicit(&R->epoch, memory_order_acquire);
		if (e != seen) {
			seen = e;
			DMB_ISH();
			atomic_store_explicit(&R->ack, e, memory_order_release);
		}
	}
	return NULL;
}

static void run_roundtrip(FILE *f, qos_class_t q, int last) {
	struct rt *R = aligned_alloc(CL, ((sizeof *R + CL - 1) / CL) * CL);
	memset(R, 0, sizeof *R);
	R->q = q;
	atomic_store(&R->epoch, 0); atomic_store(&R->ack, 0); atomic_store(&R->stop, 0);
	pthread_t th;
	pthread_create(&th, NULL, rt_peer, R);
	usleep(20000);
	int64_t *lat = malloc(sizeof(int64_t) * (size_t)g_rt);
	long got = 0, timeouts = 0;
	for (long i = 0; i < g_rt; i++) {
		unsigned long long e = (unsigned long long)i + 1;
		uint64_t t0 = now_ns();
		atomic_store_explicit(&R->epoch, e, memory_order_release);
		while (atomic_load_explicit(&R->ack, memory_order_acquire) != e) {
			if (now_ns() - t0 > 1000000000ull) break;
		}
		if (atomic_load_explicit(&R->ack, memory_order_acquire) != e) { timeouts++; continue; }
		lat[got++] = (int64_t)(now_ns() - t0);
	}
	atomic_store(&R->stop, 1);
	pthread_join(th, NULL);
	qsort(lat, (size_t)got, sizeof(int64_t), cmp_i64);
	fprintf(f,
	        "    {\"qos\":\"%s\",\"n\":%ld,\"timeouts\":%ld,\"min_ns\":%lld,"
	        "\"p50_ns\":%lld,\"p99_ns\":%lld,\"max_ns\":%lld}%s\n",
	        qos_name(q), got, timeouts, got ? (long long)lat[0] : -1,
	        got ? (long long)lat[got / 2] : -1,
	        got ? (long long)lat[(long)(got * 0.99)] : -1,
	        got ? (long long)lat[got - 1] : -1, last ? "" : ",");
	free(lat); free(R);
}

/* ---------------- main ---------------- */

int main(int argc, char **argv) {
	const char *out = NULL;
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--negative")) g_negative = 1;
		else if (!strcmp(argv[i], "--out") && i + 1 < argc) out = argv[++i];
		else if (!strcmp(argv[i], "--iters") && i + 1 < argc) g_iters = atol(argv[++i]);
		else { fprintf(stderr, "usage: %s [--out F] [--iters N] [--negative]\n", argv[0]); return 2; }
	}
	mach_timebase_info(&g_tb);
	FILE *f = out ? fopen(out, "w") : stdout;
	if (!f) { perror("open out"); return 1; }
	set_qos(QOS_CLASS_USER_INTERACTIVE);

	fprintf(f, "{\n  \"experiment\": \"INV-01-08\",\n");
	fprintf(f, "  \"mode\": \"%s\",\n", g_negative ? "negative" : "normal");
	fprintf(f, "  \"litmus_iterations\": %ld,\n", g_iters);

	thread_affinity_policy_data_t ap = {1};
	kern_return_t kr = thread_policy_set(mach_thread_self(), THREAD_AFFINITY_POLICY,
	                                     (thread_policy_t)&ap, THREAD_AFFINITY_POLICY_COUNT);
	fprintf(f, "  \"thread_affinity_policy\": {\"kern_return_t\":%d,\"success\":%s},\n",
	        (int)kr, kr == KERN_SUCCESS ? "true" : "false");

	const qos_class_t qs[] = {QOS_CLASS_USER_INTERACTIVE, QOS_CLASS_BACKGROUND};
	fprintf(f, "  \"litmus\": [\n");
	for (int t = 0; t < 2; t++)
		for (int v = 0; v < 3; v++)
			for (int q = 0; q < 2; q++)
				run_litmus(f, t, v, qs[q], t == 1 && v == 2 && q == 1);
	fprintf(f, "  ],\n");

	fprintf(f, "  \"lse\": [\n");
	static const int nt[] = {2, 4, 8};
	for (int c = 0; c < 2; c++)
		for (int i = 0; i < 3; i++)
			run_lse(f, nt[i], c, QOS_CLASS_USER_INTERACTIVE, c == 1 && i == 2);
	fprintf(f, "  ],\n");

	run_alignment(f);

	fprintf(f, "  \"roundtrip\": [\n");
	for (int q = 0; q < 2; q++) run_roundtrip(f, qs[q], q == 1);
	fprintf(f, "  ]\n}\n");
	if (out) fclose(f);
	return 0;
}
