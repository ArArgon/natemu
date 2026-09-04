/* INV-01-06 exception delivery and restartability probe.
 * Emits one JSON object on stdout.
 *
 * --negative injects a defect in the resume path (the handler writes a wrong
 * value into the resumed register) that src/check.py must report as FAIL: a
 * resume that returns control but not the requested state is not a restart.
 */
#include <errno.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <mach/task.h>
#include <mach/thread_act.h>
#include <libkern/OSCacheControl.h>
#include <pthread.h>
#include <setjmp.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/ucontext.h>
#include <mach/arm/_structs.h>
#include <unistd.h>

#define SENTINEL 0x5a5a5a5aL
#define BAD_ADDR 0x00000badc0de000UL

static int negative = 0;

/* arm64 stubs. Each faults on its first instruction and then returns x0. */
#define I_LDR_X2_X1  0xF9400022u   /* ldr x2, [x1]   */
#define I_UDF        0x00000000u   /* udf #0         */
#define I_BRK        0xD4200000u   /* brk #0         */
#define I_RET        0xD65F03C0u

typedef long (*stub2_fn)(long, void *);

static uint32_t *jit;
static size_t jit_bytes;
static uint32_t *stub_load, *stub_udf, *stub_brk;

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
	qsort(v, n, sizeof *v, cmp_u64);
	double sum = 0; for (size_t i = 0; i < n; i++) sum += ns(v[i]);
	printf("    \"%s\": {\"n\": %zu, \"min_ns\": %.1f, \"median_ns\": %.1f, "
	       "\"p99_ns\": %.1f, \"max_ns\": %.1f, \"mean_ns\": %.1f}%s\n",
	       name, n, ns(v[0]), ns(v[n / 2]), ns(v[(size_t)(n * 0.99)]),
	       ns(v[n - 1]), sum / (double)n, tail);
}

/* ---- handler ----------------------------------------------------------- */

/* Everything the handler records is per-thread, so a concurrent fault on
 * another thread cannot be mistaken for this thread's. */
static _Thread_local volatile int h_seen;
static _Thread_local volatile int h_signo, h_code;
static _Thread_local volatile uint64_t h_pc, h_sp, h_far, h_x0, h_x1;
static _Thread_local volatile uint32_t h_esr;
static _Thread_local volatile uint64_t h_enter_tick;
static _Thread_local volatile int h_use_fptr_macro;   /* resume path selector */
static _Thread_local volatile int h_resume_ok;

static void onfault(int s, siginfo_t *si, void *ucv) {
	uint64_t now = mach_absolute_time();
	ucontext_t *uc = ucv;
	_STRUCT_MCONTEXT64 *mc = (_STRUCT_MCONTEXT64 *)uc->uc_mcontext;
	h_enter_tick = now;
	h_seen = 1;
	h_signo = s;
	h_code = si ? si->si_code : 0;
	h_pc = (uint64_t)__darwin_arm_thread_state64_get_pc(mc->__ss);
	h_sp = (uint64_t)__darwin_arm_thread_state64_get_sp(mc->__ss);
	h_x0 = mc->__ss.__x[0];
	h_x1 = mc->__ss.__x[1];
	h_far = mc->__es.__far;
	h_esr = (uint32_t)mc->__es.__esr;

	/* Restart: skip the faulting instruction and hand back a chosen value in
	 * x0. Both PC-write paths are exercised so the record shows whether a
	 * resume PC must be a signed pointer on this host. */
	uint64_t next = h_pc + 4;
	if (h_use_fptr_macro)
		__darwin_arm_thread_state64_set_pc_fptr(mc->__ss, (void *)(uintptr_t)next);
	else
		mc->__ss.__pc = next;
	mc->__ss.__x[0] = negative ? (uint64_t)(SENTINEL - 1) : (uint64_t)SENTINEL;
	h_resume_ok = 1;
}

static void install(void) {
	struct sigaction sa; memset(&sa, 0, sizeof sa);
	sa.sa_sigaction = onfault; sa.sa_flags = SA_SIGINFO;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGSEGV, &sa, NULL);
	sigaction(SIGBUS, &sa, NULL);
	sigaction(SIGILL, &sa, NULL);
	sigaction(SIGTRAP, &sa, NULL);
	sigaction(SIGFPE, &sa, NULL);
}

/* ---- stub construction ------------------------------------------------- */

static int build_stubs(void) {
	jit_bytes = (size_t)getpagesize();
	jit = mmap(NULL, jit_bytes, PROT_READ | PROT_WRITE | PROT_EXEC,
	           MAP_PRIVATE | MAP_ANON | MAP_JIT, -1, 0);
	if (jit == MAP_FAILED) return 0;
	pthread_jit_write_protect_np(0);
	stub_load = jit;      stub_load[0] = I_LDR_X2_X1; stub_load[1] = I_RET;
	stub_udf  = jit + 4;  stub_udf[0]  = I_UDF;       stub_udf[1]  = I_RET;
	stub_brk  = jit + 8;  stub_brk[0]  = I_BRK;       stub_brk[1]  = I_RET;
	pthread_jit_write_protect_np(1);
	sys_icache_invalidate(jit, 12 * sizeof(uint32_t));
	return 1;
}

