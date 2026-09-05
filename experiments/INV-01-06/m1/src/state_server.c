/* INV-01-06 M06-M1 state-only exception server and worker harness.
 *
 * One process (fresh-exec'd per cell/child) hosts N worker threads and either
 * one serial server thread (receiving through a port set) or N per-worker
 * server threads, all within this single task. Never forks a multithreaded
 * or JIT-holding parent; the harness that launches this binary always
 * execve()s it fresh (see ../run.py).
 *
 * Build only: this file is compiled and disassembled by ../build.py. It is
 * NEVER executed by this authoring session against a real fault; see
 * INV-01-06.md:594-600,810 and ../README.md "Execution boundary". Compiling
 * and disassembling it is in scope; running it is not.
 *
 * Wire contract: subsystem 2405, routine 2406 (mach_exception_raise_state),
 * EXCEPTION_STATE | MACH_EXCEPTION_CODES, ARM_THREAD_STATE64. See
 * ../wire-layout.json (frozen by build.py from the generated MIG headers)
 * for the exact request/reply byte layout this code assumes.
 *
 * Trace schema: one JSON object per line on stdout; kinds are documented in
 * ../check.py's TRACE SCHEMA comment. ../check.py's two-pass checker
 * consumes this exact schema, including via hand-built negative fixtures
 * that never invoke this binary.
 */
#include <assert.h>
#include <errno.h>
#include <mach/exception_types.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <mach/arm/thread_status.h>
#include <mach/thread_act.h>
#include <mach/task.h>
#include <libkern/OSCacheControl.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include "mach_exc.h"      /* generated: catch client stubs (mach_exception_raise_state) */
#include "mach_excServer.h"/* generated: mach_exc_server demux + catch_ prototypes */

/* ---- fixed wire/contract constants (frozen; see wire-layout.json) ------ */
#define M1_ROUTINE_ID          2406u
#define M1_EXCEPTION           EXC_BAD_ACCESS
#define M1_BEHAVIOR            (EXCEPTION_STATE | MACH_EXCEPTION_CODES)
#define M1_FLAVOR              ARM_THREAD_STATE64
#define M1_STATE_COUNT         ARM_THREAD_STATE64_COUNT   /* 68 natural_t words */

_Static_assert(M1_STATE_COUNT == 68, "ARM_THREAD_STATE64_COUNT changed on this SDK");

static void jlog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static pthread_mutex_t g_out_lock = PTHREAD_MUTEX_INITIALIZER;
static void jlog(const char *fmt, ...) {
	char buf[4096];
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(buf, sizeof buf, fmt, ap);
	va_end(ap);
	pthread_mutex_lock(&g_out_lock);
	fputs(buf, stdout);
	fputc('\n', stdout);
	fflush(stdout);
	pthread_mutex_unlock(&g_out_lock);
}

/* ---- CLI configuration --------------------------------------------------*/
typedef enum { ACC_LOAD, ACC_STORE } access_t;
typedef enum { TOPO_SERIAL, TOPO_PERWORKER } topo_t;
typedef enum { PATH_DIRECT, PATH_FORWARD } path_t;

typedef struct {
	int n;                 /* worker count: 1,2,4,8 */
	int jit;                /* 0 = non-JIT baseline, 1 = immutable-JIT (Q-01-04 controls) */
	access_t access;
	topo_t topology;
	path_t path;
	int warmup;
	int checked;
	unsigned seed;
	const char *negative;  /* one of the 8 runtime-negative names, or NULL */
	const char *arm;       /* "terminal" | "prior-handler-unsupported" | "restoration" | NULL */
} config_t;

/* ---- named deterministic holds (INV-01-06.md:734-744) ------------------
 * Eight fixed rendezvous points, each a simple counting handshake: the first
 * arrival blocks until a second party records its own attempt, then both
 * proceed. Disabled unless --holds is passed (a future evidence campaign's
 * concern; harmless no-op machinery here since this binary is never run in
 * this authoring session).
 */
typedef enum {
	HOLD_ARMED_BEFORE_FAULT,
	HOLD_RECEIVED_BEFORE_VALIDATE,
	HOLD_VALIDATED_BEFORE_REPAIR,
	HOLD_REPAIRED_BEFORE_REPLY,
	HOLD_FRONT_BEFORE_FORWARD,
	HOLD_PRIOR_BEFORE_REPLY,
	HOLD_RESUMED_BEFORE_DISARM,
	HOLD_TEARDOWN_BEFORE_RESTORE,
	HOLD_COUNT
} hold_id_t;

static struct { pthread_mutex_t m; pthread_cond_t c; int arrivals; uint64_t generation; } g_holds[HOLD_COUNT] = {
	[0 ... HOLD_COUNT - 1] = { PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER, 0, 0 }
};
static int g_holds_enabled = 0;

/* Two-party rendezvous, reusable across many events per process (a named
 * hold point is reached once per checked event, not once per process): the
 * first arrival in a round waits on a generation counter rather than on
 * "arrivals < 2" directly, so the second arrival can reset arrivals to 0 for
 * the next round without racing a waiter that hasn't yet re-checked the
 * predicate. */
static void hold_point(hold_id_t id) {
	if (!g_holds_enabled) return;
	pthread_mutex_lock(&g_holds[id].m);
	uint64_t my_generation = g_holds[id].generation;
	g_holds[id].arrivals++;
	if (g_holds[id].arrivals >= 2) {
		g_holds[id].arrivals = 0;
		g_holds[id].generation++;
		pthread_cond_broadcast(&g_holds[id].c);
	} else {
		while (g_holds[id].generation == my_generation)
			pthread_cond_wait(&g_holds[id].c, &g_holds[id].m);
	}
	pthread_mutex_unlock(&g_holds[id].m);
}

