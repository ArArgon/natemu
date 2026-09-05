/* F08-2 candidate only. Included after the checked clock/allocator helpers. */
#define FB_WORKERS 4
#define FB_ITERS 250000u
#define FB_BYTES 256u
#define FB_OFFSET 78u
#define FB_BEFORE UINT32_C(0xa5a5a5a5)
#define FB_AFTER UINT32_C(0x5a5a5a5a)

struct fb_shared;
struct fb_worker {
	struct fb_shared *shared;
	unsigned id, iterations, writes, locks, unlocks;
	int qos_rc, ready, completed, error_rc, error_injected;
	unsigned error_iteration;
	const char *error_operation;
};
struct fb_shared {
	pthread_mutex_t mutex;
	atomic_uint ready;
	atomic_int go, abort;
	uint8_t *allocation;
	int control; /* 0 normal, 1 omitted store, 2 injected lock error */
	struct fb_worker workers[FB_WORKERS];
};

static void fb_error(struct fb_worker *w, const char *op, unsigned iteration,
                     int rc, int injected) {
	if (!w->error_rc) {
		w->error_operation = op; w->error_iteration = iteration;
		w->error_rc = rc; w->error_injected = injected;
	}
	atomic_store_explicit(&w->shared->abort, 1, memory_order_release);
}

static void *fallback_thread(void *arg) {
	struct fb_worker *w = arg;
	struct fb_shared *s = w->shared;
	w->qos_rc = pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
	if (w->qos_rc) { fb_error(w, "qos", 0, w->qos_rc, 0); return NULL; }
	w->ready = 1;
	atomic_fetch_add_explicit(&s->ready, 1, memory_order_release);
	while (!atomic_load_explicit(&s->go, memory_order_acquire))
		if (atomic_load_explicit(&s->abort, memory_order_acquire)) return NULL;
	for (unsigned i = 0; i < FB_ITERS; i++) {
		if (atomic_load_explicit(&s->abort, memory_order_acquire)) break;
		int injected = s->control == 2 && w->id == 0 && i == 0;
		int rc = injected ? EIO : pthread_mutex_lock(&s->mutex);
		if (rc) { fb_error(w, "lock", i, rc, injected); break; }
		w->locks++;
		uint32_t v;
		memcpy(&v, s->allocation + FB_OFFSET, sizeof v);
		v++;
		if (!(s->control == 1 && w->id == 0 && i == 0)) {
			memcpy(s->allocation + FB_OFFSET, &v, sizeof v);
			w->writes++;
		}
		rc = pthread_mutex_unlock(&s->mutex);
		if (rc) { fb_error(w, "unlock", i, rc, 0); break; }
		w->unlocks++; w->iterations++;
	}
	w->completed = w->iterations == FB_ITERS;
	return NULL;
}

static void fb_rc(FILE *f, int rc) {
	if (rc == -1) fputs("null", f); else fprintf(f, "%d", rc);
}

/* No worker status is read after failed join. This coordinator-only record
 * remains useful even if a worker still owns its mutex or allocation. */
static void fb_join_fatal(FILE *f, int creates[FB_WORKERS],
                          int joins[FB_WORKERS], unsigned created,
                          unsigned joined) {
	fprintf(f, "  \"fallback_fatal\":{\"operation\":\"join\","
	        "\"created\":%u,\"joined\":%u,\"create_rc\":[", created, joined);
	for (unsigned i = 0; i < FB_WORKERS; i++) {
		if (i) fputc(',', f); fb_rc(f, creates[i]);
	}
	fputs("],\"join_rc\":[", f);
	for (unsigned i = 0; i < FB_WORKERS; i++) {
		if (i) fputc(',', f); fb_rc(f, joins[i]);
	}
	fputs("]},\n  \"completed_normal\":false\n}\n", f);
	fflush(f); /* ordinary coordinator context, never a signal handler */
	_exit(91);
}

