#include <Hypervisor/Hypervisor.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#define BASE UINT64_C(0x40000000)
#define SIZE 0x24000u
#define ITERATIONS 8000000u
extern const char pair_start[], pair_end[], pair_boot_resume[], pair_done_resume[];
extern const char pair_load[], pair_store[], pair_split_low[], pair_split_high[];
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static unsigned ready, go, mode, writer;
static uint64_t tcr, descriptor;
struct cpu { unsigned id; uint64_t boot[6], result[8], mmfr2, pc, syndrome; };
static struct cpu cpus[2];
static void fail(const char *s) { fprintf(stderr,"FAIL %s\n",s); exit(2); }
static void check(hv_return_t rc,const char *s) {
    flockfile(stdout); printf("{\"kind\":\"api\",\"call\":\"%s\",\"rc\":%u}\n",s,rc); funlockfile(stdout);
    if(rc != HV_SUCCESS) fail(s);
}
#define HV(x) check((x),#x)
#define PT(x) do {if((x)!=0) fail(#x);} while(0)
static uint64_t addr(const char *p) { return BASE+(uintptr_t)p-(uintptr_t)pair_start; }
static void run(hv_vcpu_t v,hv_vcpu_exit_t *out,uint64_t expected,unsigned imm,struct cpu *c) {
    HV(hv_vcpu_run(v)); HV(hv_vcpu_get_reg(v,HV_REG_PC,&c->pc)); c->syndrome=out->exception.syndrome;
    if(out->reason!=HV_EXIT_REASON_EXCEPTION || (c->syndrome>>26)!=0x16 ||
       (c->syndrome&0xffff)!=imm || c->pc!=expected) fail("unexpected native exit/resume PC");
}
static void *owner(void *arg) {
    struct cpu *c=arg; hv_vcpu_t v; hv_vcpu_exit_t *out;
    HV(hv_vcpu_create(&v,&out,NULL));
    HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_ID_AA64MMFR2_EL1,&c->mmfr2));
    HV(hv_vcpu_set_reg(v,HV_REG_PC,BASE)); HV(hv_vcpu_set_reg(v,HV_REG_CPSR,0x3c5));
    HV(hv_vcpu_set_reg(v,HV_REG_X0,mode)); HV(hv_vcpu_set_reg(v,HV_REG_X1,writer));
    HV(hv_vcpu_set_reg(v,HV_REG_X2,ITERATIONS)); HV(hv_vcpu_set_reg(v,HV_REG_X8,c->id));
    HV(hv_vcpu_set_reg(v,HV_REG_X10,0xff)); HV(hv_vcpu_set_reg(v,HV_REG_X11,BASE+0x4000));
    HV(hv_vcpu_set_reg(v,HV_REG_X12,tcr));
    HV(hv_vcpu_set_sys_reg(v,HV_SYS_REG_SCTLR_EL1,0x30d00980));
    HV(hv_vcpu_set_sys_reg(v,HV_SYS_REG_ACTLR_EL1,2));
    HV(hv_vcpu_set_sys_reg(v,HV_SYS_REG_CNTV_CTL_EL0,0));
    run(v,out,addr(pair_boot_resume),0x43,c);
    for(unsigned j=0;j<6;j++) HV(hv_vcpu_get_reg(v,(hv_reg_t)(HV_REG_X9+j),&c->boot[j]));
    PT(pthread_mutex_lock(&lock)); ++ready; PT(pthread_cond_broadcast(&changed));
    while(!go) PT(pthread_cond_wait(&changed,&lock));
    PT(pthread_mutex_unlock(&lock));
    HV(hv_vcpu_set_reg(v,HV_REG_PC,addr(pair_boot_resume)));
    run(v,out,addr(pair_done_resume),0x44,c);
    for(unsigned j=0;j<8;j++) HV(hv_vcpu_get_reg(v,(hv_reg_t)(HV_REG_X0+j),&c->result[j]));
    HV(hv_vcpu_destroy(v)); return NULL;
}
static void values(const uint64_t *v,unsigned n) {
    for(unsigned j=0;j<n;j++) printf("%s\"%016llx\"",j?",":"",(unsigned long long)v[j]);
}
int main(int argc,char **argv) {
    setbuf(stdout,NULL);
    if(argc!=3 || (strcmp(argv[1],"control")&&strcmp(argv[1],"paired")) ||
       (strcmp(argv[2],"0")&&strcmp(argv[2],"1"))) fail("arguments");
    mode=!strcmp(argv[1],"paired"); writer=(unsigned)(argv[2][0]-'0');
    struct rlimit limit;
    if(getrlimit(RLIMIT_CPU,&limit)||limit.rlim_cur!=RLIM_INFINITY||limit.rlim_max!=RLIM_INFINITY)
        fail("unlimited inherited CPU required; no setter");
    tcr=16|(UINT64_C(1)<<8)|(UINT64_C(1)<<10)|(UINT64_C(3)<<12)|(UINT64_C(1)<<23)|(UINT64_C(2)<<32);
    descriptor=(BASE+0x10000)|3|(UINT64_C(1)<<10)|(UINT64_C(3)<<8)|(UINT64_C(3)<<53);
    uint8_t *ram=mmap(NULL,SIZE,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANON,-1,0);
    if(ram==MAP_FAILED) fail("mmap");
    size_t bytes=(uintptr_t)pair_end-(uintptr_t)pair_start; if(bytes>4096) fail("code size");
    memcpy(ram,pair_start,bytes);
    uint64_t *l0=(void *)(ram+0x4000),*l1=(void *)(ram+0x5000),*l2=(void *)(ram+0x6000),*l3=(void *)(ram+0x7000);
    l0[0]=(BASE+0x5000)|3; l1[1]=(BASE+0x6000)|3; l2[0]=(BASE+0x7000)|3;
    for(unsigned j=0;j<SIZE/4096;j++) l3[j]=(BASE+j*4096)|3|(UINT64_C(1)<<10)|(UINT64_C(3)<<8)|(j?(UINT64_C(3)<<53):0);
    l3[16]=descriptor; l3[32]=descriptor;
    *(uint64_t *)(ram+0x10008)=UINT64_MAX;
    __builtin___clear_cache((char *)ram,(char *)ram+bytes); __asm__ volatile("dsb sy" ::: "memory");
    printf("{\"kind\":\"layout\",\"mode\":\"%s\",\"writer\":%u,\"iterations\":%u,\"bytes\":%u,\"tcr\":%llu,\"descriptor\":%llu,\"boot_resume\":%llu,\"done_resume\":%llu,\"load_pc\":%llu,\"store_pc\":%llu,\"split_low_pc\":%llu,\"split_high_pc\":%llu}\n",argv[1],writer,ITERATIONS,SIZE,(unsigned long long)tcr,(unsigned long long)descriptor,(unsigned long long)addr(pair_boot_resume),(unsigned long long)addr(pair_done_resume),(unsigned long long)addr(pair_load),(unsigned long long)addr(pair_store),(unsigned long long)addr(pair_split_low),(unsigned long long)addr(pair_split_high));
    HV(hv_vm_create(NULL)); HV(hv_vm_map(ram,BASE,SIZE,HV_MEMORY_READ|HV_MEMORY_WRITE|HV_MEMORY_EXEC));
    pthread_t threads[2]; for(unsigned j=0;j<2;j++){cpus[j].id=j;PT(pthread_create(&threads[j],NULL,owner,&cpus[j]));}
    PT(pthread_mutex_lock(&lock)); while(ready!=2) PT(pthread_cond_wait(&changed,&lock));
    for(unsigned j=0;j<2;j++){printf("{\"kind\":\"boot\",\"cpu\":%u,\"mmfr2\":\"%016llx\",\"values\":[",j,(unsigned long long)cpus[j].mmfr2);values(cpus[j].boot,6);puts("]}");}
    for(unsigned j=0;j<2;j++){
        uint64_t *b=cpus[j].boot;
        if(((cpus[j].mmfr2>>32)&15)!=1 || b[0]!=2 || b[1]!=0xff || b[2]!=tcr || b[3]!=0x30d01985 || b[4]!=descriptor || b[5]!=descriptor) fail("native setup mismatch");
    }
    go=1;PT(pthread_cond_broadcast(&changed));PT(pthread_mutex_unlock(&lock));
    for(unsigned j=0;j<2;j++) PT(pthread_join(threads[j],NULL));
    for(unsigned j=0;j<2;j++){printf("{\"kind\":\"result\",\"cpu\":%u,\"pc\":%llu,\"syndrome\":%llu,\"regs\":[",j,(unsigned long long)cpus[j].pc,(unsigned long long)cpus[j].syndrome);values(cpus[j].result,8);puts("]}");}
    HV(hv_vm_unmap(BASE,SIZE));HV(hv_vm_destroy());if(munmap(ram,SIZE))fail("munmap");
    puts("{\"kind\":\"complete\"}");return 0;
}
