/*
 * INV-01-09 device-backing capability probe.
 *
 * Exercises, on this host/OS/SDK, the candidate unprivileged public host
 * backings Q-01-09 asks about: network via vmnet.framework (entitled and
 * unentitled) and via ordinary BSD sockets (SOCK_STREAM/SOCK_DGRAM/SOCK_RAW);
 * serial via posix_openpt/grantpt/unlockpt; entropy via getentropy, at and
 * beyond its documented 256-byte per-call limit; and block via POSIX file
 * I/O with F_NOCACHE, F_FULLFSYNC, F_PUNCHHOLE, and a sparse ftruncate.
 * Timer/APIC is Q-01-07's probe, not repeated here. Shutdown (process exit)
 * needs no capability probe.
 *
 * Apple clang only, SDK headers only, no third-party dependency. Emits one
 * JSON object to stdout per invocation. See ../README.md and
 * ../../../docs/research/investigations/INV-01-09.md for the method this
 * probe implements.
 */
#include <sys/sysctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/random.h>
#include <sys/fcntl.h>
#include <netinet/in.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dispatch/dispatch.h>
#include <vmnet/vmnet.h>

static int g_negative = 0;
static const char *g_label = "unlabeled";

/* ---- small helpers ---- */

static long
sysctl_int(const char *name)
{
    int64_t v = 0;
    size_t sz = sizeof(v);
    if (sysctlbyname(name, &v, &sz, NULL, 0) != 0)
        return -1;
    return (long)v;
}

static void
sysctl_str(const char *name, char *buf, size_t buflen)
{
    size_t sz = buflen;
    if (sysctlbyname(name, buf, &sz, NULL, 0) != 0)
        snprintf(buf, buflen, "unknown");
}

static void
json_str(const char *s)
{
    putchar('"');
    for (const char *p = s; *p; p++) {
        if (*p == '"' || *p == '\\')
            putchar('\\');
        putchar(*p);
    }
    putchar('"');
}

/* ---- vmnet_start_interface ---- */

static void
check_vmnet(void)
{
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    __block vmnet_return_t got_status = (vmnet_return_t)0;
    __block int got_callback = 0;

    xpc_object_t desc = xpc_dictionary_create(NULL, NULL, 0);
    xpc_dictionary_set_uint64(desc, vmnet_operation_mode_key, VMNET_HOST_MODE);

    dispatch_queue_t q = dispatch_queue_create("inv-01-09.vmnet", NULL);
    interface_ref iface = vmnet_start_interface(desc, q,
        ^(vmnet_return_t status, xpc_object_t __unused param) {
            got_status = status;
            got_callback = 1;
            dispatch_semaphore_signal(sem);
        });

    /* vmnet_start_interface is documented to return a non-NULL handle only
     * on success; the completion handler still fires asynchronously. Wait
     * up to 5s for it. */
    long wait_rc = dispatch_semaphore_wait(sem,
        dispatch_time(DISPATCH_TIME_NOW, 5LL * NSEC_PER_SEC));
    int timed_out = (wait_rc != 0);

    /* Only read the __block variables when the wait actually returned 0:
     * dispatch_semaphore_signal/_wait supplies the synchronization edge for
     * that read (REV-048 required change 9). On the timeout path the
     * completion block may still be running, or may run after this process
     * exits, so touching got_status/got_callback here would be an
     * unsynchronized concurrent read of a concurrently written object --
     * undefined behavior. Report the timeout from the wait result alone. */
    int callback_fired = timed_out ? 0 : got_callback;
    unsigned status_code = (!timed_out && got_callback) ? (unsigned)got_status : 0;
    int success = (!timed_out && iface != NULL && got_callback
        && got_status == VMNET_SUCCESS);

    /* The header documents a non-NULL return only "on success"; a scratch
     * run showed start_returned_nonnull and the callback status can
     * disagree (nonnull with VMNET_FAILURE). Stop the interface only when
     * the callback actually reported VMNET_SUCCESS, not merely because the
     * synchronous return was non-NULL -- calling vmnet_stop_interface on a
     * handle whose start reported failure is undefined by the header
     * (REV-048 required change 5). */
    if (success) {
        dispatch_semaphore_t stop_sem = dispatch_semaphore_create(0);
        vmnet_stop_interface(iface, q, ^(vmnet_return_t __unused s) {
            dispatch_semaphore_signal(stop_sem);
        });
        dispatch_semaphore_wait(stop_sem,
            dispatch_time(DISPATCH_TIME_NOW, 5LL * NSEC_PER_SEC));
    }
    xpc_release(desc);

    printf("\"vmnet_start_interface\":{");
    printf("\"label\":"); json_str(g_label); printf(",");
    printf("\"start_returned_nonnull\":%s,", iface != NULL ? "true" : "false");
    printf("\"callback_fired\":%s,", callback_fired ? "true" : "false");
    printf("\"timed_out\":%s,", timed_out ? "true" : "false");
    printf("\"status_code\":%u,", status_code);
    printf("\"success\":%s", success ? "true" : "false");
    printf("}");
}