static int run_fallback(FILE *f, int control) {
	struct fb_shared s;
	memset(&s, 0, sizeof s);
	s.control = control;
	atomic_init(&s.ready, 0); atomic_init(&s.go, 0); atomic_init(&s.abort, 0);
	s.allocation = xaligned(128, FB_BYTES);
	memset(s.allocation, 0, FB_BYTES);
	uint32_t before = FB_BEFORE, after = FB_AFTER, initial = 0;
	memcpy(s.allocation + FB_OFFSET - 4, &before, 4);
	memcpy(s.allocation + FB_OFFSET, &initial, 4);
	memcpy(s.allocation + FB_OFFSET + 4, &after, 4);
	int creates[FB_WORKERS], joins[FB_WORKERS];
	for (unsigned i = 0; i < FB_WORKERS; i++) {
		creates[i] = joins[i] = -1;
		s.workers[i].shared = &s; s.workers[i].id = i;
		s.workers[i].qos_rc = -1;
	}
	int init_rc = pthread_mutex_init(&s.mutex, NULL), destroy_rc = -1;
	pthread_t threads[FB_WORKERS];
	unsigned created = 0, joined = 0;
	uint64_t start = 0, end = 0, elapsed = 0;
	int timing_valid = 0;
	if (!init_rc) {
		start = mach_absolute_time();
		for (unsigned i = 0; i < FB_WORKERS; i++) {
			creates[i] = pthread_create(&threads[i], NULL, fallback_thread, &s.workers[i]);
			if (creates[i]) { atomic_store(&s.abort, 1); break; }
			created++;
		}
		while (!atomic_load_explicit(&s.abort, memory_order_acquire) &&
		       atomic_load_explicit(&s.ready, memory_order_acquire) != FB_WORKERS) {}
		atomic_store_explicit(&s.go, 1, memory_order_release);
		for (unsigned i = 0; i < created; i++) {
			joins[i] = pthread_join(threads[i], NULL);
			if (joins[i]) fb_join_fatal(f, creates, joins, created, joined);
			joined++;
		}
		end = mach_absolute_time();
		if (end > start && g_tb.numer && g_tb.denom) {
			__uint128_t ns = (__uint128_t)(end - start) * g_tb.numer / g_tb.denom;
			if (ns > 0 && ns <= UINT64_MAX) { elapsed = (uint64_t)ns; timing_valid = 1; }
		}
		int ownership_unknown = 0;
		for (unsigned i = 0; i < created; i++)
			if (s.workers[i].error_rc && !strcmp(s.workers[i].error_operation, "unlock"))
				ownership_unknown = 1;
		if (!ownership_unknown) destroy_rc = pthread_mutex_destroy(&s.mutex);
	}
	uint32_t final, final_before, final_after;
	memcpy(&final, s.allocation + FB_OFFSET, 4);
	memcpy(&final_before, s.allocation + FB_OFFSET - 4, 4);
	memcpy(&final_after, s.allocation + FB_OFFSET + 4, 4);
	uint64_t completed = 0, writes = 0;
	int okay = !control && !init_rc && !destroy_rc && created == FB_WORKERS &&
	           joined == FB_WORKERS && timing_valid && !atomic_load(&s.abort);
	for (unsigned i = 0; i < FB_WORKERS; i++) {
		struct fb_worker *w = &s.workers[i];
		completed += w->iterations; writes += w->writes;
		okay &= w->ready && w->completed && !w->qos_rc && !w->error_rc &&
		        w->iterations == FB_ITERS && w->writes == FB_ITERS &&
		        w->locks == FB_ITERS && w->unlocks == FB_ITERS;
	}
	okay &= final == FB_ITERS * FB_WORKERS && final_before == before && final_after == after;
	fprintf(f, "  \"fallback_global_lock\":{\"schema\":\"INV-01-08-fallback-v2\","
	        "\"threads\":4,\"allocation_size\":%u,\"allocation_alignment\":128,"
	        "\"allocation_address\":%llu,\"target_offset\":%u,\"target_width\":4,"
	        "\"initial_value\":0,\"final_value\":%u,\"expected_value\":1000000,"
	        "\"expected_ops\":1000000,\"ops\":%llu,\"completed_ops\":%llu,"
	        "\"actual_writes\":%llu,\"lost_updates\":%lld,"
	        "\"canary_before_initial\":%u,\"canary_before_final\":%u,"
	        "\"canary_after_initial\":%u,\"canary_after_final\":%u,"
	        "\"created\":%u,\"joined\":%u,\"init_rc\":%d,\"destroy_rc\":",
	        FB_BYTES, (unsigned long long)(uintptr_t)s.allocation, FB_OFFSET, final,
	        (unsigned long long)completed, (unsigned long long)completed,
	        (unsigned long long)writes, (long long)(1000000 - (int64_t)final),
	        before, final_before, after, final_after, created, joined, init_rc);
	fb_rc(f, destroy_rc);
	fputs(",\"create_rc\":[", f);
	for (unsigned i = 0; i < FB_WORKERS; i++) { if (i) fputc(',', f); fb_rc(f, creates[i]); }
	fputs("],\"join_rc\":[", f);
	for (unsigned i = 0; i < FB_WORKERS; i++) { if (i) fputc(',', f); fb_rc(f, joins[i]); }
	fprintf(f, "],\"start_ticks\":%llu,\"end_ticks\":%llu,\"elapsed_ns\":%llu,"
	        "\"timebase_numer\":%u,\"timebase_denom\":%u,\"timebase_rc\":0,"
	        "\"timing_valid\":%s,\"injected\":%s,\"completed_normal\":%s,\"workers\":[",
	        (unsigned long long)start, (unsigned long long)end, (unsigned long long)elapsed,
	        g_tb.numer, g_tb.denom, timing_valid ? "true" : "false",
	        control ? "true" : "false", okay ? "true" : "false");
	for (unsigned i = 0; i < FB_WORKERS; i++) {
		struct fb_worker *w = &s.workers[i];
		fprintf(f, "%s{\"id\":%u,\"qos\":\"user-interactive\",\"qos_rc\":", i ? "," : "", i);
		fb_rc(f, w->qos_rc);
		fprintf(f, ",\"ready\":%s,\"completed\":%s,\"requested_iterations\":%u,"
		        "\"completed_iterations\":%u,\"actual_writes\":%u,"
		        "\"successful_locks\":%u,\"successful_unlocks\":%u,\"first_error\":",
		        w->ready ? "true" : "false", w->completed ? "true" : "false", FB_ITERS,
		        w->iterations, w->writes, w->locks, w->unlocks);
		if (!w->error_rc) fputs("null}", f);
		else fprintf(f, "{\"operation\":\"%s\",\"iteration\":%u,\"rc\":%d,\"injected\":%s}}",
		             w->error_operation, w->error_iteration, w->error_rc,
		             w->error_injected ? "true" : "false");
	}
	fputs("]},\n", f);
	/* No freeing a mutex/allocation after unknown ownership. The caller ends
 * this process on failure; a timeout bounds peers blocked by unlock failure. */
	if (!init_rc && !destroy_rc) free(s.allocation);
	return okay;
}
