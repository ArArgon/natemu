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
#include <mach/mach_port.h>
#include <mach/mach_time.h>
#include <mach/thread_act.h>
#include <mach/thread_policy.h>
#include <errno.h>
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
#include <fcntl.h>
#include <sys/wait.h>
#include <limits.h>
#ifndef F08_REPO
#error F08_REPO must name the canonical reviewed workspace
#endif
#ifndef F08_CODE_REV
#error F08_CODE_REV must identify the producing revision
#endif

static int g_negative = 0;
static long g_iters = 1000000; /* litmus iterations per variant */
static long g_rt = 20000;      /* round-trip samples */
static struct mach_timebase_info g_tb;

static void fail_pthread(int rc, const char *what) {
	if (rc == 0) return;
	errno = rc;
	perror(what);
	exit(1);
}
static void *xaligned(size_t alignment, size_t size) {
	void *p = aligned_alloc(alignment, size);
	if (!p) { perror("aligned_alloc"); exit(1); }
	return p;
}
static long parse_positive(const char *s, const char *what) {
	char *end = NULL;
	errno = 0;
	long v = strtol(s, &end, 10);
	if (errno || !end || *end != '\0' || v <= 0) {
		fprintf(stderr, "%s must be a positive integer: %s\n", what, s);
		exit(2);
	}
	return v;
}

static inline uint64_t now_ns(void) {
	return (uint64_t)((__uint128_t)mach_absolute_time() * g_tb.numer / g_tb.denom);
}
static int cmp_i64(const void *a, const void *b) {
	int64_t x = *(const int64_t *)a, y = *(const int64_t *)b;
	return (x > y) - (x < y);
}
static void set_qos(qos_class_t q) {
	fail_pthread(pthread_set_qos_class_self_np(q, 0),
	             "pthread_set_qos_class_self_np");
}
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
	/* Full outcome histogram, indexed (r0 != 0) * 2 + (r1 != 0), so all four
	 * register-value combinations are recorded. BP-01-08-start requires this:
	 * a bare "not observed" count is uninterpretable on its own. */
	long hist[4];
	uint64_t rng; /* xorshift state for per-iteration start-offset jitter */
	qos_class_t q;
};

/* Small per-iteration delay on one side so the two sides do not always enter
 * the body in lockstep. Without it the three-phase barrier fixes the entry
 * offset and a whole class of interleavings is unreachable. */
static inline void jitter(struct lit *L, int side) {
	if (side != 1) return;
	L->rng ^= L->rng << 13; L->rng ^= L->rng >> 7; L->rng ^= L->rng << 17;
	for (unsigned n = (unsigned)(L->rng & 0x3f); n; n--) __asm__ volatile("nop");
}

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
#define LD_PLAIN(p, d) __asm__ volatile("ldr %0, [%1]" : "=&r"(d) : "r"(p) : "memory")
#define ST_REL(p, v)   __asm__ volatile("stlr %1, [%0]" ::"r"(p), "r"((uint64_t)(v)) : "memory")
#define LD_ACQ(p, d)   __asm__ volatile("ldar %0, [%1]" : "=&r"(d) : "r"(p) : "memory")
#define DMB_ISH()      __asm__ volatile("dmb ish" ::: "memory")

/* The evidence build inlines lit_body into lit_thread, where the C11
 * sense-reversing barrier also emits LDAR/STLR/DMB — so a whole-symbol grep
 * there cannot distinguish the litmus bodies from the barrier. `make disasm`
 * builds a second, otherwise identical binary with -DLITMUS_VERIFY, which
 * keeps lit_body a separate symbol so validate.sh can check the emitted
 * sequence of the bodies alone. Inlining cannot change which mnemonics the
 * inline-asm blocks emit; the verify build exists to prove a variant has not
 * degenerated in source. The measured binary is built without it. */
#ifdef LITMUS_VERIFY
__attribute__((noinline))
#endif
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
		jitter(L, a->side);
		lit_body(L, a->side);
		barrier2(L, &ls);          /* both sides done */
		if (a->side == 0) {        /* side 0 scores and resets */
			int relaxed = L->test == 0 ? (L->r0 == 0 && L->r1 == 0)
			                           : (L->r0 == 1 && L->r1 == 0);
			L->relaxed += relaxed;
			L->hist[(L->r0 != 0) * 2 + (L->r1 != 0)]++;
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
		L->hist[(L->r0 != 0) * 2 + (L->r1 != 0)]++;
		L->total++;
		L->x = L->y = 0;
		L->r0 = L->r1 = 0;
	}
}

