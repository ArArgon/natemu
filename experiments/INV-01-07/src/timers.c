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
#include <mach/mach_port.h>
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
static int g_control_selftest = 0;
static long g_n = 2000;         /* samples per wakeup/signal arm */
static long g_n_clock = 200000; /* samples for clock read cost / resolution */

static struct mach_timebase_info g_tb;
static uint64_t g_uptime_raw_resolution_ns;

static void fail_pthread(int rc, const char *what) {
	if (rc == 0) return;
	errno = rc;
	perror(what);
	exit(1);
}
static void *xmalloc(size_t size) {
	void *p = malloc(size);
	if (!p) { perror("malloc"); exit(1); }
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

static inline uint64_t abs_to_ns(uint64_t a) {
	return (uint64_t)((__uint128_t)a * g_tb.numer / g_tb.denom);
}
static inline uint64_t ns_to_abs(uint64_t n) {
	return (uint64_t)((__uint128_t)n * g_tb.denom / g_tb.numer);
}
static inline uint64_t now_ns(void) { return abs_to_ns(mach_absolute_time()); }

static int add_u64(uint64_t a, uint64_t b, uint64_t *out) {
	if (UINT64_MAX - a < b) return -1;
	*out = a + b;
	return 0;
}

static int mul_u64(uint64_t a, uint64_t b, uint64_t *out) {
	if (a != 0 && b > UINT64_MAX / a) return -1;
	*out = a * b;
	return 0;
}

enum interval_status {
	INTERVAL_OK = 0,
	INTERVAL_INVERTED,
	INTERVAL_RATIO_DOMAIN,
	INTERVAL_OVERFLOW
};

static enum interval_status interval_to_ns_ratio(
    uint64_t start, uint64_t end, uint64_t numer, uint64_t denom,
    uint64_t *out) {
	if (end < start) return INTERVAL_INVERTED;
	if (numer == 0 || denom == 0) return INTERVAL_RATIO_DOMAIN;
	__uint128_t value = (__uint128_t)(end - start) * numer / denom;
	if (value > UINT64_MAX) return INTERVAL_OVERFLOW;
	*out = (uint64_t)value;
	return INTERVAL_OK;
}

static int abs_interval_to_ns(uint64_t start, uint64_t end, uint64_t *out) {
	return interval_to_ns_ratio(start, end, g_tb.numer, g_tb.denom, out);
}

static int derive_control_slack(uint64_t numer, uint64_t denom,
                                uint64_t clock_resolution, uint64_t *out) {
	if (numer == 0 || denom == 0 || clock_resolution == 0) return -1;
	uint64_t mach_quantum = numer / denom + (numer % denom != 0);
	uint64_t quantum = mach_quantum > clock_resolution
	                       ? mach_quantum : clock_resolution;
	return mul_u64(quantum, 6, out);
}

enum diff_status { DIFF_OK = 0, DIFF_POSITIVE_OVERFLOW, DIFF_NEGATIVE_OVERFLOW };

static enum diff_status diff_u64_i64(uint64_t a, uint64_t b, int64_t *out) {
	if (a >= b) {
		uint64_t d = a - b;
		if (d > INT64_MAX) return DIFF_POSITIVE_OVERFLOW;
		*out = (int64_t)d;
	} else {
		uint64_t d = b - a;
		if (d > (uint64_t)INT64_MAX) return DIFF_NEGATIVE_OVERFLOW;
		*out = -(int64_t)d;
	}
	return DIFF_OK;
}

enum control_error {
	CTRL_C_INVERTED = 1u << 0,
	CTRL_INNER_INVERTED = 1u << 1,
	CTRL_OUTER_INVERTED = 1u << 2,
	CTRL_RATIO_DOMAIN = 1u << 3,
	CTRL_INNER_CONVERSION_OVERFLOW = 1u << 4,
	CTRL_OUTER_CONVERSION_OVERFLOW = 1u << 5,
	CTRL_INNER_EXCEEDS_OUTER = 1u << 6,
	CTRL_INDEPENDENT_ADD_OVERFLOW = 1u << 7,
	CTRL_OUTER_ADD_OVERFLOW = 1u << 8,
	CTRL_LOWER_POSITIVE_RANGE = 1u << 9,
	CTRL_LOWER_NEGATIVE_RANGE = 1u << 10,
	CTRL_UPPER_POSITIVE_RANGE = 1u << 11,
	CTRL_UPPER_NEGATIVE_RANGE = 1u << 12
};

typedef struct {
	uint64_t inner, independent, outer, width;
	int64_t lower_residual, upper_residual;
} control_observation_t;

/* Evaluate one already-converted containment sample. Residuals are positive
 * exactly when their corresponding inclusive bound is violated. Every
 * reachable range branch is evaluated so self-tests can identify it. */
static unsigned control_evaluate(uint64_t inner, uint64_t independent,
                                 uint64_t outer, uint64_t slack,
                                 control_observation_t *observation) {
	uint64_t independent_plus_slack, outer_plus_slack;
	unsigned errors = 0;
	int independent_sum_ok =
	    add_u64(independent, slack, &independent_plus_slack) == 0;
	int outer_sum_ok = add_u64(outer, slack, &outer_plus_slack) == 0;
	if (inner > outer) errors |= CTRL_INNER_EXCEEDS_OUTER;
	if (!independent_sum_ok) errors |= CTRL_INDEPENDENT_ADD_OVERFLOW;
	if (!outer_sum_ok) errors |= CTRL_OUTER_ADD_OVERFLOW;
	if (independent_sum_ok) {
		enum diff_status status = diff_u64_i64(
		    inner, independent_plus_slack, &observation->lower_residual);
		if (status == DIFF_POSITIVE_OVERFLOW)
			errors |= CTRL_LOWER_POSITIVE_RANGE;
		else if (status == DIFF_NEGATIVE_OVERFLOW)
			errors |= CTRL_LOWER_NEGATIVE_RANGE;
	}
	if (outer_sum_ok) {
		enum diff_status status = diff_u64_i64(
		    independent, outer_plus_slack, &observation->upper_residual);
		if (status == DIFF_POSITIVE_OVERFLOW)
			errors |= CTRL_UPPER_POSITIVE_RANGE;
		else if (status == DIFF_NEGATIVE_OVERFLOW)
			errors |= CTRL_UPPER_NEGATIVE_RANGE;
	}
	observation->inner = inner;
	observation->independent = independent;
	observation->outer = outer;
	observation->width = inner <= outer ? outer - inner : 0;
	return errors;
}

typedef struct {
	uint64_t m0_outer, c0, base_abs, woke, c1, m1_outer;
} control_sample_t;

typedef struct {
	long n, valid, invalid, violations, tight;
	int64_t max_lower_residual, max_upper_residual;
	uint64_t min_width;
} control_stats_t;

static void control_stats_init(control_stats_t *s) {
	memset(s, 0, sizeof *s);
	s->max_lower_residual = INT64_MIN;
	s->max_upper_residual = INT64_MIN;
	s->min_width = UINT64_MAX;
}

static unsigned interval_error(enum interval_status status, int inner) {
	if (status == INTERVAL_OK) return 0;
	if (status == INTERVAL_INVERTED)
		return inner ? CTRL_INNER_INVERTED : CTRL_OUTER_INVERTED;
	if (status == INTERVAL_RATIO_DOMAIN) return CTRL_RATIO_DOMAIN;
	return inner ? CTRL_INNER_CONVERSION_OVERFLOW
	             : CTRL_OUTER_CONVERSION_OVERFLOW;
}

/* This raw-timestamp seam is shared by production and fault injection. */
static unsigned control_observe_raw(control_sample_t sample,
                                    uint64_t numer, uint64_t denom,
                                    uint64_t slack,
                                    control_observation_t *observation) {
	memset(observation, 0, sizeof *observation);
	uint64_t inner = 0, outer = 0;
	enum interval_status inner_status = interval_to_ns_ratio(
	    sample.base_abs, sample.woke, numer, denom, &inner);
	enum interval_status outer_status = interval_to_ns_ratio(
	    sample.m0_outer, sample.m1_outer, numer, denom, &outer);
	unsigned errors = interval_error(inner_status, 1) |
	                  interval_error(outer_status, 0);
	if (sample.c1 < sample.c0) errors |= CTRL_C_INVERTED;
	if (errors) return errors;
	return control_evaluate(inner, sample.c1 - sample.c0, outer, slack,
	                        observation);
}

static unsigned control_add_raw(control_stats_t *stats,
                                control_sample_t sample,
                                uint64_t numer, uint64_t denom,
                                uint64_t slack, uint64_t tight_limit) {
	control_observation_t observation;
	stats->n++;
	unsigned errors = control_observe_raw(sample, numer, denom, slack,
	                                      &observation);
	if (errors) {
		stats->invalid++;
		return errors;
	}
	stats->valid++;
	if (observation.lower_residual > 0 || observation.upper_residual > 0)
		stats->violations++;
	if (observation.lower_residual > stats->max_lower_residual)
		stats->max_lower_residual = observation.lower_residual;
	if (observation.upper_residual > stats->max_upper_residual)
		stats->max_upper_residual = observation.upper_residual;
	if (observation.width < stats->min_width)
		stats->min_width = observation.width;
	if (observation.width <= tight_limit) stats->tight++;
	return 0;
}

static int64_t control_lower(const control_stats_t *s) {
	return s->valid ? s->max_lower_residual : 0;
}
static int64_t control_upper(const control_stats_t *s) {
	return s->valid ? s->max_upper_residual : 0;
}
static uint64_t control_width(const control_stats_t *s) {
	return s->valid ? s->min_width : 0;
}

static void emit_control_object(FILE *f, const control_stats_t *s,
                                uint64_t slack) {
	fprintf(f,
	        "{\"timebase_control_n\":%ld,"
	        "\"timebase_control_slack_ns\":%llu,"
	        "\"timebase_control_invalid_count\":%ld,"
	        "\"timebase_control_violation_count\":%ld,"
	        "\"timebase_control_max_lower_residual_ns\":%lld,"
	        "\"timebase_control_max_upper_residual_ns\":%lld,"
	        "\"timebase_control_min_width_ns\":%llu,"
	        "\"timebase_control_tight_count\":%ld}",
	        s->n, (unsigned long long)slack, s->invalid, s->violations,
	        (long long)control_lower(s), (long long)control_upper(s),
	        (unsigned long long)control_width(s), s->tight);
}

enum synthetic_kind {
	SYN_VALID, SYN_CONVERSION_PLUS, SYN_CONVERSION_MINUS,
	SYN_GAP_M0_C0, SYN_GAP_C0_BASE, SYN_GAP_WOKE_C1, SYN_GAP_C1_M1,
	SYN_ALL_WIDE
};

static control_sample_t synthetic_sample(uint64_t target_ns) {
	uint64_t ticks = target_ns * 3 / 125;
	control_sample_t sample = {
	    .m0_outer = 0,
	    .c0 = 1000,
	    .base_abs = 2,
	    .woke = 2 + ticks,
	    .c1 = 1000 + target_ns + 83,
	    .m1_outer = ticks + 4,
	};
	return sample;
}

static int synthetic_stats(enum synthetic_kind kind, uint64_t target_ns,
                           control_stats_t *stats, uint64_t *slack_out) {
	uint64_t slack;
	if (derive_control_slack(125, 3, 42, &slack) != 0) return -1;
	control_stats_init(stats);
	control_sample_t base = synthetic_sample(target_ns);
	control_sample_t sample = base;
	uint64_t numer = 125, denom = 3;
	if (kind == SYN_CONVERSION_PLUS) {
		numer = 12625;
		denom = 300;
	} else if (kind == SYN_CONVERSION_MINUS) {
		numer = 12375;
		denom = 300;
	} else if (kind != SYN_VALID) {
		uint64_t gap_ns = target_ns / 20;
		uint64_t gap_ticks = gap_ns * 3 / 125;
		if (kind != SYN_ALL_WIDE &&
		    control_add_raw(stats, base, numer, denom, slack,
		                    target_ns / 200) != 0)
			return -1;
		if (kind == SYN_GAP_M0_C0 || kind == SYN_ALL_WIDE) {
			sample.base_abs += gap_ticks;
			sample.woke += gap_ticks;
			sample.m1_outer += gap_ticks;
		} else if (kind == SYN_GAP_C0_BASE) {
			sample.base_abs += gap_ticks;
			sample.woke += gap_ticks;
			sample.m1_outer += gap_ticks;
			sample.c1 += gap_ns;
		} else if (kind == SYN_GAP_WOKE_C1) {
			sample.c1 += gap_ns;
			sample.m1_outer += gap_ticks;
		} else if (kind == SYN_GAP_C1_M1) {
			sample.m1_outer += gap_ticks;
		} else {
			return -1;
		}
		control_observation_t base_observation, gap_observation;
		uint64_t expected_width;
		if (control_observe_raw(base, numer, denom, slack,
		                        &base_observation) != 0 ||
		    control_observe_raw(sample, numer, denom, slack,
		                        &gap_observation) != 0 ||
		    add_u64(base_observation.width, gap_ns, &expected_width) != 0 ||
		    gap_observation.width != expected_width)
			return -1;
	}
	long first = kind >= SYN_GAP_M0_C0 && kind <= SYN_GAP_C1_M1 ? 1 : 0;
	for (long i = first; i < 2000; i++)
		if (control_add_raw(stats, sample, numer, denom, slack,
		                    target_ns / 200) != 0)
			return -1;
	*slack_out = slack;
	return 0;
}

static int parse_synthetic_kind(const char *name, enum synthetic_kind *kind,
                                uint64_t *target) {
	static const struct { const char *prefix; enum synthetic_kind kind; } cases[] = {
	    {"valid-", SYN_VALID},
	    {"conversion-plus-", SYN_CONVERSION_PLUS},
	    {"conversion-minus-", SYN_CONVERSION_MINUS},
	    {"gap-m0-c0-", SYN_GAP_M0_C0},
	    {"gap-c0-base-", SYN_GAP_C0_BASE},
	    {"gap-woke-c1-", SYN_GAP_WOKE_C1},
	    {"gap-c1-m1-", SYN_GAP_C1_M1},
	    {"all-wide-", SYN_ALL_WIDE},
	};
	for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
		size_t n = strlen(cases[i].prefix);
		if (strncmp(name, cases[i].prefix, n) != 0) continue;
		const char *number = name + n;
		char *end = NULL;
		errno = 0;
		unsigned long long value = strtoull(number, &end, 10);
		if (errno || !end || *end != '\0' ||
		    (value != 100000 && value != 1000000 && value != 10000000))
			return -1;
		*kind = cases[i].kind;
		*target = (uint64_t)value;
		return 0;
	}
	return -1;
}

