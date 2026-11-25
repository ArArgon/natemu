/* kms_pattern: the display cases' guest program (tests/devices/run_display.py). Raw DRM ioctls on /dev/dri/card0
 * with the kernel's UAPI headers only (no libdrm), built static for x86-64 by testkit/inputs.py's kms_pattern().
 *
 *   kms_pattern            draw the pattern and scan it out, print READY, then hold the mode until killed
 *   kms_pattern bench N    a BENCH_W x BENCH_H buffer and mode (the EDID's preferred), then copy full frames into
 *                          the mapped buffer until N bytes went, and the same into a buffer in guest RAM; print both
 *                          times (BENCH ...), then READY, and hold the mode (the pattern) as above
 *
 * The buffer (pattern mode): a dumb buffer BUF_W x BUF_H (32 bpp, pitch BUF_W x 4 = 3200 bytes), mapped through the card
 * (DRM_IOCTL_MODE_MAP_DUMB: the buffer's own mapping, the BAR the guest writes natively, not fbdev's shadow); a
 * framebuffer of WIDTH x HEIGHT on it (XRGB8888) with pitches[0] the buffer's pitch and offsets[0] Y0 rows; the
 * CRTC set to the connector's WIDTH x HEIGHT mode. Every pixel the framebuffer shows is pattern(x, y) (x, y
 * counted in the framebuffer); every other pixel of the buffer is FILL, so a reader that ignores the offset or the
 * pitch sees FILL or a shifted pattern. The runner (run_display.py) computes the same function.
 */
#include <drm/drm.h>
#include <drm/drm_fourcc.h>
#include <drm/drm_mode.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define WIDTH 640
#define HEIGHT 480
#define BUF_W 800
#define Y0 16                       /* offsets[0] = Y0 x pitch */
#define BUF_H (HEIGHT + Y0)         /* the framebuffer's rows from row Y0 must lie inside the buffer */
#define FILL 0x00ff00ffu            /* magenta: outside the framebuffer */
#define BENCH_W 1024                /* bench: the framebuffer is the whole buffer */
#define BENCH_H 768

/* The pattern: each channel a function of x and y, R and B different almost everywhere (so a swap shows). */
static uint32_t pattern(uint32_t x, uint32_t y) {
    uint32_t r = (x * 3 + y) & 0xff, g = (y * 5 + (x >> 4)) & 0xff, b = (x ^ (y * 7)) & 0xff;
    return r << 16 | g << 8 | b;
}

static void die(const char *what) {
    printf("KMS-FAIL %s: %s\n", what, strerror(errno));
    fflush(stdout);
    exit(1);
}

static int ioc(int fd, unsigned long request, void *arg) {
    int r;
    do r = ioctl(fd, request, arg);
    while (r < 0 && (errno == EINTR || errno == EAGAIN));
    return r;
}

static uint64_t now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000u + (uint64_t)t.tv_nsec;
}

/* Copies a w x h frame from `src` into the framebuffer's place in `dst` (rows `pitch` bytes apart from byte
 * `offset`), frames until `total` bytes went; the nanoseconds it took. */
static uint64_t copy_frames(uint8_t *dst, uint32_t pitch, uint32_t offset, const uint32_t *src, uint32_t w, uint32_t h,
                            uint64_t total) {
    uint64_t start = now_ns(), done = 0;
    while (done < total) {
        for (uint32_t y = 0; y < h; y++) memcpy(dst + offset + y * pitch, src + y * w, w * 4);
        done += (uint64_t)w * h * 4;
    }
    return now_ns() - start;
}