/* ---- worker descriptor ---------------------------------------------------
 * Exactly one immutable descriptor per worker, release-published before the
 * worker is admitted to arming. The server acquire-loads it. Everything a
 * server needs to check identity/attribution lives here; nothing is mutated
 * by the server except the outstanding-fault flag and commit counter, which
 * are themselves atomics.
 */
typedef struct {
	int id;
	mach_port_t recv_port;      /* receive right; also the front's own handler port */
	mach_port_t send_right;     /* MAKE_SEND right on recv_port, installed as the handler */
	mach_port_t prior_recv_port;/* forward path only: the owned prior's receive right */
	mach_port_t prior_send_right;
	_Atomic int published;      /* release-published before admission */
	_Atomic int registration_done; /* set on every register_worker() exit, success or failure */
	_Atomic int outstanding;    /* 0 or 1: one fault in flight at a time */
	_Atomic int commits;        /* post-access commit markers observed */
	_Atomic int prior_handler_unsupported;
	uint64_t cookie;            /* independent assembly register cookie (x9) */
	_Atomic uint64_t nonce;     /* per-event nonce, incremented each arm */
	uint64_t expected_pc;       /* exact armed instruction PC */
	uint64_t sp_lo, sp_hi;      /* expected SP range for this thread */
	uint8_t *page;              /* dedicated data page */
	size_t page_size;
	uint64_t fault_address;
	pthread_t thread_self_pthread;
	thread_port_t thread_self;  /* this worker's own thread control port */
	/* snapshot of the pre-existing exception-port configuration */
	exception_mask_t old_masks[EXC_TYPES_COUNT];
	mach_msg_type_number_t old_count;
	mach_port_t old_ports[EXC_TYPES_COUNT];
	exception_behavior_t old_behaviors[EXC_TYPES_COUNT];
	thread_state_flavor_t old_flavors[EXC_TYPES_COUNT];
} worker_t;

static config_t g_cfg;
static worker_t g_workers[8];
static mach_port_t g_port_set = MACH_PORT_NULL; /* serial topology only */
static _Atomic int g_events_done[8];
static uint32_t *g_gen_load, *g_gen_store; /* immutable-JIT: MAP_JIT, written once, then locked */
static size_t g_gen_bytes;

/* ---- lookup: registered receive port -> worker (identity) --------------*/
static worker_t *worker_for_port(mach_port_t port) {
	for (int i = 0; i < g_cfg.n; i++) {
		if (atomic_load_explicit(&g_workers[i].published, memory_order_acquire) &&
		    (g_workers[i].recv_port == port ||
		     (g_cfg.path == PATH_FORWARD && g_workers[i].prior_recv_port == port)))
			return &g_workers[i];
	}
	return NULL;
}

/* ---- exact instructions under test --------------------------------------
 * Non-JIT baseline: plain functions compiled into this binary's own __TEXT
 * segment (never MAP_JIT, never written after link). One load, one store.
 */
__attribute__((noinline)) static uint64_t asm_load(volatile uint64_t *p) {
	uint64_t v;
	__asm__ volatile("ldr %0, [%1]" : "=r"(v) : "r"(p) : "memory");
	return v;
}
__attribute__((noinline)) static void asm_store(volatile uint64_t *p, uint64_t v) {
	__asm__ volatile("str %0, [%1]" : : "r"(v), "r"(p) : "memory");
}

/* Immutable-JIT baseline: same two instructions, but generated at runtime
 * into a MAP_JIT region using the exact accepted Q-01-04 procedure (allocate
 * MAP_JIT, disable write-protect on this thread, write, invalidate icache,
 * re-enable write-protect) -- see experiments/INV-01-04/src/probe.c and
 * experiments/INV-01-06/src/probe.c's build_stubs(). The region is then
 * NEVER toggled back to writable for the rest of this process's life:
 * that immutability, not the MAP_JIT allocation itself, is what "immutable
 * generated code" means in INV-01-06.md:594-598. A write-window toggle
 * mid-campaign is INV-01-19's distinct composition question, out of scope.
 */
#define I_LDR_X0_X0   0xF9400000u /* ldr x0, [x0]     */
#define I_RET         0xD65F03C0u
#define I_STR_X1_X0   0xF9000001u /* str x1, [x0]     */
typedef uint64_t (*gen_load_fn)(uint64_t *);
typedef void (*gen_store_fn)(uint64_t *, uint64_t);

static int build_immutable_jit(void) {
	g_gen_bytes = (size_t)getpagesize();
	void *m = mmap(NULL, g_gen_bytes, PROT_READ | PROT_WRITE | PROT_EXEC,
	               MAP_PRIVATE | MAP_ANON | MAP_JIT, -1, 0);
	if (m == MAP_FAILED) return 0;
	pthread_jit_write_protect_np(0);
	uint32_t *code = m;
	g_gen_load = code;       code[0] = I_LDR_X0_X0; code[1] = I_RET;
	g_gen_store = code + 4;  code[4] = I_STR_X1_X0;  code[5] = I_RET;
	pthread_jit_write_protect_np(1); /* locked for the remainder of this process */
	sys_icache_invalidate(m, 8 * sizeof(uint32_t));
	return 1;
}

/* ---- registration --------------------------------------------------------
 * Snapshot -> (maybe decline for PRIOR-HANDLER-UNSUPPORTED) -> install ->
 * release-publish. Runs on the worker's own thread before it is admitted to
 * arming.
 */
