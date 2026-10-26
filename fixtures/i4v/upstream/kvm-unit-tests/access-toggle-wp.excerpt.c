#define TOGGLE_CR0_WP_TEST_BASE_FLAGS \
	(AC_PDE_PRESENT_MASK | AC_PDE_ACCESSED_MASK | \
	 AC_PTE_PRESENT_MASK | AC_PTE_ACCESSED_MASK | \
	 AC_ACCESS_WRITE_MASK)

static int do_cr0_wp_access(ac_test_t *at, int flags)
{
	const bool cr0_wp = !!(flags & AC_CPU_CR0_WP_MASK);

	at->flags = TOGGLE_CR0_WP_TEST_BASE_FLAGS | flags;
	__ac_set_expected_status(at, false);

	/*
	 * Under VMX the guest might own the CR0.WP bit, requiring KVM to
	 * manually keep track of it where needed, e.g. in the guest page
	 * table walker.
	 *
	 * Load CR0.WP with the inverse value of what will be used during
	 * the access test and toggle EFER.NX to coerce KVM into rebuilding
	 * the current MMU context based on the soon-to-be-stale CR0.WP.
	 */
	set_cr0_wp(!cr0_wp);
	set_efer_nx(1);
	set_efer_nx(0);

	if (!ac_test_do_access(at)) {
		printf("%s: %ssupervisor write with CR0.WP=%d did not %s\n",
		       __FUNCTION__, (flags & AC_FEP_MASK) ? "emulated " : "",
		       cr0_wp, cr0_wp ? "FAULT" : "SUCCEED");
		return 1;
	}

	return 0;
}

static int check_toggle_cr0_wp(ac_pt_env_t *pt_env)
{
	ac_test_t at;
	int err = 0;

	ac_test_init(&at, 0xffff923042007000ul, pt_env);
	at.flags = TOGGLE_CR0_WP_TEST_BASE_FLAGS;
	ac_test_setup_ptes(&at);

	err += do_cr0_wp_access(&at, 0);
	err += do_cr0_wp_access(&at, AC_CPU_CR0_WP_MASK);
	if (!(invalid_mask & AC_FEP_MASK)) {
		err += do_cr0_wp_access(&at, AC_FEP_MASK);
		err += do_cr0_wp_access(&at, AC_FEP_MASK | AC_CPU_CR0_WP_MASK);
	}

	return err == 0;
}
