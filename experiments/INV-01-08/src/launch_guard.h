/* Enforce external output, non-overwrite and producing-build identity. */
static void check_native(const char *out) {
    if (!out || !getenv("NATEMU_RAW_ROOT") || !getenv("NATEMU_F08_RUN_ID")) {
        fputs("F08 external output/root and run identity required\n", stderr); exit(2);
    }
    pid_t pid=fork();
    if(pid==-1) { perror("boundary-check fork"); exit(2); }
    if(!pid) {
        execlp("python3","python3",F08_SUPERVISOR,"--native-check",out,F08_COMPILED_BUNDLE,(char *)NULL);
        _exit(127);
    }
    int status; pid_t done;
    do { done=waitpid(pid,&status,0); } while(done==-1 && errno==EINTR);
    if(done!=pid || !WIFEXITED(status) || WEXITSTATUS(status)!=0) exit(2);
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