static int emit_synthetic_case(const char *name) {
	enum synthetic_kind kind;
	uint64_t target, slack;
	control_stats_t stats;
	if (parse_synthetic_kind(name, &kind, &target) != 0 ||
	    synthetic_stats(kind, target, &stats, &slack) != 0)
		return -1;
	emit_control_object(stdout, &stats, slack);
	fputc('\n', stdout);
	return 0;
}

static int control_selftest(void) {
	static const uint64_t targets[] = {100000, 1000000, 10000000};
	for (size_t i = 0; i < sizeof targets / sizeof targets[0]; i++) {
		uint64_t t = targets[i], slack;
		control_stats_t stats;
		if (synthetic_stats(SYN_VALID, t, &stats, &slack) != 0 ||
		    stats.invalid != 0 || stats.violations != 0 ||
		    stats.tight != 2000)
			return 1;
		if (synthetic_stats(SYN_CONVERSION_PLUS, t, &stats, &slack) != 0 ||
		    stats.invalid != 0 || stats.violations != 2000 ||
		    control_lower(&stats) <= 0)
			return 1;
		if (synthetic_stats(SYN_CONVERSION_MINUS, t, &stats, &slack) != 0 ||
		    stats.invalid != 0 || stats.violations != 2000 ||
		    control_upper(&stats) <= 0)
			return 1;
		for (enum synthetic_kind kind = SYN_GAP_M0_C0;
		     kind <= SYN_GAP_C1_M1; kind++) {
			if (synthetic_stats(kind, t, &stats, &slack) != 0 ||
			    stats.invalid != 0 || stats.violations != 0 ||
			    stats.tight != 1 || control_width(&stats) > t / 200)
				return 1;
		}
		if (synthetic_stats(SYN_ALL_WIDE, t, &stats, &slack) != 0 ||
		    stats.invalid != 0 || stats.violations != 0 || stats.tight != 0 ||
		    control_width(&stats) <= t / 200)
			return 1;
	}
	control_observation_t observation;
	if (control_evaluate(1000, 748, 1000, 252, &observation) != 0 ||
	    observation.lower_residual != 0 ||
	    observation.upper_residual > 0)
		return 1;
	if (control_evaluate(1000, 1252, 1000, 252, &observation) != 0 ||
	    observation.lower_residual > 0 ||
	    observation.upper_residual != 0)
		return 1;
	/* Raw inversion, conversion overflow, and signed-residual overflow all
	 * reach the same production seam and are recorded invalid. */
	control_stats_t stats;
	control_sample_t bad = synthetic_sample(100000);
	control_stats_init(&stats);
	bad.m0_outer = bad.m1_outer + 1;
	unsigned errors = control_add_raw(&stats, bad, 125, 3, 252, 500);
	if ((errors & CTRL_OUTER_INVERTED) == 0 ||
	    stats.invalid != 1)
		return 1;
	control_stats_init(&stats);
	bad = synthetic_sample(100000);
	bad.m1_outer = UINT64_MAX;
	errors = control_add_raw(&stats, bad, UINT32_MAX, 1, 252, 500);
	if ((errors & CTRL_OUTER_CONVERSION_OVERFLOW) == 0 ||
	    stats.invalid != 1)
		return 1;
	/* Both range helpers are evaluated even when the first one fails. The two
	 * samples reverse which residual is the large positive direction. */
	control_stats_init(&stats);
	bad = (control_sample_t){0, 0, 0, (uint64_t)INT64_MAX + 2,
	                         0, (uint64_t)INT64_MAX + 2};
	errors = control_add_raw(&stats, bad, 1, 1, 1, 500);
	if ((errors & CTRL_LOWER_POSITIVE_RANGE) == 0 ||
	    (errors & CTRL_UPPER_NEGATIVE_RANGE) == 0 ||
	    stats.invalid != 1)
		return 1;
	control_stats_init(&stats);
	bad = (control_sample_t){0, 0, 0, 0,
	                         (uint64_t)INT64_MAX + 2, 0};
	errors = control_add_raw(&stats, bad, 1, 1, 1, 500);
	if ((errors & CTRL_LOWER_NEGATIVE_RANGE) == 0 ||
	    (errors & CTRL_UPPER_POSITIVE_RANGE) == 0 ||
	    stats.invalid != 1)
		return 1;
	return 0;
}

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