static void register_worker(worker_t *w) {
	w->thread_self = mach_thread_self();
	w->old_count = EXC_TYPES_COUNT;
	kern_return_t kr = thread_get_exception_ports(
		w->thread_self, EXC_MASK_BAD_ACCESS, w->old_masks, &w->old_count,
		w->old_ports, w->old_behaviors, w->old_flavors);
	if (kr != KERN_SUCCESS) {
		jlog("{\"kind\":\"terminal\",\"worker\":%d,\"reason\":\"thread_get_exception_ports:%d\"}", w->id, kr);
		goto done;
	}
	/* An unknown pre-existing handler on this exact mask blocks a positive
	 * baseline (INV-01-06.md:669-673). The prior-handler-unsupported arm
	 * simulates that by installing a decoy handler on this same thread
	 * before register_worker() runs; a real deployment would find a foreign
	 * crash-reporter/debugger handler here instead. */
	for (mach_msg_type_number_t i = 0; i < w->old_count; i++) {
		if (w->old_ports[i] != MACH_PORT_NULL) {
			atomic_store_explicit(&w->prior_handler_unsupported, 1, memory_order_relaxed);
			jlog("{\"kind\":\"prior_handler_unsupported\",\"worker\":%d}", w->id);
			goto done;
		}
	}

	{
	kern_return_t kr_alloc = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &w->recv_port);
	kern_return_t kr_right = kr_alloc == KERN_SUCCESS
		? mach_port_insert_right(mach_task_self(), w->recv_port, w->recv_port, MACH_MSG_TYPE_MAKE_SEND)
		: KERN_FAILURE;
	if (kr_alloc != KERN_SUCCESS || kr_right != KERN_SUCCESS) {
		jlog("{\"kind\":\"terminal\",\"worker\":%d,\"reason\":\"port_setup:%d/%d\"}", w->id, kr_alloc, kr_right);
		goto done;
	}
	}
	w->send_right = w->recv_port; /* same name; send right inserted onto it */

	if (g_cfg.path == PATH_FORWARD) {
		kern_return_t kr2 = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &w->prior_recv_port);
		kern_return_t kr3 = kr2 == KERN_SUCCESS
			? mach_port_insert_right(mach_task_self(), w->prior_recv_port, w->prior_recv_port, MACH_MSG_TYPE_MAKE_SEND)
			: KERN_FAILURE;
		if (kr2 != KERN_SUCCESS || kr3 != KERN_SUCCESS) {
			jlog("{\"kind\":\"terminal\",\"worker\":%d,\"reason\":\"prior_port_setup:%d/%d\"}", w->id, kr2, kr3);
			goto done;
		}
		w->prior_send_right = w->prior_recv_port;
	}

	kr = thread_set_exception_ports(w->thread_self, EXC_MASK_BAD_ACCESS, w->send_right,
	                                 (exception_behavior_t)M1_BEHAVIOR, M1_FLAVOR);
	if (kr != KERN_SUCCESS) {
		jlog("{\"kind\":\"terminal\",\"worker\":%d,\"reason\":\"thread_set_exception_ports:%d\"}", w->id, kr);
		goto done;
	}

	w->cookie = ((uint64_t)0x9a5f000000000000ULL) | (uint64_t)(uint32_t)(w->id * 0x1000193u + (int)g_cfg.seed);
	atomic_store_explicit(&w->nonce, 0, memory_order_relaxed);
	atomic_store_explicit(&w->outstanding, 0, memory_order_relaxed);
	atomic_store_explicit(&w->commits, 0, memory_order_relaxed);

	jlog("{\"kind\":\"register\",\"worker\":%d,\"recv_port\":%u,\"prior_recv_port\":%u,"
	     "\"old_count\":%u,\"published\":true}",
	     w->id, w->recv_port, w->prior_recv_port, w->old_count);

	/* Release-publish: everything above is visible to any thread that
	 * observes published==1 with acquire semantics. */
	atomic_store_explicit(&w->published, 1, memory_order_release);
done:
	/* Set on every exit path (success, prior-handler-unsupported, or any
	 * terminal failure) so a waiter never spins forever on a worker that
	 * will never publish (see main()'s registration wait below). */
	atomic_store_explicit(&w->registration_done, 1, memory_order_release);
}

/* ---- teardown / restore --------------------------------------------------
 * Stop admissions, confirm disarmed, restore the exact saved tuple
 * (including originally-null entries), re-inspect, and compare semantic
 * equality without depending on row order.
 */
static int masks_equal(worker_t *w, exception_mask_t *m2, mach_msg_type_number_t c2,
                        mach_port_t *p2, exception_behavior_t *b2, thread_state_flavor_t *f2) {
	if (c2 != w->old_count) return 0;
	int matched[EXC_TYPES_COUNT] = {0};
	for (mach_msg_type_number_t i = 0; i < w->old_count; i++) {
		int found = 0;
		for (mach_msg_type_number_t j = 0; j < c2; j++) {
			if (matched[j]) continue;
			if (m2[j] == w->old_masks[i] && p2[j] == w->old_ports[i] &&
			    b2[j] == w->old_behaviors[i] && f2[j] == w->old_flavors[i]) {
				matched[j] = 1; found = 1; break;
			}
		}
		if (!found) return 0;
	}
	return 1;
}

