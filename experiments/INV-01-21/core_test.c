/* No clock reads, nanosleep, signal delivery, notification or lifecycle action. */
#include "core.h"
#include <assert.h>
#include <stdio.h>
static m21_gate gate=M21_GATE_INIT;
static m21_record rows[2];
static _Atomic uint64_t reserved,dropped;
static uint32_t receipt_order[2];static _Atomic uint64_t receipt_count;
static pthread_mutex_t fixture_mutex=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t fixture_cond=PTHREAD_COND_INITIALIZER;
static bool first_reserved,second_done,release_first;
static void *first(void *unused){
    (void)unused;assert(m21_enter(&gate)==1);uint64_t index;assert(m21_claim(&reserved,&dropped,2,&index)&&index==0);assert(!pthread_mutex_lock(&fixture_mutex));first_reserved=true;assert(!pthread_cond_broadcast(&fixture_cond));
    while(!release_first)assert(!pthread_cond_wait(&fixture_cond,&fixture_mutex));assert(!pthread_mutex_unlock(&fixture_mutex));
    rows[0].counter=10;assert(!m21_publish(&gate,&rows[0],0,receipt_order,&receipt_count,2));assert(!m21_leave(&gate));return NULL;
}
static void *second(void *unused){
    (void)unused;assert(!pthread_mutex_lock(&fixture_mutex));while(!first_reserved)assert(!pthread_cond_wait(&fixture_cond,&fixture_mutex));assert(!pthread_mutex_unlock(&fixture_mutex));
    assert(m21_enter(&gate)==1);uint64_t index;assert(m21_claim(&reserved,&dropped,2,&index)&&index==1);rows[1].counter=20;assert(!m21_publish(&gate,&rows[1],1,receipt_order,&receipt_count,2));assert(!m21_leave(&gate));
    assert(!pthread_mutex_lock(&fixture_mutex));second_done=true;assert(!pthread_cond_broadcast(&fixture_cond));assert(!pthread_mutex_unlock(&fixture_mutex));return NULL;
}
static int calls,polls,mode;
static int fake_sleep(const struct timespec *request,struct timespec *remaining){
    assert(request->tv_sec==0);++calls;
    if(mode==1||calls==1){remaining->tv_sec=0;remaining->tv_nsec=50000000;errno=EINTR;return -1;}
    if(mode==2){remaining->tv_sec=-1;errno=EINTR;return -1;}
    if(mode==3){errno=EINVAL;return -1;}
    if(mode==4){remaining->tv_sec=1;remaining->tv_nsec=0;errno=EINTR;return -1;}
    return 0;
}
static bool fake_poll(void *unused){(void)unused;++polls;return mode!=1||polls<2;}
int main(void){
    uint64_t v;assert(m21_ns(3,2,3,&v)&&v==2);assert(!m21_ns(1,0,1,&v));assert(!m21_ns(1,1,0,&v));assert(!m21_ns(UINT64_MAX,2,1,&v));assert(!m21_elapsed(900,1000,1,1,&v));v=UINT64_MAX;assert(!m21_inc(&v));
    m21_witness w={0};m21_observe(&w,1000,1);m21_observe(&w,900,2);m21_observe(&w,1100,3);assert(w.reversals==1&&w.first_before==1000&&w.first_after==900&&w.first_before_index==1&&w.first_after_index==2);
    w.reversals=UINT64_MAX;m21_observe(&w,100,4);assert(w.error);
    pthread_t a,b;assert(!pthread_create(&a,NULL,first,NULL));assert(!pthread_create(&b,NULL,second,NULL));
    assert(!pthread_mutex_lock(&fixture_mutex));while(!second_done)assert(!pthread_cond_wait(&fixture_cond,&fixture_mutex));assert(!pthread_mutex_unlock(&fixture_mutex));
    assert(!m21_close(&gate));assert(m21_enter(&gate)==0);assert(!atomic_load_explicit(&rows[0].committed,memory_order_acquire));assert(atomic_load_explicit(&rows[1].committed,memory_order_acquire)==1);
    assert(!pthread_mutex_lock(&gate.mutex));assert(gate.active==1);assert(!pthread_mutex_unlock(&gate.mutex));
    assert(!pthread_mutex_lock(&fixture_mutex));release_first=true;assert(!pthread_cond_broadcast(&fixture_cond));assert(!pthread_mutex_unlock(&fixture_mutex));
    assert(!m21_drain(&gate));assert(!pthread_join(a,NULL));assert(!pthread_join(b,NULL));assert(atomic_load_explicit(&rows[0].committed,memory_order_acquire)==1&&rows[0].counter==10&&rows[1].counter==20);assert(m21_enter(&gate)==0);
    assert(atomic_load_explicit(&receipt_count,memory_order_acquire)==2&&receipt_order[0]==1&&receipt_order[1]==0&&rows[1].receipt_id==0&&rows[0].receipt_id==1);
    assert(!m21_claim(&reserved,&dropped,2,&v)&&atomic_load(&dropped)==1);atomic_store(&dropped,UINT64_MAX);assert(!m21_claim(&reserved,&dropped,2,&v)&&atomic_load(&dropped)==UINT64_MAX);atomic_store(&reserved,UINT64_MAX);assert(!m21_claim(&reserved,&dropped,2,&v));
    for(mode=0;mode<=4;mode++){calls=polls=0;uint64_t interrupted=0,errors=0;int result=m21_period(fake_sleep,fake_poll,NULL,&interrupted,&errors);assert(result==(mode==0?0:mode==1?1:-1));assert(interrupted>=1&&polls>=2);}
    printf("M21 core arithmetic, reversal, publication/drain and injected nanosleep cases pass; record=%zu bytes\n",sizeof(m21_record));return 0;
}