static void emit_signal_summary(FILE *f, const char *mech, const char *qos,
                                stats_t s, stats_t r, long lost, int last) {
	fprintf(f,
	        "    {\"arm\":\"signal\",\"mechanism\":\"%s\",\"qos\":\"%s\","
	        "\"n\":%ld,\"lost\":%ld,\"early\":%ld,\"min_ns\":%lld,"
	        "\"p50_ns\":%lld,\"p99_ns\":%lld,\"max_ns\":%lld,"
	        "\"roundtrip_min_ns\":%lld,\"roundtrip_p50_ns\":%lld,"
	        "\"roundtrip_p99_ns\":%lld,\"roundtrip_max_ns\":%lld,"
	        "\"oneway_upper_bound_p99_ns\":%lld}%s\n",
	        mech, qos, s.n, lost, s.early, (long long)s.min,
	        (long long)s.p50, (long long)s.p99, (long long)s.max,
	        (long long)r.min, (long long)r.p50, (long long)r.p99,
	        (long long)r.max, (long long)r.p99,
	        last ? "" : ",");
}

static void emit_stats(FILE *f, const char *name, const char *mech,
                       const char *qos, long target_ns, stats_t s) {
	fprintf(f,
	        "    {\"arm\":\"%s\",\"mechanism\":\"%s\",\"qos\":\"%s\","
	        "\"target_ns\":%ld,\"n\":%ld,\"early\":%ld,\"min_ns\":%lld,"
	        "\"p50_ns\":%lld,\"p99_ns\":%lld,\"max_ns\":%lld",
	        name, mech, qos, target_ns, s.n, s.early, (long long)s.min,
	        (long long)s.p50, (long long)s.p99, (long long)s.max);
}