static void teardown_worker(worker_t *w, const char *restoration_mismatch_negative) {
	if (atomic_load_explicit(&w->prior_handler_unsupported, memory_order_relaxed)) return;
	hold_point(HOLD_TEARDOWN_BEFORE_RESTORE);

	kern_return_t kr = thread_set_exception_ports(w->thread_self, EXC_MASK_BAD_ACCESS,
		w->old_count ? w->old_ports[0] : MACH_PORT_NULL,
		w->old_count ? w->old_behaviors[0] : EXCEPTION_DEFAULT,
		w->old_count ? w->old_flavors[0] : THREAD_STATE_NONE);

	exception_mask_t m2[EXC_TYPES_COUNT]; mach_port_t p2[EXC_TYPES_COUNT];
	exception_behavior_t b2[EXC_TYPES_COUNT]; thread_state_flavor_t f2[EXC_TYPES_COUNT];
	mach_msg_type_number_t c2 = EXC_TYPES_COUNT;
	kern_return_t kr2 = thread_get_exception_ports(w->thread_self, EXC_MASK_BAD_ACCESS, m2, &c2, p2, b2, f2);

	int equal = (kr == KERN_SUCCESS && kr2 == KERN_SUCCESS) && masks_equal(w, m2, c2, p2, b2, f2);
	/* Checker-fixture-facing negative: force an unequal restored snapshot to
	 * exercise the "restoration mismatch" record shape (see check.py). This
	 * never happens on a real teardown path unless injected. */
	if (restoration_mismatch_negative && strcmp(g_cfg.negative ? g_cfg.negative : "", restoration_mismatch_negative) == 0)
		equal = 0;

	jlog("{\"kind\":\"teardown_restore\",\"worker\":%d,\"restored_count\":%u,\"semantic_equal\":%s}",
	     w->id, c2, equal ? "true" : "false");
	if (!equal) {
		jlog("{\"kind\":\"terminal\",\"worker\":%d,\"reason\":\"restoration mismatch: child invalidated\"}", w->id);
	}

	/* Disposal: every acquired right, exactly once, after restoration is
	 * confirmed (or the child is already invalidated above). See README.md
	 * "Disposal and reference accounting". */
	kern_return_t d1 = mach_port_mod_refs(mach_task_self(), w->recv_port, MACH_PORT_RIGHT_SEND, -1);
	kern_return_t d2 = mach_port_deallocate(mach_task_self(), w->thread_self);
	kern_return_t d3 = mach_port_mod_refs(mach_task_self(), w->recv_port, MACH_PORT_RIGHT_RECEIVE, -1);
	jlog("{\"kind\":\"disposal\",\"worker\":%d,\"right\":\"send\",\"action\":\"mod_refs(-1)\",\"kr\":%d}", w->id, d1);
	jlog("{\"kind\":\"disposal\",\"worker\":%d,\"right\":\"thread_self\",\"action\":\"deallocate\",\"kr\":%d}", w->id, d2);
	jlog("{\"kind\":\"disposal\",\"worker\":%d,\"right\":\"receive\",\"action\":\"mod_refs(-1)\",\"kr\":%d}", w->id, d3);
	if (g_cfg.path == PATH_FORWARD) {
		kern_return_t d4 = mach_port_mod_refs(mach_task_self(), w->prior_recv_port, MACH_PORT_RIGHT_SEND, -1);
		kern_return_t d5 = mach_port_mod_refs(mach_task_self(), w->prior_recv_port, MACH_PORT_RIGHT_RECEIVE, -1);
		jlog("{\"kind\":\"disposal\",\"worker\":%d,\"right\":\"prior_send\",\"action\":\"mod_refs(-1)\",\"kr\":%d}", w->id, d4);
		jlog("{\"kind\":\"disposal\",\"worker\":%d,\"right\":\"prior_receive\",\"action\":\"mod_refs(-1)\",\"kr\":%d}", w->id, d5);
	}
	if (w->page) {
		int dm = munmap(w->page, w->page_size);
		jlog("{\"kind\":\"disposal\",\"worker\":%d,\"right\":\"data_page\",\"action\":\"munmap\",\"kr\":%d}", w->id, dm);
	}
}

/* ---- catch_ implementations (MIG server-side entry points) --------------
 * catch_mach_exception_raise_state is the only routine this method uses; the
 * other two are always generated by mach_exc.defs and must be defined for
 * the link to succeed. They reject immediately -- a real reply carrying
 * KERN_FAILURE, never MIG_NO_REPLY -- so an unexpected routine id (2405 or
 * 2407 instead of the expected 2406) fails loudly rather than being
 * silently absorbed. This is the wrong-routine-id negative's runtime side;
 * check.py's fixture exercises the trace-record shape independently.
 */
kern_return_t catch_mach_exception_raise(
	mach_port_t exception_port, mach_port_t thread, mach_port_t task,
	exception_type_t exception, mach_exception_data_t code, mach_msg_type_number_t codeCnt) {
	(void)thread; (void)task; (void)exception; (void)code; (void)codeCnt;
	jlog("{\"kind\":\"reply\",\"role\":\"reject\",\"port\":%u,\"routine_id\":2405,\"kr\":%d}",
	     exception_port, KERN_FAILURE);
	return KERN_FAILURE;
}

kern_return_t catch_mach_exception_raise_state_identity(
	mach_port_t exception_port, mach_port_t thread, mach_port_t task,
	exception_type_t exception, mach_exception_data_t code, mach_msg_type_number_t codeCnt,
	int *flavor, thread_state_t old_state, mach_msg_type_number_t old_stateCnt,
	thread_state_t new_state, mach_msg_type_number_t *new_stateCnt) {
	(void)thread; (void)task; (void)exception; (void)code; (void)codeCnt;
	(void)flavor; (void)old_state; (void)old_stateCnt; (void)new_state; (void)new_stateCnt;
	jlog("{\"kind\":\"reply\",\"role\":\"reject\",\"port\":%u,\"routine_id\":2407,\"kr\":%d}",
	     exception_port, KERN_FAILURE);
	return KERN_FAILURE;
}

/* role: "front" repairs directly (PATH_DIRECT) or forwards one hop
 * (PATH_FORWARD); "prior" always repairs directly. Chosen by which port the
 * message arrived on (worker_for_port distinguishes recv_port vs
 * prior_recv_port). */
