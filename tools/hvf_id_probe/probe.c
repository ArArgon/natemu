#include <Hypervisor/Hypervisor.h>
#include <stdio.h>
#include <string.h>
#define F(r,name,lo) {r,#name,lo}
struct reg { hv_feature_reg_t fr; hv_sys_reg_t sr; const char* n; } regs[] = {
 {HV_FEATURE_REG_ID_AA64ISAR0_EL1, HV_SYS_REG_ID_AA64ISAR0_EL1, "ISAR0"},
 {HV_FEATURE_REG_ID_AA64ISAR1_EL1, HV_SYS_REG_ID_AA64ISAR1_EL1, "ISAR1"},
 {HV_FEATURE_REG_ID_AA64MMFR0_EL1, HV_SYS_REG_ID_AA64MMFR0_EL1, "MMFR0"},
 {HV_FEATURE_REG_ID_AA64MMFR1_EL1, HV_SYS_REG_ID_AA64MMFR1_EL1, "MMFR1"},
 {HV_FEATURE_REG_ID_AA64MMFR2_EL1, HV_SYS_REG_ID_AA64MMFR2_EL1, "MMFR2"},
 {HV_FEATURE_REG_ID_AA64PFR0_EL1, HV_SYS_REG_ID_AA64PFR0_EL1, "PFR0"},
 {HV_FEATURE_REG_ID_AA64PFR1_EL1, HV_SYS_REG_ID_AA64PFR1_EL1, "PFR1"},
 {HV_FEATURE_REG_ID_AA64DFR0_EL1, HV_SYS_REG_ID_AA64DFR0_EL1, "DFR0"},
};
int main(void){
  hv_vcpu_config_t cfg = hv_vcpu_config_create();
  uint64_t fv[8], sv[8];
  for (int i=0;i<8;i++){ if (hv_vcpu_config_get_feature_reg(cfg, regs[i].fr, &fv[i])) fv[i]=~0ull; }
  if (hv_vm_create(NULL)) { puts("vm_create failed"); return 1; }
  hv_vcpu_t v; hv_vcpu_exit_t* e;
  if (hv_vcpu_create(&v,&e,NULL)) { puts("vcpu_create failed"); return 1; }
  for (int i=0;i<8;i++){ if (hv_vcpu_get_sys_reg(v, regs[i].sr, &sv[i])) sv[i]=~0ull; }
  for (int i=0;i<8;i++) printf("%s config=%016llx vcpu=%016llx\n", regs[i].n, fv[i], sv[i]);
  return 0;
}
