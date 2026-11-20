/* x87: the floating point of ordinary i386 code. Casts to integers (FNSTCW, FLDCW, FISTP, FLDCW), 64-bit FILD and
   FISTP, the four rounding modes, comparisons, the register stack, constants, exception flags, and libm. The exact
   sections run with the x87 precision control set to double (what natemu's x87 computes in), so their results are
   bit-for-bit the same on hardware; libm and extended-precision sums are printed to ten digits. */
#define _GNU_SOURCE
#include <fenv.h>
#include <fpu_control.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>

static volatile double in[] = { 0.0, 0.4, 0.5, 0.6, 1.5, 2.5, -0.5, -1.5, -2.5, 1e9 + 0.75, -1e9 - 0.75, 2147483646.5, -2147483647.5, 123456.789 };
#define NIN (sizeof in / sizeof in[0])
static volatile long long ll[] = { 0, 1, -1, 123456789012345ll, -987654321098765ll, (1ll << 53) - 1, -(1ll << 53), 1ll << 62, -(1ll << 62) };
#define NLL (sizeof ll / sizeof ll[0])
static const int modes[4] = { FE_TONEAREST, FE_DOWNWARD, FE_UPWARD, FE_TOWARDZERO };

int main(void)
{
    puts("casts");
    for (unsigned i = 0; i < NIN; i++) {
        double v = in[i];
        printf("%.17g: int %d short %d ll %lld float %.9g\n", v, (int)v, (short)(int)(v / 65536), (long long)(v * 1024), (double)(float)v);
    }
    puts("64-bit integers");
    for (unsigned i = 0; i < NLL; i++) {
        volatile double d = ll[i]; volatile long long back = d; volatile double u = (unsigned long long)ll[i];
        printf("%lld -> %a -> %lld, unsigned %a\n", ll[i], d, back, u);
    }
    puts("rounding modes");
    for (int m = 0; m < 4; m++) {
        fesetround(modes[m]);
        long s = 0; long long t = 0; double r = 0;
        for (unsigned i = 0; i < NIN - 2; i++) { volatile double v = in[i]; s += lrint(v); t += llrint(v * 3); r += rint(v) + nearbyint(v / 2); }
        volatile double third = 1.0, three = 3.0; third /= three;
        printf("mode %d: lrint %ld llrint %lld rint %.17g cast %d 1/3 %a\n", m, s, t, r, (int)in[5], third);
    }
    fesetround(FE_TONEAREST);

    puts("double precision, exact");
    fpu_control_t cw, saved; _FPU_GETCW(saved); cw = (saved & ~_FPU_EXTENDED) | _FPU_DOUBLE; _FPU_SETCW(cw);
    volatile double s = 0, p = 1, x = 0.1;
    for (int i = 1; i <= 100000; i++) { s += 1.0 / i; p = p * 1.00001 - x / i; x = x * 0.999 + 0.001; }
    printf("sum %a product %a x %a\n", s, p, x);
    volatile double a = 2.0, b = 10.0, c = 7.25, z = 0.0, big = 1e300, tiny = 1e-300;
    printf("sqrt %a %a fmod %a %a remainder %a ldexp %a fabs %a\n", sqrt(a), sqrt(b * 1e10), fmod(b, c), fmod(-c, a), remainder(b, c), ldexp(c, -3), fabs(-c));
    printf("trunc %a floor %a ceil %a round %a\n", trunc(-c), floor(-c), ceil(c), round(2.5));
    printf("inf %a %a nan %d overflow %a underflow %a\n", a / z, -a / z, isnan(z / z), big * big, tiny * tiny);

    puts("comparisons");
    volatile double nan = NAN, one = 1.0, two = 2.0;
    printf("%d%d%d%d%d%d %d%d%d%d %d%d\n", one < two, one > two, one == one, one != two, one <= one, two >= one,
           nan < one, nan == nan, nan != nan, isunordered(nan, one), isless(one, nan), isgreater(two, one));
    printf("classify %d %d %d %d signbit %d %d\n", fpclassify(nan) == FP_NAN, fpclassify(one / 0.0 * one) == FP_INFINITE,
           fpclassify(one - one) == FP_ZERO, fpclassify(two) == FP_NORMAL, signbit(-one) != 0, signbit(one) != 0);

    puts("register stack and constants");
    double st[8], k[7];
    __asm__("fld1\n fld1\n faddp\n fld1\n fadd %%st(1)\n fld %%st(1)\n fmul %%st(1)\n fld %%st\n fsqrt\n fld %%st(3)\n fchs\n fld %%st(1)\n fxch %%st(3)\n fldz\n fld1\n"
            "fstpl 56(%0)\n fstpl 48(%0)\n fstpl 40(%0)\n fstpl 32(%0)\n fstpl 24(%0)\n fstpl 16(%0)\n fstpl 8(%0)\n fstpl (%0)" : : "r"(st) : "memory");
    for (int i = 0; i < 8; i++) printf("%a ", st[i]);
    __asm__("fldpi\n fstpl (%0)\n fldl2e\n fstpl 8(%0)\n fldl2t\n fstpl 16(%0)\n fldlg2\n fstpl 24(%0)\n fldln2\n fstpl 32(%0)\n"
            "filds 2(%1)\n fimull 4(%1)\n fiadds (%1)\n fstpl 40(%0)\n fildl 4(%1)\n fidivs 2(%1)\n fisubl 4(%1)\n fstpl 48(%0)"
            : : "r"(k), "r"((short[]){ 7, -3, 1000, 0 }) : "memory");
    printf("\n%.15g %.15g %.15g %.15g %.15g %a %a\n", k[0], k[1], k[2], k[3], k[4], k[5], k[6]);
    _FPU_SETCW(saved);

    puts("exception flags");
    feclearexcept(FE_ALL_EXCEPT); volatile double q = one / z; printf("divbyzero %d", fetestexcept(FE_DIVBYZERO) != 0);
    feclearexcept(FE_ALL_EXCEPT); q = z / z; printf(" invalid %d", fetestexcept(FE_INVALID) != 0);
    feclearexcept(FE_ALL_EXCEPT); q = one / 3.0; printf(" inexact %d", fetestexcept(FE_INEXACT) != 0);
    feclearexcept(FE_ALL_EXCEPT); q = one + two; printf(" exact %d", fetestexcept(FE_ALL_EXCEPT) == 0);
    feclearexcept(FE_ALL_EXCEPT); q = big * big; printf(" overflow %d\n", fetestexcept(FE_OVERFLOW) != 0); (void)q;

    puts("libm and extended sums, ten digits");
    double acc = 0;
    for (int i = 1; i <= 2000; i++) {
        double t = i * 0.37;
        acc += sin(t) * cos(t / 3) + atan2(t, 5.0) + log(t) / exp(t / 800) + pow(t, 0.33) + tan(t / 1000) + fmod(t, 1.7) + cbrt(t) + hypot(t, 3.0) + log1p(1 / t) + expm1(1 / t) + asin(1 / (t + 1)) + sinh(t / 400);
    }
    printf("%.10g\n", acc);
    printf("%.10g %.10g %.10g %.10g %.10g %.10g\n", sin(1.0), cos(1.0), exp(1.0), log(10.0), pow(2.0, 0.5), atan(1.0) * 4);
    printf("%.10g %.10g %.10g %Lg %Lg\n", exp2(10.5), log2(1000.0), tgamma(5.5), 1.5L * 4, sqrtl(16.0L));
    float f = 0; for (int i = 1; i <= 1000; i++) f += 1.0f / i;
    printf("%.6g %ld %ld\n", f, lround(2.5), lround(-2.5));
    return 0;
}
