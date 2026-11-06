// Native harness: runs a set of FEX blocks (an image built by build_native.py) outside the VM. Config file lines:
//   image PATH | entry OFF | n ITERS | ctr GUESTREG | reg GUESTREG VALUE (number, bufPLUS+off, stack+off) | l1 RIP OFF | bufbytes N
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <sys/mman.h>
#include <pthread.h>
#include <libkern/OSCacheControl.h>
#include <time.h>
extern void run_block(uint64_t *regs, uint64_t entry);
extern void exit_stub(void);
extern void native_dispatch(void);
extern uint64_t calib(uint64_t);
static const int SRA[16] = {4,7,5,6,8,9,10,11,12,13,14,15,16,17,19,29};
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC_RAW,&t);return t.tv_sec+t.tv_nsec*1e-9;}
int main(int argc,char**argv){
  FILE*f=fopen(argv[1],"r"); char line[512], image[400]="";
  uint32_t entry=0; unsigned shift=0; uint64_t n=1; int ctr=1; size_t bufbytes=1<<20;
  struct{int r;char v[64];} regs_spec[16]; int nreg=0; struct{uint64_t rip;uint32_t off;} l1[16]; int nl1=0;
  while(fgets(line,sizeof line,f)){
    char a[64],b[64],c[64];
    if(sscanf(line,"image %399s",image)==1) continue;
    if(sscanf(line,"entry %u",&entry)==1) continue;
    if(sscanf(line,"n %llu",(unsigned long long*)&n)==1) continue;
    if(sscanf(line,"ctr %d",&ctr)==1) continue;
    if(sscanf(line,"shift %u",&shift)==1) continue;
    if(sscanf(line,"bufbytes %zu",&bufbytes)==1) continue;
    if(sscanf(line,"reg %s %s",a,b)==2){regs_spec[nreg].r=atoi(a);strcpy(regs_spec[nreg++].v,b);continue;}
    if(sscanf(line,"l1 %s %s",a,c)==2){l1[nl1].rip=strtoull(a,0,0);l1[nl1++].off=atoi(c);continue;}
  }
  FILE*im=fopen(image,"rb"); fseek(im,0,SEEK_END); size_t len=ftell(im); fseek(im,0,SEEK_SET);
  uint8_t*code=mmap(0,1<<16,PROT_READ|PROT_WRITE|PROT_EXEC,MAP_PRIVATE|MAP_ANON|MAP_JIT,-1,0);
  pthread_jit_write_protect_np(0); fread(code,1,len,im); pthread_jit_write_protect_np(1); sys_icache_invalidate(code,len);
  uint8_t*frame=aligned_alloc(16384,32768); memset(frame,0,32768); frame+=shift; printf("frame low12=%#x ",(unsigned)((uintptr_t)frame&0xfff));
  *(uint64_t*)(frame+0xb88)=(uint64_t)exit_stub; *(uint64_t*)(frame+0xb68)=(uint64_t)native_dispatch;
  uint64_t*l1t=calloc(4096,16); uint64_t fast=0;
  for(int i=0;i<nl1;i++){l1t[(l1[i].rip&0xfff)*2]=l1[i].rip;l1t[(l1[i].rip&0xfff)*2+1]=(uint64_t)code+l1[i].off;}
  *(uint64_t*)(frame+0xa8)=(uint64_t)l1t; *(uint64_t*)(frame+0xb0)=(uint64_t)&fast;
  uint8_t*buf=aligned_alloc(16384,bufbytes+16384); for(size_t i=0;i<bufbytes;i++) buf[i]=i*7;
  uint8_t*stack=aligned_alloc(16384,65536);
  double all[9]; int nr=0;
  for(int rep=0;rep<7;rep++){
    double t[2];
    for(int k=1;k<=2;k++){
      uint64_t regs[32]={0}; regs[28]=(uint64_t)frame;
      for(int i=0;i<nreg;i++){
        uint64_t v; char*s=regs_spec[i].v;
        if(!strncmp(s,"buf+",4)) v=(uint64_t)buf+strtoull(s+4,0,0);
        else if(!strncmp(s,"stack+",6)) v=(uint64_t)stack+strtoull(s+6,0,0);
        else v=strtoull(s,0,0);
        regs[SRA[regs_spec[i].r]]=v;
      }
      regs[SRA[ctr]]=n*k; fast=0;
      double t0=now(); run_block(regs,(uint64_t)code+entry); t[k-1]=now()-t0;
      if(regs[SRA[ctr]]!=0){printf("counter not zero: %llx\n",(unsigned long long)regs[SRA[ctr]]);return 1;}
      if(rep==0&&k==2) printf("fast=%llu ",(unsigned long long)fast);
    }
    all[nr++]=(t[1]-t[0])/n*1e9;
  }
  { double t0=now(); calib(10000000); double tc=now()-t0; printf("GHz~%.2f ",64e7/tc/1e9); }
  for(int i=0;i<nr;i++)for(int j=i+1;j<nr;j++)if(all[j]<all[i]){double x=all[i];all[i]=all[j];all[j]=x;}
  printf("ns/iter %.4f (min %.4f max %.4f)\n",all[nr/2],all[0],all[nr-1]);
}