static void run_litmus(FILE *f, int test, int variant, qos_class_t q, int last) {
	static const char *tn[] = {"SB", "MP"};
	static const char *vn[] = {"plain-ldr-str", "ldar-stlr", "dmb-ish"};
	struct lit *L = xaligned(CL, ((sizeof(struct lit) + CL - 1) / CL) * CL);
	memset(L, 0, sizeof *L);
	L->iters = g_iters; L->variant = variant; L->test = test; L->q = q;
	L->rng = 0x9e3779b97f4a7c15ull ^ ((uint64_t)test << 32) ^
	         ((uint64_t)variant << 16) ^ (uint64_t)q;
	atomic_store(&L->bar, 0); atomic_store(&L->sense, 0);
	uint64_t t0 = now_ns();
	if (g_negative) {
		L->sequential = 1;
		lit_sequential(L);
	} else {
		pthread_t th[2];
		struct litarg a0 = {L, 0}, a1 = {L, 1};
		fail_pthread(pthread_create(&th[0], NULL, lit_thread, &a0), "pthread_create");
		fail_pthread(pthread_create(&th[1], NULL, lit_thread, &a1), "pthread_create");
		fail_pthread(pthread_join(th[0], NULL), "pthread_join");
		fail_pthread(pthread_join(th[1], NULL), "pthread_join");
	}
	fprintf(f,
	        "    {\"test\":\"%s\",\"variant\":\"%s\",\"qos\":\"%s\","
	        "\"sequential\":%s,\"iterations\":%ld,\"relaxed_observed\":%ld,"
	        "\"histogram\":{\"r0=0,r1=0\":%ld,\"r0=0,r1!=0\":%ld,"
	        "\"r0!=0,r1=0\":%ld,\"r0!=0,r1!=0\":%ld},"
	        "\"start_offset_jitter\":true,\"elapsed_ns\":%llu}%s\n",
	        tn[test], vn[variant], qos_name(q), L->sequential ? "true" : "false",
	        L->total, L->relaxed,
	        L->hist[0], L->hist[1], L->hist[2], L->hist[3],
	        (unsigned long long)(now_ns() - t0),
	        last ? "" : ",");
	free(L);
}

/* ---------------- LSE atomics contention ---------------- */

struct lse { _Alignas(CL) volatile uint64_t c;
             _Alignas(CL) atomic_llong attempts;
             long iters; int use_cas; qos_class_t q; };

static void *lse_thread(void *p) {
	struct lse *S = p;
	set_qos(S->q);
	volatile uint64_t *a = &S->c;
	long local_attempts = 0;
	for (long i = 0; i < S->iters; i++) {
		if (S->use_cas) {
			uint64_t old, want;
			long tries = 0;
			do {
				old = __atomic_load_n(a, __ATOMIC_RELAXED);
				want = old;
				__asm__ volatile("cas %0, %2, [%1]"
				                 : "+r"(want)
				                 : "r"(a), "r"(old + 1)
				                 : "memory");
				tries++;
			} while (want != old);
			local_attempts += tries;
		} else {
			uint64_t one = 1, dst;
			__asm__ volatile("ldadd %2, %0, [%1]"
			                 : "=&r"(dst)
			                 : "r"(a), "r"(one)
			                 : "memory");
			(void)dst;
			local_attempts++;
		}
	}
	atomic_fetch_add(&S->attempts, (long long)local_attempts);
	return NULL;
}

