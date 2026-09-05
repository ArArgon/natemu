/* Fresh M07-C1 research producer. Method194 accepted; code196 NOT accepted.
 * Compile/disassemble only until the independent code gate and launch binding
 * pass. No signal handler, historical probe entry point, or hidden warmup.
 */
#include <CommonCrypto/CommonDigest.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#ifndef C1_REPO
#error "Build must supply the canonical absolute repository path as C1_REPO"
#endif
#if !defined(__APPLE__) || !defined(__aarch64__)
#error "This reviewed method is scoped to Apple Silicon macOS"
#endif
#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "The fixed artifact encoding is little-endian uint64"
#endif

enum { READS = 200001, WORKER_READS = 200000, BATCHES = 200,
       PER_BATCH = 1000, PAIRS = 10000, CONTROLS = 256, CELLS = 20 };
static const uint64_t BUFFER_LIMIT = UINT64_C(256) * 1024 * 1024;
static const uint64_t PAYLOAD_LIMIT = UINT64_C(512) * 1024 * 1024;
/* Conservative reserve for probe-owned automatic arrays/records. This is not
 * a bound on pthread stack mappings, allocator overhead, libraries or RSS. */
static const uint64_t STACK_BUFFER_RESERVE = UINT64_C(1024) * 1024;
static const char *clock_names[6] = {
    "mach_absolute_time", "clock_gettime_nsec_np(CLOCK_UPTIME_RAW)",
    "clock_gettime(CLOCK_MONOTONIC_RAW)", "clock_gettime(CLOCK_MONOTONIC)",
    "clock_gettime(CLOCK_UPTIME_RAW)", "clock_gettime(CLOCK_REALTIME)"
};
static const clockid_t clock_ids[6] = {
    CLOCK_UPTIME_RAW, CLOCK_UPTIME_RAW, CLOCK_MONOTONIC_RAW,
    CLOCK_MONOTONIC, CLOCK_UPTIME_RAW, CLOCK_REALTIME
};
static const char *quality_names[2] = {"user-interactive", "background"};
static const qos_class_t quality_classes[2] = {QOS_CLASS_USER_INTERACTIVE, QOS_CLASS_BACKGROUND};
static const char *modes[] = {"normal", "backward", "zero-nsec", "omit-clock",
                            "omit-contention", "pthread-error", "global-inversion"};
static const char *mode;
static uint64_t seed;
static int directory_fd = -1, current_cell = -1;
static _Thread_local int current_worker = -1;
static mach_timebase_info_data_t base;
static uint64_t allocated, peak_allocated, payload_written;
static bool injected;
struct json { char data[65536]; size_t used; };
static struct json metadata[CELLS];

/* Fatal process termination does not free buffers, destroy a mutex of unknown
 * ownership, or continue after an unjoined worker. The supervisor must reap
 * the exact child and retain complete/partial files and this failure record. */
