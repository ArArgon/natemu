/* INV-01-03 address-space representation probe.
 * Emits one JSON object on stdout. No claim is made by this code; the
 * investigation record assigns evidence labels.
 *
 * --negative injects a defect (the alias write uses a different pattern than
 * the one compared) that src/check.py must report as FAIL.
 */
#include <errno.h>
#include <mach/mach.h>
#include <mach/mach_time.h>
#include <mach/mach_vm.h>
#include <mach/task_info.h>
#include <setjmp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/sysctl.h>
#include <unistd.h>

static int negative = 0;

/* ---- page sizes -------------------------------------------------------- */

static void emit_page_sizes(void) {
	size_t hw = 0, len = sizeof(hw);
	int hw_ok = sysctlbyname("hw.pagesize", &hw, &len, NULL, 0) == 0;
	printf("  \"page_size\": {\n");
	printf("    \"vm_page_size\": %llu,\n", (unsigned long long)vm_page_size);
	printf("    \"getpagesize\": %d,\n", getpagesize());
	printf("    \"sysconf__SC_PAGESIZE\": %ld,\n", sysconf(_SC_PAGESIZE));
	printf("    \"sysctl_hw_pagesize_ok\": %s,\n", hw_ok ? "true" : "false");
	printf("    \"sysctl_hw_pagesize\": %zu,\n", hw_ok ? hw : (size_t)0);
	/* PAGE_SIZE is not an independent reading: <mach/arm/vm_param.h> defines it
	 * as vm_page_size, so the pair cannot disagree. It is reported for the
	 * record and excluded from the count and from the checker's comparison. */
	printf("    \"compile_time_PAGE_SIZE\": %d,\n", (int)PAGE_SIZE);
	printf("    \"compile_time_PAGE_SIZE_is_independent\": false,\n");
	printf("    \"independent_reading_count\": %d\n", hw_ok ? 4 : 3);
	printf("  },\n");
}

/* ---- reservations ------------------------------------------------------ */

static void emit_reservations(void) {
	const unsigned long long sizes[] = {
		8ULL << 30, 64ULL << 30, 1024ULL << 30
	};
	printf("  \"reservations\": [\n");
	for (int i = 0; i < 3; i++) {
		errno = 0;
		void *p = mmap(NULL, (size_t)sizes[i], PROT_NONE,
		               MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0);
		int e = errno;
		printf("    {\"bytes\": %llu, \"ok\": %s, \"errno\": %d, \"errno_str\": \"%s\"}%s\n",
		       sizes[i], p == MAP_FAILED ? "false" : "true",
		       p == MAP_FAILED ? e : 0,
		       p == MAP_FAILED ? strerror(e) : "",
		       i == 2 ? "" : ",");
		if (p != MAP_FAILED) munmap(p, (size_t)sizes[i]);
	}
	printf("  ],\n");
}

/* ---- commit and touch -------------------------------------------------- */

static int footprint(unsigned long long *resident, unsigned long long *phys) {
	mach_task_basic_info_data_t b;
	mach_msg_type_number_t bc = MACH_TASK_BASIC_INFO_COUNT;
	task_vm_info_data_t v;
	mach_msg_type_number_t vc = TASK_VM_INFO_COUNT;
	if (task_info(mach_task_self(), MACH_TASK_BASIC_INFO,
	              (task_info_t)&b, &bc) != KERN_SUCCESS) return 0;
	if (task_info(mach_task_self(), TASK_VM_INFO,
	              (task_info_t)&v, &vc) != KERN_SUCCESS) return 0;
	*resident = (unsigned long long)b.resident_size;
	*phys = (unsigned long long)v.phys_footprint;
	return 1;
}

static void emit_commit_touch(void) {
	const size_t reserve = 8ULL << 30;
	const size_t touch = 2ULL << 30;   /* bounded sample, not the whole 8 GiB */
	size_t step = (size_t)vm_page_size;
	unsigned long long r0 = 0, p0 = 0, r1 = 0, p1 = 0;
	int have0 = footprint(&r0, &p0);
	errno = 0;
	char *p = mmap(NULL, reserve, PROT_READ | PROT_WRITE,
	               MAP_PRIVATE | MAP_ANON, -1, 0);
	int e = errno;
	printf("  \"commit_touch\": {\n");
	printf("    \"reserve_bytes\": %zu,\n", reserve);
	printf("    \"touch_bytes\": %zu,\n", touch);
	printf("    \"step_bytes\": %zu,\n", step);
	if (p == MAP_FAILED) {
		printf("    \"mapped\": false, \"errno\": %d, \"errno_str\": \"%s\"\n  },\n",
		       e, strerror(e));
		return;
	}
	size_t pages = 0;
	for (size_t off = 0; off < touch; off += step) { p[off] = 1; pages++; }
	int have1 = footprint(&r1, &p1);
	printf("    \"mapped\": true,\n");
	printf("    \"pages_touched\": %zu,\n", pages);
	printf("    \"task_info_ok\": %s,\n", (have0 && have1) ? "true" : "false");
	printf("    \"resident_before\": %llu,\n", r0);
	printf("    \"resident_after\": %llu,\n", r1);
	printf("    \"phys_footprint_before\": %llu,\n", p0);
	printf("    \"phys_footprint_after\": %llu,\n", p1);
	printf("    \"resident_delta\": %lld\n", (long long)r1 - (long long)r0);
	printf("  },\n");
	munmap(p, reserve);
}

