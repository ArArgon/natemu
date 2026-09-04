/*
 * INV-01-07 timer, clock, and cross-thread event probe.
 *
 * Measures host timekeeping and asynchronous-event facilities that a guest
 * timer/APIC and a guest IPI would have to be built on. Capability probe only:
 * it establishes no performance result and compares against no baseline.
 *
 * Writes one JSON object to the path given by --out (or stdout).
 * --negative injects a deliberate defect that the checker must flag FAIL.
 */
#include <dispatch/dispatch.h>
#include <errno.h>
#include <mach/mach_init.h>
#include <mach/mach_time.h>
#include <mach/thread_act.h>
#include <mach/thread_policy.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* mach_wait_until is declared in <mach/mach_time.h> on macOS. */

static int g_negative = 0;
static long g_n = 2000;         /* samples per wakeup/signal arm */
static long g_n_clock = 200000; /* samples for clock read cost / resolution */

static struct mach_timebase_info g_tb;

static inline uint64_t abs_to_ns(uint64_t a) {
	return (uint64_t)((__uint128_t)a * g_tb.numer / g_tb.denom);
}
static inline uint64_t ns_to_abs(uint64_t n) {
	return (uint64_t)((__uint128_t)n * g_tb.denom / g_tb.numer);
}
static inline uint64_t now_ns(void) { return abs_to_ns(mach_absolute_time()); }

/* ---------- small stats helpers ---------- */

static int cmp_i64(const void *a, const void *b) {
	int64_t x = *(const int64_t *)a, y = *(const int64_t *)b;
	return (x > y) - (x < y);
}

typedef struct {
	int64_t min, p50, p99, max;
	long n, early; /* early = samples strictly before the requested deadline */
} stats_t;

static stats_t summarize(int64_t *v, long n) {
	stats_t s = {0, 0, 0, 0, n, 0};
	if (n <= 0) return s;
	for (long i = 0; i < n; i++)
		if (v[i] < 0) s.early++;
	qsort(v, (size_t)n, sizeof(int64_t), cmp_i64);
	s.min = v[0];
	s.p50 = v[n / 2];
	s.p99 = v[(long)((double)n * 0.99) >= n ? n - 1 : (long)((double)n * 0.99)];
	s.max = v[n - 1];
	return s;
}

static void emit_stats(FILE *f, const char *name, const char *mech,
                       const char *qos, long target_ns, stats_t s) {
	fprintf(f,
	        "    {\"arm\":\"%s\",\"mechanism\":\"%s\",\"qos\":\"%s\","
	        "\"target_ns\":%ld,\"n\":%ld,\"early\":%ld,\"min_ns\":%lld,"
	        "\"p50_ns\":%lld,\"p99_ns\":%lld,\"max_ns\":%lld}",
	        name, mech, qos, target_ns, s.n, s.early, (long long)s.min,
	        (long long)s.p50, (long long)s.p99, (long long)s.max);
}

/* ---------- QoS ---------- */

static void set_qos(qos_class_t q) {
	(void)pthread_set_qos_class_self_np(q, 0);
}
static const char *qos_name(qos_class_t q) {
	return q == QOS_CLASS_USER_INTERACTIVE ? "user-interactive" : "background";
}

/* ---------- clock read cost and observed resolution ---------- */

struct clockinfo {
	const char *name;
	clockid_t id;
	int use_mach;     /* 1 = mach_absolute_time */
	int use_nsec_np;  /* 1 = clock_gettime_nsec_np */
};