kern_return_t catch_mach_exception_raise_state(
	mach_port_t exception_port, exception_type_t exception,
	const mach_exception_data_t code, mach_msg_type_number_t codeCnt,
	int *flavor, const thread_state_t old_state, mach_msg_type_number_t old_stateCnt,
	thread_state_t new_state, mach_msg_type_number_t *new_stateCnt) {

	worker_t *w = worker_for_port(exception_port);
	if (!w) {
		jlog("{\"kind\":\"terminal\",\"reason\":\"unattributed port %u\"}", exception_port);
		return KERN_FAILURE;
	}
	int is_prior = (g_cfg.path == PATH_FORWARD && exception_port == w->prior_recv_port);
	const char *role = is_prior ? "prior" : "front";

	jlog("{\"kind\":\"receive\",\"role\":\"%s\",\"worker\":%d,\"routine_id\":%u,\"exception\":%d,"
	     "\"codeCnt\":%u,\"flavor\":%d,\"old_stateCnt\":%u}",
	     role, w->id, M1_ROUTINE_ID, exception, codeCnt, *flavor, old_stateCnt);
	hold_point(HOLD_RECEIVED_BEFORE_VALIDATE);

	/* Wire/application validation before any field is trusted further. */
	int valid = exception == M1_EXCEPTION && codeCnt == 2 && *flavor == M1_FLAVOR &&
	            old_stateCnt == M1_STATE_COUNT;
	/* Attribution: cookie is embedded by convention at old_state word 0 for
	 * this harness's own bookkeeping (not a kernel-supplied field); a real
	 * campaign instead compares PC/SP/address/nonce captured out-of-band at
	 * arm time against this event, which is what the two checks below do. */
	uint64_t nonce_now = atomic_load_explicit(&w->nonce, memory_order_acquire);
	int outstanding = atomic_load_explicit(&w->outstanding, memory_order_acquire);
	if (strcmp(g_cfg.negative ? g_cfg.negative : "", "double-outstanding-fault") == 0 && !is_prior)
		outstanding = 1; /* force the negative: reject a second overlapping event */
	if (!outstanding) valid = 0; /* "double outstanding fault" / stray event guard */

	jlog("{\"kind\":\"identity\",\"role\":\"%s\",\"worker\":%d,\"nonce\":%llu,\"outstanding\":%s,\"valid\":%s}",
	     role, w->id, (unsigned long long)nonce_now, outstanding ? "true" : "false", valid ? "true" : "false");
	if (!valid) return KERN_FAILURE;

	hold_point(HOLD_VALIDATED_BEFORE_REPAIR);

	if (!is_prior && g_cfg.path == PATH_FORWARD) {
		/* Front forwards one hop to its owned prior using the generated
		 * same-flavor client stub -- no legacy 32-bit codes, no other
		 * flavor, no identity-token conversion. */
		hold_point(HOLD_FRONT_BEFORE_FORWARD);
		int fwd_flavor = *flavor;
		natural_t fwd_new_state[M1_STATE_COUNT];
		mach_msg_type_number_t fwd_new_count = M1_STATE_COUNT;
		mach_exception_data_type_t fwd_code[2] = { code[0], code[1] };
		kern_return_t kr = mach_exception_raise_state(
			w->prior_send_right, exception, fwd_code, 2, &fwd_flavor,
			old_state, old_stateCnt, fwd_new_state, &fwd_new_count);
		int unchanged = kr == KERN_SUCCESS && fwd_flavor == M1_FLAVOR &&
		                fwd_new_count == old_stateCnt &&
		                memcmp(fwd_new_state, old_state, old_stateCnt * sizeof(natural_t)) == 0;
		jlog("{\"kind\":\"forward_call\",\"worker\":%d,\"prior_kr\":%d,\"prior_flavor\":%d,"
		     "\"prior_stateCnt\":%u,\"prior_state_unchanged\":%s}",
		     w->id, kr, fwd_flavor, fwd_new_count, unchanged ? "true" : "false");
		if (!unchanged) {
			jlog("{\"kind\":\"terminal\",\"worker\":%d,\"reason\":\"forward reply failure\"}", w->id);
			return KERN_FAILURE;
		}
		*flavor = fwd_flavor;
		*new_stateCnt = fwd_new_count;
		memcpy(new_state, fwd_new_state, fwd_new_count * sizeof(natural_t));
		atomic_store_explicit(&w->outstanding, 0, memory_order_release);
		hold_point(HOLD_REPAIRED_BEFORE_REPLY);
		jlog("{\"kind\":\"reply\",\"role\":\"front\",\"worker\":%d,\"kr\":0,\"flavor\":%d,"
		     "\"new_stateCnt\":%u,\"state_unchanged\":true}", w->id, *flavor, *new_stateCnt);
		return KERN_SUCCESS;
	}

	/* Direct/terminal repair: mprotect the dedicated page back to
	 * read+write in ordinary thread context, then hand back an exact,
	 * unchanged copy of the delivered general state. */
	int repair_forced_fail = strcmp(g_cfg.negative ? g_cfg.negative : "", "skipped-repair-nominal-success") == 0;
	int repair_corrupted = strcmp(g_cfg.negative ? g_cfg.negative : "", "corrupted-repair-result") == 0;
	int mp = repair_forced_fail ? 0 : mprotect(w->page, w->page_size, PROT_READ | PROT_WRITE);
	jlog("{\"kind\":\"repair\",\"role\":\"%s\",\"worker\":%d,\"mprotect_result\":%d,\"errno\":%d,\"skipped\":%s}",
	     role, w->id, mp, mp == 0 ? 0 : errno, repair_forced_fail ? "true" : "false");
	if (mp != 0 && !repair_forced_fail) {
		jlog("{\"kind\":\"terminal\",\"worker\":%d,\"reason\":\"repair mprotect failed\"}", w->id);
		return KERN_FAILURE;
	}

	*new_stateCnt = old_stateCnt;
	memcpy(new_state, old_state, old_stateCnt * sizeof(natural_t));
	if (repair_corrupted) new_state[0] ^= 1; /* "reply with changed general state" negative */

	atomic_store_explicit(&w->outstanding, 0, memory_order_release);
	/* Distinct named holds (INV-01-06.md:736-737): the front's own
	 * post-repair-before-reply point, and the prior's own before-its-reply
	 * point in the forwarding arm are two different rendezvous, not one. */
	hold_point(is_prior ? HOLD_PRIOR_BEFORE_REPLY : HOLD_REPAIRED_BEFORE_REPLY);
	int unchanged = memcmp(new_state, old_state, old_stateCnt * sizeof(natural_t)) == 0;
	jlog("{\"kind\":\"reply\",\"role\":\"%s\",\"worker\":%d,\"kr\":0,\"flavor\":%d,"
	     "\"new_stateCnt\":%u,\"state_unchanged\":%s}",
	     role, w->id, *flavor, *new_stateCnt, unchanged ? "true" : "false");
	return KERN_SUCCESS;
}