static _Noreturn void die(const char *code, const char *api, int status) {
    char buffer[1024];
    int count = snprintf(buffer, sizeof buffer,
        "{\"schema\":\"INV-01-07-clock-c1-failure-v1\",\"code\":\"%s\","
        "\"api\":\"%s\",\"status\":%d,\"mode\":\"%s\",\"seed\":%" PRIu64 ","
        "\"cell\":%d,\"worker\":%d}\n", code, api, status,
        mode ? mode : "unknown", seed, current_cell, current_worker);
    int fd = directory_fd < 0 ? -1 : openat(directory_fd, "failure.json",
                                           O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (count > 0 && (size_t)count < sizeof buffer) {
        int target = fd < 0 ? STDERR_FILENO : fd;
        size_t offset = 0;
        while (offset < (size_t)count) {
            ssize_t done = write(target, buffer + offset, (size_t)count - offset);
            if (done < 0 && errno == EINTR) continue;
            if (done <= 0) break;  /* Exit remains failure even if reporting fails. */
            offset += (size_t)done;
        }
    }
    if (fd >= 0) { (void)fsync(fd); (void)close(fd); }
    _exit(2);
}

static void demand(bool condition, const char *code) {
    if (!condition) die(code, "integrity", 0);
}

static void add(struct json *out, const char *format, ...) __attribute__((format(printf, 2, 3)));
static void add(struct json *out, const char *format, ...) {
    va_list arguments;
    va_start(arguments, format);
    int n = vsnprintf(out->data + out->used, sizeof out->data - out->used, format, arguments);
    va_end(arguments);
    demand(n >= 0 && (size_t)n < sizeof out->data - out->used, "metadata-cap");
    out->used += (size_t)n;
}

static uint64_t checked_size(uint64_t rows, uint64_t columns) {
    __uint128_t size = (__uint128_t)rows * columns * sizeof(uint64_t);
    demand(size <= UINT64_MAX && size <= SIZE_MAX, "allocation-overflow");
    return (uint64_t)size;
}

static uint64_t *allocate(uint64_t rows, uint64_t columns) {
    uint64_t size = checked_size(rows, columns);
    demand(size <= BUFFER_LIMIT - STACK_BUFFER_RESERVE &&
           allocated <= BUFFER_LIMIT - STACK_BUFFER_RESERVE - size, "buffer-cap");
    uint64_t *p = calloc(1, (size_t)size);
    if (!p) die("allocation-failure", "calloc", errno);
    allocated += size;
    if (allocated > peak_allocated) peak_allocated = allocated;
    return p;
}

static void release(uint64_t *p, uint64_t rows, uint64_t columns) {
    uint64_t size = checked_size(rows, columns);
    demand(size <= allocated, "allocation-accounting");
    free(p);
    allocated -= size;
}

static void write_bytes(const char *name, const void *data, size_t count) {
    int fd = openat(directory_fd, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (fd < 0) die("output-open", "openat", errno);
    size_t offset = 0;
    while (offset < count) {
        ssize_t done = write(fd, (const char *)data + offset, count - offset);
        if (done < 0 && errno == EINTR) continue;
        if (done <= 0) die("output-write", "write", done < 0 ? errno : EIO);
        offset += (size_t)done;
    }
    if (fsync(fd)) die("output-sync", "fsync", errno);
    if (close(fd)) die("output-close", "close", errno);
}

static void array(struct json *out, const char *tag, const uint64_t *values,
                  uint64_t rows, uint64_t columns) {
    uint64_t size = checked_size(rows, columns);
    demand(size <= UINT32_MAX && size <= PAYLOAD_LIMIT && payload_written <= PAYLOAD_LIMIT - size,
           "payload-cap");
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    if (!CC_SHA256(values, (CC_LONG)size, digest)) die("sha256", "CC_SHA256", 0);
    char hex[65], name[96];
    for (unsigned i = 0; i < sizeof digest; ++i)
        (void)snprintf(hex + 2 * i, 3, "%02x", digest[i]);
    int n = snprintf(name, sizeof name, "cell-%02d-%s.bin", current_cell, tag);
    demand(n > 0 && (size_t)n < sizeof name, "array-name");
    write_bytes(name, values, (size_t)size);
    payload_written += size;
    add(out, "{\"path\":\"%s\",\"sha256\":\"%s\",\"records\":%" PRIu64
             ",\"columns\":%" PRIu64 ",\"encoding\":\"little-endian-uint64\"}",
        name, hex, rows, columns);
}

static bool within(const char *path, const char *root) {
    size_t n = strlen(root);
    return strncmp(path, root, n) == 0 && (path[n] == '/' || path[n] == '\0');
}

static void canonical(const char *path, char result[PATH_MAX], const char *code) {
    demand(path && path[0] == '/', code);
    if (!realpath(path, result)) die(code, "realpath", errno);
    demand(strcmp(path, result) == 0, code);
}

static void authorize(const char *output) {
    const char *fd_text = getenv("NATEMU_C1_LAUNCH_FD");
    demand(fd_text && *fd_text, "missing-launch-binding");
    errno = 0;
    char *end;
    long parsed = strtol(fd_text, &end, 10);
    demand(!errno && !*end && parsed >= 3 && parsed <= INT_MAX, "launch-fd");
    int fd = (int)parsed;
    struct stat info;
    if (fstat(fd, &info)) die("launch-fd", "fstat", errno);
    demand(S_ISFIFO(info.st_mode), "launch-not-pipe");
    char received[PATH_MAX + 256], expected[PATH_MAX + 256];
    size_t offset = 0;
    for (;;) {
        demand(offset < sizeof received, "launch-record-cap");
        ssize_t count = read(fd, received + offset, sizeof received - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) die("launch-read", "read", errno);
        if (!count) break;
        offset += (size_t)count;
    }
    if (close(fd)) die("launch-close", "close", errno);
    int size = snprintf(expected, sizeof expected, "C1-LAUNCH-v1\n%ld\n%" PRIu64 "\n%s\n%s\n",
                        (long)getppid(), seed, mode, output);
    demand(size > 0 && (size_t)size < sizeof expected && offset == (size_t)size
           && memcmp(received, expected, offset) == 0, "launch-binding-mismatch");
    /* An exact matching parent is an accidental-bypass guard, not proof that
     * a hostile parent is trusted. Runner196 verifies actual reviewed bytes. */
}

static void open_output(const char *output) {
    char root[PATH_MAX], repo[PATH_MAX], parent[PATH_MAX], resolved[PATH_MAX];
    canonical(getenv("NATEMU_RAW_ROOT"), root, "raw-root");
    canonical(C1_REPO, repo, "repository");
    demand(strcmp(root, "/") != 0 && !within(root, repo) && !within(repo, root), "root-overlap");
    demand(output && output[0] == '/' && strlen(output) < sizeof parent, "output-path");
    memcpy(parent, output, strlen(output) + 1);
    char *slash = strrchr(parent, '/');
    demand(slash && slash != parent && slash[1], "output-parent");
    const char *name = output + (slash - parent) + 1;
    demand(strcmp(name, ".") != 0 && strcmp(name, "..") != 0, "output-name");
    for (const char *p = name; *p; ++p)
        demand((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
               (*p >= '0' && *p <= '9') || *p == '.' || *p == '-' || *p == '_', "output-name");
    *slash = '\0';
    canonical(parent, resolved, "output-parent");
    demand(within(parent, root) && !within(output, repo), "output-root");
    struct stat s;
    demand(stat(root, &s) == 0 && S_ISDIR(s.st_mode), "raw-root-directory");
    char identity[PATH_MAX];
    int n = snprintf(identity, sizeof identity, "%s/.git", repo);
    demand(n > 0 && (size_t)n < sizeof identity && stat(identity, &s) == 0, "repository-identity");
    if (mkdir(output, 0700)) die("output-not-fresh", "mkdir", errno);
    directory_fd = open(output, O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
    if (directory_fd < 0) die("output-directory", "open", errno);
}

struct quality { int requested, set_status, get_status, relative; qos_class_t got, current; };
static struct quality set_quality(int q) {
    struct quality result = {.requested = q};
    result.set_status = pthread_set_qos_class_self_np(quality_classes[q], 0);
    if (result.set_status) die("qos-set", "pthread_set_qos_class_self_np", result.set_status);
    result.get_status = pthread_get_qos_class_np(pthread_self(), &result.got, &result.relative);
    if (result.get_status) die("qos-get", "pthread_get_qos_class_np", result.get_status);
    result.current = qos_class_self();
    demand(result.got == quality_classes[q] && result.current == quality_classes[q]
           && result.relative == 0, "qos-mismatch");
    return result;
}

static void quality_json(struct json *out, const struct quality *q) {
    /* These are actual returned requested/current API classes, not effective
     * override priority, observed core type, or physical placement. */
    add(out, "{\"requested\":\"%s\",\"set_status\":%d,\"get_status\":%d,"
             "\"get_class\":\"%s\",\"current_class\":\"%s\",\"relative_priority\":%d}",
        quality_names[q->requested], q->set_status, q->get_status,
        q->got == quality_classes[0] ? quality_names[0] : quality_names[1],
        q->current == quality_classes[0] ? quality_names[0] : quality_names[1], q->relative);
}

static uint64_t to_ns(struct timespec value) {
    demand(value.tv_sec >= 0 && value.tv_nsec >= 0 && value.tv_nsec < 1000000000, "timespec-domain");
    __uint128_t result = (__uint128_t)value.tv_sec * 1000000000 + (uint64_t)value.tv_nsec;
    demand(result <= UINT64_MAX, "timespec-overflow");
    return (uint64_t)result;
}

static _Noreturn void clock_failure(const char *code, const char *api, int returned, int error) {
    /* Keep the POSIX return and errno distinct. failure.json's status is
     * errno for these clock APIs, but a positive errno is not their return. */
    char record[256];
    int length = snprintf(record, sizeof record,
        "{\"api\":\"%s\",\"return_value\":%d,\"errno\":%d,\"cell\":%d}\n",
        api, returned, error, current_cell);
    demand(length > 0 && (size_t)length < sizeof record, "api-failure-record");
    write_bytes("api-failure.json", record, (size_t)length);
    die(code, api, error);
}

static struct timespec get_resolution(clockid_t clock) {
    struct timespec value;
    int status = clock_getres(clock, &value);
    if (status) clock_failure("resolution-api", "clock_getres", status, errno);
    demand(to_ns(value) > 0, "resolution-zero");
    return value;
}

static void resolution_json(struct json *out, struct timespec value) {
    add(out, "{\"status\":0,\"seconds\":%" PRIu64 ",\"nanoseconds\":%" PRIu64 "}",
        (uint64_t)value.tv_sec, (uint64_t)value.tv_nsec);
}

static uint64_t uptime(void) {
    uint64_t value = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    if (!value) clock_failure("nsec-zero", "clock_gettime_nsec_np", 0, errno);
    return value;
}

static __attribute__((noinline)) uint64_t read_value(int clock, uint64_t *row) {
    if (clock == 0) return row[0] = mach_absolute_time();
    if (clock == 1) {
        uint64_t value = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
        if (!injected && current_cell == 1 && strcmp(mode, "zero-nsec") == 0) {
            injected = true;
            value = 0;
            errno = EINVAL;  /* Explicit injected API seam, not an OS failure. */
        }
        if (!value) clock_failure("nsec-zero", "clock_gettime_nsec_np", 0, errno);
        return row[0] = value;
    }
    struct timespec value;
    int status = clock_gettime(clock_ids[clock], &value);
    if (status) clock_failure("clock-read-api", "clock_gettime", status, errno);
    uint64_t ns = to_ns(value);
    row[0] = (uint64_t)value.tv_sec;
    row[1] = (uint64_t)value.tv_nsec;
    row[2] = ns;
    row[3] = (uint64_t)status;
    return ns;
}

struct scan { uint64_t forward, zero, backward, minimum; };
static void step(struct scan *s, uint64_t before, uint64_t after) {
    if (after < before) ++s->backward;
    else if (after == before) ++s->zero;
    else {
        ++s->forward;
        uint64_t difference = after - before;
        if (!s->minimum || difference < s->minimum) s->minimum = difference;
    }
}

static void scan_json(struct json *out, struct scan s) {
    add(out, "{\"forward\":%" PRIu64 ",\"zero\":%" PRIu64 ",\"backward\":%" PRIu64
             ",\"min_positive_step\":", s.forward, s.zero, s.backward);
    if (s.minimum) add(out, "%" PRIu64 "}", s.minimum);
    else add(out, "null}");
}

static void noise(uint64_t *rows) {
    for (uint64_t i = 0; i < PAIRS; ++i) {
        rows[3 * i] = i;
        rows[3 * i + 1] = mach_absolute_time();
        rows[3 * i + 2] = mach_absolute_time();
    }
}

static void controls(uint64_t *rows) {
    __uint128_t target = ((__uint128_t)1000000 * base.denom + base.numer - 1) / base.numer;
    demand(target > 0 && target <= UINT64_MAX, "control-target-overflow");
    for (uint64_t i = 0; i < CONTROLS; ++i) {
        uint64_t *r = rows + 7 * i;
        r[0] = i;
        r[1] = mach_absolute_time();
        r[2] = uptime();
        r[3] = mach_absolute_time();
        do {
            r[4] = mach_absolute_time();
            demand(r[4] >= r[3], "control-busy-backward");
        } while (r[4] - r[3] < (uint64_t)target);
        r[5] = uptime();
        r[6] = mach_absolute_time();
    }
}

static void integer128(struct json *out, __uint128_t value) {
    char text[40];
    char *at = text + sizeof text - 1;
    *at = '\0';
    do { *--at = (char)('0' + value % 10); value /= 10; } while (value);
    add(out, "%s", at);
}

static void cost_ratio(struct json *out, uint64_t ticks) {
    /* Wide multiplication precedes division. Reduced JSON integers preserve
     * the exact ns/read ratio rather than using a rounded display value. */
    __uint128_t numerator = (__uint128_t)ticks * base.numer;
    __uint128_t denominator = (__uint128_t)base.denom * PER_BATCH;
    __uint128_t a = numerator, b = denominator;
    while (b) { __uint128_t r = a % b; a = b; b = r; }
    add(out, "{\"numerator\":"); integer128(out, numerator / a);
    add(out, ",\"denominator\":"); integer128(out, denominator / a); add(out, "}");
}

static int compare_ticks(const void *left, const void *right) {
    uint64_t a = *(const uint64_t *)left, b = *(const uint64_t *)right;
    return (a > b) - (a < b);
}

static void phase_json(struct json *out, const char *name, uint64_t *pairs, uint64_t *brackets) {
    char tag[32];
    (void)snprintf(tag, sizeof tag, "%s-noise", name);
    add(out, "{\"noise\":"); array(out, tag, pairs, PAIRS, 3);
    (void)snprintf(tag, sizeof tag, "%s-containment", name);
    add(out, ",\"containment\":"); array(out, tag, brackets, CONTROLS, 7); add(out, "}");
}

static void clock_cell(int id) {
    int q = id / 6, clock = id % 6;
    struct quality quality = set_quality(q);
    struct timespec uptime_resolution = get_resolution(CLOCK_UPTIME_RAW);
    struct timespec resolution = {0, 0};
    if (clock) resolution = get_resolution(clock_ids[clock]);
    uint64_t columns = clock < 2 ? 2 : 5;
    uint64_t *readings = allocate(READS, columns);
    uint64_t *batches = allocate(BATCHES, 3);
    uint64_t *before = allocate(PAIRS, 3), *after = allocate(PAIRS, 3);
    uint64_t *pre_control = allocate(CONTROLS, 7), *post_control = allocate(CONTROLS, 7);
    volatile uint64_t sink = 0;
    for (uint64_t i = 0; i < READS; ++i) {
        uint64_t *row = readings + columns * i;
        row[0] = i;
        sink += read_value(clock, row + 1);  /* Defined modulo2^64 liveness sink. */
    }
    noise(before); controls(pre_control);
    for (uint64_t i = 0; i < BATCHES; ++i) {
        uint64_t temporary[4];
        batches[3 * i] = i;
        batches[3 * i + 1] = mach_absolute_time();
        for (uint64_t j = 0; j < PER_BATCH; ++j) sink += read_value(clock, temporary);
        batches[3 * i + 2] = mach_absolute_time();
    }
    noise(after); controls(post_control);
    if (id == 0 && strcmp(mode, "backward") == 0) {
        demand(readings[1] > 0, "control-precondition");
        for (uint64_t i = 1; i < READS; ++i)
            demand(readings[columns * i + 1] >= readings[columns * (i - 1) + 1], "control-precondition");
        readings[columns + 1] = readings[1] - 1;
        injected = true;
    }
    struct scan scan = {0};
    uint64_t value_column = clock < 2 ? 1 : 3;
    for (uint64_t i = 1; i < READS; ++i)
        step(&scan, readings[columns * (i - 1) + value_column], readings[columns * i + value_column]);
    uint64_t elapsed[BATCHES];
    for (uint64_t i = 0; i < BATCHES; ++i) {
        demand(batches[3 * i + 2] >= batches[3 * i + 1], "batch-backward");
        elapsed[i] = batches[3 * i + 2] - batches[3 * i + 1];
    }
    qsort(elapsed, BATCHES, sizeof elapsed[0], compare_ticks);
    struct json *out = &metadata[id];
    add(out, "{\"clock\":\"%s\",\"qos\":\"%s\",\"qos_record\":", clock_names[clock], quality_names[q]);
    quality_json(out, &quality);
    add(out, ",\"resolution\":");
    if (clock) resolution_json(out, resolution); else add(out, "null");
    add(out, ",\"uptime_resolution\":"); resolution_json(out, uptime_resolution);
    add(out, ",\"readings\":");
    uint64_t retained = READS;
    if (id == 0 && strcmp(mode, "omit-clock") == 0) { --retained; injected = true; }
    array(out, "readings", readings, retained, columns);
    add(out, ",\"batches\":"); array(out, "batches", batches, BATCHES, 3);
    add(out, ",\"pre\":"); phase_json(out, "pre", before, pre_control);
    add(out, ",\"post\":"); phase_json(out, "post", after, post_control);
    add(out, ",\"scan\":"); scan_json(out, scan);
    add(out, ",\"cost_summary\":{\"min\":"); cost_ratio(out, elapsed[0]);
    add(out, ",\"p50\":"); cost_ratio(out, elapsed[99]);
    add(out, ",\"p99\":"); cost_ratio(out, elapsed[197]);
    add(out, ",\"max\":"); cost_ratio(out, elapsed[199]);
    add(out, "},\"sink\":%" PRIu64 "}", (uint64_t)sink);
    release(readings, READS, columns); release(batches, BATCHES, 3);
    release(before, PAIRS, 3); release(after, PAIRS, 3);
    release(pre_control, CONTROLS, 7); release(post_control, CONTROLS, 7);
}

struct contention;
struct worker {
    struct contention *owner;
    int id, create_status, join_status;
    pthread_t thread;
    struct quality quality;
    uint64_t completed, lock_calls, unlock_calls;
};
struct contention {
    pthread_mutex_t mutex;
    int workers, quality, init_status, destroy_status;
    uint64_t count, capacity;
    uint64_t *trace;
    struct worker worker[8];
};

static void *sample_worker(void *argument) {
    struct worker *worker = argument;
    struct contention *shared = worker->owner;
    current_worker = worker->id;
    worker->quality = set_quality(shared->quality);
    for (uint64_t i = 0; i < WORKER_READS; ++i) {
        if (current_cell == 13 && worker->id == 0 && i == 0 && strcmp(mode, "pthread-error") == 0)
            die("pthread-lock", "pthread_mutex_lock", EINVAL); /* No real lock/body runs. */
        ++worker->lock_calls;
        int result = pthread_mutex_lock(&shared->mutex);
        if (result) die("pthread-lock", "pthread_mutex_lock", result);
        demand(shared->count < shared->capacity, "trace-overflow");
        uint64_t *row = shared->trace + 3 * shared->count;
        row[0] = shared->count;
        row[1] = (uint64_t)worker->id;
        row[2] = mach_absolute_time();
        ++shared->count;
        ++worker->unlock_calls;
        result = pthread_mutex_unlock(&shared->mutex);
        if (result) die("pthread-unlock", "pthread_mutex_unlock", result);
        ++worker->completed;
    }
    return NULL;
}

static void contention_cell(int id) {
    int index = id - 12;
    struct contention shared = {.workers = 1 << (index % 4), .quality = index / 4};
    (void)set_quality(shared.quality);
    shared.capacity = (uint64_t)shared.workers * WORKER_READS;
    shared.trace = allocate(shared.capacity, 3);
    shared.init_status = pthread_mutex_init(&shared.mutex, NULL);
    if (shared.init_status) die("pthread-init", "pthread_mutex_init", shared.init_status);
    for (int i = 0; i < shared.workers; ++i) {
        struct worker *w = &shared.worker[i];
        w->owner = &shared; w->id = i;
        w->create_status = pthread_create(&w->thread, NULL, sample_worker, w);
        if (w->create_status) die("pthread-create", "pthread_create", w->create_status);
    }
    for (int i = 0; i < shared.workers; ++i) {
        struct worker *w = &shared.worker[i];
        w->join_status = pthread_join(w->thread, NULL);
        if (w->join_status) die("pthread-join", "pthread_join", w->join_status);
    }
    /* All private worker status reads and mutex destruction occur only after
     * every successful join. Any failed join terminates without either. */
    shared.destroy_status = pthread_mutex_destroy(&shared.mutex);
    if (shared.destroy_status) die("pthread-destroy", "pthread_mutex_destroy", shared.destroy_status);
    demand(shared.count == shared.capacity, "native-trace-count");
    if (id == 13 && strcmp(mode, "global-inversion") == 0) {
        for (uint64_t i = 1; i < shared.count; ++i)
            demand(shared.trace[3 * i + 2] >= shared.trace[3 * (i - 1) + 2], "control-precondition");
        uint64_t first_worker = shared.trace[1];
        bool changed = false;
        for (uint64_t i = 1; i < shared.count; ++i) {
            if (shared.trace[3 * i + 1] == first_worker) continue;
            uint64_t previous = shared.trace[3 * (i - 1) + 2];
            demand(previous > 0, "control-precondition");
            shared.trace[3 * i + 2] = previous - 1;
            changed = true; injected = true; break;
        }
        demand(changed, "control-precondition");
    }
    struct scan global = {0}, workers[8] = {{0}};
    uint64_t previous[8] = {0}, counts[8] = {0}, transitions = 0;
    for (uint64_t i = 0; i < shared.count; ++i) {
        uint64_t *row = shared.trace + 3 * i;
        demand(row[0] == i && row[1] < (uint64_t)shared.workers, "native-trace-id");
        int worker = (int)row[1];
        if (counts[worker]) step(&workers[worker], previous[worker], row[2]);
        previous[worker] = row[2]; ++counts[worker];
        if (i) {
            step(&global, shared.trace[3 * (i - 1) + 2], row[2]);
            transitions += row[1] != shared.trace[3 * (i - 1) + 1];
        }
    }
    struct json *out = &metadata[id];
    add(out, "{\"qos\":\"%s\",\"workers\":%d,\"mutex_init_status\":%d,"
             "\"mutex_destroy_status\":%d,\"worker_records\":[",
        quality_names[shared.quality], shared.workers, shared.init_status, shared.destroy_status);
    for (int i = 0; i < shared.workers; ++i) {
        struct worker *w = &shared.worker[i];
        demand(counts[i] == WORKER_READS && w->completed == WORKER_READS, "native-worker-count");
        add(out, "%s{\"id\":%d,\"qos_record\":", i ? "," : "", i); quality_json(out, &w->quality);
        add(out, ",\"create_status\":%d,\"join_status\":%d,\"completed\":%" PRIu64
                 ",\"lock_calls\":%" PRIu64 ",\"unlock_calls\":%" PRIu64
                 ",\"lock_errors\":0,\"unlock_errors\":0,\"scan\":",
            w->create_status, w->join_status, w->completed, w->lock_calls, w->unlock_calls);
        scan_json(out, workers[i]); add(out, "}");
    }
    add(out, "],\"trace\":");
    uint64_t retained = shared.count;
    if (id == 13 && strcmp(mode, "omit-contention") == 0) { --retained; injected = true; }
    array(out, "trace", shared.trace, retained, 3);
    add(out, ",\"global_scan\":"); scan_json(out, global);
    add(out, ",\"transitions\":%" PRIu64 "}", transitions);
    release(shared.trace, shared.capacity, 3);
}

static uint64_t preflight(void) {
    uint64_t clocks = checked_size(4 * READS, 2) + checked_size(8 * READS, 5)
        + checked_size(12 * BATCHES, 3) + checked_size(24 * PAIRS, 3)
        + checked_size(24 * CONTROLS, 7);
    uint64_t traces = checked_size(UINT64_C(6000000), 3);
    uint64_t payload = clocks + traces;
    demand(payload == UINT64_C(226962048) && payload <= PAYLOAD_LIMIT, "planned-payload");
    uint64_t maximum_clock = checked_size(READS, 5) + checked_size(BATCHES, 3)
        + checked_size(2 * PAIRS, 3) + checked_size(2 * CONTROLS, 7);
    uint64_t maximum_trace = checked_size(8 * WORKER_READS, 3);
    /* Sum the large local arrays/aggregates even when scopes cannot overlap,
     * include all eight worker quality records, and allow another64KiB for
     * small local records, compiler copies/alignment and scratch buffers.
     * Full library/OS stack allocations are not probe sample/metadata buffers.
     * The separate1MiB reserve must exceed this conservative upper bound. */
    uint64_t named_stack = 2 * sizeof(struct json) + 2 * (PATH_MAX + 256) + 5 * PATH_MAX
        + BATCHES * sizeof(uint64_t) + 4 * sizeof(uint64_t) + sizeof(struct contention)
        + 8 * sizeof(struct quality) + 8 * sizeof(struct scan) + 16 * sizeof(uint64_t)
        + 1024 + 40 + 65 + 96 + 64 + CELLS * sizeof(unsigned) + UINT64_C(65536);
    demand(named_stack <= STACK_BUFFER_RESERVE, "stack-buffer-reserve");
    uint64_t maximum = sizeof metadata + STACK_BUFFER_RESERVE +
        (maximum_clock > maximum_trace ? maximum_clock : maximum_trace);
    demand(maximum <= BUFFER_LIMIT, "planned-buffer-cap");
    allocated = peak_allocated = sizeof metadata;
    struct json out = {0};
    add(&out, "{\"schema\":\"INV-01-07-clock-c1-preflight-v1\",\"planned_payload_bytes\":%" PRIu64
              ",\"maximum_owned_buffer_bytes\":%" PRIu64 ",\"buffer_limit_bytes\":%" PRIu64
              ",\"payload_limit_bytes\":%" PRIu64 ",\"probe_stack_buffer_reserve_bytes\":%" PRIu64
              ",\"named_stack_buffer_upper_bound_bytes\":%" PRIu64 "}\n",
        payload, maximum, BUFFER_LIMIT, PAYLOAD_LIMIT, STACK_BUFFER_RESERVE, named_stack);
    write_bytes("preflight.json", out.data, out.used);
    return payload;
}

int main(int argc, char **argv) {
    if (argc != 4) die("arguments", "argv", 0);
    errno = 0;
    char *end;
    seed = strtoull(argv[2], &end, 10);
    demand(!errno && *argv[2] && !*end && (seed == 70701 || seed == 70702 || seed == 70703), "seed");
    bool found = false;
    for (size_t i = 0; i < sizeof modes / sizeof modes[0]; ++i)
        if (strcmp(argv[3], modes[i]) == 0) { mode = modes[i]; found = true; break; }
    demand(found, "mode");
    authorize(argv[1]);
    open_output(argv[1]);
    uint64_t expected_payload = preflight();
    kern_return_t result = mach_timebase_info(&base);
    if (result != KERN_SUCCESS) die("timebase-api", "mach_timebase_info", result);
    demand(base.numer > 0 && base.denom > 0, "timebase-domain");
    unsigned order[CELLS];
    for (unsigned i = 0; i < CELLS; ++i) order[i] = i;
    uint32_t state = (uint32_t)seed;
    for (unsigned i = CELLS - 1; i > 0; --i) {
        state = state * UINT32_C(1664525) + UINT32_C(1013904223);
        unsigned j = state % (i + 1), temporary = order[i];
        order[i] = order[j]; order[j] = temporary;
    }
    for (unsigned i = 0; i < CELLS; ++i) {
        current_cell = (int)order[i];
        if (current_cell < 12) clock_cell(current_cell); else contention_cell(current_cell);
        char name[32];
        (void)snprintf(name, sizeof name, "cell-%02d.json", current_cell);
        write_bytes(name, metadata[current_cell].data, metadata[current_cell].used);
        demand(allocated == sizeof metadata, "cell-buffer-leak");
    }
    demand(strcmp(mode, "normal") == 0 || injected, "injection-not-reached");
    uint64_t omitted = strcmp(mode, "omit-clock") == 0 ? 16 :
                       strcmp(mode, "omit-contention") == 0 ? 24 : 0;
    demand(payload_written == expected_payload - omitted, "completed-payload");
    struct json out = {0};
    add(&out, "{\"schema\":\"INV-01-07-clock-c1-v1\",\"seed\":%" PRIu64
              ",\"mode\":\"%s\",\"timebase\":{\"status\":%d,\"numer\":%u,\"denom\":%u},"
              "\"clock_cells\":[", seed, mode, result, base.numer, base.denom);
    for (int i = 0; i < 12; ++i) add(&out, "%s%s", i ? "," : "", metadata[i].data);
    add(&out, "],\"contention_cells\":[");
    for (int i = 12; i < CELLS; ++i) add(&out, "%s%s", i == 12 ? "" : ",", metadata[i].data);
    add(&out, "]}\n"); write_bytes("record.json", out.data, out.used);
    out.used = 0;
    add(&out, "{\"schema\":\"INV-01-07-clock-c1-order-v1\",\"seed\":%" PRIu64 ",\"order\":[", seed);
    for (int i = 0; i < CELLS; ++i) add(&out, "%s%u", i ? "," : "", order[i]);
    add(&out, "]}\n"); write_bytes("execution.json", out.data, out.used);
    out.used = 0;
    add(&out, "{\"schema\":\"INV-01-07-clock-c1-resources-v1\",\"peak_heap_global_buffer_bytes\":%" PRIu64
              ",\"peak_owned_buffer_upper_bound_bytes\":%" PRIu64
              ",\"probe_stack_buffer_reserve_bytes\":%" PRIu64
              ",\"array_payload_bytes\":%" PRIu64 ",\"remaining_heap_global_buffer_bytes\":%" PRIu64 "}\n",
        peak_allocated, peak_allocated + STACK_BUFFER_RESERVE, STACK_BUFFER_RESERVE,
        payload_written, allocated);
    write_bytes("resources.json", out.data, out.used);
    if (fsync(directory_fd)) die("directory-sync", "fsync", errno);
    if (close(directory_fd)) die("directory-close", "close", errno);
    directory_fd = -1;
    return 0;
}