/* ---- mach_vm_remap alias ----------------------------------------------- */

static void emit_alias(void) {
	size_t sz = (size_t)vm_page_size * 4;
	mach_vm_address_t src = 0, dst = 0;
	kern_return_t kr_alloc = mach_vm_allocate(mach_task_self(), &src, sz,
	                                          VM_FLAGS_ANYWHERE);
	printf("  \"alias\": {\n");
	printf("    \"region_bytes\": %zu,\n", sz);
	printf("    \"kr_allocate\": %d,\n", (int)kr_alloc);
	if (kr_alloc != KERN_SUCCESS) { printf("    \"attempted\": false\n  },\n"); return; }

	vm_prot_t cur = VM_PROT_NONE, max = VM_PROT_NONE;
	kern_return_t kr = mach_vm_remap(mach_task_self(), &dst, sz, 0,
	                                 VM_FLAGS_ANYWHERE, mach_task_self(), src,
	                                 /*copy=*/FALSE, &cur, &max,
	                                 VM_INHERIT_NONE);
	printf("    \"attempted\": true,\n");
	printf("    \"kr_remap\": %d,\n", (int)kr);
	printf("    \"cur_prot\": %d,\n", (int)cur);
	printf("    \"max_prot\": %d,\n", (int)max);
	if (kr != KERN_SUCCESS) {
		printf("    \"distinct_addresses\": false,\n");
		printf("    \"bytes_total\": 0,\n    \"self_readback_matched\": 0,\n");
		printf("    \"bytes_matched\": 0,\n");
		printf("    \"readthrough_ok\": false\n  },\n");
		mach_vm_deallocate(mach_task_self(), src, sz);
		return;
	}
	unsigned char *a = (unsigned char *)src, *b = (unsigned char *)dst;
	size_t n = 4096, matched = 0;
	for (size_t i = 0; i < n; i++)
		a[i] = (unsigned char)((negative ? i + 1 : i) & 0xff);
	size_t self_matched = 0;
	for (size_t i = 0; i < n; i++)
		if (a[i] == (unsigned char)(i & 0xff)) self_matched++;
	for (size_t i = 0; i < n; i++)
		if (b[i] == (unsigned char)(i & 0xff)) matched++;
	/* write back through the alias and read through the original */
	size_t back_matched = 0;
	for (size_t i = 0; i < n; i++) b[i] = (unsigned char)((i * 7) & 0xff);
	for (size_t i = 0; i < n; i++)
		if (a[i] == (unsigned char)((i * 7) & 0xff)) back_matched++;
	printf("    \"distinct_addresses\": %s,\n", src == dst ? "false" : "true");
	printf("    \"bytes_total\": %zu,\n", n);
	printf("    \"self_readback_matched\": %zu,\n", self_matched);
	printf("    \"bytes_matched\": %zu,\n", matched);
	printf("    \"bytes_matched_reverse\": %zu,\n", back_matched);
	printf("    \"readthrough_ok\": %s\n", matched == n ? "true" : "false");
	printf("  },\n");
	mach_vm_deallocate(mach_task_self(), dst, sz);
	mach_vm_deallocate(mach_task_self(), src, sz);
}

/* ---- sub-host-page mprotect granularity -------------------------------- */

static sigjmp_buf jb;
static volatile sig_atomic_t caught_signo;
static void onfault(int s, siginfo_t *si, void *uc) {
	(void)si; (void)uc; caught_signo = s; siglongjmp(jb, 1);
}
static int writable(volatile char *p) {
	struct sigaction sa, o1, o2;
	memset(&sa, 0, sizeof sa);
	sa.sa_sigaction = onfault;
	sa.sa_flags = SA_SIGINFO;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGSEGV, &sa, &o1);
	sigaction(SIGBUS, &sa, &o2);
	int ok;
	caught_signo = 0;
	if (sigsetjmp(jb, 1) == 0) { *p = 42; ok = 1; } else { ok = 0; }
	sigaction(SIGSEGV, &o1, NULL);
	sigaction(SIGBUS, &o2, NULL);
	return ok;
}