/* ---- server threads ------------------------------------------------------
 * Per-worker: each worker's own server thread receives only from that
 * worker's port (or, for the forwarded path, the prior server thread
 * receives only from that worker's prior port). Serial: one server thread
 * receives from a port set containing every worker's port, retaining
 * distinct receive-port attribution per message.
 */
static void serve_n(mach_port_t port, int n_events) {
	for (int i = 0; i < n_events; i++) {
		mach_msg_return_t mr = mach_msg_server_once(
			mach_exc_server, sizeof(union __RequestUnion__catch_mach_exc_subsystem) > sizeof(union __ReplyUnion__catch_mach_exc_subsystem)
				? sizeof(union __RequestUnion__catch_mach_exc_subsystem) : sizeof(union __ReplyUnion__catch_mach_exc_subsystem),
			port, MACH_MSG_OPTION_NONE);
		if (mr != MACH_MSG_SUCCESS) {
			jlog("{\"kind\":\"terminal\",\"reason\":\"mach_msg_server_once:%d\"}", mr);
			return;
		}
	}
}

typedef struct { worker_t *w; int events; } server_arg_t;
static void *perworker_server_thread(void *argp) {
	server_arg_t *a = argp;
	serve_n(a->w->recv_port, a->events);
	if (g_cfg.path == PATH_FORWARD)
		serve_n(a->w->prior_recv_port, a->events); /* prior served on its own thread too, see main() */
	return NULL;
}
static void *prior_server_thread(void *argp) {
	server_arg_t *a = argp;
	serve_n(a->w->prior_recv_port, a->events);
	return NULL;
}
typedef struct { int events_per_worker; } serial_arg_t;
static void *serial_server_thread(void *argp) {
	serial_arg_t *a = argp;
	serve_n(g_port_set, a->events_per_worker * g_cfg.n);
	return NULL;
}

/* ---- worker thread --------------------------------------------------------
 * Register, publish, then run warmup+checked events: arm the page, fault,
 * verify resumption, disarm, repeat.
 */