/* ---- BSD sockets ---- */

static void
check_socket(const char *name, int type, int proto)
{
    errno = 0;
    int fd = socket(AF_INET, type, proto);
    int err = errno;
    printf("\"%s\":{\"fd_ok\":%s,\"errno_val\":%d,\"errno_str\":", name,
        fd >= 0 ? "true" : "false", fd >= 0 ? 0 : err);
    json_str(fd >= 0 ? "" : strerror(err));
    printf("}");
    if (fd >= 0)
        close(fd);
}

/* ---- pty ---- */

static void
check_pty(void)
{
    int ok_open = 0, ok_grant = 0, ok_unlock = 0;
    int fd = posix_openpt(O_RDWR | O_NOCTTY);
    if (fd >= 0) {
        ok_open = 1;
        ok_grant = (grantpt(fd) == 0);
        ok_unlock = (unlockpt(fd) == 0);
        close(fd);
    }
    printf("\"pty\":{\"posix_openpt_ok\":%s,\"grantpt_ok\":%s,\"unlockpt_ok\":%s}",
        ok_open ? "true" : "false", ok_grant ? "true" : "false",
        ok_unlock ? "true" : "false");
}

/* ---- entropy ---- */

static void
check_entropy(void)
{
    unsigned char buf256[256];
    errno = 0;
    int rc256 = getentropy(buf256, sizeof(buf256));
    int err256 = errno;

    unsigned char buf257[257];
    errno = 0;
    int rc257 = getentropy(buf257, sizeof(buf257));
    int err257 = errno;
    int over_limit_ok = (rc257 == 0);
    if (g_negative)
        over_limit_ok = 1; /* fabricated: negative control, see check.py */

    printf("\"getentropy_256\":{\"ok\":%s,\"errno_val\":%d},",
        rc256 == 0 ? "true" : "false", rc256 == 0 ? 0 : err256);
    printf("\"getentropy_over_limit\":{\"attempted_size\":257,\"ok\":%s,\"errno_val\":%d}",
        over_limit_ok ? "true" : "false", rc257 == 0 ? 0 : err257);
}

/* ---- block I/O: F_NOCACHE, F_FULLFSYNC, F_PUNCHHOLE, sparse ftruncate ---- */

