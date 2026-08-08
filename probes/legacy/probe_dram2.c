#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>
#include <sys/mman.h>
#include <unistd.h>
#include <string.h>
static uint64_t now_ns(){struct timespec ts; clock_gettime(CLOCK_MONOTONIC_RAW,&ts); return ts.tv_sec*1000000000ULL+ts.tv_nsec; }
static uint64_t bench_plain(void *base, size_t it){
    char *b=base;
    char *p0=b, *p1=b+64*1024*1024, *p2=b+128*1024*1024, *p3=b+192*1024*1024;
    char *p4=b+256*1024*1024, *p5=b+320*1024*1024, *p6=b+384*1024*1024, *p7=b+448*1024*1024;
    size_t stride=4160;
    uint64_t sum=0;
    uint64_t s=now_ns();
    for(size_t i=0;i<it;i++){
        uint64_t v0,v1,v2,v3,v4,v5,v6,v7;
        __asm__ volatile("ldr %0,[%8]\n\t ldr %1,[%9]\n\t ldr %2,[%10]\n\t ldr %3,[%11]\n\t ldr %4,[%12]\n\t ldr %5,[%13]\n\t ldr %6,[%14]\n\t ldr %7,[%15]"
            : "=&r"(v0),"=&r"(v1),"=&r"(v2),"=&r"(v3),"=&r"(v4),"=&r"(v5),"=&r"(v6),"=&r"(v7)
            : "r"(p0),"r"(p1),"r"(p2),"r"(p3),"r"(p4),"r"(p5),"r"(p6),"r"(p7):"memory");
        sum+=v0+v1+v2+v3+v4+v5+v6+v7;
        p0+=stride; if(p0>=b+64*1024*1024) p0=b;
        p1+=stride; if(p1>=b+128*1024*1024) p1=b+64*1024*1024;
        p2+=stride; if(p2>=b+192*1024*1024) p2=b+128*1024*1024;
        p3+=stride; if(p3>=b+256*1024*1024) p3=b+192*1024*1024;
        p4+=stride; if(p4>=b+320*1024*1024) p4=b+256*1024*1024;
        p5+=stride; if(p5>=b+384*1024*1024) p5=b+320*1024*1024;
        p6+=stride; if(p6>=b+448*1024*1024) p6=b+384*1024*1024;
        p7+=stride; if(p7>=b+512*1024*1024) p7=b+448*1024*1024;
    }
    uint64_t e=now_ns();
    volatile uint64_t sink=sum; (void)sink;
    return e-s;
}
static uint64_t bench_ldapr(void *base, size_t it){
    char *b=base;
    char *p0=b, *p1=b+64*1024*1024, *p2=b+128*1024*1024, *p3=b+192*1024*1024;
    char *p4=b+256*1024*1024, *p5=b+320*1024*1024, *p6=b+384*1024*1024, *p7=b+448*1024*1024;
    size_t stride=4160;
    uint64_t sum=0;
    uint64_t s=now_ns();
    for(size_t i=0;i<it;i++){
        uint64_t v0,v1,v2,v3,v4,v5,v6,v7;
        __asm__ volatile("ldapr %0,[%8]\n\t ldapr %1,[%9]\n\t ldapr %2,[%10]\n\t ldapr %3,[%11]\n\t ldapr %4,[%12]\n\t ldapr %5,[%13]\n\t ldapr %6,[%14]\n\t ldapr %7,[%15]"
            : "=&r"(v0),"=&r"(v1),"=&r"(v2),"=&r"(v3),"=&r"(v4),"=&r"(v5),"=&r"(v6),"=&r"(v7)
            : "r"(p0),"r"(p1),"r"(p2),"r"(p3),"r"(p4),"r"(p5),"r"(p6),"r"(p7):"memory");
        sum+=v0+v1+v2+v3+v4+v5+v6+v7;
        p0+=stride; if(p0>=b+64*1024*1024) p0=b;
        p1+=stride; if(p1>=b+128*1024*1024) p1=b+64*1024*1024;
        p2+=stride; if(p2>=b+192*1024*1024) p2=b+128*1024*1024;
        p3+=stride; if(p3>=b+256*1024*1024) p3=b+192*1024*1024;
        p4+=stride; if(p4>=b+320*1024*1024) p4=b+256*1024*1024;
        p5+=stride; if(p5>=b+384*1024*1024) p5=b+320*1024*1024;
        p6+=stride; if(p6>=b+448*1024*1024) p6=b+384*1024*1024;
        p7+=stride; if(p7>=b+512*1024*1024) p7=b+448*1024*1024;
    }
    uint64_t e=now_ns();
    volatile uint64_t sink=sum; (void)sink;
    return e-s;
}
int main(){
    size_t sz=512*1024*1024;
    void *big=mmap(NULL,sz,PROT_READ|PROT_WRITE,MAP_ANON|MAP_PRIVATE,-1,0);
    for(size_t o=0;o<sz;o+=16384) ((volatile char*)big)[o]=0x5A;
    printf("ready 512MB\n");
    size_t it=300000;
    for(int trial=0;trial<5;trial++){
        uint64_t t_plain=bench_plain(big,it);
        uint64_t t_ldapr=bench_ldapr(big,it);
        double ns_p=(double)t_plain/(it*8), ns_l=(double)t_ldapr/(it*8);
        printf("trial %d plain %.2f ns/load (%.1f ms) ldapr %.2f ns/load (%.1f ms) ratio %.3f\n", trial, ns_p, t_plain/1e6, ns_l, t_ldapr/1e6, ns_l/ns_p);
    }
    munmap(big,sz);
    return 0;
}