/* ---- one fault-and-resume case ----------------------------------------- */

static void emit_case(const char *name, uint32_t *stub, void *arg,
                      int use_fptr, const char *tail) {
	h_seen = 0; h_resume_ok = 0; h_use_fptr_macro = use_fptr;
	uint64_t t0 = mach_absolute_time();
	long r = ((stub2_fn)stub)(0, arg);
	printf("    \"%s\": {\n", name);
	printf("      \"fault_seen\": %s,\n", h_seen ? "true" : "false");
	printf("      \"signal\": %d,\n", h_signo);
	printf("      \"si_code\": %d,\n", h_code);
	printf("      \"pc_at_fault_is_stub\": %s,\n",
	       h_pc == (uint64_t)(uintptr_t)stub ? "true" : "false");
	printf("      \"sp_nonzero\": %s,\n", h_sp ? "true" : "false");
	printf("      \"x0_at_fault\": %llu,\n", (unsigned long long)h_x0);
	printf("      \"x1_at_fault\": \"0x%llx\",\n", (unsigned long long)h_x1);
	printf("      \"esr\": \"0x%08x\",\n", h_esr);
	printf("      \"far\": \"0x%llx\",\n", (unsigned long long)h_far);
	printf("      \"far_matches_access\": %s,\n",
	       (uint64_t)(uintptr_t)arg == h_far ? "true" : "false");
	printf("      \"resume_path\": \"%s\",\n", use_fptr ? "set_pc_fptr_macro" : "direct___pc");
	printf("      \"resumed\": true,\n");
	printf("      \"declared_resume_value\": %ld,\n", (long)SENTINEL);
	printf("      \"observed_return_value\": %ld,\n", r);
	printf("      \"delivery_ns\": %.1f\n", ns(h_enter_tick - t0));
	printf("    }%s\n", tail);
}

/* ---- concurrent faults ------------------------------------------------- */

struct cctx { void *bad; int ok; uint64_t far; long ret; };
static atomic_int gate;

static void *cthread(void *p) {
	struct cctx *c = p;
	while (!atomic_load(&gate)) ;
	h_seen = 0; h_use_fptr_macro = 0;
	c->ret = ((stub2_fn)stub_load)(0, c->bad);
	c->far = h_far;
	c->ok = h_seen && h_far == (uint64_t)(uintptr_t)c->bad;
	return NULL;
}

static void emit_concurrent(void) {
	struct cctx a = {(void *)(BAD_ADDR + 0x1000), 0, 0, 0};
	struct cctx b = {(void *)(BAD_ADDR + 0x2000), 0, 0, 0};
	pthread_t t0, t1;
	atomic_store(&gate, 0);
	pthread_create(&t0, NULL, cthread, &a);
	pthread_create(&t1, NULL, cthread, &b);
	atomic_store(&gate, 1);
	pthread_join(t0, NULL); pthread_join(t1, NULL);
	printf("  \"concurrent_faults\": {\n");
	printf("    \"threads\": 2,\n");
	printf("    \"thread_a\": {\"expected_far\": \"0x%llx\", \"observed_far\": \"0x%llx\", "
	       "\"correct\": %s, \"return_value\": %ld},\n",
	       (unsigned long long)(uintptr_t)a.bad, (unsigned long long)a.far,
	       a.ok ? "true" : "false", a.ret);
	printf("    \"thread_b\": {\"expected_far\": \"0x%llx\", \"observed_far\": \"0x%llx\", "
	       "\"correct\": %s, \"return_value\": %ld},\n",
	       (unsigned long long)(uintptr_t)b.bad, (unsigned long long)b.far,
	       b.ok ? "true" : "false", b.ret);
	printf("    \"declared_resume_value\": %ld\n", (long)SENTINEL);
	printf("  },\n");
}

/* ---- delivery latency -------------------------------------------------- */

static void emit_latency(void) {
	enum { N = 3000 };
	static uint64_t d[N];
	h_use_fptr_macro = 0;
	for (int i = 0; i < N; i++) {
		h_seen = 0;
		uint64_t t0 = mach_absolute_time();
		((stub2_fn)stub_load)(0, (void *)BAD_ADDR);
		d[i] = h_enter_tick - t0;
	}
	printf("  \"delivery_latency\": {\n");
	printf("    \"note\": \"faulting instruction to first statement of the handler\",\n");
	stats("signal_delivery", d, N, "");
	printf("  },\n");
}

/* ---- Mach exception ports (API availability only) ---------------------- */

