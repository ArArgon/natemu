// Checks tools/models/gen_tiny_vectors.py's vectors against the hardware's tininess after rounding: on a host with FEAT_AFP (the M4),
// FPCR.AH = 1 (with NEP: FPCR = 6, as natemu runs it) detects underflow after rounding, as x86 does, so for every inexact result
// UFC must equal the vector's `tiny` (an exact tiny result raises no UFC on Arm and is skipped).
//   clang -O0 -o tiny_vectors_hw tools/models/tiny_vectors_hw.c && ./tiny_vectors_hw crates/abi/tests/tiny_vectors.txt
#include <stdio.h>
#include <stdint.h>
#include <string.h>
static inline void wr(uint64_t x){__asm__ volatile("msr fpcr, %0"::"r"(x));}
static inline uint64_t rs(void){uint64_t x;__asm__ volatile("mrs %0, fpsr":"=r"(x));return x;}
static inline void ws(uint64_t x){__asm__ volatile("msr fpsr, %0"::"r"(x));}
int main(int argc,char**argv){
  FILE*f=fopen(argv[1],"r"); char line[256]; long n=0,bad=0,checked=0;
  while(fgets(line,sizeof line,f)){
    if(line[0]=='#')continue;
    int kind,wide,rc,np,nc,tiny; unsigned long long a,b,c;
    if(sscanf(line,"%d %d %d %d %d %llx %llx %llx %d",&kind,&wide,&rc,&np,&nc,&a,&b,&c,&tiny)!=9)continue;
    n++;
    int arm=(int[]){0,2,1,3}[rc];
    float fa,fb,fc,fr; double da,db,dc,dr; uint32_t u; uint64_t v;
    if(kind==3){ if(np) a^= wide?(1ull<<63):(1ull<<31); if(nc) c^= wide?(1ull<<63):(1ull<<31);} 
    wr((uint64_t)arm<<22|6); ws(0);
    if(kind==4){ v=a; memcpy(&da,&v,8); __asm__ volatile("fcvt %s0, %d1":"=w"(fr):"w"(da)); }
    else if(!wide){ u=a; memcpy(&fa,&u,4); u=b; memcpy(&fb,&u,4); u=c; memcpy(&fc,&u,4);
      if(kind==1) __asm__ volatile("fmul %s0, %s1, %s2":"=w"(fr):"w"(fa),"w"(fb));
      else if(kind==2) __asm__ volatile("fdiv %s0, %s1, %s2":"=w"(fr):"w"(fa),"w"(fb));
      else __asm__ volatile("fmadd %s0, %s1, %s2, %s3":"=w"(fr):"w"(fa),"w"(fb),"w"(fc)); }
    else { v=a; memcpy(&da,&v,8); v=b; memcpy(&db,&v,8); v=c; memcpy(&dc,&v,8);
      if(kind==1) __asm__ volatile("fmul %d0, %d1, %d2":"=w"(dr):"w"(da),"w"(db));
      else if(kind==2) __asm__ volatile("fdiv %d0, %d1, %d2":"=w"(dr):"w"(da),"w"(db));
      else __asm__ volatile("fmadd %d0, %d1, %d2, %d3":"=w"(dr):"w"(da),"w"(db),"w"(dc)); }
    uint64_t fl=rs(); wr(0);
    int ufc=fl>>3&1, ixc=fl>>4&1;
    if(kind==2 && !wide) { /* b==0 -> div by zero: model says not tiny; hw ufc 0 */ }
    if(ixc){ checked++; if(ufc!=tiny){ bad++; if(bad<10) printf("MISMATCH %s hw ufc=%d model tiny=%d\n",line,ufc,tiny);} }
    else if(ufc){ bad++; printf("UFC without IXC %s",line);} 
  }
  printf("vectors %ld, compared (inexact) %ld, mismatches %ld\n",n,checked,bad); return bad!=0;
}
