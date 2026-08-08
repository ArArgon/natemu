#include <Hypervisor/hypervisor.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <time.h>
static uint64_t now_ns(){struct timespec ts; clock_gettime(CLOCK_MONOTONIC_RAW,&ts); return ts.tv_sec*1000000000ULL+ts.tv_nsec; }
int main(){
    printf("HV fault/map timing (M2 Max, macOS 26.5.2)\n");
    hv_vm_config_t cfg=hv_vm_config_create();
    hv_vm_config_set_ipa_granule(cfg, HV_IPA_GRANULE_4KB);
    hv_return_t r=hv_vm_create(cfg);
    printf("hv_vm_create 4K -> 0x%x\n",r);
    if(r) return 1;
    hv_vcpu_t vcpu; hv_vcpu_exit_t *exit;
    r=hv_vcpu_create(&vcpu,&exit,NULL);
    printf("vcpu_create 0x%x\n",r);
    // Map a page for code
    size_t pgsz=getpagesize(); // 16384 but VM granule 4K
    void *code_mem=mmap(NULL,pgsz,PROT_READ|PROT_WRITE,MAP_ANON|MAP_PRIVATE,-1,0);
    hv_ipa_t code_ipa=0x400000;
    r=hv_vm_map(code_mem, code_ipa, 4096, HV_MEMORY_READ|HV_MEMORY_WRITE|HV_MEMORY_EXEC);
    printf("code map 4K at 0x%llx -> 0x%x\n", (unsigned long long)code_ipa, r);
    // Simple loop that accesses unmapped IPA -> should exit with data abort
    // Use guest code: ldr x0, [x1] where x1 = 0x800000 (unmapped), then hvc
    uint32_t *code=(uint32_t*)code_mem;
    int idx=0;
    // mov x1, #0x800000 alias: movz x1, #0x8000, lsl #8? Simpler use 2 movs
    // Use: mov x1, #0x8000, lsl #8 = 0x800000 -> encoding d2a10021? Let's use literal pool: ldr x1, [pc, #8]; ldr x0,[x1]; hvc #0; .quad 0x800000
    code[idx++]=0x58000081; // ldr x1, #16 (pc+16)
    code[idx++]=0xf9400020; // ldr x0, [x1]
    code[idx++]=0xd4000002; // hvc #0
    code[idx++]=0xd503201f; // nop
    code[idx++]=0; code[idx++]=0;
    code[idx++]=0x00800000; code[idx++]=0x00000000; // 64-bit addr 0x800000 little endian: low 0x00800000 high 0
    // Actually 0x58000081 loads from pc+16: pc is at code_ipa, so [pc+16] = code_ipa+16+16? Need compute. Simpler place addr at offset 16 from code start: code[4]=addr low, code[5]=addr high
    // Our ldr x1, #16 will load from pc+16 => at idx 0, pc = code_ipa, ldr literal offset 16 => addr = code_ipa+16 = code_ipa+0x10, which is code[4]
    // So okay.
    __builtin___clear_cache((char*)code_mem,(char*)code_mem+pgsz);
    // Now time a fault+map cycle: run vcpu, expect data abort exit, then map the fault IPA, then rerun
    hv_vcpu_set_reg(vcpu, HV_REG_PC, code_ipa);
    hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5);
    // First run should fault on unmapped 0x800000
    uint64_t t0=now_ns();
    r=hv_vcpu_run(vcpu);
    uint64_t t1=now_ns();
    printf("first run -> 0x%x reason %u ESR 0x%llx FAR 0x%llx IPA 0x%llx time %llu ns\n", r, exit->reason, (unsigned long long)exit->exception.syndrome, (unsigned long long)exit->exception.virtual_address, (unsigned long long)exit->exception.physical_address, (unsigned long long)(t1-t0));
    // Check if data abort
    uint32_t ec=(exit->exception.syndrome>>26)&0x3f;
    printf("EC 0x%x %s DFSC 0x%x\n", ec, ec==0x24?"Data abort":"other", (unsigned)(exit->exception.syndrome & 0x3f));
    // Now map the fault IPA
    void *data_mem=mmap(NULL,4096,PROT_READ|PROT_WRITE,MAP_ANON|MAP_PRIVATE,-1,0);
    memset(data_mem,0xAB,4096);
    hv_ipa_t fault_ipa=0x800000;
    // If exit provides IPA, use it; else use 0x800000
    if(exit->exception.physical_address) fault_ipa=exit->exception.physical_address;
    printf("mapping fault IPA 0x%llx\n", (unsigned long long)fault_ipa);
    uint64_t tm0=now_ns();
    r=hv_vm_map(data_mem, fault_ipa & ~0xFFFULL, 4096, HV_MEMORY_READ|HV_MEMORY_WRITE);
    uint64_t tm1=now_ns();
    printf("hv_vm_map -> 0x%x time %llu ns\n", r, (unsigned long long)(tm1-tm0));
    // Rerun
    hv_vcpu_set_reg(vcpu, HV_REG_PC, code_ipa);
    t0=now_ns();
    r=hv_vcpu_run(vcpu);
    t1=now_ns();
    printf("second run (after map) -> 0x%x reason %u ESR 0x%llx time %llu ns\n", r, exit->reason, (unsigned long long)exit->exception.syndrome, (unsigned long long)(t1-t0));
    if(exit->reason==1 && ec==0x16) printf("second run reached HVC as expected\n");
    // Measure hv_vm_unmap + remap cycle
    tm0=now_ns();
    r=hv_vm_unmap(fault_ipa & ~0xFFFULL, 4096);
    tm1=now_ns();
    printf("hv_vm_unmap time %llu ns -> 0x%x\n", (unsigned long long)(tm1-tm0), r);
    // Loop 1000 map/unmap
    int N=1000;
    size_t bigsz= N*4096;
    void *big=mmap(NULL,bigsz,PROT_READ|PROT_WRITE,MAP_ANON|MAP_PRIVATE,-1,0);
    hv_ipa_t base_ipa=0x1000000;
    uint64_t tot=0;
    t0=now_ns();
    for(int i=0;i<N;i++){
        void *p=(char*)big+i*4096;
        hv_ipa_t ipa=base_ipa+i*4096;
        hv_vm_map(p, ipa, 4096, HV_MEMORY_READ|HV_MEMORY_WRITE);
    }
    uint64_t t2=now_ns();
    printf("%d x hv_vm_map 4K: total %.1f ms avg %.1f us/map\n", N, (t2-t0)/1e6, (t2-t0)/1e3/N);
    t0=now_ns();
    for(int i=0;i<N;i++){
        hv_ipa_t ipa=base_ipa+i*4096;
        hv_vm_unmap(ipa,4096);
    }
    t1=now_ns();
    printf("%d x hv_vm_unmap 4K: total %.1f ms avg %.1f us/unmap\n", N, (t1-t0)/1e6, (t1-t0)/1e3/N);
    hv_vcpu_destroy(vcpu);
    hv_vm_destroy();
    munmap(code_mem,pgsz);
    munmap(data_mem,4096);
    munmap(big,bigsz);
    return 0;
}
