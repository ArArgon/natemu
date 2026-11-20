/* x87ext: what natemu's x87 does not do. Each line is right on hardware and expected to differ under natemu, whose
   x87 registers hold doubles (docs/floating-point.md): 64-bit significands, the long double range. The case is listed
   as an expected failure; a line that starts matching is news. */
#include <float.h>
#include <math.h>
#include <stdio.h>

int main(void)
{
    volatile long double one = 1.0L, eps = LDBL_EPSILON, max = LDBL_MAX, third = 3.0L;
    printf("1 + epsilon > 1: %d\n", one + eps > one);
    printf("LDBL_MAX is finite: %d\n", isfinite(max) != 0);
    printf("1/3 %La\n", one / third);
    volatile long long v = (1ll << 62) + 1; volatile long double w = v; volatile long long back = w;
    printf("2^62+1 through long double: %lld\n", back);
    unsigned char pi[10]; __asm__("fldpi\n fstpt (%0)" : : "r"(pi) : "memory");
    printf("pi "); for (int i = 9; i >= 0; i--) printf("%02x", pi[i]);
    printf("\nsqrtl(2) %.19Lg\n", sqrtl(2.0L));
    return 0;
}
