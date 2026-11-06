// Disposable launch-context adapter. SETEXEC preserves the supervised PID/group.
#include <mach/mach.h>
#include <mach/task_special_ports.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
extern char **environ;
static void checked(const char *name, int rc) {
    printf("{\"op\":\"%s\",\"rc\":%d,\"errno\":0,\"value\":0}\n", name, rc);
    fflush(stdout);
    if (rc) exit(26);
}
int main(int argc, char **argv) {
    if (argc < 4 || (strcmp(argv[1], "ordinary") && strcmp(argv[1], "sanitized"))) return 26;
    posix_spawnattr_t attributes;
    checked("launch_attr_init", posix_spawnattr_init(&attributes));
    checked("launch_setexec", posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETEXEC));
    if (!strcmp(argv[1], "sanitized")) {
        checked("launch_clear_thread_exceptions", thread_set_exception_ports(
            mach_thread_self(), EXC_MASK_ALL, MACH_PORT_NULL,
            EXCEPTION_DEFAULT | MACH_EXCEPTION_CODES, THREAD_STATE_NONE));
        checked("launch_clear_registered", mach_ports_register(mach_task_self(), NULL, 0));
        checked("launch_null_bootstrap", posix_spawnattr_setspecialport_np(
            &attributes, MACH_PORT_NULL, TASK_BOOTSTRAP_PORT));
        checked("launch_null_exceptions", posix_spawnattr_setexceptionports_np(
            &attributes, EXC_MASK_ALL, MACH_PORT_NULL, EXCEPTION_DEFAULT | MACH_EXCEPTION_CODES,
            THREAD_STATE_NONE));
    }
    // No CPU-limit setter, task-access/host changes, guest or policy changes.
    int rc = posix_spawn(NULL, argv[2], NULL, &attributes, &argv[2], environ);
    checked("launch_exec_returned", rc ? rc : -1); // Successful SETEXEC never returns.
    return 26;
}