/* ---------- QoS ---------- */

static void set_qos(qos_class_t q) {
	fail_pthread(pthread_set_qos_class_self_np(q, 0),
	             "pthread_set_qos_class_self_np");
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
	if (clock_gettime(c->id, &ts) != 0) { perror("clock_gettime"); exit(1); }
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
		if (cl[k].use_nsec_np) {
			if (!have_res || res.tv_sec < 0 || res.tv_nsec < 0 ||
			    res.tv_nsec >= 1000000000L ||
			    (uint64_t)res.tv_sec >
			        (UINT64_MAX - (uint64_t)res.tv_nsec) / 1000000000ull) {
				fprintf(stderr, "invalid CLOCK_UPTIME_RAW resolution\n");
				exit(1);
			}
			g_uptime_raw_resolution_ns =
			    (uint64_t)res.tv_sec * 1000000000ull + (uint64_t)res.tv_nsec;
			if (g_uptime_raw_resolution_ns == 0) {
				fprintf(stderr, "zero CLOCK_UPTIME_RAW resolution\n");
				exit(1);
			}
		}
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
	fail_pthread(pthread_mutex_init(&s.m, NULL), "pthread_mutex_init");
	s.iters = 200000;
	atomic_store(&s.inversions, 0);
	atomic_store(&s.worst_backward_ns, 0);
	pthread_t t[4];
	for (int i = 0; i < 4; i++)
		fail_pthread(pthread_create(&t[i], NULL, xclock_thread, &s), "pthread_create");
	for (int i = 0; i < 4; i++)
		fail_pthread(pthread_join(t[i], NULL), "pthread_join");
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
	int64_t *err = xmalloc(sizeof(int64_t) * (size_t)g_n);
	int64_t setup_ns = 0, setup_n = 0;
	/* Accepted containment control (REV-139). The independent interval is
	 * bracketed by converted-Mach inner and outer intervals; paired-read
	 * scheduling can widen the bracket but cannot create a false violation. */
	uint64_t ctrl_slack_ns;
	if (derive_control_slack(g_tb.numer, g_tb.denom,
	                         g_uptime_raw_resolution_ns,
	                         &ctrl_slack_ns) != 0) {
		fprintf(stderr, "invalid timebase-control slack\n");
		exit(1);
	}
	uint64_t tight_limit_ns = (uint64_t)target_ns / 200;
	int enforce_control = strcmp(mech, "mach_wait_until") == 0 ||
	                      strcmp(mech, "nanosleep") == 0;
	control_stats_t ctrl_stats;
	control_stats_init(&ctrl_stats);
	set_qos(q);
	for (long i = 0; i < g_n; i++) {
		uint64_t m0_outer = mach_absolute_time();
		uint64_t ctrl0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
		uint64_t base_abs = mach_absolute_time();
		uint64_t deadline_abs;
		if (add_u64(base_abs, ns_to_abs((uint64_t)target_ns),
		            &deadline_abs) != 0) {
			fprintf(stderr, "wakeup deadline overflow\n");
			exit(1);
		}
		uint64_t woke;
		if (strcmp(mech, "mach_wait_until") == 0) {
			if (mach_wait_until(deadline_abs) != KERN_SUCCESS) {
				fprintf(stderr, "mach_wait_until failed\n");
				exit(1);
			}
			woke = mach_absolute_time();
		} else if (strcmp(mech, "nanosleep") == 0) {
			struct timespec ts = {target_ns / 1000000000L,
			                      target_ns % 1000000000L};
			int rc;
			do { rc = nanosleep(&ts, &ts); } while (rc == -1 && errno == EINTR);
			if (rc == -1) { perror("nanosleep"); exit(1); }
			woke = mach_absolute_time();
		} else { /* dispatch_source timer; leeway 0, optionally STRICT */
			uintptr_t mask = strcmp(mech, "dispatch_source_timer_strict") == 0
			                     ? DISPATCH_TIMER_STRICT : 0;
			uint64_t setup0 = mach_absolute_time();
			dispatch_queue_t qq = dispatch_get_global_queue(
			    q == QOS_CLASS_USER_INTERACTIVE ? QOS_CLASS_USER_INTERACTIVE
			                                    : QOS_CLASS_BACKGROUND,
			    0);
			dispatch_source_t src = dispatch_source_create(
			    DISPATCH_SOURCE_TYPE_TIMER, 0, mask, qq);
			__block uint64_t fired = 0;
			dispatch_semaphore_t sem = dispatch_semaphore_create(0);
			if (!src || !sem) { fprintf(stderr, "dispatch allocation failed\n"); exit(1); }
			dispatch_source_set_event_handler(src, ^{
				fired = mach_absolute_time();
				dispatch_semaphore_signal(sem);
			});
			dispatch_source_set_timer(
			    src, dispatch_time(DISPATCH_TIME_NOW, target_ns),
			    DISPATCH_TIME_FOREVER, 0);
			/* Setup cost is inside this arm's measured interval, so it is
			 * measured rather than only disclosed (REV-045 change 14). The
			 * deadline is re-taken after setup so the reported error is the
			 * timer's, and setup is reported separately. */
			setup_ns += (int64_t)abs_to_ns(mach_absolute_time() - setup0);
			setup_n++;
			dispatch_resume(src);
			dispatch_semaphore_wait(sem, DISPATCH_TIME_FOREVER);
			dispatch_source_cancel(src);
			dispatch_release(src);
			dispatch_release(sem);
			woke = fired;
		}
		uint64_t ctrl1 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
		uint64_t m1_outer = mach_absolute_time();
		uint64_t error_magnitude;
		int64_t e;
		if ((woke >= deadline_abs
		         ? abs_interval_to_ns(deadline_abs, woke, &error_magnitude)
		         : abs_interval_to_ns(woke, deadline_abs, &error_magnitude)) != 0 ||
		    error_magnitude > INT64_MAX) {
			fprintf(stderr, "wakeup error overflow\n");
			exit(1);
		}
		e = woke >= deadline_abs ? (int64_t)error_magnitude
		                         : -(int64_t)error_magnitude;
		control_sample_t control_sample = {
		    .m0_outer = m0_outer,
		    .c0 = ctrl0,
		    .base_abs = base_abs,
		    .woke = woke,
		    .c1 = ctrl1,
		    .m1_outer = m1_outer,
		};
		if (control_add_raw(&ctrl_stats, control_sample,
		                    g_tb.numer, g_tb.denom, ctrl_slack_ns,
		                    tight_limit_ns) != 0 && enforce_control) {
			fprintf(stderr, "invalid timebase-control sample\n");
			exit(1);
		}
		if (g_negative) e -= target_ns / 4; /* injected defect: impossible early wake */
		err[i] = e;
	}
	set_qos(QOS_CLASS_USER_INTERACTIVE);
	emit_stats(f, "wakeup", mech, qos_name(q), target_ns, summarize(err, g_n));
	fprintf(f,
	        ",\"timebase_control_n\":%ld,"
	        "\"timebase_control_slack_ns\":%llu,"
	        "\"timebase_control_invalid_count\":%ld,"
	        "\"timebase_control_violation_count\":%ld,"
	        "\"timebase_control_max_lower_residual_ns\":%lld,"
	        "\"timebase_control_max_upper_residual_ns\":%lld,"
	        "\"timebase_control_min_width_ns\":%llu,"
	        "\"timebase_control_tight_count\":%ld,"
	        "\"setup_p_mean_ns\":%lld}%s\n",
	        ctrl_stats.n, (unsigned long long)ctrl_slack_ns,
	        ctrl_stats.invalid, ctrl_stats.violations,
	        (long long)control_lower(&ctrl_stats),
	        (long long)control_upper(&ctrl_stats),
	        (unsigned long long)control_width(&ctrl_stats), ctrl_stats.tight,
	        (long long)(setup_n ? setup_ns / setup_n : 0),
	        last ? "" : ",");
	free(err);
}