static void
check_block_io(const char *scratch_dir)
{
    char path[1024];
    snprintf(path, sizeof(path), "%s/inv-01-09-block.tmp", scratch_dir);

    int open_ok = 0, nocache_ok = 0, fullfsync_ok = 0;
    int pwrite_ok = 0, pread_ok = 0, data_match = 0;
    long long st_blocks_before_punch = -1, st_blocks_after_punch = -1;
    int punchhole_ok = 0, punchhole_freed_blocks = 0;
    int ftruncate_ok = 0;
    long long req_size = 100LL * 1024 * 1024;
    long long st_size = -1, st_blocks_after_grow = -1;
    int is_sparse = 0;

    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0600);
    if (fd >= 0) {
        open_ok = 1;
        nocache_ok = (fcntl(fd, F_NOCACHE, 1) != -1);
        fullfsync_ok = (fcntl(fd, F_FULLFSYNC) != -1);

        char wbuf[4096];
        memset(wbuf, 0xA5, sizeof(wbuf));
        ssize_t wn = pwrite(fd, wbuf, sizeof(wbuf), 0);
        pwrite_ok = (wn == (ssize_t)sizeof(wbuf));

        char rbuf[4096];
        memset(rbuf, 0, sizeof(rbuf));
        ssize_t rn = pread(fd, rbuf, sizeof(rbuf), 0);
        pread_ok = (rn == (ssize_t)sizeof(rbuf));
        data_match = pread_ok && (memcmp(wbuf, rbuf, sizeof(wbuf)) == 0);

        /* F_PUNCHHOLE and the sparse ftruncate below both run on this same
         * written file (not a second, never-written file), and st_blocks is
         * captured immediately before and after the punch, so punchhole_ok
         * means "a hole was actually created" rather than merely "fcntl did
         * not return -1" (REV-048 required change 6). */
        struct stat st_before;
        if (fstat(fd, &st_before) == 0)
            st_blocks_before_punch = (long long)st_before.st_blocks;

        fpunchhole_t ph;
        memset(&ph, 0, sizeof(ph));
        ph.fp_offset = 0;
        ph.fp_length = sizeof(wbuf);
        punchhole_ok = (fcntl(fd, F_PUNCHHOLE, &ph) != -1);

        struct stat st_after;
        if (fstat(fd, &st_after) == 0)
            st_blocks_after_punch = (long long)st_after.st_blocks;
        punchhole_freed_blocks = (st_blocks_before_punch >= 0
            && st_blocks_after_punch >= 0
            && st_blocks_after_punch < st_blocks_before_punch);

        /* Grow this same written-then-punched file well beyond its current
         * size without writing more, and confirm the grown region stays
         * unallocated: the sparse-file property CLM-01-09-19 describes,
         * tested on the file the chain actually wrote. */
        ftruncate_ok = (ftruncate(fd, (off_t)req_size) == 0);
        struct stat st_grow;
        if (fstat(fd, &st_grow) == 0) {
            st_size = (long long)st_grow.st_size;
            st_blocks_after_grow = (long long)st_grow.st_blocks;
            /* st_blocks is in 512-byte units regardless of st_blksize. */
            is_sparse = (st_blocks_after_grow * 512 < st_size / 2);
        }

        close(fd);
        unlink(path);
    }

    printf("\"block_io\":{\"open_ok\":%s,\"f_nocache_ok\":%s,\"f_fullfsync_ok\":%s,"
        "\"pwrite_ok\":%s,\"pread_ok\":%s,\"data_match\":%s,"
        "\"st_blocks_before_punch\":%lld,\"st_blocks_after_punch\":%lld,"
        "\"punchhole_ok\":%s,\"punchhole_freed_blocks\":%s},",
        open_ok ? "true" : "false", nocache_ok ? "true" : "false",
        fullfsync_ok ? "true" : "false", pwrite_ok ? "true" : "false",
        pread_ok ? "true" : "false", data_match ? "true" : "false",
        st_blocks_before_punch, st_blocks_after_punch,
        punchhole_ok ? "true" : "false",
        punchhole_freed_blocks ? "true" : "false");

    printf("\"sparse_ftruncate\":{\"ftruncate_ok\":%s,\"requested_size\":%lld,"
        "\"st_size\":%lld,\"st_blocks\":%lld,\"is_sparse\":%s}",
        ftruncate_ok ? "true" : "false", req_size, st_size, st_blocks_after_grow,
        is_sparse ? "true" : "false");
}

int
main(int argc, char **argv)
{
    const char *scratch_dir = NULL;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--negative") == 0) {
            g_negative = 1;
        } else if (strcmp(argv[i], "--label") == 0 && i + 1 < argc) {
            g_label = argv[++i];
        } else if (strcmp(argv[i], "--scratch-dir") == 0 && i + 1 < argc) {
            scratch_dir = argv[++i];
        } else {
            fprintf(stderr,
                "usage: %s [--negative] [--label LABEL] --scratch-dir DIR\n",
                argv[0]);
            return 2;
        }
    }
    /* --scratch-dir has no default: a direct invocation from the experiment
     * tree must not be able to write temp files under experiments/
     * (REV-048 required change 10). run.sh always passes it explicitly. */
    if (scratch_dir == NULL) {
        fprintf(stderr, "usage: %s [--negative] [--label LABEL] --scratch-dir DIR\n",
            argv[0]);
        return 2;
    }

    char model[256];
    sysctl_str("hw.model", model, sizeof(model));

    printf("{");
    printf("\"experiment\":\"INV-01-09\",");
    printf("\"mode\":\"%s\",", g_negative ? "negative" : "normal");
    printf("\"label\":"); json_str(g_label); printf(",");
    printf("\"host\":{\"hw_pagesize\":%ld,\"hw_ncpu\":%ld,\"hw_model\":",
        sysctl_int("hw.pagesize"), sysctl_int("hw.ncpu"));
    json_str(model);
    printf("},");

    check_vmnet(); printf(",");
    check_socket("socket_stream", SOCK_STREAM, 0); printf(",");
    check_socket("socket_dgram", SOCK_DGRAM, 0); printf(",");
    check_socket("socket_raw", SOCK_RAW, IPPROTO_ICMP); printf(",");
    check_pty(); printf(",");
    check_entropy(); printf(",");
    check_block_io(scratch_dir);

    printf("}\n");
    return 0;
}
