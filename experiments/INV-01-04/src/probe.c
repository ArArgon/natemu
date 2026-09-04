/* INV-01-04 executable-memory (MAP_JIT) probe.
 * Emits one JSON object on stdout.
 *
 * --negative injects a code-generation defect (the stub is assembled to return
 * 41 instead of the declared 42) that src/check.py must report as FAIL.
 */
#include <errno.h>
#include <libkern/OSCacheControl.h>
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

#define STUB_RESULT 42
static int negative = 0;

/* arm64: MOVZ W0, #imm16 ; RET */
static uint32_t movz_w0(uint32_t imm) { return 0x52800000u | ((imm & 0xffffu) << 5); }
#define RET_INSN 0xD65F03C0u

typedef int (*stub_fn)(void);

/* ---- fault trapping (per-thread) --------------------------------------- */

static _Thread_local sigjmp_buf tjb;
static _Thread_local volatile sig_atomic_t t_signo;
static _Thread_local volatile sig_atomic_t t_code;
static _Thread_local volatile uintptr_t t_addr;
static _Thread_local volatile sig_atomic_t t_armed;

static void onfault(int s, siginfo_t *si, void *uc) {
	(void)uc;
	if (!t_armed) _exit(97);          /* unexpected fault: fail loudly */
	t_signo = s;
	t_code = si ? si->si_code : 0;
	t_addr = si ? (uintptr_t)si->si_addr : 0;
	siglongjmp(tjb, 1);
}

static void install_handlers(void) {
	struct sigaction sa;
	memset(&sa, 0, sizeof sa);
	sa.sa_sigaction = onfault;
	sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGSEGV, &sa, NULL);
	sigaction(SIGBUS, &sa, NULL);
}

/* ---- timing ------------------------------------------------------------ */

static double tb_num = 1.0, tb_den = 1.0;
static void timebase_init(void) {
	mach_timebase_info_data_t tb;
	mach_timebase_info(&tb);
	tb_num = tb.numer; tb_den = tb.denom;
}
static double ticks_to_ns(uint64_t t) { return (double)t * tb_num / tb_den; }
static int cmp_u64(const void *a, const void *b) {
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
	return x < y ? -1 : x > y ? 1 : 0;
}
static void emit_stats(const char *name, uint64_t *v, size_t n, const char *tail) {
	qsort(v, n, sizeof *v, cmp_u64);
	double sum = 0;
	for (size_t i = 0; i < n; i++) sum += ticks_to_ns(v[i]);
	printf("    \"%s\": {\"n\": %zu, \"min_ns\": %.1f, \"median_ns\": %.1f, "
	       "\"p99_ns\": %.1f, \"max_ns\": %.1f, \"mean_ns\": %.1f}%s\n",
	       name, n, ticks_to_ns(v[0]), ticks_to_ns(v[n / 2]),
	       ticks_to_ns(v[(size_t)(n * 0.99)]), ticks_to_ns(v[n - 1]),
	       sum / (double)n, tail);
}

/* ---- allocation limits ------------------------------------------------- */

static void emit_alloc_limits(void) {
	const unsigned long long sizes[] = {256ULL << 20, 1ULL << 30, 4ULL << 30};
	printf("  \"map_jit_allocation\": [\n");
	for (int i = 0; i < 3; i++) {
		errno = 0;
		void *p = mmap(NULL, (size_t)sizes[i], PROT_READ | PROT_WRITE | PROT_EXEC,
		               MAP_PRIVATE | MAP_ANON | MAP_JIT, -1, 0);
		int e = errno;
		printf("    {\"bytes\": %llu, \"ok\": %s, \"errno\": %d, \"errno_str\": \"%s\"}%s\n",
		       sizes[i], p == MAP_FAILED ? "false" : "true",
		       p == MAP_FAILED ? e : 0, p == MAP_FAILED ? strerror(e) : "",
		       i == 2 ? "" : ",");
		if (p != MAP_FAILED) munmap(p, (size_t)sizes[i]);
	}
	printf("  ],\n");
}

/* ---- shared JIT region ------------------------------------------------- */

static uint32_t *jit;
static size_t jit_bytes;

static int map_jit_region(void) {
	jit_bytes = (size_t)getpagesize();
	jit = mmap(NULL, jit_bytes, PROT_READ | PROT_WRITE | PROT_EXEC,
	           MAP_PRIVATE | MAP_ANON | MAP_JIT, -1, 0);
	return jit != MAP_FAILED;
}

static void write_stub(int value) {
	pthread_jit_write_protect_np(0);
	jit[0] = movz_w0((uint32_t)value);
	jit[1] = RET_INSN;
	pthread_jit_write_protect_np(1);
	sys_icache_invalidate(jit, 2 * sizeof(uint32_t));
}

/* ---- concurrent execution while another thread holds write access ------- */

static atomic_int a_writable, b_done, a_release;
static int b_result, b_signo, b_faulted;

static void *thread_b(void *arg) {
	(void)arg;
	while (!atomic_load(&a_writable)) usleep(200);
	t_armed = 1;
	if (sigsetjmp(tjb, 1) == 0) {
		b_result = ((stub_fn)jit)();
		b_faulted = 0;
	} else {
		b_faulted = 1;
		b_signo = (int)t_signo;
	}
	t_armed = 0;
	atomic_store(&b_done, 1);
	return NULL;
}

