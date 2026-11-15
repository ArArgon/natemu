// Runs straight-line AArch64 words natively (macOS arm64), for tests of fexc's emitted code: the semantic check of the AFP fallback's
// software tests (tools/fexc/test_slice1.py, AfpDenormal), whose generated code only needs registers and memory.
//   exec_words [FPCR] < in > out
// FPCR (hex, default: the process's) is set before the words run: 0x6 is the M4 as natemu runs it (AH, NEP), 0 an M2's.
// in (little-endian): u32 nwords, the words, u32 nptr, nptr x (u32 reg, u32 offset), x[32] u64, v[32] 16 bytes, buf[8192] bytes.
// The words run with x28 = buf (FEX's state register), x<reg> = buf + offset for each pointer pair, the other registers as given, and
// end by a jump back (FEX's code uses x30 as an ordinary temporary, so no `ret`; x16 is the jump's scratch). out: x[32] u64, v[32]
// 16 bytes, buf[8192].
#include <libkern/OSCacheControl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

typedef struct { uint64_t x[32]; uint8_t v[32][16]; } Regs;
void exec_block(Regs* r, void* code);
extern char exec_resume[];
__asm__(
  ".text\n.globl _exec_block\n.p2align 2\n_exec_block:\n"
  "  sub sp, sp, #176\n"
  "  stp x19, x20, [sp, #0]\n  stp x21, x22, [sp, #16]\n  stp x23, x24, [sp, #32]\n  stp x25, x26, [sp, #48]\n  stp x27, x28, [sp, #64]\n"
  "  stp x29, x30, [sp, #80]\n  stp d8, d9, [sp, #96]\n  stp d10, d11, [sp, #112]\n  stp d12, d13, [sp, #128]\n  stp d14, d15, [sp, #144]\n"
  "  str x0, [sp, #160]\n  mov x9, x1\n  add x10, x0, #256\n"
  "  ldp q0, q1, [x10, #0]\n  ldp q2, q3, [x10, #32]\n  ldp q4, q5, [x10, #64]\n  ldp q6, q7, [x10, #96]\n  ldp q8, q9, [x10, #128]\n"
  "  ldp q10, q11, [x10, #160]\n  ldp q12, q13, [x10, #192]\n  ldp q14, q15, [x10, #224]\n  ldp q16, q17, [x10, #256]\n  ldp q18, q19, [x10, #288]\n"
  "  ldp q20, q21, [x10, #320]\n  ldp q22, q23, [x10, #352]\n  ldp q24, q25, [x10, #384]\n  ldp q26, q27, [x10, #416]\n  ldp q28, q29, [x10, #448]\n"
  "  ldp q30, q31, [x10, #480]\n"
  "  ldp x2, x3, [x0, #16]\n  ldp x4, x5, [x0, #32]\n  ldp x6, x7, [x0, #48]\n  ldr x8, [x0, #64]\n  ldp x10, x11, [x0, #80]\n"
  "  ldp x12, x13, [x0, #96]\n  ldp x14, x15, [x0, #112]\n  ldp x16, x17, [x0, #128]\n  ldp x18, x19, [x0, #144]\n  ldp x20, x21, [x0, #160]\n"
  "  ldp x22, x23, [x0, #176]\n  ldp x24, x25, [x0, #192]\n  ldp x26, x27, [x0, #208]\n  ldp x28, x29, [x0, #224]\n"
  "  ldr x1, [x0, #8]\n  ldr x0, [x0, #0]\n  br x9\n"
  ".globl _exec_resume\n_exec_resume:\n"
  "  str x0, [sp, #168]\n  ldr x0, [sp, #160]\n"   // (x0 was the block's; keep it in the frame until the regs pointer is back)
  "  stp x1, x2, [x0, #8]\n  stp x3, x4, [x0, #24]\n  stp x5, x6, [x0, #40]\n  stp x7, x8, [x0, #56]\n  stp x9, x10, [x0, #72]\n"
  "  stp x11, x12, [x0, #88]\n  stp x13, x14, [x0, #104]\n  stp x15, x16, [x0, #120]\n  stp x17, x18, [x0, #136]\n  stp x19, x20, [x0, #152]\n"
  "  stp x21, x22, [x0, #168]\n  stp x23, x24, [x0, #184]\n  stp x25, x26, [x0, #200]\n  stp x27, x28, [x0, #216]\n  str x29, [x0, #232]\n"
  "  ldr x1, [sp, #168]\n  str x1, [x0, #0]\n  add x10, x0, #256\n"
  "  stp q0, q1, [x10, #0]\n  stp q2, q3, [x10, #32]\n  stp q4, q5, [x10, #64]\n  stp q6, q7, [x10, #96]\n  stp q8, q9, [x10, #128]\n"
  "  stp q10, q11, [x10, #160]\n  stp q12, q13, [x10, #192]\n  stp q14, q15, [x10, #224]\n  stp q16, q17, [x10, #256]\n  stp q18, q19, [x10, #288]\n"
  "  stp q20, q21, [x10, #320]\n  stp q22, q23, [x10, #352]\n  stp q24, q25, [x10, #384]\n  stp q26, q27, [x10, #416]\n  stp q28, q29, [x10, #448]\n"
  "  stp q30, q31, [x10, #480]\n"
  "  ldp x19, x20, [sp, #0]\n  ldp x21, x22, [sp, #16]\n  ldp x23, x24, [sp, #32]\n  ldp x25, x26, [sp, #48]\n  ldp x27, x28, [sp, #64]\n"
  "  ldp x29, x30, [sp, #80]\n  ldp d8, d9, [sp, #96]\n  ldp d10, d11, [sp, #112]\n  ldp d12, d13, [sp, #128]\n  ldp d14, d15, [sp, #144]\n"
  "  add sp, sp, #176\n  ret\n");

#define BUF 8192
static void need(size_t n, size_t got) { if (n != got) { fprintf(stderr, "exec_words: short input\n"); exit(2); } }

int main(int argc, char** argv) {
  uint32_t n;
  need(1, fread(&n, 4, 1, stdin));
  uint32_t* words = calloc(n + 4, 4);
  need(n, fread(words, 4, n, stdin));
  words[n] = 0x58000050;   // ldr x16, #8
  words[n + 1] = 0xd61f0200;   // br x16
  *(uint64_t*)&words[n + 2] = (uint64_t)exec_resume;
  uint32_t np;
  need(1, fread(&np, 4, 1, stdin));
  uint32_t ptr[64][2];
  if (np > 64) return 2;
  need(np, fread(ptr, 8, np, stdin));
  Regs r;
  need(1, fread(&r, sizeof r, 1, stdin));
  uint8_t* buf = mmap(NULL, BUF, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
  need(BUF, fread(buf, 1, BUF, stdin));
  for (uint32_t i = 0; i < np; ++i) r.x[ptr[i][0]] = (uint64_t)(buf + ptr[i][1]);
  r.x[28] = (uint64_t)buf;
  void* code = mmap(NULL, 16384, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANON | MAP_JIT, -1, 0);
  if (code == MAP_FAILED) { perror("mmap"); return 1; }
  pthread_jit_write_protect_np(0);
  memcpy(code, words, 4 * (n + 4));
  pthread_jit_write_protect_np(1);
  sys_icache_invalidate(code, 4 * (n + 4));
  if (argc > 1) { uint64_t fpcr = strtoull(argv[1], NULL, 16); __asm__ volatile("msr fpcr, %0" ::"r"(fpcr)); }
  exec_block(&r, code);
  fwrite(&r, sizeof r, 1, stdout);
  fwrite(buf, 1, BUF, stdout);
  return 0;
}
