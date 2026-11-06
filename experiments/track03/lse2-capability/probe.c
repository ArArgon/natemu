#include <Hypervisor/Hypervisor.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <os/object.h>

static void row(const char *op, hv_return_t rc, uint64_t value) {
    printf("{\"op\":\"%s\",\"rc\":%u,\"value\":\"0x%016" PRIx64 "\"}\n",
           op, (unsigned)rc, rc == HV_SUCCESS ? value : UINT64_C(0));
    fflush(stdout);
}

int main(void) {
    hv_vcpu_config_t config = hv_vcpu_config_create();
    printf("{\"op\":\"config_create\",\"present\":%s}\n", config ? "true" : "false");
    fflush(stdout);
    if (!config) return 1;
    uint64_t feature = 0, current = 0;
    hv_return_t frc = hv_vcpu_config_get_feature_reg(config,
        HV_FEATURE_REG_ID_AA64MMFR2_EL1, &feature);
    row("config_mmfr2", frc, feature);
    hv_return_t rc = hv_vm_create(NULL);
    row("vm_create", rc, 0);
    if (rc != HV_SUCCESS) {
        os_release(config);
        puts("{\"op\":\"config_release\",\"completed\":true}");
        return 1;
    }
    hv_vcpu_t cpu = 0;
    hv_vcpu_exit_t *exit_info = NULL;
    rc = hv_vcpu_create(&cpu, &exit_info, config);
    row("vcpu_create", rc, 0);
    int failed = rc != HV_SUCCESS;
    if (!failed) {
        hv_return_t src = hv_vcpu_get_sys_reg(cpu, HV_SYS_REG_ID_AA64MMFR2_EL1, &current);
        row("vcpu_mmfr2", src, current);
        rc = hv_vcpu_destroy(cpu);
        row("vcpu_destroy", rc, 0);
        failed |= rc != HV_SUCCESS;
    }
    rc = hv_vm_destroy();
    row("vm_destroy", rc, 0);
    failed |= rc != HV_SUCCESS;
    os_release(config);
    puts("{\"op\":\"config_release\",\"completed\":true}");
    return failed ? 1 : 0;
}