/* ---------- concurrent timer threads ---------- */

struct cw {
	long target_ns, n;
	int64_t *err;
};

static void *cw_thread(void *a) {
	struct cw *w = a;
	set_qos(QOS_CLASS_USER_INTERACTIVE);
	for (long i = 0; i < w->n; i++) {
		uint64_t deadline = mach_absolute_time() +
		                    ns_to_abs((uint64_t)w->target_ns);
		if (mach_wait_until(deadline) != KERN_SUCCESS) {
			fprintf(stderr, "mach_wait_until failed\n");
			exit(1);
		}
		w->err[i] = (int64_t)abs_to_ns(mach_absolute_time()) -
		            (int64_t)abs_to_ns(deadline);
	}
	return NULL;
}

/* One `mach_wait_until` timer per thread at a fixed target, all threads
 * running at user-interactive QoS, so the reported distribution is the
 * aggregate a multi-vCPU guest tick would impose. Pooled across threads:
 * per-thread separation would be a placement statement, which CLM-01-07-08
 * forbids this probe from making. */
static void concurrent_wakeup_arm(FILE *f, int nthreads, long target_ns,
                                  int last) {
	long per = g_n;
	pthread_t th[8];
	struct cw w[8];
	int64_t *all = xmalloc(sizeof(int64_t) * (size_t)per * (size_t)nthreads);
	for (int i = 0; i < nthreads; i++) {
		w[i].target_ns = target_ns;
		w[i].n = per;
		w[i].err = all + (size_t)i * (size_t)per;
		fail_pthread(pthread_create(&th[i], NULL, cw_thread, &w[i]),
		             "pthread_create");
	}
	for (int i = 0; i < nthreads; i++)
		fail_pthread(pthread_join(th[i], NULL), "pthread_join");
	if (g_negative)
		for (long i = 0; i < per * nthreads; i++) all[i] -= target_ns / 4;
	stats_t s = summarize(all, per * (long)nthreads);
	fprintf(f,
	        "    {\"arm\":\"concurrent_wakeup\",\"mechanism\":\"mach_wait_until\","
	        "\"qos\":\"user-interactive\",\"threads\":%d,\"target_ns\":%ld,"
	        "\"aggregate_wakeups_per_s\":%.0f,\"n\":%ld,\"early\":%ld,"
	        "\"min_ns\":%lld,\"p50_ns\":%lld,\"p99_ns\":%lld,\"max_ns\":%lld}%s\n",
	        nthreads, target_ns,
	        (double)nthreads * 1e9 / (double)target_ns,
	        s.n, s.early, (long long)s.min, (long long)s.p50,
	        (long long)s.p99, (long long)s.max, last ? "" : ",");
	free(all);
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
	fail_pthread(pthread_mutex_init(&c.m, NULL), "pthread_mutex_init");
	fail_pthread(pthread_cond_init(&c.cv, NULL), "pthread_cond_init");
	c.sem = dispatch_semaphore_create(0);
	if (!c.sem) { fprintf(stderr, "dispatch_semaphore_create failed\n"); exit(1); }
	c.q = q;
	g_sig = &c;
	void *(*fn)(void *) = strcmp(mech, "pthread_cond_signal") == 0 ? cond_target
	                      : strcmp(mech, "dispatch_semaphore_signal") == 0
	                          ? sem_target
	                          : spin_target;
	fail_pthread(pthread_create(&c.target, NULL, fn, &c), "pthread_create");
	while (!atomic_load(&c.ready)) sched_yield();
	usleep(20000);

	int64_t *lat = xmalloc(sizeof(int64_t) * (size_t)g_n);
	/* Round-trip control (REV-044 change 1). `lat` subtracts a timestamp the
	 * *target* thread took from one the sender took, which assumes a
	 * system-wide comparable time base that no source in the inventory
	 * states. `rt` is measured entirely on the sender's clock — send, then
	 * observe the flag — so it needs no such assumption. One-way latency is
	 * a diagnostic only. Full rt bounds the causal forward component for
	 * the same completed event; rt/2 does not without symmetry. */
	int64_t *rt = xmalloc(sizeof(int64_t) * (size_t)g_n);
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
			fail_pthread(pthread_kill(c.target, SIGUSR1), "pthread_kill");
		}
		uint64_t spin_start = t0;
		while (!atomic_load(&c.flag)) {
			if (now_ns() - spin_start > 1000000000ull) break; /* 1 s timeout */
		}
		if (!atomic_load(&c.flag)) { lost++; continue; }
		int64_t round = (int64_t)(now_ns() - t0);
		int64_t d = (int64_t)atomic_load(&c.recv_ns) - (int64_t)t0;
		if (g_negative) d = -d; /* injected defect: negative delivery latency */
		rt[got] = round;
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
	fail_pthread(pthread_join(c.target, NULL), "pthread_join");

	stats_t s = summarize(lat, got);
	stats_t r = summarize(rt, got);   /* summarize sorts; do lat first */
	emit_signal_summary(f, mech, qos_name(q), s, r, lost, last);
	free(lat);
	free(rt);
	dispatch_release(c.sem);
	pthread_mutex_destroy(&c.m);
	pthread_cond_destroy(&c.cv);
}