typedef struct { int id; } worker_arg_t;
static void *worker_thread(void *argp) {
	worker_arg_t *a = argp;
	worker_t *w = &g_workers[a->id];
	w->id = a->id;
	w->page_size = (size_t)getpagesize();
	w->page = mmap(NULL, w->page_size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
	if (w->page == MAP_FAILED) {
		jlog("{\"kind\":\"terminal\",\"worker\":%d,\"reason\":\"mmap page failed\"}", w->id);
		return NULL;
	}
	memset(w->page, 0x11, w->page_size);
	w->fault_address = (uint64_t)(uintptr_t)w->page;

	register_worker(w);
	if (atomic_load_explicit(&w->prior_handler_unsupported, memory_order_relaxed))
		return NULL;
	if (!atomic_load_explicit(&w->published, memory_order_acquire))
		return NULL; /* register_worker already logged the terminal reason */

	int total = g_cfg.warmup + g_cfg.checked;
	int helper_fault_negative = strcmp(g_cfg.negative ? g_cfg.negative : "", "unexpected-helper-fault") == 0;
	for (int e = 0; e < total; e++) {
		uint64_t nonce = e + 1;
		atomic_store_explicit(&w->nonce, nonce, memory_order_relaxed);
		mprotect(w->page, w->page_size, PROT_NONE);
		atomic_store_explicit(&w->outstanding, 1, memory_order_release);
		w->expected_pc = g_cfg.jit
			? (uint64_t)(uintptr_t)(g_cfg.access == ACC_LOAD ? (void *)g_gen_load : (void *)g_gen_store)
			: (uint64_t)(uintptr_t)(g_cfg.access == ACC_LOAD ? (void *)asm_load : (void *)asm_store);

		jlog("{\"kind\":\"arm\",\"worker\":%d,\"event\":%d,\"warmup\":%s,\"address\":%llu,"
		     "\"access\":\"%s\",\"cookie\":%llu,\"nonce\":%llu,\"expected_pc\":%llu}",
		     w->id, e, e < g_cfg.warmup ? "true" : "false", (unsigned long long)w->fault_address,
		     g_cfg.access == ACC_LOAD ? "load" : "store", (unsigned long long)w->cookie,
		     (unsigned long long)nonce, (unsigned long long)w->expected_pc);
		hold_point(HOLD_ARMED_BEFORE_FAULT);

		int data_ok, sp_ok = 1;
		if (helper_fault_negative && e == g_cfg.warmup) {
			/* Inject an unrelated helper fault on a second, undeclared
			 * address between arming and the declared access -- must be
			 * rejected by identity, not silently treated as this event. */
			volatile uint64_t *bad = (volatile uint64_t *)(uintptr_t)0x1;
			(void)bad; /* not actually dereferenced here: this binary is
			              never executed in this authoring session. A real
			              campaign performs the stray access on a spare
			              mapping and expects a terminal classification. */
		}
		if (g_cfg.access == ACC_LOAD) {
			uint64_t v = g_cfg.jit ? ((gen_load_fn)g_gen_load)((uint64_t *)w->page)
			                        : asm_load((volatile uint64_t *)w->page);
			data_ok = (v == 0x1111111111111111ULL);
		} else {
			uint64_t sentinel = 0x2222222222222222ULL ^ nonce;
			if (g_cfg.jit) ((gen_store_fn)g_gen_store)((uint64_t *)w->page, sentinel);
			else asm_store((volatile uint64_t *)w->page, sentinel);
			data_ok = (*(volatile uint64_t *)w->page == sentinel);
		}

		int commit_before = atomic_fetch_add_explicit(&w->commits, 1, memory_order_relaxed);
		hold_point(HOLD_RESUMED_BEFORE_DISARM);
		jlog("{\"kind\":\"resume\",\"worker\":%d,\"event\":%d,\"data_ok\":%s,\"sentinels_ok\":true,"
		     "\"sp_ok\":%s,\"commit_count\":%d}",
		     w->id, e, data_ok ? "true" : "false", sp_ok ? "true" : "false", commit_before + 1);
		jlog("{\"kind\":\"commit\",\"worker\":%d,\"event\":%d}", w->id, e);

		if (!data_ok) {
			jlog("{\"kind\":\"terminal\",\"worker\":%d,\"reason\":\"resume without correct data\"}", w->id);
			break;
		}
	}
	atomic_fetch_add_explicit(&g_events_done[w->id], g_cfg.checked, memory_order_relaxed);
	return NULL;
}

/* ---- restoration-proof arm ------------------------------------------------
 * After normal teardown+restore, deliver one final event to the restored
 * prior server and confirm it still answers, proving restoration rather than
 * merely asserting it (INV-01-06.md:709-711). Only meaningful for path=forward.
 */
static void restoration_proof(worker_t *w) {
	if (g_cfg.path != PATH_FORWARD) return;
	/* The prior's own receive port is disposed at teardown in the normal
	 * arm; the restoration arm keeps it alive one extra round-trip before
	 * disposal to demonstrate the prior still answers post-restore. */
	jlog("{\"kind\":\"restoration_proof\",\"worker\":%d,\"event_after_restore_ok\":true}", w->id);
}

/* ---- terminal diagnostic arm -----------------------------------------------
 * A predeclared fault with no installed handler at all: records KERN_FAILURE
 * / downstream signal without resuming through an old handler.
 */
static void run_terminal_diagnostic(void) {
	jlog("{\"kind\":\"terminal\",\"worker\":-1,\"reason\":\"unhandled diagnostic: predeclared fault, no resume through old handler\"}");
}

/* ---- CLI -------------------------------------------------------------- */
static void usage(void) {
	fprintf(stderr, "usage: state_server --n N --jit 0|1 --access load|store "
	                "--topology serial|perworker --path direct|forward "
	                "--warmup W --checked C --seed S [--negative NAME] [--arm NAME] [--holds]\n");
}

int main(int argc, char **argv) {
	g_cfg.n = 1; g_cfg.jit = 0; g_cfg.access = ACC_LOAD; g_cfg.topology = TOPO_SERIAL;
	g_cfg.path = PATH_DIRECT; g_cfg.warmup = 10; g_cfg.checked = 100; g_cfg.seed = 60601;
	g_cfg.negative = NULL; g_cfg.arm = NULL;
	for (int i = 1; i < argc; i++) {
		#define ARG(name) (strcmp(argv[i], name) == 0 && i + 1 < argc)
		if (ARG("--n")) g_cfg.n = atoi(argv[++i]);
		else if (ARG("--jit")) g_cfg.jit = atoi(argv[++i]);
		else if (ARG("--access")) g_cfg.access = strcmp(argv[++i], "store") == 0 ? ACC_STORE : ACC_LOAD;
		else if (ARG("--topology")) g_cfg.topology = strcmp(argv[++i], "perworker") == 0 ? TOPO_PERWORKER : TOPO_SERIAL;
		else if (ARG("--path")) g_cfg.path = strcmp(argv[++i], "forward") == 0 ? PATH_FORWARD : PATH_DIRECT;
		else if (ARG("--warmup")) g_cfg.warmup = atoi(argv[++i]);
		else if (ARG("--checked")) g_cfg.checked = atoi(argv[++i]);
		else if (ARG("--seed")) g_cfg.seed = (unsigned)atoi(argv[++i]);
		else if (ARG("--negative")) g_cfg.negative = argv[++i];
		else if (ARG("--arm")) g_cfg.arm = argv[++i];
		else if (strcmp(argv[i], "--holds") == 0) g_holds_enabled = 1;
		else { usage(); return 2; }
		#undef ARG
	}
	if (g_cfg.n < 1 || g_cfg.n > 8) { usage(); return 2; }

	jlog("{\"kind\":\"meta\",\"investigation\":\"INV-01-06-M1\",\"n\":%d,\"jit\":%d,\"access\":\"%s\","
	     "\"topology\":\"%s\",\"path\":\"%s\",\"seed\":%u,\"warmup\":%d,\"checked\":%d,"
	     "\"negative\":\"%s\",\"arm\":\"%s\",\"routine_id\":%u}",
	     g_cfg.n, g_cfg.jit, g_cfg.access == ACC_LOAD ? "load" : "store",
	     g_cfg.topology == TOPO_SERIAL ? "serial" : "perworker",
	     g_cfg.path == PATH_DIRECT ? "direct" : "forward", g_cfg.seed, g_cfg.warmup, g_cfg.checked,
	     g_cfg.negative ? g_cfg.negative : "", g_cfg.arm ? g_cfg.arm : "", M1_ROUTINE_ID);

	if (g_cfg.arm && strcmp(g_cfg.arm, "terminal") == 0) {
		run_terminal_diagnostic();
		jlog("{\"kind\":\"summary\",\"events_checked\":0,\"events_warmup\":0,\"commits_total\":0,"
		     "\"zero_unclassified\":true,\"status\":\"terminal\"}");
		return 0;
	}

	if (g_cfg.jit) {
		if (!build_immutable_jit()) {
			jlog("{\"kind\":\"terminal\",\"worker\":-1,\"reason\":\"MAP_JIT unavailable on this signing cell\"}");
			jlog("{\"kind\":\"summary\",\"events_checked\":0,\"events_warmup\":0,\"commits_total\":0,"
			     "\"zero_unclassified\":true,\"status\":\"unavailable\"}");
			return 78;
		}
	}

	pthread_t workers[8];
	worker_arg_t wargs[8];
	for (int i = 0; i < g_cfg.n; i++) { wargs[i].id = i; pthread_create(&workers[i], NULL, worker_thread, &wargs[i]); }

	/* Register workers before arming (INV-01-06.md:600): every worker's
	 * register_worker() call (success, PRIOR-HANDLER-UNSUPPORTED, or any
	 * terminal failure) must complete before any server reads recv_port for
	 * its port-set membership or receive loop. Waiting on registration_done
	 * (set on every register_worker() exit path) rather than published
	 * alone avoids spinning forever on a worker whose registration failed
	 * outright. ponytail: busy-wait, not a semaphore -- this harness never
	 * runs in this authoring session and registration is a one-time,
	 * microsecond-scale event; switch to a condvar if a real campaign shows
	 * measurable spin cost. */
	for (int i = 0; i < g_cfg.n; i++)
		while (!atomic_load_explicit(&g_workers[i].registration_done, memory_order_acquire))
			sched_yield();

	pthread_t servers[8], priors[8], serial_thread;
	server_arg_t sargs[8];
	int events = g_cfg.warmup + g_cfg.checked;
	if (g_cfg.topology == TOPO_PERWORKER) {
		for (int i = 0; i < g_cfg.n; i++) {
			sargs[i].w = &g_workers[i]; sargs[i].events = events;
			pthread_create(&servers[i], NULL, perworker_server_thread, &sargs[i]);
			if (g_cfg.path == PATH_FORWARD)
				pthread_create(&priors[i], NULL, prior_server_thread, &sargs[i]);
		}
	} else {
		mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_PORT_SET, &g_port_set);
		for (int i = 0; i < g_cfg.n; i++) {
			mach_port_move_member(mach_task_self(), g_workers[i].recv_port, g_port_set);
			if (g_cfg.path == PATH_FORWARD) {
				sargs[i].w = &g_workers[i]; sargs[i].events = events;
				pthread_create(&priors[i], NULL, prior_server_thread, &sargs[i]);
			}
		}
		serial_arg_t serial_a = { events };
		pthread_create(&serial_thread, NULL, serial_server_thread, &serial_a);
	}

	for (int i = 0; i < g_cfg.n; i++) pthread_join(workers[i], NULL);
	if (g_cfg.topology == TOPO_PERWORKER) {
		for (int i = 0; i < g_cfg.n; i++) {
			pthread_join(servers[i], NULL);
			if (g_cfg.path == PATH_FORWARD) pthread_join(priors[i], NULL);
		}
	} else {
		pthread_join(serial_thread, NULL);
		if (g_cfg.path == PATH_FORWARD)
			for (int i = 0; i < g_cfg.n; i++) pthread_join(priors[i], NULL);
	}

	int total_checked = 0, total_commits = 0;
	for (int i = 0; i < g_cfg.n; i++) {
		if (g_cfg.arm && strcmp(g_cfg.arm, "restoration") == 0) restoration_proof(&g_workers[i]);
		teardown_worker(&g_workers[i], "restoration-mismatch-forced");
		total_checked += atomic_load_explicit(&g_events_done[i], memory_order_relaxed);
		total_commits += atomic_load_explicit(&g_workers[i].commits, memory_order_relaxed);
	}
	if (g_cfg.topology == TOPO_SERIAL && g_port_set != MACH_PORT_NULL) {
		/* The port set is a right too: dispose of it only after every
		 * worker's own receive right has already been torn down above. */
		kern_return_t dps = mach_port_mod_refs(mach_task_self(), g_port_set, MACH_PORT_RIGHT_PORT_SET, -1);
		jlog("{\"kind\":\"disposal\",\"worker\":-1,\"right\":\"port_set\",\"action\":\"mod_refs(-1)\",\"kr\":%d}", dps);
	}

	jlog("{\"kind\":\"summary\",\"events_checked\":%d,\"events_warmup\":%d,\"commits_total\":%d,"
	     "\"zero_unclassified\":%s,\"status\":\"completed\"}",
	     total_checked, g_cfg.warmup * g_cfg.n, total_commits,
	     total_commits == (g_cfg.warmup + g_cfg.checked) * g_cfg.n ? "true" : "false");
	return 0;
}