static void run_lse(FILE *f, int nthreads, int use_cas, qos_class_t q, int last) {
	struct lse *S = xaligned(CL, ((sizeof *S + CL - 1) / CL) * CL);
	memset(S, 0, sizeof *S);
	S->iters = 1000000; S->use_cas = use_cas; S->q = q;
	pthread_t th[8];
	uint64_t t0 = now_ns();
	for (int i = 0; i < nthreads; i++)
		fail_pthread(pthread_create(&th[i], NULL, lse_thread, S), "pthread_create");
	for (int i = 0; i < nthreads; i++)
		fail_pthread(pthread_join(th[i], NULL), "pthread_join");
	uint64_t el = now_ns() - t0;
	long expected = (long)S->iters * nthreads;
	long long attempts = atomic_load(&S->attempts);
	/* ops/ns_per_op count successful increments; attempts/ns_per_attempt count
	 * executed atomic instructions. For `cas` under contention the two differ
	 * by retry amplification, and charging retries to the instruction's cost
	 * would misattribute the scaling (REV-044/REV-045). */
	fprintf(f,
	        "    {\"op\":\"%s\",\"threads\":%d,\"qos\":\"%s\",\"ops\":%ld,"
	        "\"attempts\":%lld,\"attempts_per_success\":%.4f,"
	        "\"counter\":%llu,\"lost_updates\":%lld,\"elapsed_ns\":%llu,"
	        "\"ns_per_op\":%.2f,\"ns_per_attempt\":%.2f}%s\n",
	        use_cas ? "cas" : "ldadd", nthreads, qos_name(q), expected,
	        attempts, (double)attempts / (double)expected,
	        (unsigned long long)S->c, (long long)(expected - (long)S->c),
	        (unsigned long long)el, (double)el / (double)expected,
	        (double)el / (double)(attempts ? attempts : 1),
	        last ? "" : ",");
	free(S);
}

/* ---------------- misaligned atomic probe ---------------- */

static sigjmp_buf g_jb;
static volatile sig_atomic_t g_sig = 0, g_code = 0, g_in_probe = 0;

_Static_assert(ATOMIC_LONG_LOCK_FREE == 2 && sizeof(uintptr_t) == sizeof(unsigned long),
               "signal probe address requires always-lock-free word atomic");
static _Atomic uintptr_t g_probe_address;

static void fault_handler(int s, siginfo_t *si, void *uc) {
	(void)uc;
	if (!g_in_probe || !si) _exit(90);
	uintptr_t address = atomic_load_explicit(&g_probe_address, memory_order_relaxed);
	if ((uintptr_t)si->si_addr < address || (uintptr_t)si->si_addr - address >= 4) _exit(90);
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
	g_in_probe = 0;
	atomic_store_explicit(&g_probe_address, (uintptr_t)p, memory_order_relaxed);
	if (sigsetjmp(g_jb, 1) == 0) {
		g_in_probe = 1;
		uint32_t add = 1, dst = 0;
		switch (kind) {
		case 0: __asm__ volatile("ldaddal %w2, %w0, [%1]" : "=&r"(dst) : "r"(p), "r"(add) : "memory"); break;
		case 1: __asm__ volatile("ldr %w0, [%1]" : "=&r"(dst) : "r"(p) : "memory"); break;
		default: __asm__ volatile("ldar %w0, [%1]" : "=&r"(dst) : "r"(p) : "memory"); break;
		}
		out->val = dst;
	} else {
		out->faulted = 1; out->sig = g_sig; out->code = g_code;
	}
	g_in_probe = 0;
}

#include "fallback.h"

static void run_alignment(FILE *f) {
	struct sigaction sa, prev_bus, prev_segv;
	memset(&sa, 0, sizeof sa);
	sa.sa_sigaction = fault_handler;
	sa.sa_flags = SA_SIGINFO | SA_NODEFER;
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGBUS, &sa, &prev_bus) != 0 ||
	    sigaction(SIGSEGV, &sa, &prev_segv) != 0) {
		perror("sigaction");
		exit(1);
	}

	volatile uint8_t *buf = mmap(NULL, 65536, PROT_READ | PROT_WRITE,
	                             MAP_PRIVATE | MAP_ANON, -1, 0);
	if (buf == MAP_FAILED) { perror("mmap"); exit(1); }
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

	munmap((void *)buf, 65536);
	/* Restore the previous dispositions so later arms do not run under a
	 * handler installed for this one (REV-045 required change 11). */
	if (sigaction(SIGBUS, &prev_bus, NULL) != 0 ||
	    sigaction(SIGSEGV, &prev_segv, NULL) != 0) {
		perror("sigaction restore");
		exit(1);
	}
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
	struct rt *R = xaligned(CL, ((sizeof *R + CL - 1) / CL) * CL);
	memset(R, 0, sizeof *R);
	R->q = q;
	atomic_store(&R->epoch, 0); atomic_store(&R->ack, 0); atomic_store(&R->stop, 0);
	pthread_t th;
	fail_pthread(pthread_create(&th, NULL, rt_peer, R), "pthread_create");
	usleep(20000);
	int64_t *lat = malloc(sizeof(int64_t) * (size_t)g_rt);
	if (!lat) { perror("malloc"); exit(1); }
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
	fail_pthread(pthread_join(th, NULL), "pthread_join");
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
#include "launch_guard.h"

