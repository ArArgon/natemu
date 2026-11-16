// hvf_trap: trapped FP exceptions inside a Hypervisor.framework VM, taken at the guest's own EL1 vector (no host exit).
// EL1 code sets FPCR, erets to an EL0 loop of N `fmul d0, d1, d2`; each trapping fmul enters the EL1 lower-EL sync
// vector, which counts it, keeps the ESR, steps ELR past it and erets. EL0 ends with `svc`; EL1 reads CNTVCT and `hvc`s
// out. MMU off; only registers are used. Handler variants: 0 = bare (entry, ESR test, ELR+4, eret); 1 = also re-runs
// the op with traps off and FPSR isolated (mrs/msr fpsr, msr fpcr x2, fmul), as a real handler would.
// Build: clang -O2 -o hvf_trap hvf_trap.c -framework Hypervisor && codesign -s - --entitlements hvf.entitlements -f hvf_trap
// Run:   ./hvf_trap        (prints one line per case; never touches the host's files)
#include <Hypervisor/Hypervisor.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
extern char guest_start[], guest_entry[], guest_end[];
__asm__(".text\n.p2align 14\n.globl _guest_start\n_guest_start:\n"
  // vector table at +0 (VBAR): only the current-EL SPx sync (+0x200) and lower-EL A64 sync (+0x400) are used.
  ".p2align 11\nLvec:\n"
  ".org Lvec + 0x200\n b Lel1_unexpected\n"
  ".org Lvec + 0x400\n"
  " mrs x20, esr_el1\n lsr x21, x20, #26\n cmp x21, #0x2c\n b.ne Llower_other\n"
  " add x22, x22, #1\n mov x23, x20\n cbz x19, 1f\n"
  " mrs x27, fpsr\n msr fpsr, xzr\n msr fpcr, xzr\n fmul d0, d1, d2\n mrs x28, fpsr\n msr fpcr, x24\n orr x27, x27, x28\n msr fpsr, x27\n"
  "1: mrs x21, elr_el1\n add x21, x21, #4\n msr elr_el1, x21\n eret\n"
  ".org Lvec + 0x800\n"
  "Llower_other: cmp x21, #0x15\n b.ne Lel1_unexpected\n"           // svc from EL0: the loop is done
  " isb\n mrs x1, cntvct_el0\n sub x0, x1, x26\n mov x1, x22\n mov x2, x23\n fmov x3, d0\n mrs x4, cntfrq_el0\n hvc #0\n"
  "Lel1_unexpected: mrs x2, esr_el1\n mov x0, #-1\n mrs x1, elr_el1\n hvc #0\n"
  // entry at EL1h: x0 = FPCR for EL0, x1 = iterations, x2 = handler variant, x3 = d1 bits, x4 = d2 bits
  ".globl _guest_entry\n_guest_entry:\n"
  " mov x19, x2\n mov x24, x0\n mov x22, #0\n mov x23, #0\n mov x9, x1\n"
  " fmov d1, x3\n fmov d2, x4\n mov x5, #0x5678\n movk x5, #0x1234, lsl #16\n fmov d0, x5\n"
  " msr fpcr, x0\n msr spsr_el1, xzr\n adr x6, Lel0_loop\n msr elr_el1, x6\n isb\n mrs x26, cntvct_el0\n eret\n"
  "Lel0_loop: fmul d0, d1, d2\n subs x9, x9, #1\n b.ne Lel0_loop\n svc #0\n"
  ".globl _guest_end\n_guest_end:\n");
#define CK(x) do { hv_return_t r_ = (x); if (r_) { fprintf(stderr, "%s failed: %#x\n", #x, r_); exit(1); } } while (0)
static const uint64_t BASE = 0x40000000;
static void run(hv_vcpu_t cpu, hv_vcpu_exit_t* ex, const char* name, uint64_t fpcr, uint64_t n, uint64_t variant, uint64_t a, uint64_t b) {
  CK(hv_vcpu_set_reg(cpu, HV_REG_PC, BASE + (guest_entry - guest_start)));
  CK(hv_vcpu_set_reg(cpu, HV_REG_CPSR, 0x3c5));  // EL1h, DAIF masked
  uint64_t args[5] = {fpcr, n, variant, a, b};
  for (int i = 0; i < 5; ++i) CK(hv_vcpu_set_reg(cpu, HV_REG_X0 + i, args[i]));
  CK(hv_vcpu_run(cpu));
  uint64_t x[5]; for (int i = 0; i < 5; ++i) CK(hv_vcpu_get_reg(cpu, HV_REG_X0 + i, &x[i]));
  if (ex->reason != HV_EXIT_REASON_EXCEPTION || x[0] == (uint64_t)-1) {
    printf("%-34s unexpected exit: reason %u syndrome %#llx x0 %#llx x1(elr) %#llx x2(esr) %#llx\n", name, ex->reason,
           ex->exception.syndrome, x[0], x[1], x[2]); return; }
  double ns = (double)x[0] * 1e9 / (double)x[4] / (double)n;
  printf("%-34s %8.2f ns/iter  traps %llu  last ESR %#llx (EC %#llx ISS %#llx)  d0 after %#llx (%s)\n", name, ns, x[1], x[2],
         x[2] >> 26, x[2] & 0x1ffffff, x[3], x[3] == 0x12345678 ? "destination unchanged" : "written");
}
int main(void) {
  size_t len = guest_end - guest_start, size = 0x10000;
  void* mem = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
  memcpy(mem, guest_start, len);
  CK(hv_vm_create(NULL));
  CK(hv_vm_map(mem, BASE, size, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
  hv_vcpu_t cpu; hv_vcpu_exit_t* ex;
  CK(hv_vcpu_create(&cpu, &ex, NULL));
  CK(hv_vcpu_set_sys_reg(cpu, HV_SYS_REG_VBAR_EL1, BASE));
  CK(hv_vcpu_set_sys_reg(cpu, HV_SYS_REG_CPACR_EL1, 3ull << 20));  // FP/SIMD at EL0 and EL1
  CK(hv_vcpu_set_sys_reg(cpu, HV_SYS_REG_SCTLR_EL1, 0x30d00800));   // MMU and caches off (RES1 bits only)
  uint64_t one = 0x3ff0000000000000ull, half = 0x3fe0000000000000ull, mn = 0x0010000000000000ull, inf = 0x7ff0000000000000ull;
  const uint64_t UFE = 1u << 11, IOE = 1u << 8, N = 1000000;
  run(cpu, ex, "traps off, normal fmul", 0, 10 * N, 0, one, one);
  run(cpu, ex, "UFE on, normal fmul", UFE, 10 * N, 0, one, one);
  run(cpu, ex, "traps off, tiny fmul", 0, 10 * N, 0, mn, half);
  run(cpu, ex, "UFE, tiny fmul, bare handler", UFE, N, 0, mn, half);
  run(cpu, ex, "IOE, inf*0 fmul, bare handler", IOE, N, 0, inf, 0);
  run(cpu, ex, "UFE, tiny fmul, re-run handler", UFE, N, 1, mn, half);
  run(cpu, ex, "IOE, inf*0 fmul, re-run handler", IOE, N, 1, inf, 0);
  return 0;
}
