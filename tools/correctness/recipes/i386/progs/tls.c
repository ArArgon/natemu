/* tls: thread-local data through GS in the main thread: initialised and zero data below the thread pointer (negative
   offsets), a read through GS with a register offset, errno, the stack protector's value at %gs:0x14, and a
   variable reached through ___tls_get_addr. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static __thread int a = 0x11223344;
static __thread char name[16] = "tdata";
static __thread long long big;
static __thread int arr[1000];
__thread int gd __attribute__((tls_model("global-dynamic"))) = 77;

__attribute__((noinline)) static int guarded(const char *s) { char b[32]; strcpy(b, s); return (int)strlen(b); }
__attribute__((noinline)) static int *gd_addr(void) { return &gd; }

int main(void)
{
    char *tp; __asm__("movl %%gs:0, %0" : "=r"(tp));
    long off = (char *)&a - tp;
    int through; __asm__("movl %%gs:(%1), %0" : "=r"(through) : "r"(off));
    printf("a=%x name=%s big=%lld below-tp=%s through-gs=%x\n", a, name, big, off < 0 ? "yes" : "no", through);
    a += 1; big = 0x1122334455667788ll; strcpy(name, "changed");
    __asm__("movl %%gs:(%1), %0" : "=r"(through) : "r"(off));
    printf("a=%x name=%s big=%llx through-gs=%x\n", a, name, big, through);
    long s = 0;
    for (int i = 0; i < 1000; i++) arr[i] = i * 3;
    for (int i = 0; i < 1000; i++) s += arr[i];
    printf("arr %ld\n", s);
    errno = 0; int fd = open("/nonexistent/file", O_RDONLY);
    printf("open %d errno %d\n", fd, errno);
    printf("guarded %d gd %d\n", guarded("stack protector"), *gd_addr());
    *gd_addr() = 78; printf("gd %d\n", gd);
    return 0;
}
