static pt_element_t ac_test_permissions(ac_test_t *at, unsigned flags,
					bool writable, bool user,
					bool executable)
{
	bool kwritable = !F(AC_CPU_CR0_WP) && !F(AC_ACCESS_USER);
	pt_element_t expected = 0;

	if (F(AC_ACCESS_USER) && !user)
		at->expected_fault = 1;

	if (F(AC_ACCESS_WRITE) && !writable && !kwritable)
		at->expected_fault = 1;

	if (F(AC_ACCESS_FETCH) && !executable)
		at->expected_fault = 1;

	if (F(AC_ACCESS_FETCH) && user && F(AC_CPU_CR4_SMEP))
		at->expected_fault = 1;

	if (user && !F(AC_ACCESS_FETCH) && F(AC_PKU_PKEY) && F(AC_CPU_CR4_PKE)) {
		if (F(AC_PKU_AD)) {
			at->expected_fault = 1;
			at->expected_error |= PFERR_PK_MASK;
		} else if (F(AC_ACCESS_WRITE) && F(AC_PKU_WD) && !kwritable) {
			at->expected_fault = 1;
			at->expected_error |= PFERR_PK_MASK;
		}
	}

	if (!at->expected_fault) {
		expected |= PT_ACCESSED_MASK;
		if (F(AC_ACCESS_WRITE))
			expected |= PT_DIRTY_MASK;
	}

	return expected;
}