static void *thread_a(void *arg) {
	(void)arg;
	pthread_jit_write_protect_np(0);       /* this thread holds write access */
	atomic_store(&a_writable, 1);
	while (!atomic_load(&a_release)) usleep(200);
	pthread_jit_write_protect_np(1);
	return NULL;
}

static void emit_concurrency(void) {
	pthread_t a, b;
	pthread_create(&a, NULL, thread_a, NULL);
	pthread_create(&b, NULL, thread_b, NULL);
	while (!atomic_load(&b_done)) usleep(200);
	atomic_store(&a_release, 1);
	pthread_join(a, NULL);
	pthread_join(b, NULL);
	printf("  \"concurrent_execute_while_other_thread_writable\": {\n");
	printf("    \"executed\": %s,\n", b_faulted ? "false" : "true");
	printf("    \"return_value\": %d,\n", b_faulted ? -1 : b_result);
	printf("    \"faulted\": %s,\n", b_faulted ? "true" : "false");
	printf("    \"signal\": %d\n", b_faulted ? b_signo : 0);
	printf("  },\n");
}

int main(int argc, char **argv) {
	for (int i = 1; i < argc; i++)
		if (strcmp(argv[i], "--negative") == 0) negative = 1;
	timebase_init();
	install_handlers();

	printf("{\n");
	printf("  \"investigation\": \"INV-01-04\",\n");
	printf("  \"mode\": \"%s\",\n", negative ? "negative" : "normal");
	printf("  \"pthread_jit_write_protect_supported_np\": %d,\n",
	       pthread_jit_write_protect_supported_np());
	emit_alloc_limits();

	if (!map_jit_region()) {
		printf("  \"jit_region_mapped\": false,\n  \"errno\": %d\n}\n", errno);
		return 0;
	}
	printf("  \"jit_region_mapped\": true,\n");
	printf("  \"jit_region_bytes\": %zu,\n", jit_bytes);

	/* toggle cost: one protect-off/protect-on pair per sample */
	enum { N = 20000 };
	static uint64_t off_ns[N], on_ns[N];
	for (int i = 0; i < N; i++) {
		uint64_t t0 = mach_absolute_time();
		pthread_jit_write_protect_np(0);
		uint64_t t1 = mach_absolute_time();
		pthread_jit_write_protect_np(1);
		uint64_t t2 = mach_absolute_time();
		off_ns[i] = t1 - t0;
		on_ns[i] = t2 - t1;
	}
	printf("  \"toggle_cost\": {\n");
	emit_stats("write_protect_off", off_ns, N, ",");
	emit_stats("write_protect_on", on_ns, N, "");
	printf("  },\n");

	/* The raw single-toggle interval sits at or below the timebase tick, so a
	 * batched form is measured as well: K pairs per timed interval, reported
	 * as ticks per pair. */
	enum { BN = 2000, BK = 100 };
	static uint64_t batch[BN];
	for (int i = 0; i < BN; i++) {
		uint64_t t0 = mach_absolute_time();
		for (int k = 0; k < BK; k++) {
			pthread_jit_write_protect_np(0);
			pthread_jit_write_protect_np(1);
		}
		batch[i] = (mach_absolute_time() - t0) / BK;
	}
	printf("  \"toggle_cost_batched\": {\n");
	printf("    \"pairs_per_sample\": %d,\n", BK);
	emit_stats("off_on_pair", batch, BN, "");
	printf("  },\n");

	/* write, invalidate, execute */
	int declared = STUB_RESULT;
	write_stub(negative ? STUB_RESULT - 1 : STUB_RESULT);
	int got = -1, exec_faulted = 0;
	t_armed = 1;
	if (sigsetjmp(tjb, 1) == 0) got = ((stub_fn)jit)();
	else exec_faulted = 1;
	t_armed = 0;
	printf("  \"stub\": {\n");
	printf("    \"declared_value\": %d,\n", declared);
	printf("    \"executed\": %s,\n", exec_faulted ? "false" : "true");
	printf("    \"return_value\": %d,\n", got);
	printf("    \"icache_invalidated\": true\n");
	printf("  },\n");

	emit_concurrency();

	/* execute from this thread while this thread holds write access */
	pthread_jit_write_protect_np(0);
	int self_got = -1, self_faulted = 0;
	t_armed = 1;
	if (sigsetjmp(tjb, 1) == 0) self_got = ((stub_fn)jit)();
	else self_faulted = 1;
	t_armed = 0;
	pthread_jit_write_protect_np(1);
	printf("  \"self_execute_while_writable\": {\"executed\": %s, "
	       "\"return_value\": %d, \"signal\": %d},\n",
	       self_faulted ? "false" : "true", self_faulted ? -1 : self_got,
	       self_faulted ? (int)t_signo : 0);

	/* write without toggling: expected to fault. Also a Q-01-06 datum. */
	int w_faulted = 0;
	t_armed = 1;
	if (sigsetjmp(tjb, 1) == 0) { jit[0] = RET_INSN; }
	else w_faulted = 1;
	t_armed = 0;
	printf("  \"write_without_toggle\": {\"faulted\": %s, \"signal\": %d, "
	       "\"si_code\": %d, \"si_addr_is_region\": %s}\n",
	       w_faulted ? "true" : "false", w_faulted ? (int)t_signo : 0,
	       w_faulted ? (int)t_code : 0,
	       (w_faulted && t_addr == (uintptr_t)jit) ? "true" : "false");
	printf("}\n");
	return 0;
}