/* ---------- thread affinity policy ---------- */

static void probe_affinity(FILE *f) {
	thread_affinity_policy_data_t p = {1};
	mach_port_t self = mach_thread_self();
	kern_return_t kr = thread_policy_set(
	    self, THREAD_AFFINITY_POLICY, (thread_policy_t)&p,
	    THREAD_AFFINITY_POLICY_COUNT);
	mach_port_deallocate(mach_task_self(), self);
	fprintf(f,
	        "  \"thread_affinity_policy\": {\"call\":\"thread_policy_set("
	        "THREAD_AFFINITY_POLICY, tag=1)\",\"kern_return_t\":%d,"
	        "\"success\":%s},\n",
	        (int)kr, kr == KERN_SUCCESS ? "true" : "false");
}

/* ---------- main ---------- */

int main(int argc, char **argv) {
	/* Non-evidence regression seam: production summary and emitter, with
	 * asymmetric paired synthetic values. No clock or thread probe runs. */
	if (argc == 3 && !strcmp(argv[1], "--signal-summary-case")) {
		int64_t forward[2000], round[2000];
		int invalid = !strcmp(argv[2], "above-roundtrip");
		if (!invalid && strcmp(argv[2], "asymmetric")) return 2;
		for (long i = 0; i < 2000; i++) {
			forward[i] = (invalid ? 110000 : 90000) + (i % 2);
			round[i] = 100000 + (i % 2);
		}
		stats_t s = summarize(forward, 2000);
		stats_t rt = summarize(round, 2000);
		emit_signal_summary(stdout, "pthread_kill_SIGUSR1_spinning",
		                    "user-interactive", s, rt, 0, 1);
		return 0;
	}
	const char *out = NULL;
	const char *control_case = NULL;
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--negative")) g_negative = 1;
		else if (!strcmp(argv[i], "--control-selftest")) g_control_selftest = 1;
		else if (!strcmp(argv[i], "--control-case") && i + 1 < argc)
			control_case = argv[++i];
		else if (!strcmp(argv[i], "--out") && i + 1 < argc) out = argv[++i];
		else if (!strcmp(argv[i], "--n") && i + 1 < argc)
			g_n = parse_positive(argv[++i], "--n");
		else { fprintf(stderr, "usage: %s [--out F] [--n N] [--negative] [--control-selftest | --control-case NAME]\n", argv[0]); return 2; }
	}
	if (g_control_selftest) {
		if (out || control_case || g_negative || g_n != 2000) {
			fprintf(stderr, "--control-selftest does not accept other options\n");
			return 2;
		}
		int rc = control_selftest();
		fprintf(stderr, "INV-01-07 control self-test %s\n", rc ? "FAIL" : "PASS");
		return rc;
	}
	if (control_case) {
		if (out || g_negative || g_n != 2000) {
			fprintf(stderr, "--control-case does not accept other options\n");
			return 2;
		}
		if (emit_synthetic_case(control_case) != 0) {
			fprintf(stderr, "unknown or invalid control case: %s\n", control_case);
			return 2;
		}
		return 0;
	}
	if (mach_timebase_info(&g_tb) != KERN_SUCCESS) { perror("timebase"); return 1; }
	if (g_tb.numer == 0 || g_tb.denom == 0) {
		fprintf(stderr, "invalid Mach timebase ratio\n");
		return 1;
	}
	atomic_ullong lockfree_ullong;
	atomic_int lockfree_int;
	if (!atomic_is_lock_free(&lockfree_ullong) || !atomic_is_lock_free(&lockfree_int)) {
		fprintf(stderr, "signal-handler atomics are not lock-free\n");
		return 1;
	}
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
	static const char *mechs[] = {"mach_wait_until", "nanosleep",
	                              "dispatch_source_timer_leeway0",
	                              "dispatch_source_timer_strict"};
	enum { NMECH = 4 };
	const qos_class_t qs[] = {QOS_CLASS_USER_INTERACTIVE, QOS_CLASS_BACKGROUND};
	for (int m = 0; m < NMECH; m++)
		for (int t = 0; t < 3; t++)
			for (int q = 0; q < 2; q++)
				wakeup_arm(f, mechs[m], targets[t], qs[q],
				           m == NMECH - 1 && t == 2 && q == 1);
	fprintf(f, "  ],\n");

	/* Aggregate wakeup rate at 2, 4, and 8 concurrent timer threads: the
	 * declared guest requirement is a 1 kHz tick *per vCPU* at 2/4/8 vCPUs,
	 * and a single-thread arm cannot speak to it (REV-044 change 9). */
	fprintf(f, "  \"concurrent_wakeup\": [\n");
	static const int nthr[] = {2, 4, 8};
	for (int i = 0; i < 3; i++)
		concurrent_wakeup_arm(f, nthr[i], 1000000, i == 2);
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