int main(int argc, char **argv) {
	const char *out = NULL;
	const char *mode = "normal";
	int control = 0, standalone = 0, mode_seen = 0;
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--negative")) { if (mode_seen++) return 2; mode = "negative"; }
		else if (!strcmp(argv[i], "--mode") && i + 1 < argc) { if (mode_seen++) return 2; mode = argv[++i]; }
		else if (!strcmp(argv[i], "--out") && i + 1 < argc) out = argv[++i];
		else if (!strcmp(argv[i], "--iters") && i + 1 < argc)
			g_iters = parse_positive(argv[++i], "--iters");
		else { fprintf(stderr, "usage: %s [--out F] [--iters N] [--negative]\n", argv[0]); return 2; }
	}
	if (!strcmp(mode, "negative")) g_negative = 1;
	else if (!strcmp(mode, "fallback-drop-write")) { control = 1; standalone = 1; }
	else if (!strcmp(mode, "fallback-lock-error")) { control = 2; standalone = 1; }
	else if (!strcmp(mode, "fallback-only")) standalone = 1;
	else if (strcmp(mode, "normal") && strcmp(mode, "unexpected-fault")) return 2;
	if (g_iters != 1000000) { fputs("F08-2 requires fixed full litmus count\n", stderr); return 2; }
	if (mach_timebase_info(&g_tb) != KERN_SUCCESS || g_tb.numer == 0 || g_tb.denom == 0) {
		fprintf(stderr, "mach_timebase_info failed or returned an invalid ratio\n");
		return 1;
	}
	authorize_native(out, mode);
	if (!strcmp(mode, "unexpected-fault")) unexpected_fault_control();
	int output_fd = open(out, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
	if (output_fd == -1) { perror("exclusive output"); return 2; }
	FILE *f = fdopen(output_fd, "w");
	if (!f) { perror("fdopen"); close(output_fd); return 2; }
	set_qos(QOS_CLASS_USER_INTERACTIVE);

	fprintf(f, "{\n  \"experiment\": \"INV-01-08\",\n");
	fprintf(f, "  \"mode\": \"%s\",\n", mode);
	fprintf(f, "  \"litmus_iterations\": %ld,\n", g_iters);

	fprintf(f, "  \"timebase\": {\"numer\":%u,\"denom\":%u},\n",
	        g_tb.numer, g_tb.denom);


	fprintf(f, "  \"schema\":\"INV-01-08-result-v2\",\"method\":\"F08-2\","
	        "\"code_revision\":\"%s\",\"authorization_sha256\":\"%s\","
	        "\"code_bundle_sha256\":\"%s\",\n",
	        F08_CODE_REV, getenv("NATEMU_F08_AUTH_SHA256"), getenv("NATEMU_F08_BUNDLE_SHA256"));

	if (standalone) {
		int okay = run_fallback(f, control);
		fprintf(f, "  \"completed_normal\":false,\"standalone\":true}\n");
		if (fclose(f)) return 2;
		return okay ? 0 : 1;
	}
	thread_affinity_policy_data_t ap = {1};
	mach_port_t self = mach_thread_self();
	kern_return_t kr = thread_policy_set(self, THREAD_AFFINITY_POLICY,
	                                     (thread_policy_t)&ap, THREAD_AFFINITY_POLICY_COUNT);
	mach_port_deallocate(mach_task_self(), self);
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
	if (!run_fallback(f, 0)) {
		fputs("  \"completed_normal\":false}\n", f);
		fclose(f); return 1;
	}

	fprintf(f, "  \"roundtrip\": [\n");
	for (int q = 0; q < 2; q++) run_roundtrip(f, qs[q], q == 1);
	fprintf(f, "  ],\n  \"completed_normal\":%s,\"standalone\":false}\n", g_negative ? "false" : "true");
	if (fclose(f)) return 2;
	return 0;
}