static void *mach_probe_thread(void *unused) {
	(void)unused;
	mach_port_t p = MACH_PORT_NULL;
	kern_return_t kr_alloc = mach_port_allocate(mach_task_self(),
	                                            MACH_PORT_RIGHT_RECEIVE, &p);
	kern_return_t kr_right = kr_alloc == KERN_SUCCESS
		? mach_port_insert_right(mach_task_self(), p, p, MACH_MSG_TYPE_MAKE_SEND)
		: KERN_FAILURE;

	exception_mask_t masks[EXC_TYPES_COUNT];
	mach_port_t ports[EXC_TYPES_COUNT];
	exception_behavior_t behaviors[EXC_TYPES_COUNT];
	thread_state_flavor_t flavors[EXC_TYPES_COUNT];
	mach_msg_type_number_t count = EXC_TYPES_COUNT;
	kern_return_t kr_get = task_get_exception_ports(
		mach_task_self(), EXC_MASK_BAD_ACCESS, masks, &count, ports,
		behaviors, flavors);

	kern_return_t kr_set = KERN_FAILURE, kr_restore = KERN_FAILURE;
	if (kr_right == KERN_SUCCESS) {
		/* Installed on this scratch thread only, which takes no fault, then
		 * removed immediately. No exception is delivered through it here. */
		kr_set = thread_set_exception_ports(
			mach_thread_self(), EXC_MASK_BAD_ACCESS, p,
			(exception_behavior_t)(EXCEPTION_DEFAULT | MACH_EXCEPTION_CODES),
			ARM_THREAD_STATE64);
		kr_restore = thread_set_exception_ports(
			mach_thread_self(), EXC_MASK_BAD_ACCESS, MACH_PORT_NULL,
			EXCEPTION_DEFAULT, THREAD_STATE_NONE);
	}
	printf("  \"mach_exception_ports\": {\n");
	printf("    \"kr_port_allocate\": %d,\n", (int)kr_alloc);
	printf("    \"kr_insert_send_right\": %d,\n", (int)kr_right);
	printf("    \"kr_task_get_exception_ports\": %d,\n", (int)kr_get);
	printf("    \"task_bad_access_handlers_installed\": %u,\n", (unsigned)count);
	printf("    \"kr_thread_set_exception_ports\": %d,\n", (int)kr_set);
	printf("    \"kr_thread_restore_exception_ports\": %d,\n", (int)kr_restore);
	printf("    \"exception_delivered_through_port\": false\n");
	printf("  },\n");
	if (p != MACH_PORT_NULL) mach_port_deallocate(mach_task_self(), p);
	return NULL;
}

int main(int argc, char **argv) {
	for (int i = 1; i < argc; i++)
		if (strcmp(argv[i], "--negative") == 0) negative = 1;
	timebase_init();
	install();

	printf("{\n");
	printf("  \"investigation\": \"INV-01-06\",\n");
	printf("  \"mode\": \"%s\",\n", negative ? "negative" : "normal");
	printf("  \"state_structs\": {\n");
	printf("    \"__DARWIN_OPAQUE_ARM_THREAD_STATE64\": %d,\n",
	       __DARWIN_OPAQUE_ARM_THREAD_STATE64);
	printf("    \"has_feature_ptrauth_calls\": %d,\n",
#if __has_feature(ptrauth_calls)
	       1
#else
	       0
#endif
	       );
	printf("    \"sizeof_arm_exception_state64\": %zu,\n",
	       sizeof(_STRUCT_ARM_EXCEPTION_STATE64));
	printf("    \"sizeof_esr_field\": %zu,\n",
	       sizeof(((_STRUCT_ARM_EXCEPTION_STATE64 *)0)->__esr));
	printf("    \"sizeof_far_field\": %zu,\n",
	       sizeof(((_STRUCT_ARM_EXCEPTION_STATE64 *)0)->__far));
	printf("    \"exception_state_variant\": \"%s\"\n",
	       sizeof(((_STRUCT_ARM_EXCEPTION_STATE64 *)0)->__esr) == 8
	       ? "v2 (64-bit esr)" : "base (32-bit esr)");
	printf("  },\n");

	if (!build_stubs()) {
		printf("  \"stubs_built\": false,\n  \"errno\": %d\n}\n", errno);
		return 0;
	}
	printf("  \"stubs_built\": true,\n");

	printf("  \"fault_and_resume\": {\n");
	emit_case("unmapped_load_direct_pc", stub_load, (void *)BAD_ADDR, 0, ",");
	emit_case("unmapped_load_set_pc_fptr", stub_load, (void *)BAD_ADDR, 1, ",");
	emit_case("undefined_instruction", stub_udf, (void *)BAD_ADDR, 0, ",");
	emit_case("breakpoint_brk", stub_brk, (void *)BAD_ADDR, 0, "");
	printf("  },\n");

	emit_latency();
	emit_concurrent();

	pthread_t mt;
	pthread_create(&mt, NULL, mach_probe_thread, NULL);
	pthread_join(mt, NULL);

	printf("  \"end\": true\n}\n");
	return 0;
}
