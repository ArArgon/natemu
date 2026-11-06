// System-wide Apple PMU counts around one child command (needs root: kpc_force_all_ctrs_set).
//   kpcrun EV1,EV2,... -- CMD ARGS...   -> one JSON line {"events":{NAME:COUNT,...},"wall_s":T,"rc":RC,"child_stdout":"..."}
//   kpcrun --check EV1,EV2,...          -> resolve the events and the counter map only (no root needed)
// FIXED_CYCLES and FIXED_INSTRUCTIONS are always added. Counts are summed over all CPUs (the child is not pinned), all
// exception levels (kpep flag 0), so other processes' activity during the child adds to them: use differences between runs.
// API: the private kperf/kperfdata frameworks as used by public demos (e.g. ibireme's kpc_demo.c); declarations below.
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
typedef uint64_t kpc_config_t;
static int (*kpc_force_all_ctrs_set)(int), (*kpc_set_config)(uint32_t, kpc_config_t *), (*kpc_set_counting)(uint32_t);
static int (*kpc_get_cpu_counters)(int, uint32_t, int *, uint64_t *); static uint32_t (*kpc_get_counter_count)(uint32_t);
static int (*kpep_db_create)(const char *, void **), (*kpep_config_create)(void *, void **), (*kpep_config_force_counters)(void *);
static int (*kpep_db_event)(void *, const char *, void **), (*kpep_config_add_event)(void *, void **, uint32_t, uint32_t *);
static int (*kpep_config_kpc_classes)(void *, uint32_t *), (*kpep_config_kpc_count)(void *, size_t *);
static int (*kpep_config_kpc_map)(void *, size_t *, size_t), (*kpep_config_kpc)(void *, kpc_config_t *, size_t);
static void die(const char *s, int rc) { fprintf(stderr, "kpcrun: %s (rc %d)\n", s, rc); exit(3); }
static void *sym(void *h, const char *n) { void *p = dlsym(h, n); if (!p) die(n, -1); return p; }
#define LOAD(h, n) *(void **)&n = sym(h, #n)
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC_RAW, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
int main(int argc, char **argv) {
    int check = argc > 1 && !strcmp(argv[1], "--check"); int a = check ? 2 : 1;
    if (argc < a + 1 || (!check && (argc < 4 || strcmp(argv[2], "--")))) die("usage: kpcrun EVENTS -- CMD... | kpcrun --check EVENTS", 2);
    void *kp = dlopen("/System/Library/PrivateFrameworks/kperf.framework/kperf", RTLD_LAZY);
    void *kd = dlopen("/System/Library/PrivateFrameworks/kperfdata.framework/kperfdata", RTLD_LAZY);
    if (!kp || !kd) die("dlopen kperf/kperfdata", -1);
    LOAD(kp, kpc_force_all_ctrs_set); LOAD(kp, kpc_set_config); LOAD(kp, kpc_set_counting); LOAD(kp, kpc_get_cpu_counters);
    LOAD(kp, kpc_get_counter_count); LOAD(kd, kpep_db_create); LOAD(kd, kpep_config_create); LOAD(kd, kpep_config_force_counters);
    LOAD(kd, kpep_db_event); LOAD(kd, kpep_config_add_event); LOAD(kd, kpep_config_kpc_classes); LOAD(kd, kpep_config_kpc_count);
    LOAD(kd, kpep_config_kpc_map); LOAD(kd, kpep_config_kpc);
    void *db, *cfg; int rc;
    if ((rc = kpep_db_create(NULL, &db))) die("kpep_db_create", rc);
    if ((rc = kpep_config_create(db, &cfg))) die("kpep_config_create", rc);
    if ((rc = kpep_config_force_counters(cfg))) die("kpep_config_force_counters", rc);
    char list[1024]; snprintf(list, sizeof list, "FIXED_CYCLES,FIXED_INSTRUCTIONS,%s", argv[a]);
    const char *names[32]; int ne = 0;
    for (char *t = strtok(list, ","); t && ne < 32; t = strtok(NULL, ",")) {
        void *ev; if ((rc = kpep_db_event(db, t, &ev))) { fprintf(stderr, "kpcrun: unknown event %s\n", t); exit(3); }
        uint32_t err = 0; if ((rc = kpep_config_add_event(cfg, &ev, 0, &err))) { fprintf(stderr, "kpcrun: cannot add %s (rc %d err %u)\n", t, rc, err); exit(3); }
        names[ne++] = t;
    }
    uint32_t classes = 0; size_t nreg = 0, map[32] = {0}; kpc_config_t regs[32] = {0};
    if ((rc = kpep_config_kpc_classes(cfg, &classes)) || (rc = kpep_config_kpc_count(cfg, &nreg)) ||
        (rc = kpep_config_kpc_map(cfg, map, sizeof map)) || (rc = kpep_config_kpc(cfg, regs, sizeof regs))) die("kpep_config_kpc*", rc);
    if (check) {
        printf("{\"classes\":%u,\"config_regs\":%zu,\"map\":{", classes, nreg);
        for (int i = 0; i < ne; i++) printf("%s\"%s\":%zu", i ? "," : "", names[i], map[i]);
        printf("}}\n"); return 0;
    }
    if ((rc = kpc_force_all_ctrs_set(1))) die("kpc_force_all_ctrs_set (run as root)", rc);
    if ((rc = kpc_set_config(classes, regs))) die("kpc_set_config", rc);
    if ((rc = kpc_set_counting(classes))) die("kpc_set_counting", rc);
    uint32_t nc = kpc_get_counter_count(classes); long ncpu = sysconf(_SC_NPROCESSORS_CONF);
    uint64_t *b0 = calloc((size_t)ncpu * nc, 8), *b1 = calloc((size_t)ncpu * nc, 8); int cur;
    int pfd[2]; if (pipe(pfd)) die("pipe", -1);
    if ((rc = kpc_get_cpu_counters(1, classes, &cur, b0))) die("kpc_get_cpu_counters", rc);
    double t0 = now(); pid_t pid = fork();
    if (!pid) { dup2(pfd[1], 1); close(pfd[0]); execvp(argv[3], argv + 3); _exit(127); }
    close(pfd[1]); static char out[1 << 16]; size_t got = 0; ssize_t r;
    while ((r = read(pfd[0], out + got, sizeof out - 1 - got)) > 0) got += (size_t)r;
    int st; waitpid(pid, &st, 0); double t1 = now();
    if ((rc = kpc_get_cpu_counters(1, classes, &cur, b1))) die("kpc_get_cpu_counters", rc);
    kpc_set_counting(0); kpc_force_all_ctrs_set(0);
    out[got] = 0; for (char *p = out; *p; p++) if (*p == '"' || *p == '\\' || *p == '\n') *p = ' ';
    printf("{\"events\":{");
    for (int i = 0; i < ne; i++) {
        uint64_t s = 0; for (long c = 0; c < ncpu; c++) s += b1[c * nc + map[i]] - b0[c * nc + map[i]];
        printf("%s\"%s\":%llu", i ? "," : "", names[i], (unsigned long long)s);
    }
    printf("},\"wall_s\":%.6f,\"rc\":%d,\"child_stdout\":\"%s\"}\n", t1 - t0, WIFEXITED(st) ? WEXITSTATUS(st) : -1, out);
    return 0;
}
