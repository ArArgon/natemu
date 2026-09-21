// Disposable EL1 extracted-body probe. Reuses native-handoff's paging/API idioms.
#include <Hypervisor/Hypervisor.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <mach/mach_time.h>
#include "execution-generated.h"
#ifndef CODE_LIMIT
#define CODE_LIMIT 0x4000u
#endif
#ifndef STATE_BYTES
#define STATE_BYTES 4096u
#endif
#ifndef PROBE_FPCR
#define PROBE_FPCR 0u
#endif
#define BASE UINT64_C(0x40000000)
#define SIZE 0x40000u
#define STATE 0x18000u
#define DATA 0x20000u
#define STACK 0x30000u
extern const char execution_start[], execution_end[], execution_boot_resume[];
extern const char execution_vectors[], execution_fault_resume[];
static unsigned api_count;
static void fail(const char *s) { fprintf(stderr,"FAIL %s\n",s); exit(2); }
static void hvcheck(hv_return_t r,const char *s) { ++api_count; if(r!=HV_SUCCESS){fprintf(stderr,"API %s rc=%u\n",s,r);fail(s);} }
#define HV(x) hvcheck((x),#x)
static uint64_t addr(const char *p) {return BASE+(uintptr_t)p-(uintptr_t)execution_start;}
static void hex(const void *p,size_t n) {const unsigned char *s=p;putchar('"');for(size_t i=0;i<n;i++)printf("%02x",s[i]);putchar('"');}
static uint64_t counter(void) {uint64_t x;__asm__ volatile("isb\n\tmrs %0,cntvct_el0":"=r"(x)::"memory");return x;}
static void file_read(const char *path,void *p,size_t n) {FILE *f=fopen(path,"rb");if(!f||fread(p,1,n,f)!=n||fgetc(f)!=EOF)fail("input length/read");fclose(f);}
int main(int argc,char **argv) {
    setbuf(stdout,NULL);
    if(argc!=3)fail("schedule input-directory");
    struct rlimit lim;if(getrlimit(RLIMIT_CPU,&lim)||lim.rlim_cur!=RLIM_INFINITY||lim.rlim_max!=RLIM_INFINITY)fail("CPU limit must be inherited unlimited");
    uint8_t *ram=mmap(NULL,SIZE,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANON,-1,0);if(ram==MAP_FAILED)fail("mmap");
    size_t code_size=(uintptr_t)execution_end-(uintptr_t)execution_start;if(code_size>CODE_LIMIT)fail("code size");memcpy(ram,execution_start,code_size);
    uint64_t tcr=16|(1ul<<8)|(1ul<<10)|(3ul<<12)|(1ul<<23)|(2ul<<32);
    uint64_t *l0=(void*)(ram+CODE_LIMIT),*l1=(void*)(ram+CODE_LIMIT+0x1000),*l2=(void*)(ram+CODE_LIMIT+0x2000),*l3=(void*)(ram+CODE_LIMIT+0x3000);
    l0[0]=(BASE+CODE_LIMIT+0x1000)|3;l1[1]=(BASE+CODE_LIMIT+0x2000)|3;l2[0]=(BASE+CODE_LIMIT+0x3000)|3;
    for(unsigned i=0;i<SIZE/4096;i++)l3[i]=(BASE+i*4096)|3|(1ul<<10)|(3ul<<8)|(i<CODE_LIMIT/4096?0:(3ul<<53));
    __builtin___clear_cache((char*)ram,(char*)ram+code_size);__asm__ volatile("dsb sy":::"memory");
    HV(hv_vm_create(NULL));HV(hv_vm_map(ram,BASE,SIZE,HV_MEMORY_READ|HV_MEMORY_WRITE|HV_MEMORY_EXEC));
    hv_vcpu_t v;hv_vcpu_exit_t *out;HV(hv_vcpu_create(&v,&out,NULL));
    HV(hv_vcpu_set_reg(v,HV_REG_PC,BASE));HV(hv_vcpu_set_reg(v,HV_REG_CPSR,0x3c5));
    HV(hv_vcpu_set_reg(v,HV_REG_X10,0xff));HV(hv_vcpu_set_reg(v,HV_REG_X11,BASE+CODE_LIMIT));HV(hv_vcpu_set_reg(v,HV_REG_X12,tcr));
    HV(hv_vcpu_set_sys_reg(v,HV_SYS_REG_SCTLR_EL1,0x30d00980));HV(hv_vcpu_set_sys_reg(v,HV_SYS_REG_ACTLR_EL1,2));
    HV(hv_vcpu_set_sys_reg(v,HV_SYS_REG_CPACR_EL1,3ul<<20));HV(hv_vcpu_set_sys_reg(v,HV_SYS_REG_CNTV_CTL_EL0,0));
    HV(hv_vcpu_set_sys_reg(v,HV_SYS_REG_VBAR_EL1,addr(execution_vectors)));
    HV(hv_vcpu_run(v));uint64_t pc;HV(hv_vcpu_get_reg(v,HV_REG_PC,&pc));
    if(out->reason!=HV_EXIT_REASON_EXCEPTION||(out->exception.syndrome>>26)!=0x16||(out->exception.syndrome&0xffff)!=0x43||pc!=addr(execution_boot_resume))fail("boot checkpoint");
    uint64_t actlr,mair,actual_tcr,sctlr,hz,guest_hz,isar0,mmfr1=0;HV(hv_vcpu_get_reg(v,HV_REG_X9,&guest_hz));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_ID_AA64ISAR0_EL1,&isar0));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_ACTLR_EL1,&actlr));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_MAIR_EL1,&mair));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_TCR_EL1,&actual_tcr));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_SCTLR_EL1,&sctlr));
    __asm__ volatile("mrs %0,cntfrq_el0":"=r"(hz));
