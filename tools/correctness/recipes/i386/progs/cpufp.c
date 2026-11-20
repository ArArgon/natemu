/* cpufp: a floating-point workload (a small n-body step, polynomial sums, casts to integers), built for i686 (x87)
   and, as cpufp.64, for x86-64 (SSE): timed like cpuint. The i686 build sets the x87 precision control to double and
   the program calls nothing of libm but sqrt, so every operation is the same IEEE double operation in both builds and
   under natemu, and the answers are equal to the last bit. */
#include <math.h>
#include <stdio.h>
#ifdef __i386__
#include <fpu_control.h>
#endif

int main(void)
{
    enum { B = 64 };
#ifdef __i386__
    fpu_control_t cw; _FPU_GETCW(cw); cw = (cw & ~_FPU_EXTENDED) | _FPU_DOUBLE; _FPU_SETCW(cw);
#endif
    double px[B], py[B], vx[B], vy[B], m[B], e = 0; long long casts = 0;
    for (int i = 0; i < B; i++) { px[i] = (i * 37 % 64 - 32) / 8.0; py[i] = (i * 53 % 64 - 32) / 8.0 + i / 128.0; vx[i] = vy[i] = 0; m[i] = 1 + i % 3; }
    for (int step = 0; step < 2500; step++) {
        for (int i = 0; i < B; i++) {
            double ax = 0, ay = 0;
            for (int j = 0; j < B; j++) {
                double dx = px[j] - px[i], dy = py[j] - py[i], d2 = dx * dx + dy * dy + 0.01;
                double inv = m[j] / (d2 * sqrt(d2)); ax += dx * inv; ay += dy * inv;
            }
            vx[i] += ax * 1e-4; vy[i] += ay * 1e-4;
        }
        for (int i = 0; i < B; i++) { px[i] += vx[i] * 1e-2; py[i] += vy[i] * 1e-2; casts += (int)(px[i] * 1000) + (int)(py[i] * 1000); }
    }
    for (int i = 0; i < B; i++) e += m[i] * (vx[i] * vx[i] + vy[i] * vy[i]);
    double poly = 0; for (int i = 1; i <= 2000000; i++) { double t = i * 1e-6; poly += ((t * 0.5 + 0.25) * t - 0.125) * t + 1.0 / i; }
    printf("cpufp %a %a %lld\n", e, poly, casts);
    return 0;
}
