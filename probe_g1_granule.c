#include <Hypervisor/hypervisor.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>
#include <stdlib.h>
int main(){
    printf("=== granule 4K isolated map test ===\n");
    hv_vm_config_t cfg = hv_vm_config_create();
    hv_vm_config_set_ipa_granule(cfg, HV_IPA_GRANULE_4KB);
    hv_return_t r = hv_vm_create(cfg);
    printf("create 4K vm %x\n", r);
    void *m = mmap(NULL, 16384, PROT_READ|PROT_WRITE, MAP_ANON|MAP_PRIVATE, -1,0);
    printf("m %p\n", m);
    // Try 4K map at IPA 0x0 size 4096
    r = hv_vm_map(m, 0x0, 4096, HV_MEMORY_READ|HV_MEMORY_WRITE);
    printf("map 4K at 0x0 size 4096 -> 0x%x %s\n", r, r==0?"OK":"FAIL");
    if(r==0) { hv_vm_unmap(0x0, 4096); }
    // Try unaligned host VA: offset 4096 inside page
    void *misaligned = (void*)((char*)m + 4096);
    r = hv_vm_map(misaligned, 0x5000, 4096, HV_MEMORY_READ|HV_MEMORY_WRITE);
    printf("map with host VA +4096 (misaligned 4K but not 16K) -> 0x%x %s\n", r, r==0?"OK (4K host granularity)":"FAIL (still 16K host alignment required)");
    hv_vm_destroy(); munmap(m,16384);

    printf("\n=== granule 16K map test (default) ===\n");
    cfg = hv_vm_config_create();
    hv_vm_config_set_ipa_granule(cfg, HV_IPA_GRANULE_16KB);
    r = hv_vm_create(cfg);
    printf("create 16K vm %x\n", r);
    m = mmap(NULL, 16384, PROT_READ|PROT_WRITE, MAP_ANON|MAP_PRIVATE, -1,0);
    r = hv_vm_map(m, 0x0, 4096, HV_MEMORY_READ|HV_MEMORY_WRITE);
    printf("map 4K at 0x0 size 4096 in 16K VM -> 0x%x %s (expected BAD_ARGUMENT)\n", r, r==0xfae94003?"BAD_ARGUMENT as expected":r==0?"UNEXPECTED OK":"other");
    hv_vm_destroy(); munmap(m,16384);
    return 0;
}
