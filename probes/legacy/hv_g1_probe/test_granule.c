#include <Hypervisor/hypervisor.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>
int main(){
    printf("pagesize host %d\n", getpagesize());
    hv_ipa_granule_t g;
    hv_vm_config_get_default_ipa_granule(&g);
    printf("default granule %d\n", g);
    // Try 4K VM and map tests
    hv_vm_config_t cfg = hv_vm_config_create();
    hv_vm_config_set_ipa_granule(cfg, HV_IPA_GRANULE_4KB);
    hv_return_t r = hv_vm_create(cfg);
    printf("create 4K vm %x\n", r);
    if(r!=0) return 1;
    // Try mapping 16KB (should succeed)
    size_t pgsz = getpagesize(); // 16384
    void *m16 = mmap(NULL, pgsz, PROT_READ|PROT_WRITE, MAP_ANON|MAP_PRIVATE, -1,0);
    printf("m16 %p\n", m16);
    r = hv_vm_map(m16, 0x0, pgsz, HV_MEMORY_READ|HV_MEMORY_WRITE);
    printf("map 16K at 0x0 -> %x %s\n", r, r==0?"OK":"FAIL");
    // Try mapping 4K subregion: mmap a 16K then try mapping 4K slice at 4K offset
    // Host VA must be page aligned (16K), so 4K offset not allowed regardless of granule?
    void *m2 = mmap(NULL, pgsz, PROT_READ|PROT_WRITE, MAP_ANON|MAP_PRIVATE, -1,0);
    // try mapping size 4096
    r = hv_vm_map(m2, 0x4000, 4096, HV_MEMORY_READ|HV_MEMORY_WRITE);
    printf("map 4K size 4096 at IPA 0x4000 -> %x (expect BAD_ARGUMENT if still 16K granule, OK if 4K)\n", r);
    // try IPA 4096 aligned but size 4096
    r = hv_vm_map(m2, 0x1000, 4096, HV_MEMORY_READ|HV_MEMORY_WRITE);
    printf("map 4K at IPA 0x1000 -> %x\n", r);
    // try 16K but IPA 4K aligned only?
    // Clean
    hv_vm_destroy();
    munmap(m16, pgsz);
    munmap(m2, pgsz);
    return 0;
}