#ifdef COMPARISON_PROBE
    HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_ID_AA64MMFR1_EL1,&mmfr1));if(((mmfr1>>44)&15)==0)fail("AFP capability");
#endif
    mach_timebase_info_data_t tb; if(mach_timebase_info(&tb)!=KERN_SUCCESS)fail("timebase");
    printf("{\"kind\":\"boot\",\"actlr\":%llu,\"mair\":%llu,\"tcr\":%llu,\"sctlr\":%llu,\"hz\":%llu,\"guest_hz\":%llu,\"isar0\":%llu,\"mmfr1\":%llu,\"timebase\":[%u,%u],\"code_bytes\":%zu}\n",actlr,mair,actual_tcr,sctlr,hz,guest_hz,isar0,mmfr1,tb.numer,tb.denom,code_size);
    if(actlr!=2||mair!=255||actual_tcr!=tcr||sctlr!=0x30d01985||((isar0>>52)&15)<1)fail("readbacks");
    FILE *schedule=fopen(argv[1],"r");if(!schedule)fail("schedule");
    unsigned index,variant,fault,n,serial=0;
    while(fscanf(schedule,"%u %u %u %u",&index,&variant,&n,&fault)==4) {
        if(index>=BODY_COUNT||variant>1||n<1||n>1048576)fail("schedule bounds");
#ifdef BOUNDARY_PROBE
        if(fault>7)fail("fault mode");
        unsigned base_index=bodies[index].base_index;
#else
        if(fault>1)fail("fault mode");
        unsigned base_index=index%21;
#endif
        char path[4096];if(snprintf(path,sizeof(path),"%s/%u-%u-%u.input",argv[2],index,variant,fault)>=(int)sizeof(path))fail("path");
        uint8_t input[31*8+32*16+STATE_BYTES+4096];file_read(path,input,sizeof(input));
        uint64_t regs[31];memcpy(regs,input,sizeof(regs));
        memcpy(ram+STATE,input+31*8+32*16,STATE_BYTES);memcpy(ram+DATA,input+31*8+32*16+STATE_BYTES,4096);
        memset(ram+STACK,0,4096);*(uint64_t*)(ram+STACK+8)=n;
        __asm__ volatile("dsb sy":::"memory");
        for(unsigned i=0;i<31;i++)HV(hv_vcpu_set_reg(v,(hv_reg_t)(HV_REG_X0+i),regs[i]));
        for(unsigned i=0;i<32;i++){hv_simd_fp_uchar16_t q;memcpy(&q,input+31*8+i*16,16);HV(hv_vcpu_set_simd_fp_reg(v,(hv_simd_fp_reg_t)(HV_SIMD_FP_REG_Q0+i),q));}
        HV(hv_vcpu_set_sys_reg(v,HV_SYS_REG_SP_EL1,BASE+STACK));
        HV(hv_vcpu_set_reg(v,HV_REG_CPSR,0x3c5|((base_index/7==1)?(1ul<<29):0)));HV(hv_vcpu_set_reg(v,HV_REG_FPCR,PROBE_FPCR));HV(hv_vcpu_set_reg(v,HV_REG_FPSR,0));
#ifdef COMPARISON_PROBE
        uint64_t configured_fpcr;HV(hv_vcpu_get_reg(v,HV_REG_FPCR,&configured_fpcr));if(configured_fpcr!=PROBE_FPCR)fail("FPCR readback");
#endif
        HV(hv_vcpu_set_reg(v,HV_REG_PC,addr(bodies[index].start)));
        uint64_t host_tick_before=counter(),host_ns_before=mach_continuous_time();
        HV(hv_vcpu_run(v));
        uint64_t host_ns_after=mach_continuous_time(),host_tick_after=counter();
        HV(hv_vcpu_get_reg(v,HV_REG_PC,&pc));
        uint64_t syndrome=out->exception.syndrome;
        if(out->reason!=HV_EXIT_REASON_EXCEPTION||(syndrome>>26)!=0x16)fail("unexpected exit");
        if((!fault&&((syndrome&0xffff)!=0x44||pc!=addr(bodies[index].resume)))||(fault==1&&((syndrome&0xffff)!=0x46||pc!=addr(execution_fault_resume))))fail("reply identity");
#ifdef BOUNDARY_PROBE
        if(fault>=2){const char *site=(fault==4||fault==5||fault==7)?bodies[index].reject_vector:bodies[index].reject_scalar;if((syndrome&0xffff)!=0x47||pc!=addr(site))fail("guard rejection identity");}
#endif
        for(unsigned i=0;i<31;i++)HV(hv_vcpu_get_reg(v,(hv_reg_t)(HV_REG_X0+i),regs+i));
        uint64_t cpsr,esr,far,elr;HV(hv_vcpu_get_reg(v,HV_REG_CPSR,&cpsr));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_ESR_EL1,&esr));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_FAR_EL1,&far));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_ELR_EL1,&elr));
        __asm__ volatile("dsb sy":::"memory");
        printf("{\"kind\":\"sample\",\"serial\":%u,\"index\":%u,\"variant\":%u,\"n\":%u,\"fault\":%u,\"start\":%llu,\"end\":%llu,\"host_ticks\":%llu,\"host_mach\":%llu,\"cpsr\":%llu,\"esr\":%llu,\"far\":%llu,\"elr\":%llu,\"body_address\":%llu,\"registers\":",serial++,index,variant,n,fault,*(uint64_t*)(ram+STACK),regs[30],host_tick_after-host_tick_before,host_ns_after-host_ns_before,cpsr,esr,far,elr,addr(bodies[index].body));hex(regs,sizeof(regs));
        uint8_t vectors[512];for(unsigned i=0;i<32;i++){hv_simd_fp_uchar16_t q;HV(hv_vcpu_get_simd_fp_reg(v,(hv_simd_fp_reg_t)(HV_SIMD_FP_REG_Q0+i),&q));memcpy(vectors+i*16,&q,16);}printf(",\"vectors\":");hex(vectors,512);printf(",\"state\":");hex(ram+STATE,STATE_BYTES);printf(",\"data\":");hex(ram+DATA,512);
#ifdef COMPARISON_PROBE
        uint64_t fpcr,fpsr;HV(hv_vcpu_get_reg(v,HV_REG_FPCR,&fpcr));HV(hv_vcpu_get_reg(v,HV_REG_FPSR,&fpsr));printf(",\"fpcr\":%llu,\"fpsr\":%llu",fpcr,fpsr);
#endif
#ifdef BOUNDARY_PROBE
        printf(",\"boundary\":");hex(ram+STACK+32,224);printf(",\"pc\":%llu,\"hvc\":%llu",pc,syndrome&0xffff);
#ifdef GUARDED_FP_PROBE
        printf(",\"reject_address\":%llu",addr(bodies[index].reject_scalar));
#endif
#endif
        puts("}");
    }
    if(!feof(schedule))fail("schedule parse");fclose(schedule);
    HV(hv_vcpu_destroy(v));HV(hv_vm_unmap(BASE,SIZE));HV(hv_vm_destroy());if(munmap(ram,SIZE))fail("munmap");
    printf("{\"kind\":\"teardown\",\"samples\":%u,\"api_calls\":%u}\n",serial,api_count);return 0;
}
