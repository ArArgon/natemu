/* hello: start-up (static: libc's INT 0x80 start; dynamic: ld.so), arguments, the auxiliary vector, a system call
   through INT 0x80 and one through the kernel's helper (AT_SYSINFO: SYSENTER on an Intel processor, SYSCALL on AMD),
   libc's formatting, sorting and allocation, 64-bit integer arithmetic. Output is the same on any x86 machine. */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/auxv.h>
#include <sys/syscall.h>
#include <sys/utsname.h>

static int cmp(const void *a, const void *b) { return *(const int *)a - *(const int *)b; }

int main(int argc, char **argv)
{
    printf("hello argc=%d argv1=%s env=%s\n", argc, argc > 1 ? argv[1] : "-", getenv("I386_ENV") ? getenv("I386_ENV") : "-");
    printf("sizes long=%zu ptr=%zu longlong=%zu longdouble=%zu\n", sizeof(long), sizeof(void *), sizeof(long long), sizeof(long double));
    struct utsname u; uname(&u); printf("machine %s\n", u.machine);
    long pid; __asm__ volatile("int $0x80" : "=a"(pid) : "a"(SYS_getpid) : "memory");
    printf("int80 getpid %s\n", pid == getpid() ? "ok" : "BAD");
    unsigned long vs = getauxval(AT_SYSINFO);
    long r = -1;
    if (vs) __asm__ volatile("call *%1" : "=a"(r) : "r"(vs), "a"(SYS_getppid) : "memory", "cc");
    printf("AT_SYSINFO %s, getppid through it %s\n", vs ? "present" : "absent", r == getppid() ? "ok" : "BAD");
    printf("vdso %s pagesz %lu\n", getauxval(AT_SYSINFO_EHDR) ? "present" : "absent", getauxval(AT_PAGESZ));

    printf("%.3f %e %g %5.1f|%-8d|%08x|%+d\n", 3.14159, 1e10 / 3, 0.1 + 0.2, -2.25, 42, 0xbeef, 7);
    printf("%.17g %.17g\n", strtod("2.718281828459045", NULL), atof("1e-3"));
    unsigned x = 12345; int v[200];
    for (int i = 0; i < 200; i++) { x = x * 1103515245u + 12345u; v[i] = (int)(x >> 16) % 1000; }
    qsort(v, 200, sizeof v[0], cmp);
    printf("sorted %d %d %d %d\n", v[0], v[1], v[100], v[199]);
    unsigned long long a = 0x123456789abcdef0ull, b = 0xfedcba987ull;
    printf("u64 %llu %llu %llu %lld\n", a * b, a / b, a % b, (long long)a >> 5);
    size_t n = 3 << 20; char *p = malloc(n); memset(p, 0x5a, n);
    p = realloc(p, 2 * n); memmove(p + n, p, n);
    unsigned s = 0; for (size_t i = 0; i < 2 * n; i += 4097) s += (unsigned char)p[i];
    printf("heap %u len %zu\n", s, strlen(strcpy(p, "thirty-two bits")));
    free(p);
    char buf[64]; snprintf(buf, sizeof buf, "%s/%d", "path", 386); puts(buf);
    return 0;
}
