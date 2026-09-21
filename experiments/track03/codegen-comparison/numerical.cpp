// Standalone numerical kernels, not an x87 frontend or captive runtime.
#include <bit>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <cfenv>
#include "cephes_128bit.h"

static softfloat_state state {softfloat_tininess_afterRounding, softfloat_round_near_even, 0, 80};
static volatile uint64_t sink;
static uint64_t ns() { timespec t; clock_gettime(CLOCK_MONOTONIC_RAW, &t); return uint64_t(t.tv_sec)*1000000000+t.tv_nsec; }
static extFloat80_t extend(double x) { return f64_to_extF80(&state, std::bit_cast<float64_t>(x)); }
// These calls are the actual retained FEX FADD/FSUB implementation kernels.
__attribute__((noinline)) static extFloat80_t cancel80(extFloat80_t x, extFloat80_t y) {
  return extF80_sub(&state, extF80_add(&state, x, y), x);
}
// Native binary64 arithmetic control: not labelled emitted x87 instructions.
__attribute__((noinline)) static double cancel64(double x, double y) { return (x+y)-x; }
__attribute__((noinline)) static double sin64(double x) { return sin(x); }
// Finite-input path from retained X80SoftFloat::FSIN, with the same infinity check.
__attribute__((noinline)) static extFloat80_t sin80(extFloat80_t x) {
  if ((x.signExp & 0x7fff) == 0x7fff && x.signif == 0x8000000000000000ULL) {
    state.exceptionFlags |= softfloat_flag_invalid;
    return {0xc000000000000000ULL, 0x7fff};
  }
  return f128_to_extF80(&state, FEXCore::cephes_128bit::sinl(extF80_to_f128(&state,x)));
}
static void result(const char* operation, int id, double x, double y, double a, extFloat80_t b, extFloat80_t input) {
  printf("{\"kind\":\"accuracy\",\"operation\":\"%s\",\"id\":%d,\"x\":\"%a\",\"y\":\"%a\",\"input80_signexp\":%u,\"input80_signif\":\"%016llx\",\"binary64\":\"%016llx\",\"ext80_signexp\":%u,\"ext80_signif\":\"%016llx\",\"soft_flags\":%u}\n",
    operation,id,x,y,input.signExp,(unsigned long long)input.signif,(unsigned long long)std::bit_cast<uint64_t>(a),b.signExp,(unsigned long long)b.signif,state.exceptionFlags);
}
int main() {
  if (fesetround(FE_TONEAREST)) return 2;
  uint64_t fpcr; asm volatile("mrs %0, fpcr" : "=r"(fpcr));
  // Nearest, no flush-to-zero; record actual Linux FP control state.
  if (fpcr & ((3ULL<<22)|(1ULL<<24)|(1ULL<<19))) return 3;
  printf("{\"kind\":\"control\",\"fpcr\":%llu,\"soft_precision\":80,\"soft_rounding\":0}\n",(unsigned long long)fpcr);
  double xs[12], ys[12]; extFloat80_t xe[12], ye[12];
  for (int i=0;i<12;i++) { xs[i]=ldexp(1.0,52+i); ys[i]=1.0; xe[i]=extend(xs[i]); ye[i]=extend(ys[i]); state.exceptionFlags=0; result("cancel",i,xs[i],ys[i],cancel64(xs[i],ys[i]),cancel80(xe[i],ye[i]),xe[i]); }
  double ss[14] = {-1.0,-0.75,-0.5,-0.125,0.0,0.125,0.5,0.75,1.0,2.0,4.0,8.0,0x1p-60,1.0}; extFloat80_t se[14];
  for (int i=0;i<14;i++) { se[i]=extend(ss[i]); if (i==13) se[i].signif+=8; state.exceptionFlags=0; result("sin",i,ss[i],0,sin64(ss[i]),sin80(se[i]),se[i]); }
  // Alternating order and two lengths expose order and loop-size sensitivity.
  // Indexed, runtime-populated operands and retained output checksum prevent folding.
  for (int length=0;length<2;length++) for (int batch=0;batch<7;batch++) for (int j=0;j<4;j++) {
    int variant = (batch&1) ? 3-j : j;
    int n = (variant<2 ? 100000 : 10000)*(length?4:1);
    uint64_t checksum=0, t=ns();
    for (int i=0;i<n;i++) {
      int k=i%(variant<2 ? 12 : 14);
      if (variant==0) checksum += std::bit_cast<uint64_t>(cancel64(xs[k],ys[k]));
      else if (variant==1) { auto z=cancel80(xe[k],ye[k]); checksum += z.signif ^ z.signExp; }
      else if (variant==2) checksum += std::bit_cast<uint64_t>(sin64(ss[k]));
      else { auto z=sin80(se[k]); checksum += z.signif ^ z.signExp; }
    }
    uint64_t elapsed=ns()-t; sink=checksum;
    printf("{\"kind\":\"timing\",\"variant\":%d,\"length\":%d,\"batch\":%d,\"n\":%d,\"ns\":%llu,\"checksum\":\"%016llx\"}\n",variant,length,batch,n,(unsigned long long)elapsed,(unsigned long long)checksum);
  }
}
