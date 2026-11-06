// Out-of-line replacements for Linux facilities FEXCore touches while
// *compiling* (the compiler tool is single-threaded and never receives signals).
#ifdef __APPLE__
#undef syscall
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <unistd.h>

extern "C" long fexc_syscall(long number, ...) {
  switch (number) {
  case 1001: // futex: report an immediate (spurious) wake. FEXCore's waiters re-check
             // their condition in a loop, so this degrades to polling; wakes are no-ops.
    sched_yield();
    return 0;
  case 1008: // rt_sigprocmask: no signals are delivered to this tool; pretend success.
    return 0;
  case 1006: { // getrandom(buf, len, flags)
    va_list ap; va_start(ap, number);
    void* buf = va_arg(ap, void*); size_t len = va_arg(ap, size_t);
    va_end(ap);
    arc4random_buf(buf, len);
    return long(len);
  }
  default: // getcpu/tgkill/statx/renameat2/pidfd_open: not available on macOS.
    errno = ENOSYS;
    return -1;
  }
}
#endif
