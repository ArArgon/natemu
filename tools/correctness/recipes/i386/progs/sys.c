/* sys: the 32-bit system call entry with ordinary programs' calls: processes (fork, vfork, exec of a 64-bit and of a
   32-bit program, wait), pipes, memory mappings, files with 64-bit offsets, directories, sockets (socketcall), futex
   through a process-shared semaphore, select, clocks (the vDSO's). Values that vary between runs are only checked. */
#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <semaphore.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    if (argc > 2) { printf("child %s\n", argv[2]); return 9; }      /* exec'd by itself below */
    setvbuf(stdout, 0, _IONBF, 0);
    int pfd[2], st; pipe(pfd);
    pid_t c = fork();
    if (!c) { close(pfd[0]); write(pfd[1], "from child", 10); _exit(3); }
    char b[64] = {0}; close(pfd[1]); int n = read(pfd[0], b, sizeof b); waitpid(c, &st, 0);
    printf("fork: read %d '%s' exit %d\n", n, b, WEXITSTATUS(st));
    c = vfork(); if (!c) { execl("/bin/echo", "echo", "exec of a 64-bit program", (char *)0); _exit(127); }
    waitpid(c, &st, 0); printf("vfork exec exit %d\n", WEXITSTATUS(st));
    c = fork(); if (!c) { execl("/proc/self/exe", argv[0], "x", "again", (char *)0); _exit(127); }
    waitpid(c, &st, 0); printf("self exec exit %d\n", WEXITSTATUS(st));
    c = fork(); if (!c) { raise(SIGKILL); _exit(1); }
    waitpid(c, &st, 0); printf("killed %d sig %d\n", WIFSIGNALED(st), WTERMSIG(st));

    size_t len = 1 << 20; unsigned char *m = mmap(0, len, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    for (size_t i = 0; i < len; i += 4096) m[i] = i >> 12;
    printf("mmap %d", m != MAP_FAILED);
    m = mremap(m, len, 2 * len, MREMAP_MAYMOVE); printf(" mremap %d %d", m != MAP_FAILED, m[255 * 4096]);
    printf(" mprotect %d", mprotect(m + 4096, 4096, PROT_READ)); printf(" munmap %d\n", munmap(m, 2 * len));
    void *brk0 = sbrk(0); sbrk(65536); printf("brk %d\n", (char *)sbrk(0) - (char *)brk0);

    int fd = open("big", O_RDWR | O_CREAT | O_TRUNC, 0644);
    off_t at = lseek(fd, 5000000000ll, SEEK_SET); write(fd, "tail", 4);
    struct stat sb; fstat(fd, &sb); pread(fd, b, 4, 5000000000ll); b[4] = 0;
    printf("lseek %lld size %lld mode %o '%s'", (long long)at, (long long)sb.st_size, sb.st_mode & 0777, b); printf(" truncate %d\n", ftruncate(fd, 10));
    close(fd); mkdir("d", 0755); close(open("d/one", O_CREAT, 0600)); close(open("d/two", O_CREAT, 0600));
    DIR *d = opendir("d"); int ents = 0; while (readdir(d)) ents++; closedir(d);
    char cwd[256]; getcwd(cwd, sizeof cwd);
    int rn = rename("d/one", "d/uno"), un = unlink("d/uno"); un |= unlink("d/two"); un |= rmdir("d"); un |= unlink("big");
    printf("dir entries %d rename %d unlink %d cwd-abs %d\n", ents, rn, un, cwd[0] == '/');

    int sv[2]; socketpair(AF_UNIX, SOCK_STREAM, 0, sv);
    send(sv[0], "socket", 6, 0); n = recv(sv[1], b, sizeof b, 0); b[n] = 0;
    fd_set rs; FD_ZERO(&rs); FD_SET(sv[1], &rs); struct timeval tv = {0, 20000};
    printf("socketpair '%s'", b); printf(" select-empty %d", select(sv[1] + 1, &rs, 0, 0, &tv));
    send(sv[0], "x", 1, 0); FD_SET(sv[1], &rs); printf(" select-ready %d\n", select(sv[1] + 1, &rs, 0, 0, 0));

    sem_t *s = mmap(0, 4096, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0); sem_init(s, 1, 0);
    c = fork(); if (!c) { usleep(20000); sem_post(s); _exit(0); }
    printf("sem_wait %d", sem_wait(s)); waitpid(c, &st, 0); printf(" child %d\n", WEXITSTATUS(st));

    struct timespec t0, t1, w = {0, 30000000}; clock_gettime(CLOCK_MONOTONIC, &t0); nanosleep(&w, 0); clock_gettime(CLOCK_MONOTONIC, &t1);
    long long ns = (t1.tv_sec - t0.tv_sec) * 1000000000ll + t1.tv_nsec - t0.tv_nsec;
    struct timeval g; gettimeofday(&g, 0); time_t now = time(0);
    printf("slept %d clocks-agree %d after-2026 %d\n", ns >= 30000000 && ns < 3000000000ll, llabs((long long)now - g.tv_sec) <= 1, now > 1767225600);
    errno = 0; n = write(-1, "", 1); printf("bad fd %d errno %d\n", n, errno);
    return 0;
}
