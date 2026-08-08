#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>
uint64_t now_ns(){struct timespec ts; clock_gettime(CLOCK_MONOTONIC,&ts); return (uint64_t)ts.tv_sec*1000000000ULL+ts.tv_nsec;}
int main(){
    size_t n=2048; // 16KB exactly
    uint64_t *arr=NULL; uint8_t *perm=NULL;
    posix_memalign((void**)&arr,16384,n*sizeof(uint64_t));
    posix_memalign((void**)&perm,16384,64);
    for(size_t i=0;i<n;i++) arr[i]=i;
    for(size_t i=0;i<64;i++) perm[i]= (i%5==0)?0:1; // mostly uniform

    uint64_t iters=10000000;
    // Prevent optimization: use volatile pointer walk with inline asm barrier
    uint64_t t0=now_ns(); volatile uint64_t s=0;
    uint64_t *p=arr;
    for(uint64_t i=0;i<iters;i++){
        uint64_t addr = (uint64_t)(uintptr_t)p;
        // baseline: plain load, no check (simulates homogeneous fast path where check elided)
        asm volatile("" : "+r"(addr) :: "memory");
        s += *p;
        p++; if(p>=arr+n) p=arr;
    }
    uint64_t t1=now_ns();
    volatile uint64_t s2=0;
    p=arr;
    uint64_t taken=0;
    for(uint64_t i=0;i<iters;i++){
        uint64_t addr = (uint64_t)(uintptr_t)p;
        // degraded: Captive check: AND tmp, addr, #0x3000 ; CBNZ
        if(addr & 0x3000){
            uint8_t pr = perm[((uintptr_t)p>>3)&63];
            // permission mismatch check
            if((pr & 1)==0) taken++;
            asm volatile("" : "+r"(pr) :: "memory");
        }
        s2 += *p;
        p++; if(p>=arr+n) p=arr;
    }
    uint64_t t2=now_ns();
    // branchless variant: always load perm (worst case)
    volatile uint64_t s3=0;
    p=arr;
    for(uint64_t i=0;i<iters;i++){
        uint8_t pr = perm[((uintptr_t)p>>3)&63];
        asm volatile("" : "+r"(pr) :: "memory");
        s3 += *p + pr;
        p++; if(p>=arr+n) p=arr;
    }
    uint64_t t3=now_ns();
    double base=(double)(t1-t0)/iters, deg=(double)(t2-t1)/iters, bl=(double)(t3-t2)/iters;
    printf("base %.3f ns (%.1f cyc) deg %.3f ns (%.1f cyc) branchless %.3f ns (%.1f cyc)\n", base, base/0.286, deg, deg/0.286, bl, bl/0.286);
    printf("overhead deg-base %.3f ns (%.1f cyc) +%.1f%%  taken %.1f%%\n", deg-base,(deg-base)/0.286,(deg/base-1)*100, (double)taken/iters*100);
    printf("branchless overhead %.3f ns (%.1f cyc)\n", bl-base,(bl-base)/0.286);
    printf("sink %llu %llu %llu\n",(unsigned long long)s,(unsigned long long)s2,(unsigned long long)s3);
    free(arr); free(perm); return 0;
}
