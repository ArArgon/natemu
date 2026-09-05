/* Direct launches have the same reviewed-authorization/root boundary as the
 * supervisor. This helper performs no clock measurement or native control. */
static void authorize_native(const char *out, const char *mode) {
	const char *authorization = getenv("NATEMU_F08_AUTHORIZATION");
	if (!out || !authorization || !getenv("NATEMU_RAW_ROOT") ||
	    !getenv("NATEMU_F08_AUTH_SHA256") || !getenv("NATEMU_F08_BUNDLE_SHA256")) {
		fputs("F08 launch refused: external output/root and reviewed authorization required\n", stderr);
		exit(2);
	}
	char numer[16], denom[16];
	snprintf(numer, sizeof numer, "%u", g_tb.numer);
	snprintf(denom, sizeof denom, "%u", g_tb.denom);
	pid_t pid = fork();
	if (pid == -1) { perror("authorization fork"); exit(2); }
	if (!pid) {
		execlp("python3", "python3", F08_REPO "/experiments/INV-01-08/src/supervisor.py",
		       "--native-authorize", authorization, out, mode, numer, denom, F08_CODE_REV, (char *)NULL);
		_exit(127);
	}
	int status;
	pid_t done;
	do { done = waitpid(pid, &status, 0); } while (done == -1 && errno == EINTR);
	if (done != pid || !WIFEXITED(status) || WEXITSTATUS(status) != 0) exit(2);
}

static void unexpected_fault_control(void) {
	void *page = mmap(NULL, 16384, PROT_NONE, MAP_PRIVATE | MAP_ANON, -1, 0);
	if (page == MAP_FAILED) { perror("unexpected-fault mmap"); exit(2); }
	struct sigaction sa;
	memset(&sa, 0, sizeof sa);
	sa.sa_sigaction = fault_handler;
	sa.sa_flags = SA_SIGINFO;
	if (sigemptyset(&sa.sa_mask) || sigaction(SIGBUS, &sa, NULL) ||
	    sigaction(SIGSEGV, &sa, NULL)) { perror("unexpected-fault sigaction"); exit(2); }
	g_in_probe = 0;
	volatile uint8_t observed = *(volatile uint8_t *)page;
	(void)observed;
	_exit(92); /* access unexpectedly completed: never normal success */
}