int main(int argc, char **argv) {
    uint64_t bench = 0;
    if (argc == 3 && strcmp(argv[1], "bench") == 0) bench = strtoull(argv[2], NULL, 0);
    if (argc != 1 && !bench) { fprintf(stderr, "usage: kms_pattern [bench BYTES]\n"); return 2; }
    setvbuf(stdout, NULL, _IONBF, 0);
    /* The geometry: the framebuffer w x h, Y0 rows into a buffer bw x bh. */
    const uint32_t w = bench ? BENCH_W : WIDTH, h = bench ? BENCH_H : HEIGHT;
    const uint32_t bw = bench ? BENCH_W : BUF_W, bh = bench ? BENCH_H : BUF_H, y0 = bench ? 0 : Y0;
    int fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (fd < 0) die("open /dev/dri/card0");
    if (ioc(fd, DRM_IOCTL_SET_MASTER, NULL) < 0) die("DRM_IOCTL_SET_MASTER");

    /* The first connected connector, its first encoder's CRTC (or the first CRTC). */
    uint32_t crtcs[8], connectors[8];
    struct drm_mode_card_res res = {0};
    if (ioc(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) < 0) die("GETRESOURCES");
    if (!res.count_crtcs || !res.count_connectors || res.count_crtcs > 8 || res.count_connectors > 8) {
        errno = EINVAL; die("resources");
    }
    res.crtc_id_ptr = (uintptr_t)crtcs;
    res.connector_id_ptr = (uintptr_t)connectors;
    res.count_fbs = res.count_encoders = 0;
    if (ioc(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) < 0) die("GETRESOURCES (ids)");
    struct drm_mode_modeinfo modes[256], mode = {0};
    struct drm_mode_get_connector conn;
    int found = 0;
    for (uint32_t i = 0; i < res.count_connectors && !found; i++) {
        memset(&conn, 0, sizeof conn);
        conn.connector_id = connectors[i];
        if (ioc(fd, DRM_IOCTL_MODE_GETCONNECTOR, &conn) < 0) die("GETCONNECTOR");
        if (conn.connection != 1 || conn.count_modes == 0) continue;     /* DRM_MODE_CONNECTED */
        /* the kernel copies the modes only when they all fit, and then reports how many there are */
        if (conn.count_modes > 256) { errno = E2BIG; die("modes"); }
        uint32_t room = conn.count_modes;
        conn.modes_ptr = (uintptr_t)modes;
        conn.count_props = conn.count_encoders = 0;
        if (ioc(fd, DRM_IOCTL_MODE_GETCONNECTOR, &conn) < 0) die("GETCONNECTOR (modes)");
        if (conn.count_modes > room) { errno = EAGAIN; die("modes changed"); }
        for (uint32_t m = 0; m < conn.count_modes; m++)
            if (modes[m].hdisplay == w && modes[m].vdisplay == h) { mode = modes[m]; found = 1; break; }
    }
    if (!found) { errno = ENOENT; die("a connected connector with the mode"); }

    /* The dumb buffer, its mapping and the pattern. */
    struct drm_mode_create_dumb create = {.width = bw, .height = bh, .bpp = 32};
    if (ioc(fd, DRM_IOCTL_MODE_CREATE_DUMB, &create) < 0) die("CREATE_DUMB");
    if (create.pitch != bw * 4) { printf("KMS-FAIL pitch %u, expected %u\n", create.pitch, bw * 4); return 1; }
    struct drm_mode_map_dumb map = {.handle = create.handle};
    if (ioc(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) < 0) die("MAP_DUMB");
    uint8_t *buf = mmap(NULL, create.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, map.offset);
    if (buf == MAP_FAILED) die("mmap");
    for (uint32_t y = 0; y < bh; y++) {
        uint32_t *row = (uint32_t *)(buf + y * create.pitch);
        for (uint32_t x = 0; x < bw; x++) row[x] = (y >= y0 && x < w) ? pattern(x, y - y0) : FILL;
    }

    /* The framebuffer at Y0 rows into the buffer, and the mode. */
    struct drm_mode_fb_cmd2 fb = {.width = w, .height = h, .pixel_format = DRM_FORMAT_XRGB8888};
    fb.handles[0] = create.handle;
    fb.pitches[0] = create.pitch;
    fb.offsets[0] = y0 * create.pitch;
    if (ioc(fd, DRM_IOCTL_MODE_ADDFB2, &fb) < 0) die("ADDFB2");
    struct drm_mode_crtc crtc = {.crtc_id = crtcs[0], .fb_id = fb.fb_id, .set_connectors_ptr = (uintptr_t)&conn.connector_id,
                                 .count_connectors = 1, .mode = mode, .mode_valid = 1};
    if (ioc(fd, DRM_IOCTL_MODE_SETCRTC, &crtc) < 0) die("SETCRTC");
    printf("KMS-MODE connector=%u crtc=%u fb=%u buffer=%ux%u pitch=%u size=%llu offset0=%u mode=%s\n", conn.connector_id,
           crtcs[0], fb.fb_id, bw, bh, create.pitch, (unsigned long long)create.size, fb.offsets[0], mode.name);

    if (bench) {
        uint32_t *src = malloc((size_t)w * h * 4);
        uint8_t *ram = malloc(create.size);
        if (!src || !ram) die("malloc");
        for (uint32_t y = 0; y < h; y++)
            for (uint32_t x = 0; x < w; x++) src[y * w + x] = pattern(x, y);
        memset(ram, 0, create.size);
        uint64_t vram_ns = copy_frames(buf, create.pitch, fb.offsets[0], src, w, h, bench);
        uint64_t ram_ns = copy_frames(ram, create.pitch, fb.offsets[0], src, w, h, bench);
        printf("BENCH bytes=%llu frame=%ux%u vram_ns=%llu ram_ns=%llu\n", (unsigned long long)bench, w, h,
               (unsigned long long)vram_ns, (unsigned long long)ram_ns);
        /* the framebuffer now holds the last frame copied, the pattern: held below for the runner's snapshots */
    }
    printf("READY\n");
    for (;;) pause();          /* hold the mode: fbdev restores its own when the program exits */
}
