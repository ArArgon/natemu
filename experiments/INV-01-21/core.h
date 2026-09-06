/* Shared producer ownership/arithmetic, also exercised without clocks or timers. */
#ifndef M21_CORE_H
#define M21_CORE_H
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <time.h>
#define M21_CAP 32768u
#define M21_PERIOD UINT64_C(100000000)
typedef struct {
    uint64_t seq, counter, clocks[5], actor,thread_id,receipt_id;
    int32_t requested, set_rc, get_rc, got, relative, status, value, source;
    _Atomic uint64_t committed;
} m21_record;
_Static_assert(sizeof(m21_record)<=128,"record allocation contract");
typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t drained;
    bool open;
    uint64_t active;
} m21_gate;
#define M21_GATE_INIT {PTHREAD_MUTEX_INITIALIZER,PTHREAD_COND_INITIALIZER,true,0}
typedef struct {
    uint64_t reads,last,last_index,reversals,first_before,first_after,first_before_index,first_after_index;
    bool initialized,error;
} m21_witness;
static inline bool m21_inc(uint64_t *v) { if(*v==UINT64_MAX)return false; ++*v;return true; }
static inline bool m21_atomic_inc(_Atomic uint64_t *v,uint64_t *previous) {
    uint64_t old=atomic_load(v);
    for(;;){if(old==UINT64_MAX)return false;if(atomic_compare_exchange_weak(v,&old,old+1)){*previous=old;return true;}}
}
static inline bool m21_claim(_Atomic uint64_t *reserved,_Atomic uint64_t *dropped,uint64_t capacity,uint64_t *index) {
    if(!m21_atomic_inc(reserved,index))return false;
    if(*index>=capacity){uint64_t old;m21_atomic_inc(dropped,&old);return false;}return true;
}
static inline bool m21_ns(uint64_t t,uint32_t n,uint32_t d,uint64_t *out) {
    if(!n||!d)return false;
    __uint128_t v=(__uint128_t)t*n/d;if(v>UINT64_MAX)return false;*out=(uint64_t)v;return true;
}
static inline bool m21_elapsed(uint64_t now,uint64_t before,uint32_t n,uint32_t d,uint64_t *out) {
    return now>=before&&m21_ns(now-before,n,d,out);
}
/* Called for every actual ordered actor read, BEFORE the density test. */
static inline void m21_observe(m21_witness *w,uint64_t raw,uint64_t index) {
    if(!m21_inc(&w->reads)){w->error=true;return;}
    if(w->initialized&&raw<w->last) {
        if(!w->reversals){w->first_before=w->last;w->first_after=raw;w->first_before_index=w->last_index;w->first_after_index=index;}
        if(!m21_inc(&w->reversals))w->error=true;
    }
    w->last=raw;w->last_index=index;w->initialized=true;
}
static inline int m21_enter(m21_gate *g) {
    int rc=pthread_mutex_lock(&g->mutex);if(rc)return -1;
    bool admitted=g->open;if(admitted&&!m21_inc(&g->active)){pthread_mutex_unlock(&g->mutex);return -1;}
    if(pthread_mutex_unlock(&g->mutex))return -1;return admitted?1:0;
}
static inline int m21_leave(m21_gate *g) {
    int rc=pthread_mutex_lock(&g->mutex);if(rc)return rc;
    if(!g->active){pthread_mutex_unlock(&g->mutex);return EINVAL;}
    --g->active;rc=pthread_cond_broadcast(&g->drained);int unlock=pthread_mutex_unlock(&g->mutex);return rc?rc:unlock;
}
static inline int m21_close(m21_gate *g) {
    int rc=pthread_mutex_lock(&g->mutex);if(rc)return rc;g->open=false;return pthread_mutex_unlock(&g->mutex);
}
static inline int m21_drain(m21_gate *g) {
    int rc=pthread_mutex_lock(&g->mutex);if(rc)return rc;
    while(g->active){rc=pthread_cond_wait(&g->drained,&g->mutex);if(rc){pthread_mutex_unlock(&g->mutex);return rc;}}
    return pthread_mutex_unlock(&g->mutex);
}
/* Completion receipts have their own mutex order, unrelated to reservations.
 * The index queue is auxiliary scratch; payloads remain in their fixed arrays. */
static inline int m21_publish(m21_gate *g,m21_record *row,uint32_t index,uint32_t *order,_Atomic uint64_t *count,uint64_t capacity) {
    int rc=pthread_mutex_lock(&g->mutex);if(rc)return rc;uint64_t receipt=atomic_load(count);
    if(receipt>=capacity){pthread_mutex_unlock(&g->mutex);return EOVERFLOW;}
    row->receipt_id=receipt;atomic_store_explicit(&row->committed,1,memory_order_release);
    order[receipt]=index;atomic_store_explicit(count,receipt+1,memory_order_release);
    return pthread_mutex_unlock(&g->mutex);
}
static inline bool m21_remainder(struct timespec r) {return r.tv_sec>=0&&r.tv_nsec>=0&&r.tv_nsec<1000000000;}
/* 0=completed request, 1=retry, -1=permanent/invalid outcome. */
static inline int m21_sleep_result(int rc,int error,struct timespec remainder) {
    if(!rc)return 0;return error==EINTR&&m21_remainder(remainder)?1:-1;
}
static inline int m21_period(int (*sleep_fn)(const struct timespec *,struct timespec *),bool (*poll)(void *),void *context,uint64_t *interruptions,uint64_t *errors) {
    struct timespec request={0,100000000},remaining={0};
    for(;;){
        if(!poll(context))return 1;
        int rc=sleep_fn(&request,&remaining),saved=errno;bool keep=poll(context);
        int result=m21_sleep_result(rc,saved,remaining);
        if(result==0)return 0;
        if(result<0||remaining.tv_sec>request.tv_sec||(remaining.tv_sec==request.tv_sec&&remaining.tv_nsec>request.tv_nsec)){
            m21_inc(errors);return -1;
        }
        if(!m21_inc(interruptions))return -1;if(!keep)return 1;request=remaining;
    }
}
#endif