static void emit_granularity(void) {
	size_t hp = (size_t)vm_page_size;
	size_t guest = 4096;
	char *p = mmap(NULL, hp * 2, PROT_READ | PROT_WRITE,
	               MAP_PRIVATE | MAP_ANON, -1, 0);
	printf("  \"granularity\": {\n");
	printf("    \"host_page\": %zu,\n    \"guest_page\": %zu,\n", hp, guest);
	if (p == MAP_FAILED) { printf("    \"mapped\": false\n  },\n"); return; }
	memset(p, 0, hp * 2);
	/* Protect the second 4 KiB slot of the first host page: 4 KiB-aligned,
	 * not host-page-aligned. */
	errno = 0;
	int rc = mprotect(p + guest, guest, PROT_READ);
	int e = errno;
	printf("    \"mapped\": true,\n");
	printf("    \"mprotect_offset\": %zu,\n    \"mprotect_len\": %zu,\n", guest, guest);
	printf("    \"mprotect_rc\": %d,\n", rc);
	printf("    \"mprotect_errno\": %d,\n", rc == 0 ? 0 : e);
	printf("    \"mprotect_errno_str\": \"%s\",\n", rc == 0 ? "" : strerror(e));
	printf("    \"slot_writable\": [");
	for (size_t i = 0; i < hp * 2 / guest; i++)
		printf("%s%s", i ? ", " : "", writable(p + i * guest) ? "true" : "false");
	printf("]\n  },\n");
	munmap(p, hp * 2);
}

/* ---- mincore / madvise availability ------------------------------------ */

static void emit_advice(void) {
	size_t sz = (size_t)vm_page_size * 4;
	char *p = mmap(NULL, sz, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
	printf("  \"advice\": {\n");
	if (p == MAP_FAILED) { printf("    \"mapped\": false\n  }\n"); return; }
	memset(p, 1, sz);
	char vec[64];
	errno = 0;
	int mc = mincore(p, sz, vec);
	int mce = errno;
	unsigned b0 = (unsigned char)vec[0];
	/* A second reading after clearing the vector and re-writing one page, so
	 * the record shows whether MINCORE_MODIFIED tracks a fresh write. This is
	 * the fault-free dirty-reporting candidate BP-01-05-start left open; the
	 * bits are documented in mincore(2) and in <sys/mman.h>. */
	memset(vec, 0, sizeof vec);
	((volatile char *)p)[sz - 1] = 2;
	errno = 0;
	int mc2 = mincore(p, sz, vec);
	unsigned bl = (unsigned char)vec[sz / (size_t)vm_page_size - 1];
	errno = 0;
	int mv = madvise(p, sz, MADV_FREE_REUSABLE);
	int mve = errno;
	memset(vec, 0, sizeof vec);
	int mc3 = mincore(p, sz, vec);
	unsigned b_after = (unsigned char)vec[0];
	printf("    \"mapped\": true,\n");
	printf("    \"mincore_rc\": %d, \"mincore_errno\": %d,\n", mc, mc == 0 ? 0 : mce);
	printf("    \"mincore_first_byte\": %u,\n", b0);
	printf("    \"mincore_first_byte_flags\": {"
	       "\"INCORE\": %s, \"REFERENCED\": %s, \"MODIFIED\": %s, "
	       "\"REFERENCED_OTHER\": %s, \"MODIFIED_OTHER\": %s, "
	       "\"PAGED_OUT\": %s, \"COPIED\": %s, \"ANONYMOUS\": %s},\n",
	       b0 & MINCORE_INCORE ? "true" : "false",
	       b0 & MINCORE_REFERENCED ? "true" : "false",
	       b0 & MINCORE_MODIFIED ? "true" : "false",
	       b0 & MINCORE_REFERENCED_OTHER ? "true" : "false",
	       b0 & MINCORE_MODIFIED_OTHER ? "true" : "false",
	       b0 & MINCORE_PAGED_OUT ? "true" : "false",
	       b0 & MINCORE_COPIED ? "true" : "false",
	       b0 & MINCORE_ANONYMOUS ? "true" : "false");
	printf("    \"mincore_rc_after_write\": %d,\n", mc2);
	printf("    \"mincore_written_page_byte\": %u,\n", bl);
	printf("    \"mincore_written_page_modified\": %s,\n",
	       bl & MINCORE_MODIFIED ? "true" : "false");
	printf("    \"madvise_free_reusable_rc\": %d, \"madvise_free_reusable_errno\": %d,\n",
	       mv, mv == 0 ? 0 : mve);
	printf("    \"mincore_rc_after_madvise\": %d,\n", mc3);
	printf("    \"mincore_first_byte_after_madvise\": %u\n", b_after);
	printf("  }\n");
	munmap(p, sz);
}

int main(int argc, char **argv) {
	for (int i = 1; i < argc; i++)
		if (strcmp(argv[i], "--negative") == 0) negative = 1;
	printf("{\n");
	printf("  \"investigation\": \"INV-01-03\",\n");
	printf("  \"mode\": \"%s\",\n", negative ? "negative" : "normal");
	mach_timebase_info_data_t tb;
	mach_timebase_info(&tb);
	printf("  \"mach_timebase_info\": {\"numer\": %u, \"denom\": %u, "
	       "\"tick_ns\": %.6f},\n",
	       tb.numer, tb.denom, (double)tb.numer / (double)tb.denom);
	emit_page_sizes();
	emit_reservations();
	emit_commit_touch();
	emit_alias();
	emit_granularity();
	emit_advice();
	printf("}\n");
	return 0;
}