static uint64_t read_clock(const struct clockinfo *c) {
	if (c->use_mach) return mach_absolute_time();
	if (c->use_nsec_np) return clock_gettime_nsec_np(c->id);
	struct timespec ts;
	clock_gettime(c->id, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void probe_clocks(FILE *f) {
	static const struct clockinfo cl[] = {
	    {"mach_absolute_time", 0, 1, 0},
	    {"clock_gettime_nsec_np(CLOCK_UPTIME_RAW)", CLOCK_UPTIME_RAW, 0, 1},
	    {"clock_gettime(CLOCK_MONOTONIC_RAW)", CLOCK_MONOTONIC_RAW, 0, 0},
	    {"clock_gettime(CLOCK_MONOTONIC)", CLOCK_MONOTONIC, 0, 0},
	    {"clock_gettime(CLOCK_UPTIME_RAW)", CLOCK_UPTIME_RAW, 0, 0},
	    {"clock_gettime(CLOCK_REALTIME)", CLOCK_REALTIME, 0, 0},
	};
	fprintf(f, "  \"clocks\": [\n");
	for (unsigned k = 0; k < sizeof cl / sizeof cl[0]; k++) {
		struct timespec res;
		int have_res = (cl[k].use_mach == 0 &&
		                clock_getres(cl[k].id, &res) == 0);
		/* read cost: median over batches of 1000 reads */
		enum { B = 200, PER = 1000 };
		int64_t cost[B];
		volatile uint64_t sink = 0;
		for (int b = 0; b < B; b++) {
			uint64_t t0 = mach_absolute_time();
			for (int i = 0; i < PER; i++) sink += read_clock(&cl[k]);
			uint64_t t1 = mach_absolute_time();
			cost[b] = (int64_t)(abs_to_ns(t1 - t0) / PER);
		}
		stats_t cs = summarize(cost, B);
		/* observed granularity: smallest nonzero forward step */
		uint64_t prev = read_clock(&cl[k]);
		uint64_t step = UINT64_MAX;
		long backward = 0;
		for (long i = 0; i < g_n_clock; i++) {
			uint64_t v = read_clock(&cl[k]);
			if (v < prev) backward++;
			else if (v > prev && v - prev < step) step = v - prev;
			prev = v;
		}
		uint64_t step_ns = (step == UINT64_MAX)
		                       ? 0
		                       : (cl[k].use_mach ? abs_to_ns(step) : step);
		char resbuf[32];
		if (have_res)
			snprintf(resbuf, sizeof resbuf, "%lld",
			         (long long)(res.tv_sec * 1000000000ll + res.tv_nsec));
		else
			snprintf(resbuf, sizeof resbuf, "null");
		fprintf(f,
		        "    {\"clock\":\"%s\",\"getres_ns\":%s,"
		        "\"read_cost_p50_ns\":%lld,\"read_cost_p99_ns\":%lld,"
		        "\"observed_step_ns\":%llu,\"backward_reads\":%ld,"
		        "\"reads\":%ld,\"sink\":%llu}%s\n",
		        cl[k].name, resbuf,
		        (long long)cs.p50, (long long)cs.p99,
		        (unsigned long long)step_ns, backward, g_n_clock,
		        (unsigned long long)sink,
		        k + 1 == sizeof cl / sizeof cl[0] ? "" : ",");
	}
	fprintf(f, "  ],\n");
}

/* ---------- cross-thread monotonic-clock comparability ---------- */
/* Routed here by BP-01-12-start: two vCPU threads reading the guest TSC must
 * never see time move backwards. Threads alternate under one lock and record
 * any inversion between successive timestamps taken on different threads. */

struct xclock {
	pthread_mutex_t m;
	uint64_t last;
	long turn, iters;
	atomic_long inversions;
	atomic_llong worst_backward_ns;
};

static void *xclock_thread(void *arg) {
	struct xclock *s = arg;
	for (long i = 0; i < s->iters; i++) {
		pthread_mutex_lock(&s->m);
		uint64_t t = now_ns();
		if (s->last && t < s->last) {
			atomic_fetch_add(&s->inversions, 1);
			long long d = (long long)(s->last - t);
			if (d > atomic_load(&s->worst_backward_ns))
				atomic_store(&s->worst_backward_ns, d);
		}
		s->last = t;
		pthread_mutex_unlock(&s->m);
	}
	return NULL;
}

static void probe_cross_thread_clock(FILE *f) {
	struct xclock s;
	memset(&s, 0, sizeof s);
	pthread_mutex_init(&s.m, NULL);
	s.iters = 200000;
	atomic_store(&s.inversions, 0);
	atomic_store(&s.worst_backward_ns, 0);
	pthread_t t[4];
	for (int i = 0; i < 4; i++) pthread_create(&t[i], NULL, xclock_thread, &s);
	for (int i = 0; i < 4; i++) pthread_join(t[i], NULL);
	long inv = atomic_load(&s.inversions);
	long long worst = atomic_load(&s.worst_backward_ns);
	if (g_negative) { inv += 1; worst = 137; } /* injected defect */
	fprintf(f,
	        "  \"cross_thread_clock\": {\"threads\":4,\"reads\":%ld,"
	        "\"inversions\":%ld,\"worst_backward_ns\":%lld},\n",
	        s.iters * 4, inv, worst);
	pthread_mutex_destroy(&s.m);
}

/* ---------- wakeup latency ---------- */

struct dt_ctx {
	dispatch_semaphore_t done;
	uint64_t fired_ns;
};

static void wakeup_arm(FILE *f, const char *mech, long target_ns,
                       qos_class_t q, int last) {
	int64_t *err = malloc(sizeof(int64_t) * (size_t)g_n);
	set_qos(q);
	for (long i = 0; i < g_n; i++) {
		uint64_t deadline_abs = mach_absolute_time() + ns_to_abs((uint64_t)target_ns);
		uint64_t woke;
		if (strcmp(mech, "mach_wait_until") == 0) {
			mach_wait_until(deadline_abs);
			woke = mach_absolute_time();
		} else if (strcmp(mech, "nanosleep") == 0) {
			struct timespec ts = {target_ns / 1000000000L,
			                      target_ns % 1000000000L};
			while (nanosleep(&ts, &ts) == -1 && errno == EINTR) {}
			woke = mach_absolute_time();
		} else { /* dispatch_source timer, leeway 0 */
			dispatch_queue_t qq = dispatch_get_global_queue(
			    q == QOS_CLASS_USER_INTERACTIVE ? QOS_CLASS_USER_INTERACTIVE
			                                    : QOS_CLASS_BACKGROUND,
			    0);
			dispatch_source_t src = dispatch_source_create(
			    DISPATCH_SOURCE_TYPE_TIMER, 0, 0, qq);
			__block uint64_t fired = 0;
			dispatch_semaphore_t sem = dispatch_semaphore_create(0);
			dispatch_source_set_event_handler(src, ^{
				fired = mach_absolute_time();
				dispatch_semaphore_signal(sem);
			});
			dispatch_source_set_timer(
			    src, dispatch_time(DISPATCH_TIME_NOW, target_ns),
			    DISPATCH_TIME_FOREVER, 0);
			dispatch_resume(src);
			dispatch_semaphore_wait(sem, DISPATCH_TIME_FOREVER);
			dispatch_source_cancel(src);
			dispatch_release(src);
			dispatch_release(sem);
			woke = fired;
		}
		int64_t e = (int64_t)abs_to_ns(woke) - (int64_t)abs_to_ns(deadline_abs);
		if (g_negative) e -= target_ns / 4; /* injected defect: impossible early wake */
		err[i] = e;
	}
	set_qos(QOS_CLASS_USER_INTERACTIVE);
	emit_stats(f, "wakeup", mech, qos_name(q), target_ns, summarize(err, g_n));
	fprintf(f, "%s\n", last ? "" : ",");
	free(err);
}

/* ---------- cross-thread event delivery ---------- */

struct sig_ctx {
	pthread_mutex_t m;
	pthread_cond_t cv;
	dispatch_semaphore_t sem;
	atomic_ullong send_ns, recv_ns;
	atomic_int ready, stop, flag;
	pthread_t target;
	qos_class_t q;
};
static struct sig_ctx *g_sig;

static void sigusr1_handler(int s) {
	(void)s;
	atomic_store(&g_sig->recv_ns, now_ns());
	atomic_store(&g_sig->flag, 1);
}

static void *cond_target(void *a) {
	struct sig_ctx *c = a;
	set_qos(c->q);
	pthread_mutex_lock(&c->m);
	atomic_store(&c->ready, 1);
	while (!atomic_load(&c->stop)) {
		pthread_cond_wait(&c->cv, &c->m);
		atomic_store(&c->recv_ns, now_ns());
		atomic_store(&c->flag, 1);
	}
	pthread_mutex_unlock(&c->m);
	return NULL;
}
static void *sem_target(void *a) {
	struct sig_ctx *c = a;
	set_qos(c->q);
	atomic_store(&c->ready, 1);
	while (!atomic_load(&c->stop)) {
		dispatch_semaphore_wait(c->sem, DISPATCH_TIME_FOREVER);
		atomic_store(&c->recv_ns, now_ns());
		atomic_store(&c->flag, 1);
	}
	return NULL;
}
/* The pthread_kill arm interrupts a thread that is *running*, not parked:
 * this is the closest public analogue of an IPI to a busy vCPU. */
static void *spin_target(void *a) {
	struct sig_ctx *c = a;
	set_qos(c->q);
	atomic_store(&c->ready, 1);
	volatile uint64_t x = 0;
	while (!atomic_load(&c->stop)) x++;
	return NULL;
}

static void signal_arm(FILE *f, const char *mech, qos_class_t q, int last) {
	struct sig_ctx c;
	memset(&c, 0, sizeof c);
	pthread_mutex_init(&c.m, NULL);
	pthread_cond_init(&c.cv, NULL);
	c.sem = dispatch_semaphore_create(0);
	c.q = q;
	g_sig = &c;
	void *(*fn)(void *) = strcmp(mech, "pthread_cond_signal") == 0 ? cond_target
	                      : strcmp(mech, "dispatch_semaphore_signal") == 0
	                          ? sem_target
	                          : spin_target;
	pthread_create(&c.target, NULL, fn, &c);
	while (!atomic_load(&c.ready)) sched_yield();
	usleep(20000);

	int64_t *lat = malloc(sizeof(int64_t) * (size_t)g_n);
	long lost = 0, got = 0;
	for (long i = 0; i < g_n; i++) {
		atomic_store(&c.flag, 0);
		uint64_t t0 = now_ns();
		atomic_store(&c.send_ns, t0);
		if (fn == cond_target) {
			pthread_mutex_lock(&c.m);
			pthread_cond_signal(&c.cv);
			pthread_mutex_unlock(&c.m);
		} else if (fn == sem_target) {
			dispatch_semaphore_signal(c.sem);
		} else {
			pthread_kill(c.target, SIGUSR1);
		}
		uint64_t spin_start = t0;
		while (!atomic_load(&c.flag)) {
			if (now_ns() - spin_start > 1000000000ull) break; /* 1 s timeout */
		}
		if (!atomic_load(&c.flag)) { lost++; continue; }
		int64_t d = (int64_t)atomic_load(&c.recv_ns) - (int64_t)t0;
		if (g_negative) d = -d; /* injected defect: negative delivery latency */
		lat[got++] = d;
		usleep(50);
	}
	atomic_store(&c.stop, 1);
	if (fn == cond_target) {
		pthread_mutex_lock(&c.m);
		pthread_cond_broadcast(&c.cv);
		pthread_mutex_unlock(&c.m);
	} else if (fn == sem_target) {
		dispatch_semaphore_signal(c.sem);
	}
	pthread_join(c.target, NULL);

	stats_t s = summarize(lat, got);
	fprintf(f,
	        "    {\"arm\":\"signal\",\"mechanism\":\"%s\",\"qos\":\"%s\","
	        "\"n\":%ld,\"lost\":%ld,\"early\":%ld,\"min_ns\":%lld,"
	        "\"p50_ns\":%lld,\"p99_ns\":%lld,\"max_ns\":%lld}%s\n",
	        mech, qos_name(q), s.n, lost, s.early, (long long)s.min,
	        (long long)s.p50, (long long)s.p99, (long long)s.max,
	        last ? "" : ",");
	free(lat);
	dispatch_release(c.sem);
	pthread_mutex_destroy(&c.m);
	pthread_cond_destroy(&c.cv);
}

/* ---------- thread affinity policy ---------- */

static void probe_affinity(FILE *f) {
	thread_affinity_policy_data_t p = {1};
	kern_return_t kr = thread_policy_set(
	    mach_thread_self(), THREAD_AFFINITY_POLICY, (thread_policy_t)&p,
	    THREAD_AFFINITY_POLICY_COUNT);
	fprintf(f,
	        "  \"thread_affinity_policy\": {\"call\":\"thread_policy_set("
	        "THREAD_AFFINITY_POLICY, tag=1)\",\"kern_return_t\":%d,"
	        "\"success\":%s},\n",
	        (int)kr, kr == KERN_SUCCESS ? "true" : "false");
}

/* ---------- main ---------- */

int main(int argc, char **argv) {
	const char *out = NULL;
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--negative")) g_negative = 1;
		else if (!strcmp(argv[i], "--out") && i + 1 < argc) out = argv[++i];
		else if (!strcmp(argv[i], "--n") && i + 1 < argc) g_n = atol(argv[++i]);
		else { fprintf(stderr, "usage: %s [--out F] [--n N] [--negative]\n", argv[0]); return 2; }
	}
	if (mach_timebase_info(&g_tb) != KERN_SUCCESS) { perror("timebase"); return 1; }
	struct sigaction sa;
	memset(&sa, 0, sizeof sa);
	sa.sa_handler = sigusr1_handler;
	sigemptyset(&sa.sa_mask);
	sa.sa_flags = SA_RESTART;
	if (sigaction(SIGUSR1, &sa, NULL) != 0) { perror("sigaction"); return 1; }

	FILE *f = out ? fopen(out, "w") : stdout;
	if (!f) { perror("open out"); return 1; }
	set_qos(QOS_CLASS_USER_INTERACTIVE);

	fprintf(f, "{\n  \"experiment\": \"INV-01-07\",\n");
	fprintf(f, "  \"mode\": \"%s\",\n", g_negative ? "negative" : "normal");
	fprintf(f, "  \"samples_per_arm\": %ld,\n", g_n);
	fprintf(f, "  \"timebase\": {\"numer\":%u,\"denom\":%u},\n", g_tb.numer, g_tb.denom);
	probe_affinity(f);
	probe_clocks(f);
	probe_cross_thread_clock(f);

	fprintf(f, "  \"wakeup\": [\n");
	static const long targets[] = {100000, 1000000, 10000000};
	static const char *mechs[] = {"mach_wait_until", "nanosleep", "dispatch_source_timer_leeway0"};
	const qos_class_t qs[] = {QOS_CLASS_USER_INTERACTIVE, QOS_CLASS_BACKGROUND};
	for (int m = 0; m < 3; m++)
		for (int t = 0; t < 3; t++)
			for (int q = 0; q < 2; q++)
				wakeup_arm(f, mechs[m], targets[t], qs[q],
				           m == 2 && t == 2 && q == 1);
	fprintf(f, "  ],\n");

	fprintf(f, "  \"signal\": [\n");
	static const char *smechs[] = {"pthread_cond_signal", "dispatch_semaphore_signal", "pthread_kill_SIGUSR1_spinning"};
	for (int m = 0; m < 3; m++)
		for (int q = 0; q < 2; q++)
			signal_arm(f, smechs[m], qs[q], m == 2 && q == 1);
	fprintf(f, "  ]\n}\n");
	if (out) fclose(f);
	return 0;
}
