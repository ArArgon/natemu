/* INV-01-18 M18-2. Research instrument, never production emulator code.
 * No inherited probe implementation is used. All evidence runs require a
 * separately accepted code gate and the Python supervisor's immutable packet.
 */
#define _DARWIN_C_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <libkern/OSCacheControl.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#if !defined(__aarch64__) || !defined(__APPLE__)
#error This candidate must be built for arm64 Darwin.
#endif

enum { MAX_N=8, ACTORS=13, RING=4096, ARENA_BYTES=16*1024*1024,
       CAP_BYTES=256*1024*1024, PAIRS=10000, CAL_BATCHES=64, COST_BATCHES=1000 };
enum { BUILDING=1, PUBLISHED=2, SUPERSEDED=3, RETIRED=4, DISCARDED=5 };
typedef struct { uint64_t x, y; } Pair;
typedef struct { uint64_t version; uint32_t slot; uint16_t owner, page; uint32_t nonce; } Desc;
typedef struct { uint64_t floor; uint32_t page, stop; } Request;
_Static_assert(sizeof(Desc)==24,"reviewed immutable descriptor layout");
_Static_assert(sizeof(Request)==16,"reviewed immutable epoch layout");
typedef struct { const char *kind; uint64_t a,b,c,d,e,f; } Event;
typedef struct { Event events[RING]; _Atomic uint64_t put, get; } Trace;
typedef struct {
    unsigned id;
    pthread_t thread;
    _Atomic uint64_t req, ack, resume, hazard, calls, boot_req, boot_ack;
    _Atomic uint64_t build_req, build_done, install_done;
    _Atomic uint64_t stimulus_req, stimulus_done;
    unsigned stimulus_page;
    uint64_t epoch, floor[2];
    uint64_t pre[PAIRS], post[PAIRS];
} Worker;
typedef struct { uint32_t token; unsigned actor; } Build;
typedef struct { _Atomic uint64_t want, request, complete, restore_req, restore_done; Build job; pthread_t thread; } Producer;

static struct {
    char arm, sub[24], mode[24], signing, qos[24], layout[24], pressure[24];
    char hook[64], negative[64], cell[80], baseline[24];
    unsigned n, producers, size, rate, pages, held;
    uint64_t seed, warmup, publications, call_floor, batch;
} cfg;
static Worker workers[MAX_N];
static Producer producers[2];
static Trace traces[ACTORS];
static _Thread_local unsigned actor;
static _Atomic bool running, trace_done, failed, ready;
static _Atomic unsigned booted, owner_grant;
static _Atomic uint64_t entry[MAX_N][2], page_version[2], attempt, pending_invalidation;
static _Atomic uint64_t guest_word[2], guest_backing[2];
static _Atomic uint64_t hook_arrived, hook_released, hook_seen, waiters;
static _Atomic uint64_t writer_round;
static _Atomic uint64_t held_token, held_page, scanned_token, negative_once;
static uint64_t hook_attempt, hook_calls;
static Desc *desc;
static _Atomic unsigned char *life;
static Request *requests;
static uint32_t *slot_token;
static uint32_t *pressure_token;
static Pair *indirection;
static bool pressure_prefix;
static bool timed_stage;
static uint32_t forced_slot=UINT32_MAX;
static uint64_t setup_publications, setup_held, scratch_generations;
static uint64_t *retire_clock;
static _Atomic uint64_t last_seen[MAX_N][2];
/* Token/counter identity is 64-bit and nonwrapping. Build/slot indexes may use
 * a checked 32-bit subset only after the exact capacity preflight. */
static uint64_t next_token=1, max_token;
static uint32_t slot_count;
static uint64_t epoch, request_cap, map_version=1;
static unsigned char *arena;
static size_t host_page, stride, code_offset, data_length;
static void *data_window;
static int backing=-1;
static pthread_t collector_thread, hook_thread;
static mach_timebase_info_data_t timebase;
static void requested_qos(void);

static void relax(void) { sched_yield(); }

static void emit(const char *kind, uint64_t a,uint64_t b,uint64_t c,uint64_t d,uint64_t e,uint64_t f) {
    Trace *t=&traces[actor];
    uint64_t p=atomic_load_explicit(&t->put,memory_order_relaxed);
    while(p-atomic_load_explicit(&t->get,memory_order_acquire)>=RING) relax();
    t->events[p%RING]=(Event){kind,a,b,c,d,e,f};
    atomic_store_explicit(&t->put,p+1,memory_order_release);
}

static void die(const char *why) {
    emit("failure",0,0,0,0,0,0);
    fprintf(stderr,"INV18 invariant: %s\n",why);
    atomic_store(&failed,true);
    /* The external supervisor preserves partial stdout; never resume damage. */
    fflush(stderr);
    _exit(70);
}
static void unavailable(const char *api,int error) {
    emit("unavailable",(uint64_t)(unsigned)error,0,0,0,0,0);
    fprintf(stderr,"INV18 UNAVAILABLE %s errno=%d\n",api,error); fflush(stderr); _exit(78);
}
#define REQUIRE(test,why) do { if(!(test)) die(why); } while(0)

/* Durable, cause-specific witness, including failures before the collector
 * drains its bounded ring. stderr has no instruction-execution role. */
static void negative_trigger(unsigned code,uint64_t token,uint64_t detail) {
    uint64_t zero=0;
    if(atomic_compare_exchange_strong(&negative_once,&zero,code)) {
        fprintf(stderr,"INV18 NEGATIVE %u token=%"PRIu64" detail=%"PRIu64"\n",code,token,detail);
        fflush(stderr);
        emit("negative-trigger",code,token,detail,0,0,0);
    }
}

static uint64_t phase_start(void) { return !strcmp(cfg.mode,"cost")&&timed_stage?mach_absolute_time():0; }
static void phase_end(unsigned phase,uint64_t start) {
    if(!start) return;
    uint64_t end=mach_absolute_time();
    REQUIRE(end>=start,"backward phase clock");
    emit("cost-phase",phase,atomic_load(&writer_round),end-start,start,end,0);
}

static void *collect(void *unused) {
    (void)unused; actor=12;
    requested_qos();
    bool negative_emitted=false;
    for(;;) {
        bool drained=true;
        for(unsigned i=0;i<ACTORS;i++) {
            Trace *t=&traces[i];
            uint64_t g=atomic_load_explicit(&t->get,memory_order_relaxed);
            uint64_t p=atomic_load_explicit(&t->put,memory_order_acquire);
            while(g<p) {
                Event v=t->events[g%RING];
                if(!negative_emitted&&!strcmp(v.kind,"call")&&!strncmp(cfg.negative,"trace-",6)) {
                    negative_emitted=true;
                    fprintf(stderr,"INV18 TRACE_NEGATIVE %s actor=%u seq=%"PRIu64"\n",cfg.negative,i,g);
                    fflush(stderr);
                    if(!strcmp(cfg.negative,"trace-missing")) { ++g; continue; }
                    if(!strcmp(cfg.negative,"trace-truncated")) { fputs("{\"kind\":\"call\",\n",stdout); ++g; continue; }
                    if(!strcmp(cfg.negative,"trace-duplicate"))
                        printf("{\"kind\":\"call\",\"actor\":%u,\"seq\":%"PRIu64",\"v\":[0,0,0,0,0,0]}\n",i,g);
                }
                if(printf("{\"kind\":\"%s\",\"actor\":%u,\"seq\":%"PRIu64",\"v\":[%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64"]}\n",
                          v.kind,i,g,v.a,v.b,v.c,v.d,v.e,v.f)<0) _exit(74);
                ++g;
            }
            atomic_store_explicit(&t->get,g,memory_order_release);
            if(g!=atomic_load_explicit(&t->put,memory_order_acquire)) drained=false;
        }
        fflush(stdout);
        if(ferror(stdout)) _exit(74);
        if(atomic_load(&trace_done)&&drained) return NULL;
        relax();
    }
}

static uint64_t mix(uint64_t x) {
    x+=UINT64_C(0x9e3779b97f4a7c15);
    x=(x^(x>>30))*UINT64_C(0xbf58476d1ce4e5b9);
    x=(x^(x>>27))*UINT64_C(0x94d049bb133111eb);
    return x^(x>>31);
}
static Pair encoded_pair(uint64_t token,uint32_t nonce) {
    uint64_t input=token^cfg.seed^mix(nonce+UINT64_C(0x18dec0de));
    return (Pair){mix(input),mix(input^UINT64_C(0xa596c3f02d7841be))};
}
static Pair expected(uint64_t token) { return encoded_pair(token,desc[token].nonce); }
static bool all_fields_differ(Pair a,Pair b) {
    for(unsigned h=0;h<4;h++)
        if((uint16_t)(a.x>>(16*h))==(uint16_t)(b.x>>(16*h))||
           (uint16_t)(a.y>>(16*h))==(uint16_t)(b.y>>(16*h))) return false;
    return true;
}
static unsigned entry_owner(unsigned worker) { return cfg.arm=='V'?worker:0; }
static bool stopping(void) { return cfg.arm=='S'||cfg.arm=='M'; }

static uint64_t hook_arm_round(const char *name) {
    bool protected_hold=cfg.arm=='I'&&cfg.rate==64&&
        (!strcmp(name,"reader-after-load")||!strcmp(name,"reader-after-hazard")||
         !strcmp(name,"reader-after-validation")||!strcmp(name,"reader-in-call")||!strcmp(name,"reader-after-call"));
    return protected_hold?4:3;
}
static void hook(const char *name,unsigned identity) {
    if(!cfg.hook[0]||strcmp(cfg.hook,name)||identity!=cfg.held) return;
    /* Arm after bootstrap; one hold per child, with actual competing attempts. */
    if(atomic_load(&writer_round)<hook_arm_round(name)) return;
    uint64_t zero=0;
    if(!atomic_compare_exchange_strong(&hook_seen,&zero,1)) return;
    hook_attempt=atomic_load(&attempt); hook_calls=0;
    for(unsigned i=0;i<cfg.n;i++) hook_calls+=atomic_load(&workers[i].calls);
    emit("hook-arrive",atomic_load(&attempt),atomic_load(&writer_round),identity,
         atomic_load(&held_token),atomic_load(&held_page),0);
    atomic_store_explicit(&hook_arrived,1,memory_order_release);
    while(!atomic_load_explicit(&hook_released,memory_order_acquire)) relax();
    emit("hook-release",atomic_load(&attempt),0,0,0,0,0);
}

static void *control_hook(void *unused) {
    (void)unused; actor=11;
    requested_qos();
    while(!atomic_load_explicit(&hook_arrived,memory_order_acquire)) {
        if(!atomic_load(&running)) return NULL;
        relax();
    }
    uint64_t before=hook_attempt, calls=hook_calls;
    if(!strcmp(cfg.hook,"translator-before-check")||!strcmp(cfg.hook,"coordinator-after-check")) {
        atomic_store_explicit(&pending_invalidation,1,memory_order_release);
        emit("invalidation-attempt",before,0,0,0,0,0);
    } else if(cfg.arm=='I'&&cfg.rate==64&&
              (!strcmp(cfg.hook,"reader-after-load")||!strcmp(cfg.hook,"reader-after-hazard")||
               !strcmp(cfg.hook,"reader-after-validation")||!strcmp(cfg.hook,"reader-in-call")||
               !strcmp(cfg.hook,"reader-after-call"))) {
        uint64_t token=atomic_load(&held_token),page=atomic_load(&held_page);
        REQUIRE(token,"held hook needs nonempty entry");
        while(atomic_load(&entry[0][page])==token) relax();
        if(strcmp(cfg.hook,"reader-after-load")) {
            while(atomic_load(&scanned_token)!=token) relax();
            REQUIRE(atomic_load(&life[token])==SUPERSEDED,"protected superseded lifetime");
            emit("held-retirement",token,atomic_load(&workers[cfg.held].hazard),SUPERSEDED,page,0,0);
            if(!strcmp(cfg.negative,"early-retire")) {
                negative_trigger(6,token,atomic_load(&workers[cfg.held].hazard));
                die("negative early retirement detected before reuse");
            }
        } else emit("held-revalidation",token,atomic_load(&entry[0][page]),page,0,0,0);
    } else if(!strncmp(cfg.hook,"writer-",7)&&!stopping()&&!(cfg.arm=='V'&&cfg.n==1)) {
        /* Before publication, old-current reader progress is valid/required. */
        for(;;) {
            uint64_t now=0;
            for(unsigned i=0;i<cfg.n;i++) now+=atomic_load(&workers[i].calls);
            if(now>calls) break;
            relax();
        }
        uint64_t now=0; for(unsigned i=0;i<cfg.n;i++) now+=atomic_load(&workers[i].calls);
        emit("old-progress",calls,now,0,0,0,0);
    } else if(!strcmp(cfg.hook,"reader-before-ack")) {
        Worker *held=&workers[cfg.held];
        uint64_t request;
        do { request=atomic_load_explicit(&held->req,memory_order_acquire); relax(); }
        while(!request||request==atomic_load_explicit(&held->ack,memory_order_acquire));
        for(unsigned i=0;i<cfg.n;i++) if(i!=cfg.held)
            while(atomic_load_explicit(&workers[i].ack,memory_order_acquire)!=request) relax();
        emit("held-epoch",request,atomic_load(&held->ack),cfg.held,cfg.n-1,0,0);
    } else {
        while(atomic_load(&attempt)==before && atomic_load(&waiters)==0) relax();
        emit("competitor-attempt",before,atomic_load(&attempt),atomic_load(&waiters),0,0,0);
    }
    atomic_store_explicit(&hook_released,1,memory_order_release);
    return NULL;
}

/* A generated call uses this ordinary host helper only in deterministic-hook
 * children. The JIT prologue saves LR; no C atomic encoding is invented. */
static void held_call(void *arg) { Worker *w=arg; hook("reader-in-call",w->id); }

static uint32_t mov16(unsigned reg,unsigned half,uint16_t value) {
    return (half?UINT32_C(0xf2800000):UINT32_C(0xd2800000))|(half<<21)|((uint32_t)value<<5)|reg;
}
static int write_callback(void *context) {
    if(!context) return 0; /* Explicit normal-context restoration candidate. */
    Build *b=context;
    if(!b->token||b->token>=max_token) __builtin_trap();
    Desc d=desc[b->token];
    if(d.slot>=slot_count||cfg.size+code_offset>stride) __builtin_trap();
    uint32_t *out=(uint32_t *)(arena+(size_t)d.slot*stride+code_offset);
    Pair value=expected(b->token);
    if(!strcmp(cfg.negative,"wrong-generation")) {
        negative_trigger(1,b->token,b->token+1); value=encoded_pair(b->token+1,0);
    }
    for(unsigned j=0;j<cfg.size/4;j++) out[j]=UINT32_C(0xd503201f); /* NOP */
    unsigned pos=0;
    if(cfg.hook[0]) {
        out[pos++]=UINT32_C(0xa9bf7bfd); /* stp x29,x30,[sp,#-16]! */
        out[pos++]=UINT32_C(0xaa0003f0); /* mov x16,x0 */
        out[pos++]=UINT32_C(0xaa0103e0); /* mov x0,x1 */
        out[pos++]=UINT32_C(0xd63f0200); /* blr x16 */
        out[pos++]=UINT32_C(0xa8c17bfd); /* ldp x29,x30,[sp],#16 */
    }
    for(unsigned h=0;h<4;h++) out[pos++]=mov16(0,h,(uint16_t)(value.x>>(16*h)));
    hook("writer-partial",cfg.arm=='V'?d.owner:0);
    for(unsigned h=0;h<4;h++) out[pos++]=mov16(1,h,(uint16_t)(value.y>>(16*h)));
    out[cfg.size/4-1]=UINT32_C(0xd65f03c0); /* RET after explicit NOP padding */
    return 0;
}
PTHREAD_JIT_WRITE_ALLOW_CALLBACKS_NP(write_callback);

static void build_code(Build b) {
    if(!cfg.baseline[0]) REQUIRE(pthread_jit_write_with_callback_np(write_callback,&b)==0,"write callback");
    Desc d=desc[b.token];
    hook("writer-written",cfg.arm=='V'?d.owner:0);
    if(!cfg.baseline[0]&&strcmp(cfg.negative,"omit-cache")&&strcmp(cfg.negative,"early-cache-publication"))
        sys_icache_invalidate(arena+(size_t)d.slot*stride+code_offset,cfg.size);
    hook("writer-maintained",cfg.arm=='V'?d.owner:0);
    Pair v=expected(b.token);
    emit("built",b.token,d.slot,d.version,d.owner,v.x,v.y);
    if(!cfg.baseline[0]&&b.token<=2*cfg.n) {
        uint32_t *words=(uint32_t *)(arena+(size_t)d.slot*stride+code_offset);
        for(unsigned j=0;j<cfg.size/4;j++) emit("code-word",b.token,j,words[j],0,0,0);
    }
}

static void service_epoch(Worker *w) {
    uint64_t e=atomic_load_explicit(&w->req,memory_order_acquire);
    if(e==w->epoch) return;
    REQUIRE(e>w->epoch&&e<request_cap,"epoch identity");
    hook("reader-before-ack",w->id);
    Request r=requests[e];
    REQUIRE(r.page<2&&atomic_load(&w->hazard)==0,"ack with live hazard");
    w->floor[r.page]=r.floor; w->epoch=e;
    emit("ack",e,r.page,r.floor,0,0,0);
    bool lost=w->id==cfg.held&&!strcmp(cfg.negative,"lost-ack");
    bool stale=w->id==cfg.held&&!strcmp(cfg.negative,"stale-ack");
    if(lost) negative_trigger(8,e,w->id);
    if(stale) negative_trigger(4,e,e-1);
    if(!lost) atomic_store_explicit(&w->ack,stale?e-1:e,memory_order_release);
    hook("reader-after-ack",w->id);
    if(r.stop) {
        atomic_fetch_add(&waiters,1);
        while(atomic_load_explicit(&w->resume,memory_order_acquire)!=e) relax();
        atomic_fetch_sub(&waiters,1);
        if(cfg.arm=='M'&&!strcmp(cfg.sub,"jit-protect")) {
            REQUIRE(pthread_jit_write_with_callback_np(write_callback,NULL)==0,"restore callback");
            emit("restore-thread",e,w->id,0,0,0,0);
        }
        emit("resume",e,0,0,0,0,0);
    }
}

static void clock_pairs(uint64_t *pairs) {
    for(unsigned j=0;j<PAIRS;j++) {
        uint64_t a=mach_absolute_time(),b=mach_absolute_time();
        REQUIRE(b>=a,"backward clock"); pairs[j]=b-a;
    }
}

static void requested_qos(void) {
    qos_class_t requested=!strcmp(cfg.qos,"background")?QOS_CLASS_BACKGROUND:QOS_CLASS_DEFAULT;
    REQUIRE(pthread_set_qos_class_self_np(requested,0)==0,"participant qos");
    int relative=0; qos_class_t actual=QOS_CLASS_UNSPECIFIED;
    REQUIRE(pthread_get_qos_class_np(pthread_self(),&actual,&relative)==0,"read returned qos");
    emit("qos",requested,actual,(uint64_t)(int64_t)relative,0,0,0);
}

static void *reader(void *arg) {
    Worker *w=arg; actor=w->id+1;
    requested_qos();
    if(!strcmp(cfg.mode,"cost")) clock_pairs(w->pre);
    atomic_fetch_add(&booted,1);
    while(!atomic_load_explicit(&ready,memory_order_acquire)) {
        uint64_t kick=atomic_load_explicit(&w->boot_req,memory_order_acquire);
        atomic_store_explicit(&w->boot_ack,kick,memory_order_release);
        relax();
    }
    unsigned page=0;
    while(atomic_load(&running)) {
        service_epoch(w);
        uint64_t stimulus=atomic_load_explicit(&w->stimulus_req,memory_order_acquire);
        if(stimulus>atomic_load(&w->stimulus_done)) {
            unsigned target=w->stimulus_page;
            uint64_t prior=atomic_fetch_add(&guest_word[target],1);
            emit("stimulus-worker",stimulus,target,prior,prior+1,w->id,0);
            atomic_store_explicit(&w->stimulus_done,stimulus,memory_order_release);
        }
        uint64_t job=atomic_load_explicit(&w->build_req,memory_order_acquire);
        if(cfg.arm=='V'&&job>atomic_load(&w->build_done)) {
            build_code((Build){(uint32_t)job,actor});
            atomic_store_explicit(&w->build_done,job,memory_order_release);
            while(atomic_load_explicit(&w->install_done,memory_order_acquire)<job) {
                service_epoch(w); relax();
            }
        }
        hook("reader-before-load",w->id);
        service_epoch(w); /* A hold before load may have acquired a new STOP. */
        uint64_t token=atomic_load(&entry[entry_owner(w->id)][page]);
        if(cfg.hook[0]&&w->id==cfg.held&&token) {
            atomic_store(&held_token,token); atomic_store(&held_page,page);
        }
        if(cfg.arm=='I') {
            if(token) hook("reader-after-load",w->id);
            atomic_store(&w->hazard,token);
            /* Hook-only schedule admission: establish that this advertised
             * token is still current before demanding delayed retirement.
             * A stale pre-advertisement load must instead take the ordinary
             * revalidation retry; it is not a protected-token witness. */
            if(token&&(!cfg.hook[0]||token==atomic_load(&entry[0][page]))) hook("reader-after-hazard",w->id);
            if(token!=atomic_load(&entry[0][page])) {
                emit("retry",token,page,0,0,0,0); atomic_store(&w->hazard,0); continue;
            }
            if(token) hook("reader-after-validation",w->id);
        }
        if(!token) { atomic_store(&w->hazard,0); page^=1; relax(); continue; }
        REQUIRE(token<max_token,"unknown token");
        Desc d=desc[token];
        REQUIRE(d.page==page&&d.version>=w->floor[page],"stale admitted entry");
        unsigned char state=atomic_load_explicit(&life[token],memory_order_acquire);
        REQUIRE(state==PUBLISHED||state==SUPERSEDED,"retired/building admission");
        Pair want=expected((uint32_t)token);
        emit("admit",token,state,w->epoch,page,w->floor[page],atomic_load(&w->hazard));
        if(strcmp(cfg.negative,"omit-isb")) __asm__ volatile("isb" ::: "memory");
        typedef Pair (*Fn)(void (*)(void *),void *);
        Pair got=!strcmp(cfg.baseline,"data-indirection")?indirection[token]:
                 (cfg.baseline[0]?want:((Fn)(arena+(size_t)d.slot*stride+code_offset))(held_call,w));
        if(!strcmp(cfg.negative,"corrupt-return")) { negative_trigger(2,token,1); got.y^=1; }
        uint64_t observed_map=cfg.arm=='M'?*(volatile uint64_t *)data_window:0;
        emit("call",token,d.version,got.x,got.y,observed_map,d.slot);
        if(cfg.arm=='M') emit("mapping-witness",token,map_version,observed_map,w->epoch,0,0);
        REQUIRE(got.x==want.x&&got.y==want.y,"returned pair");
        REQUIRE(cfg.arm!='M'||observed_map==map_version,"mapping contents");
        hook("reader-after-call",w->id);
        atomic_store(&w->hazard,0);
        atomic_store_explicit(&last_seen[w->id][page],token,memory_order_release);
        atomic_fetch_add_explicit(&w->calls,1,memory_order_release);
        page^=1;
    }
    if(!strcmp(cfg.mode,"cost")) {
        clock_pairs(w->post);
        for(unsigned j=0;j<PAIRS;j++) emit("worker-pair",j,w->pre[j],w->post[j],0,0,0);
    }
    emit("worker-done",atomic_load(&w->calls),w->epoch,0,0,0,0);
    return NULL;
}

static void producer_restore(Producer *producer) {
    uint64_t requested=atomic_load_explicit(&producer->restore_req,memory_order_acquire);
    if(requested!=atomic_load(&producer->restore_done)) {
        REQUIRE(pthread_jit_write_with_callback_np(write_callback,NULL)==0,"producer restore callback");
        emit("restore-thread",requested,actor,1,0,0,0);
        atomic_store_explicit(&producer->restore_done,requested,memory_order_release);
    }
}
static void *producer(void *arg) {
    unsigned p=(unsigned)(uintptr_t)arg; actor=9+p;
    requested_qos();
    uint64_t ticket=1;
    while(atomic_load(&running)) {
        producer_restore(&producers[p]);
        emit("producer-request",p,ticket,0,0,0,0);
        atomic_store_explicit(&producers[p].want,ticket,memory_order_release);
        hook("producer-before-grant",p);
        while(atomic_load_explicit(&producers[p].request,memory_order_acquire)!=ticket) {
            if(!atomic_load(&running)) return NULL;
            producer_restore(&producers[p]);
            relax();
        }
        REQUIRE(atomic_load(&owner_grant)==p+1,"producer grant mismatch");
        Build b=producers[p].job;
        build_code(b);
        atomic_store_explicit(&producers[p].complete,ticket,memory_order_release);
        ++ticket;
    }
    return NULL;
}

static uint64_t request_epoch(unsigned page,uint64_t floor,bool stop) {
    uint64_t start=phase_start();
    REQUIRE(epoch+1<request_cap&&epoch!=UINT64_MAX,"epoch exhaustion");
    uint64_t e=++epoch;
    requests[e]=(Request){floor,page,stop};
    emit("request",e,page,floor,stop,0,0);
    for(unsigned i=0;i<cfg.n;i++) atomic_store_explicit(&workers[i].req,e,memory_order_release);
    for(unsigned i=0;i<cfg.n;i++) {
        while(atomic_load_explicit(&workers[i].ack,memory_order_acquire)!=e) {
            atomic_fetch_add(&attempt,1); relax();
        }
    }
    emit("all-ack",e,page,floor,stop,0,0); phase_end(2,start); return e;
}
static void resume_all(uint64_t e) {
    uint64_t counts[MAX_N]={0},start=phase_start();
    for(unsigned i=0;i<cfg.n;i++) counts[i]=atomic_load(&workers[i].calls);
    for(unsigned i=0;i<cfg.n;i++) atomic_store_explicit(&workers[i].resume,e,memory_order_release);
    emit("resume-issued",e,0,0,0,0,0);
    if(start) {
        for(unsigned i=0;i<cfg.n;i++) while(atomic_load_explicit(&workers[i].calls,memory_order_acquire)==counts[i]) relax();
        phase_end(4,start);
    }
}

static bool still_current(uint32_t t) {
    for(unsigned i=0;i<(cfg.arm=='V'?cfg.n:1);i++)
        for(unsigned p=0;p<2;p++) if(atomic_load(&entry[i][p])==t) return true;
    return false;
}
static bool retire(uint32_t t) {
    if(!t||atomic_load(&life[t])==RETIRED) return true;
    if(atomic_load(&life[t])==BUILDING||atomic_load(&life[t])==DISCARDED) return false;
    if(still_current(t)) return false;
    if(!retire_clock[desc[t].slot]) retire_clock[desc[t].slot]=phase_start();
    uint64_t mask=0;
    if(cfg.arm=='I') for(unsigned i=0;i<cfg.n;i++)
        if(atomic_load(&workers[i].hazard)==t) mask|=UINT64_C(1)<<i;
    atomic_fetch_add(&attempt,1);
    emit("scan",t,mask,epoch,0,0,0);
    if(mask) {
        if(t==atomic_load(&held_token)) atomic_store(&scanned_token,t);
        return false;
    }
    atomic_store_explicit(&life[t],RETIRED,memory_order_release);
    phase_end(5,retire_clock[desc[t].slot]); retire_clock[desc[t].slot]=0;
    emit("retire",t,desc[t].slot,epoch,0,0,0); return true;
}

static void invalidate(unsigned page) {
    uint64_t unaffected=atomic_load(&page_version[page^1]);
    uint64_t v=atomic_load(&page_version[page]);
    REQUIRE(v!=UINT64_MAX,"page version wrap");
    atomic_store(&page_version[page],++v);
    uint32_t old[MAX_N]={0};
    for(unsigned i=0;i<(cfg.arm=='V'?cfg.n:1);i++) {
        old[i]=(uint32_t)atomic_exchange(&entry[i][page],0);
        if(old[i]) atomic_store(&life[old[i]],SUPERSEDED);
    }
    emit("invalidate-start",page,v,unaffected,0,0,0);
    uint64_t e=request_epoch(page,v,stopping());
    for(unsigned i=0;i<(cfg.arm=='V'?cfg.n:1);i++) REQUIRE(retire(old[i]),"invalidation drain");
    REQUIRE(atomic_load(&page_version[page^1])==unaffected,"unrelated page version changed");
    emit("invalidate-complete",page,v,e,unaffected,atomic_load(&page_version[page^1]),0);
    if(stopping()) resume_all(e);
}

static void guest_stimulus(uint64_t sequence,unsigned page) {
    unsigned type=(unsigned)((sequence-1)%4);
    unsigned target=type==3?(page^1):page;
    uint64_t before=atomic_load(&guest_backing[target]);
    unsigned writer=0;
    if(type==1) {
        writer=(unsigned)((sequence-1)%cfg.n)+1;
        Worker *w=&workers[writer-1];
        w->stimulus_page=target;
        atomic_store_explicit(&w->stimulus_req,sequence,memory_order_release);
        while(atomic_load_explicit(&w->stimulus_done,memory_order_acquire)!=sequence) relax();
    } else if(type==2) {
        REQUIRE(before!=UINT64_MAX,"guest backing identity wrap");
        atomic_store(&guest_backing[target],before+1);
    } else atomic_fetch_add(&guest_word[target],1);
    emit("guest-stimulus",sequence,type,target,before,atomic_load(&guest_backing[target]),writer);
    invalidate(target);
}

static uint32_t allocate_desc(unsigned page,unsigned worker) {
    REQUIRE(next_token<max_token,"immutable metadata capacity");
    uint32_t slot=UINT32_MAX;
    if(forced_slot!=UINT32_MAX) { slot=forced_slot; forced_slot=UINT32_MAX; }
    else if(stopping()) slot=page;
    else for(;;) {
        for(uint32_t s=0;s<slot_count;s++) {
            uint32_t t=slot_token[s];
            if(t==UINT32_MAX) continue; /* Deliberately retained pressure slot. */
            if(!t||atomic_load(&life[t])==RETIRED||(cfg.arm=='I'&&retire(t))) { slot=s; break; }
        }
        if(slot!=UINT32_MAX) break;
        emit("backpressure",next_token,slot_count,0,0,0,0);
        if(!strcmp(cfg.pressure,"near-full")) {
            for(uint32_t s=0;s<slot_count;s++) if(pressure_token[s]) {
                uint32_t held=pressure_token[s];
                REQUIRE(atomic_load(&life[held])==RETIRED,"pressure release of execution right");
                slot_token[s]=held; pressure_token[s]=0;
                emit("pressure-release",held,s,next_token,0,0,0); break;
            }
        }
        relax();
    }
    uint32_t old=slot_token[slot];
    if(old&&old!=UINT32_MAX) {
        REQUIRE(atomic_load(&life[old])==RETIRED,"overwrite nonretired code");
        hook("writer-before-reuse",0);
        emit("reuse",old,next_token,slot,0,0,0);
        if(!strcmp(cfg.negative,"aba")) { negative_trigger(7,old,next_token); die("negative reused address has a different token"); }
    }
    REQUIRE(next_token<UINT32_MAX,"bounded descriptor index");
    uint32_t t=(uint32_t)next_token++;
    desc[t]=(Desc){atomic_load(&page_version[page]),slot,(uint16_t)worker,(uint16_t)page,0};
    if(old) {
        Pair prior=expected(old);
        while(!all_fields_differ(prior,expected(t))) {
            REQUIRE(desc[t].nonce!=UINT32_MAX,"generation nonce exhausted");
            ++desc[t].nonce;
        }
    }
    atomic_store(&life[t],BUILDING); slot_token[slot]=t;
    if(indirection) indirection[t]=expected(t);
    emit("descriptor",t,slot,desc[t].version,worker,page,desc[t].nonce); return t;
}

static void mapping_change(void) {
    REQUIRE(data_window&&data_length,"owned mapping");
    if(cfg.baseline[0]) return;
    uint64_t start=phase_start();
    if(!strcmp(cfg.sub,"data-map")) {
        map_version=map_version==1?2:1;
        off_t offset=(off_t)((map_version-1)*data_length);
        errno=0; void *r=mmap(data_window,data_length,PROT_READ,MAP_PRIVATE|MAP_FIXED,backing,offset);
        emit("mapping",1,(uintptr_t)data_window,data_length,map_version,r==data_window,errno);
        emit("mapping-offset",(uint64_t)offset,map_version,data_length,(uintptr_t)r,errno,0);
        if(r!=data_window) unavailable("MAP_FIXED",errno);
    } else {
        void *target=!strcmp(cfg.sub,"jit-protect")?(void *)arena:data_window;
        size_t length=target==(void *)arena?ARENA_BYTES:data_length;
        int original=target==(void *)arena?(PROT_READ|PROT_WRITE|PROT_EXEC):PROT_READ;
        errno=0; int a=mprotect(target,length,PROT_NONE),ea=errno;
        errno=0; int b=a? -1:mprotect(target,length,original),eb=errno;
        emit("mapping",target==(void *)arena?3:2,(uintptr_t)target,length,a==0,b==0,((uint64_t)(uint32_t)ea<<32)|(uint32_t)eb);
        if(a||b) unavailable("mprotect roundtrip",a?ea:eb);
        if(target==(void *)arena) {
            REQUIRE(pthread_jit_write_with_callback_np(write_callback,NULL)==0,"coordinator restore");
            emit("restore-thread",epoch,0,1,0,0,0);
            if(cfg.producers==2) {
                for(unsigned p=0;p<2;p++) atomic_store_explicit(&producers[p].restore_req,epoch,memory_order_release);
                for(unsigned p=0;p<2;p++) while(atomic_load_explicit(&producers[p].restore_done,memory_order_acquire)!=epoch) relax();
            }
            sys_icache_invalidate(arena,ARENA_BYTES);
        }
    }
    phase_end(3,start);
}

static void publish_one(unsigned page,unsigned worker,uint64_t iteration) {
    uint64_t start=phase_start();
    uint64_t mutation_start=0;
    uint32_t old=(uint32_t)atomic_load(&entry[entry_owner(worker)][page]);
    uint64_t stop_epoch=0;
    if(stopping()) {
        stop_epoch=request_epoch(page,atomic_load(&page_version[page]),true);
        mutation_start=phase_start();
        hook("writer-quiescent",0);
        if(old) { atomic_store(&entry[0][page],0); REQUIRE(retire(old),"quiescent retirement"); }
    }
    uint32_t t=allocate_desc(page,worker);
    if(!strcmp(cfg.pressure,"near-full")&&!stopping()) {
        uint32_t free_slots=0;
        for(uint32_t s=0;s<slot_count;s++) {
            uint32_t prior=slot_token[s];
            if(!prior||(prior!=UINT32_MAX&&atomic_load(&life[prior])==RETIRED)) ++free_slots;
        }
        emit("pressure-capacity",t,free_slots,slot_count,0,0,0);
    }
    if(cfg.arm=='V') {
        Worker *w=&workers[worker];
        uint64_t writer_start=phase_start();
        atomic_store_explicit(&w->build_req,t,memory_order_release);
        while(atomic_load_explicit(&w->build_done,memory_order_acquire)!=t) { atomic_fetch_add(&attempt,1); relax(); }
        phase_end(8,writer_start);
    } else if(cfg.producers==2) {
        unsigned p=(unsigned)(iteration%2);
        for(unsigned q=0;q<2;q++) while(atomic_load_explicit(&producers[q].want,memory_order_acquire)<=atomic_load(&producers[q].complete)) {
            atomic_fetch_add(&attempt,1); relax();
        }
        uint64_t writer_start=phase_start();
        REQUIRE(atomic_exchange(&owner_grant,p+1)==0,"double writer grant");
        if(!strcmp(cfg.negative,"double-grant")) {
            unsigned unowned=0;
            bool accepted=atomic_compare_exchange_strong(&owner_grant,&unowned,2-p);
            REQUIRE(!accepted&&unowned==p+1,"negative grant precondition");
            negative_trigger(5,t,p+1); die("negative second grant detected");
        }
        producers[p].job=(Build){t,9+p};
        uint64_t job=atomic_load_explicit(&producers[p].want,memory_order_acquire);
        emit("grant",p,job,t,0,0,0);
        atomic_store_explicit(&producers[p].request,job,memory_order_release);
        while(atomic_load_explicit(&producers[p].complete,memory_order_acquire)!=job) { atomic_fetch_add(&attempt,1); relax(); }
        phase_end(8,writer_start);
        REQUIRE(atomic_exchange(&owner_grant,0)==p+1,"grant completion");
        emit("grant-complete",p,job,t,0,0,0);
    } else {
        uint64_t writer_start=phase_start();
        build_code((Build){t,0}); phase_end(8,writer_start);
    }
    hook("translator-before-check",0);
    if(atomic_exchange(&pending_invalidation,0)) {
        if(stop_epoch) resume_all(stop_epoch);
        invalidate(page);
        emit("translation-rejected",t,desc[t].version,atomic_load(&page_version[page]),0,0,0);
        atomic_store(&life[t],RETIRED);
        if(cfg.arm=='V') atomic_store_explicit(&workers[worker].install_done,t,memory_order_release);
        publish_one(page,worker,iteration); return;
    }
    REQUIRE(desc[t].version==atomic_load(&page_version[page]),"final version check");
    hook("coordinator-after-check",0);
    if(!strcmp(cfg.negative,"stale-translation")||!strcmp(cfg.negative,"unowned-install")) {
        /* Candidate bytes remain unpublished. Execute the competing complete
         * invalidation, then test the now-forbidden delayed installation before
         * any unsafe entry store. No fabricated stale version or native UAF. */
        invalidate(page);
        REQUIRE(desc[t].version<atomic_load(&page_version[page]),"negative invalidation precondition");
        negative_trigger(!strcmp(cfg.negative,"stale-translation")?3:9,t,atomic_load(&page_version[page]));
        die("negative stale/unowned install rejected");
    }
    if(cfg.arm=='M'&&!pressure_prefix) mapping_change();
    phase_end(7,mutation_start); /* all-ACK through complete mutation/cache */
    atomic_store_explicit(&life[t],PUBLISHED,memory_order_release);
    uint64_t reader_ready=phase_start();
    atomic_store(&entry[entry_owner(worker)][page],t);
    if(!strcmp(cfg.negative,"early-cache-publication"))
        sys_icache_invalidate(arena+(size_t)desc[t].slot*stride+code_offset,cfg.size);
    if(old&&atomic_load(&life[old])!=RETIRED) atomic_store(&life[old],SUPERSEDED);
    emit("publish",t,page,worker,old,desc[t].version,0);
    phase_end(1,start);
    atomic_fetch_add(&attempt,1);
    hook("writer-published",0);
    if(cfg.arm=='V') {
        REQUIRE(retire(old),"V old call must finish before build");
        atomic_store_explicit(&workers[worker].install_done,t,memory_order_release);
    } else if(cfg.arm=='E') {
        request_epoch(page,desc[t].version,false); REQUIRE(retire(old),"epoch retirement");
    } else if(cfg.arm=='I') (void)retire(old);
    if(stop_epoch) { hook("writer-mutated",0); resume_all(stop_epoch); }
    if(reader_ready) {
        for(unsigned i=0;i<cfg.n;i++) {
            if(cfg.arm=='V'&&i!=worker) continue;
            while(atomic_load_explicit(&last_seen[i][page],memory_order_acquire)!=t) relax();
        }
        phase_end(6,reader_ready);
    }
    if(atomic_exchange(&pending_invalidation,0)) invalidate(page);
}

static uint64_t delta(uint64_t a,uint64_t b) { REQUIRE(b>=a,"backwards interval"); return b-a; }
static void boot_handshake(void) {
    static uint64_t kick;
    ++kick;
    for(unsigned i=0;i<cfg.n;i++) atomic_store_explicit(&workers[i].boot_req,kick,memory_order_release);
    for(unsigned i=0;i<cfg.n;i++) while(atomic_load_explicit(&workers[i].boot_ack,memory_order_acquire)!=kick) relax();
}
static int compare_u64(const void *a,const void *b) { uint64_t x=*(const uint64_t *)a,y=*(const uint64_t *)b; return (x>y)-(x<y); }
static uint64_t calibration(uint64_t pre[PAIRS]) {
    uint64_t sorted[PAIRS]; memcpy(sorted,pre,sizeof sorted); qsort(sorted,PAIRS,sizeof *sorted,compare_u64);
    REQUIRE(sorted[9899]<=UINT64_MAX/20,"threshold overflow");
    uint64_t floor=sorted[9899]*20; if(floor<100) floor=100;
    static const unsigned sizes[]={1,16,256,4096}; uint64_t selected=0;
    for(unsigned s=0;s<4;s++) {
        bool qualifies=sorted[PAIRS-1]!=0;
        for(unsigned b=0;b<CAL_BATCHES;b++) {
            uint64_t begin=mach_absolute_time();
            for(unsigned j=0;j<sizes[s];j++) boot_handshake();
            uint64_t elapsed=delta(begin,mach_absolute_time());
            emit("calibration",sizes[s],b,elapsed,floor,0,0);
            qualifies=qualifies&&elapsed>floor;
        }
        if(qualifies&&!selected) selected=sizes[s];
    }
    return selected;
}

static bool pressure_member(uint32_t slot) {
    uint32_t live=2*(cfg.arm=='V'?cfg.n:1);
    return strcmp(cfg.pressure,"normal")&&slot>=live+1&&
           (!strcmp(cfg.pressure,"near-full")||(slot&1));
}
static void witnessed_calls(uint32_t token,unsigned owner) {
    unsigned page=desc[token].page;
    for(unsigned i=0;i<cfg.n;i++) {
        if(cfg.arm=='V'&&i!=owner) continue;
        while(atomic_load_explicit(&last_seen[i][page],memory_order_acquire)!=token) relax();
        emit("setup-call-witness",token,i,page,atomic_load(&workers[i].calls),0,0);
    }
}
static void hold_retired(uint32_t token,unsigned origin) {
    REQUIRE(atomic_load(&life[token])==RETIRED&&!still_current(token),"pressure genuine retirement");
    uint32_t slot=desc[token].slot;
    REQUIRE(slot_token[slot]==token&&!pressure_token[slot],"pressure slot identity");
    slot_token[slot]=UINT32_MAX; pressure_token[slot]=token;
    emit("pressure-hold",token,slot,origin,0,0,0);
}
static uint32_t free_slots(void) {
    uint32_t count=0;
    for(uint32_t s=0;s<slot_count;s++) {
        uint32_t t=slot_token[s];
        if(!t||(t!=UINT32_MAX&&atomic_load(&life[t])==RETIRED)) ++count;
    }
    return count;
}
static void reserve_pressure_slots(void) {
    if(!strcmp(cfg.pressure,"normal")) return;
    pressure_prefix=true;
    emit("stage",1,0,0,0,0,0); /* genuine published-retired pressure setup */
    for(uint32_t s=0;s<slot_count;s++) {
        if(!pressure_member(s)) continue;
        unsigned owner=cfg.arm=='V'?s%cfg.n:0;
        forced_slot=s;
        publish_one(0,owner,setup_publications);
        uint32_t t=(uint32_t)atomic_load(&entry[entry_owner(owner)][0]);
        REQUIRE(desc[t].slot==s,"pressure constructed slot");
        witnessed_calls(t,owner);
        invalidate(0); /* actual removal, exact ACKs, arm-specific retirement */
        hold_retired(t,0); ++setup_held; ++setup_publications;
    }
    emit("stage",2,setup_held,setup_publications,epoch,0,0); /* bootstrap */
    pressure_prefix=false;
}
static uint32_t scratch_reserve(uint32_t slot,unsigned ordinal) {
    REQUIRE(slot_token[slot]==0&&free_slots()==1,"scratch last free precondition");
    forced_slot=slot;
    uint32_t t=allocate_desc(0,0);
    ++scratch_generations;
    emit("scratch-reserve",t,slot,ordinal,free_slots(),0,0);
    REQUIRE(free_slots()==0,"scratch no-free attempt");
    emit("allocator-reject",t,slot,0,1,0,0); /* cause 1: scratch + retained capacity */
    return t;
}
static void scratch_discard(uint32_t token) {
    uint32_t slot=desc[token].slot;
    REQUIRE(atomic_load(&life[token])==BUILDING&&slot_token[slot]==token&&!still_current(token),"scratch acquired execution right");
    atomic_store(&life[token],DISCARDED); slot_token[slot]=0;
    emit("scratch-discard",token,slot,0,0,0,0);
}
static void pressure_diagnostic(void) {
    if(!strcmp(cfg.pressure,"normal")) return;
    if(!strcmp(cfg.pressure,"near-full")) {
        pressure_prefix=true; emit("stage",3,0,0,0,0,0); /* allocator-only */
        uint32_t slot=2*(cfg.arm=='V'?cfg.n:1);
        REQUIRE(free_slots()==1,"near-full initial free count");
        uint32_t first=scratch_reserve(slot,1); scratch_discard(first);
        uint32_t second=scratch_reserve(slot,2);
        emit("scratch-reuse",first,second,slot,0,0,0);
        if(!stopping()) {
            uint32_t released=0;
            for(uint32_t s=0;s<slot_count;s++) if(pressure_token[s]) { released=pressure_token[s]; break; }
            REQUIRE(released,"genuine retained slot missing");
            unsigned owner=cfg.arm=='V'?desc[released].owner:0;
            uint32_t old=(uint32_t)atomic_load(&entry[entry_owner(owner)][0]);
            emit("stage",4,released,old,owner,second,0); /* actual replacement */
            /* Ordinary allocator sees no free slot and releases one H member
             * before choosing it. The scratch reservation remains BUILDING. */
            publish_one(0,owner,setup_publications);
            uint32_t replacement=(uint32_t)atomic_load(&entry[entry_owner(owner)][0]);
            REQUIRE(desc[replacement].slot==desc[released].slot,"pressure release/reuse identity");
            witnessed_calls(replacement,owner);
            while(!retire(old)) relax();
            hold_retired(old,1); ++setup_publications;
            emit("pressure-swap",released,replacement,old,owner,second,0);
        } else emit("pressure-inplace",cfg.arm,second,0,0,0,0);
        scratch_discard(second);
        REQUIRE(free_slots()==1,"near-full restored free count");
        pressure_prefix=false;
    }
    uint32_t held=0;
    for(uint32_t s=0;s<slot_count;s++) {
        uint32_t t=slot_token[s]==UINT32_MAX?pressure_token[s]:slot_token[s];
        held+=pressure_token[s]!=0;
        emit("pressure-layout",s,t,pressure_token[s]!=0,t?atomic_load(&life[t]):0,0,0);
    }
    REQUIRE(held==setup_held,"retained-set swap count");
    emit("setup-summary",setup_held,setup_publications,scratch_generations,epoch,free_slots(),0);
    emit("stage",5,0,0,0,0,0); /* unchanged warmup, then separately tagged stress */
}

static void copy_option(char *destination,size_t capacity,const char *value) {
    if(strlen(value)>=capacity) exit(64);
    strcpy(destination,value);
}
static uint64_t parse_number(const char *value) {
    char *end=NULL;
    if(*value<'0'||*value>'9') exit(64);
    errno=0; unsigned long long number=strtoull(value,&end,10);
    if(errno||!end||*end) exit(64);
    return (uint64_t)number;
}
static unsigned parse_unsigned(const char *value) {
    uint64_t number=parse_number(value);
    if(number>UINT_MAX) exit(64);
    return (unsigned)number;
}
static __attribute__((noinline)) uint64_t unrelated_aot(volatile uint64_t *value) {
    return *value;
}

static int protection_control(void) {
    /* A sacrificial process before *any* thread creation; no fault handler
     * returns to modified code. The parent checks durable arm + exact status. */
    bool jit=!strcmp(cfg.sub,"jit-protect");
    size_t length=jit?ARENA_BYTES:data_length*2;
    int permissions=PROT_READ|PROT_WRITE|(jit?PROT_EXEC:0);
    unsigned char *region=mmap(NULL,length,permissions,MAP_PRIVATE|MAP_ANON|(jit?MAP_JIT:0),-1,0);
    if(region==MAP_FAILED) { fprintf(stderr,"INV18 UNAVAILABLE control mmap errno=%d\n",errno); return 78; }
    if(jit) {
        arena=region; max_token=2; desc=calloc(2,sizeof *desc);
        if(!desc) return 71;
        desc[1]=(Desc){1,0,0,0,0};
        Build b={1,0};
        if(pthread_jit_write_with_callback_np(write_callback,&b)) return 70;
        sys_icache_invalidate(region+code_offset,cfg.size);
    } else {
        *(uint64_t *)region=UINT64_C(0x1864);
        *(uint64_t *)(region+data_length)=UINT64_C(0x1864);
    }
    size_t protected_bytes=jit?length:data_length;
    bool restored=!strcmp(cfg.negative,"protection-restored");
    bool neighbor=!strcmp(cfg.negative,"protection-neighbor");
    volatile uint64_t accessible_data=UINT64_C(0x1864);
    volatile uint64_t *accessible=jit?&accessible_data:(volatile uint64_t *)(region+data_length);
    uintptr_t fn_address=(uintptr_t)unrelated_aot, data_address=(uintptr_t)accessible;
    if(jit&&((fn_address>=(uintptr_t)region&&fn_address<(uintptr_t)region+length)||
             (data_address>=(uintptr_t)region&&data_address<(uintptr_t)region+length))) return 70;
    if(neighbor) {
        uint64_t before=unrelated_aot(accessible);
        printf("{\"kind\":\"protection-access\",\"phase\":\"before\",\"function\":%"PRIuPTR",\"data\":%"PRIuPTR",\"value\":%"PRIu64"}\n",fn_address,data_address,before);
        if(before!=UINT64_C(0x1864)) return 70;
    }
    errno=0; int protect_result=mprotect(region,protected_bytes,PROT_NONE), protect_error=errno;
    printf("{\"kind\":\"protection-api\",\"phase\":\"protect\",\"address\":%"PRIuPTR",\"length\":%zu,\"result\":%d,\"errno\":%d}\n",
           (uintptr_t)region,protected_bytes,protect_result,protect_error);
    if(protect_result) return 78;
    if(restored) {
        errno=0; int restore_result=mprotect(region,protected_bytes,permissions),restore_error=errno;
        printf("{\"kind\":\"protection-api\",\"phase\":\"restore\",\"address\":%"PRIuPTR",\"length\":%zu,\"result\":%d,\"errno\":%d}\n",
               (uintptr_t)region,protected_bytes,restore_result,restore_error);
        if(restore_result) return 78;
        if(jit) {
            int callback_result=pthread_jit_write_with_callback_np(write_callback,NULL);
            printf("{\"kind\":\"protection-restore-callback\",\"result\":%d}\n",callback_result);
            if(callback_result) return 70;
            sys_icache_invalidate(region+code_offset,cfg.size);
        }
    }
    printf("{\"kind\":\"protection-arm\",\"control\":\"%s\",\"jit\":%s,\"address\":%"PRIuPTR",\"length\":%zu,\"owned_length\":%zu,\"accessible_control\":\"%s\"}\n",
           cfg.negative,jit?"true":"false",(uintptr_t)region,protected_bytes,length,
           jit?"unrelated-AOT-and-data-not-adjacent":"adjacent-owned-data-page");
    fflush(stdout); if(fsync(STDOUT_FILENO)) return 74;
    uint64_t value;
    if(jit&&!neighbor) {
        __asm__ volatile("isb" ::: "memory");
        Pair answer=((Pair (*)(void (*)(void *),void *))(region+code_offset))(NULL,NULL);
        Pair want=expected(1);
        printf("{\"kind\":\"protection-jit-return\",\"x\":%"PRIu64",\"y\":%"PRIu64"}\n",answer.x,answer.y);
        if(answer.x!=want.x||answer.y!=want.y) return 70;
        value=UINT64_C(0x1864);
    } else if(neighbor) {
        value=unrelated_aot(accessible);
        printf("{\"kind\":\"protection-access\",\"phase\":\"after\",\"function\":%"PRIuPTR",\"data\":%"PRIuPTR",\"value\":%"PRIu64"}\n",fn_address,data_address,value);
    } else value=*(volatile uint64_t *)region;
    printf("{\"kind\":\"protection-complete\",\"value\":%"PRIu64"}\n",value);
    fflush(stdout); if(fsync(STDOUT_FILENO)) return 74;
    return value==UINT64_C(0x1864)?0:70;
}
static void parse(int argc,char **argv) {
    cfg.n=1; cfg.producers=1; cfg.size=256; cfg.rate=1; cfg.pages=1; cfg.signing='A';
    cfg.arm='I'; cfg.seed=18001; cfg.warmup=1024; cfg.publications=4096; cfg.call_floor=16384;
    strcpy(cfg.mode,"correctness"); strcpy(cfg.layout,"normal"); strcpy(cfg.pressure,"normal"); strcpy(cfg.qos,"default");
    for(int i=1;i<argc;i+=2) {
        if(i+1==argc) { fprintf(stderr,"missing argument\n"); exit(64); }
        const char *key=argv[i],*v=argv[i+1];
#define NUM(flag,field) if(!strcmp(key,flag)) cfg.field=parse_unsigned(v)
#define STR(flag,field) if(!strcmp(key,flag)) copy_option(cfg.field,sizeof cfg.field,v)
        NUM("--n",n); else NUM("--producers",producers); else NUM("--size",size);
        else NUM("--rate",rate); else NUM("--pages",pages); else NUM("--held",held);
        else if(!strcmp(key,"--seed")) cfg.seed=parse_number(v);
        else STR("--mode",mode); else STR("--qos",qos); else STR("--layout",layout);
        else STR("--pressure",pressure); else STR("--hook",hook); else STR("--negative",negative); else STR("--cell",cell); else STR("--baseline",baseline);
        else if(!strcmp(key,"--signing")) { if(strlen(v)!=1) exit(64); cfg.signing=v[0]; }
        else if(!strcmp(key,"--arm")) { cfg.arm=v[0]; if(v[1]=='-') snprintf(cfg.sub,sizeof cfg.sub,"%s",v+2); }
        else { fprintf(stderr,"unknown option %s\n",key); exit(64); }
#undef NUM
#undef STR
    }
    if(!strchr("ISEVM",cfg.arm)||!(cfg.n==1||cfg.n==2||cfg.n==4||cfg.n==8)||
       !(cfg.size==64||cfg.size==256||cfg.size==4096)||!(cfg.producers==1||cfg.producers==2)||
       !(cfg.rate==1||cfg.rate==64)||!(cfg.pages==1||cfg.pages==4)||!cfg.cell[0]) exit(64);
    if(strcmp(cfg.mode,"cost")&&strcmp(cfg.mode,"correctness")&&strcmp(cfg.mode,"preflight")&&strcmp(cfg.mode,"sensitivity")) exit(64);
    if(cfg.arm=='V'&&cfg.producers!=1) exit(64);
    if(!strchr("ABC",cfg.signing)||cfg.held>=(cfg.n>cfg.producers?cfg.n:cfg.producers)) exit(64);
    if(strcmp(cfg.qos,"default")&&strcmp(cfg.qos,"background")) exit(64);
    if(strcmp(cfg.layout,"normal")&&strcmp(cfg.layout,"cache-boundary")&&strcmp(cfg.layout,"page-boundary")) exit(64);
    if(strcmp(cfg.pressure,"normal")&&strcmp(cfg.pressure,"near-full")&&strcmp(cfg.pressure,"fragmented")) exit(64);
    if(cfg.baseline[0]&&strcmp(cfg.baseline,"data-indirection")&&strcmp(cfg.baseline,"handshake")) exit(64);
    if(cfg.arm=='M'&&strcmp(cfg.sub,"data-map")&&strcmp(cfg.sub,"data-protect")&&strcmp(cfg.sub,"jit-protect")) exit(64);
    if(!strcmp(cfg.mode,"preflight")||cfg.hook[0]||cfg.negative[0]) { cfg.warmup=4; cfg.publications=32; cfg.call_floor=128; }
}

int main(int argc,char **argv) {
    parse(argc,argv);
    const char *raw=getenv("NATEMU_RAW_ROOT"),*gate=getenv("INV18_RUNTIME_AUTHORIZED"),*repo=getenv("INV18_REPO_ROOT");
    char resolved_raw[PATH_MAX],resolved_repo[PATH_MAX];
    if(!raw||!repo||!gate||strcmp(gate,"independent-code-gate")||!realpath(raw,resolved_raw)||!realpath(repo,resolved_repo)) {
        fprintf(stderr,"runtime gate/raw root absent\n"); return 77;
    }
    size_t repo_len=strlen(resolved_repo);
    if(!strncmp(resolved_raw,resolved_repo,repo_len)&&(resolved_raw[repo_len]=='/'||resolved_raw[repo_len]==0)) {
        fprintf(stderr,"repository-internal raw root rejected\n"); return 77;
    }
    if(!strcmp(cfg.mode,"cost")&&!getenv("INV18_TIMER_PREREQUISITE")) { fprintf(stderr,"cost prerequisite absent\n"); return 77; }
    setvbuf(stdout,NULL,_IOLBF,0);
    host_page=(size_t)getpagesize(); stride=host_page*2;
    size_t prologue=cfg.hook[0]?20:0;
    code_offset=!strcmp(cfg.layout,"page-boundary")?host_page-16-prologue:(!strcmp(cfg.layout,"cache-boundary")?48-prologue:0);
    slot_count=ARENA_BYTES/stride; data_length=cfg.pages*host_page;
    REQUIRE(mach_timebase_info(&timebase)==KERN_SUCCESS&&timebase.numer&&timebase.denom,"timebase");
    printf("{\"kind\":\"header\",\"investigation\":\"INV-01-18\",\"method\":\"M18-2\",\"cell\":\"%s\",\"mode\":\"%s\",\"arm\":\"%c\",\"n\":%u,\"seed\":%"PRIu64",\"page_size\":%zu,\"size\":%u,\"code_offset\":%zu,\"numer\":%u,\"denom\":%u,\"signing\":\"%c\",\"negative\":\"%s\",\"hook\":\"%s\",\"baseline\":\"%s\"}\n",
           cfg.cell,cfg.mode,cfg.arm,cfg.n,cfg.seed,host_page,cfg.size,code_offset,timebase.numer,timebase.denom,cfg.signing,cfg.negative,cfg.hook,cfg.baseline);
    if(!strncmp(cfg.negative,"protection-",11)) return protection_control();
    atomic_store(&running,true);
    if(pthread_create(&collector_thread,NULL,collect,NULL)) return 71;
    requested_qos();
    arena=mmap(NULL,ARENA_BYTES,PROT_READ|PROT_WRITE|PROT_EXEC,MAP_PRIVATE|MAP_ANON|MAP_JIT,-1,0);
    if(arena==MAP_FAILED) { emit("unavailable",errno,1,0,0,0,0); atomic_store(&trace_done,true); pthread_join(collector_thread,NULL); return 78; }
    emit("capability",pthread_jit_write_protect_supported_np(),(uintptr_t)arena,ARENA_BYTES,cfg.signing,0,0);
    if(cfg.arm=='M') {
        char path[4096]; REQUIRE(snprintf(path,sizeof path,"%s/inv18-map-XXXXXX",raw)<(int)sizeof path,"raw path");
        backing=mkstemp(path); REQUIRE(backing>=0,"mapping backing");
        uint64_t *buffer=calloc(1,data_length); REQUIRE(buffer,"mapping buffer");
        for(uint64_t v=1;v<=2;v++) { for(size_t j=0;j<data_length/8;j++) buffer[j]=v; REQUIRE(write(backing,buffer,data_length)==(ssize_t)data_length,"backing write"); }
        free(buffer); REQUIRE(fsync(backing)==0,"backing fsync");
        data_window=mmap(NULL,data_length,PROT_READ,MAP_PRIVATE,backing,0); REQUIRE(data_window!=MAP_FAILED,"data window");
        emit("owned-map",(uintptr_t)data_window,data_length,host_page,0,0,0);
    }
    for(unsigned i=0;i<cfg.n;i++) { workers[i].id=i; REQUIRE(pthread_create(&workers[i].thread,NULL,reader,&workers[i])==0,"reader create"); }
    while(atomic_load(&booted)!=cfg.n) relax();
    uint64_t pre[PAIRS]={0},post[PAIRS]={0};
    cfg.batch=1;
    if(!strcmp(cfg.mode,"cost")) {
        clock_pairs(pre); cfg.batch=calibration(pre);
        if(!cfg.batch) { emit("resolution-unresolved",1,0,0,0,0,0); goto finish_without_run; }
        cfg.publications=cfg.batch*COST_BATCHES;
    }
    uint64_t total=cfg.warmup+cfg.publications+16;
    uint64_t held_plan=0;
    for(uint32_t s=0;s<slot_count;s++) held_plan+=pressure_member(s);
    uint64_t scratch_plan=!strcmp(cfg.pressure,"near-full")?2:0;
    uint64_t replacement_plan=scratch_plan&&!stopping()?1:0;
    uint64_t tokens=total*(cfg.arm=='V'?cfg.n:1)+held_plan+scratch_plan+replacement_plan+1;
    request_cap=total*3+16+2*held_plan+2*replacement_plan;
    /* 1 MiB conservative reserve covers coordinator pre/post/sort buffers,
     * stdio storage, config, mailboxes and other fixed owned metadata. This is
     * not an RSS or OS thread-stack reservation cap. All dynamic arrays use
     * their actual sizeof expressions, including baseline-only pair storage. */
    uint64_t bytes=tokens*(sizeof(Desc)+sizeof(*life)+(!strcmp(cfg.baseline,"data-indirection")?sizeof(Pair):0))+request_cap*sizeof(Request)+sizeof traces+sizeof workers+slot_count*(sizeof *slot_token+sizeof *pressure_token+sizeof *retire_clock)+(1<<20);
    emit("allocation-plan",tokens,request_cap,bytes,CAP_BYTES,sizeof(Desc),sizeof(Request));
    emit("setup-plan",held_plan,scratch_plan,replacement_plan,2*held_plan+2*replacement_plan,bytes,0);
    if(tokens>=UINT32_MAX||bytes>CAP_BYTES) { emit("capacity-unresolved",tokens,bytes,CAP_BYTES,0,0,0); goto finish_without_run; }
    max_token=tokens;
    desc=calloc(max_token,sizeof *desc); life=calloc(max_token,sizeof *life);
    requests=calloc(request_cap,sizeof *requests); slot_token=calloc(slot_count,sizeof *slot_token); pressure_token=calloc(slot_count,sizeof *pressure_token); retire_clock=calloc(slot_count,sizeof *retire_clock);
    if(!strcmp(cfg.baseline,"data-indirection")) indirection=calloc(max_token,sizeof *indirection);
    REQUIRE(desc&&life&&requests&&slot_token&&pressure_token&&retire_clock&&
            (strcmp(cfg.baseline,"data-indirection")||indirection),"metadata allocation");
    for(unsigned p=0;p<2;p++) { atomic_store(&page_version[p],1); atomic_store(&guest_backing[p],p+1); }
    if(cfg.producers==2) for(unsigned i=0;i<2;i++) REQUIRE(pthread_create(&producers[i].thread,NULL,producer,(void *)(uintptr_t)i)==0,"producer create");
    if(cfg.hook[0]) REQUIRE(pthread_create(&hook_thread,NULL,control_hook,NULL)==0,"hook controller");
    emit("participants",cfg.n,1,1,cfg.producers==2?2:0,cfg.hook[0]?1:0,0);
    atomic_store_explicit(&ready,true,memory_order_release);
    reserve_pressure_slots();
    for(unsigned p=0;p<2;p++) {
        for(unsigned i=0;i<(cfg.arm=='V'?cfg.n:1);i++) publish_one(p,i,0);
        for(unsigned i=0;i<cfg.n;i++) while(!atomic_load_explicit(&last_seen[i][p],memory_order_acquire)) relax();
    }
    for(unsigned i=0;i<cfg.n;i++) while(atomic_load(&workers[i].calls)<4) relax();
    pressure_diagnostic();
    uint64_t throughput_start=0,calls_start=0,retention_peak=0,stress_start[MAX_N]={0};
    for(uint64_t round=0;round<cfg.warmup+cfg.publications;round++) {
        atomic_store(&writer_round,round+3);
        unsigned jitter=(unsigned)(mix(cfg.seed^round)&3);
        emit("seed-delay",round,jitter,0,0,0,0);
        for(unsigned j=0;j<jitter;j++) relax(); /* frozen seed-selected dispatcher positions */
        if(cfg.hook[0]&&!strncmp(cfg.hook,"reader-",7)&&
           strcmp(cfg.hook,"reader-before-ack")&&strcmp(cfg.hook,"reader-after-ack")&&
           round+3>=hook_arm_round(cfg.hook)&&!atomic_load(&hook_seen)) {
            /* Force the intended initial held-reader state before allowing
             * its competing writer transition. This exists only in hook
             * children and cannot alter unhooked stress/cost admission. */
            while(!atomic_load_explicit(&hook_arrived,memory_order_acquire)) relax();
        }
        if(!strcmp(cfg.mode,"cost")&&round==cfg.warmup) timed_stage=true;
        if(round==cfg.warmup) {
            for(unsigned i=0;i<cfg.n;i++) {
                stress_start[i]=atomic_load_explicit(&workers[i].calls,memory_order_acquire);
                emit("stress-begin",i,stress_start[i],cfg.warmup,0,0,0);
            }
        }
        uint64_t begin=0;
        if(!strcmp(cfg.mode,"cost")&&round>=cfg.warmup&&(round-cfg.warmup)%cfg.batch==0) begin=mach_absolute_time();
        if(!strcmp(cfg.mode,"cost")&&round==cfg.warmup) {
            throughput_start=mach_absolute_time();
            for(unsigned i=0;i<cfg.n;i++) calls_start+=atomic_load(&workers[i].calls);
        }
        unsigned page=(unsigned)(round%2);
        if(round%cfg.rate==0) guest_stimulus(round/cfg.rate+1,(unsigned)((round/cfg.rate)%2));
        for(unsigned i=0;i<(cfg.arm=='V'?cfg.n:1);i++) publish_one(page,i,round);
        emit("round",round,page,round>=cfg.warmup,0,0,0);
        uint64_t retained=0,superseded=0;
        for(uint32_t s=0;s<slot_count;s++) {
            uint32_t t=slot_token[s]==UINT32_MAX?pressure_token[s]:slot_token[s];
            if(t) { unsigned state=atomic_load(&life[t]); retained+=(state==RETIRED)*stride; superseded+=(state==SUPERSEDED)*stride; }
        }
        if(retained>retention_peak) retention_peak=retained;
        emit("retention",round,retained,superseded,retention_peak,0,0);
        /* Each batch start is retained on the coordinator, never another thread. */
        static uint64_t batch_start;
        if(begin) batch_start=begin;
        if(!strcmp(cfg.mode,"cost")&&round>=cfg.warmup&&(round-cfg.warmup+1)%cfg.batch==0)
            emit("cost-batch",(round-cfg.warmup)/cfg.batch,cfg.batch,delta(batch_start,mach_absolute_time()),0,0,0);
    }
    if(throughput_start) {
        uint64_t calls_end=0;
        for(unsigned i=0;i<cfg.n;i++) calls_end+=atomic_load(&workers[i].calls);
        uint64_t throughput_end=mach_absolute_time();
        REQUIRE(throughput_end>=throughput_start&&calls_end>=calls_start,"throughput interval");
        emit("throughput",throughput_start,throughput_end,calls_end-calls_start,
             cfg.publications*(cfg.arm=='V'?cfg.n:1),retention_peak,0);
    }
    for(unsigned i=0;i<cfg.n;i++) {
        while(atomic_load_explicit(&workers[i].calls,memory_order_acquire)-stress_start[i]<cfg.call_floor) relax();
    }
    if(!strcmp(cfg.mode,"cost")) {
        clock_pairs(post);
        for(unsigned j=0;j<PAIRS;j++) emit("coordinator-pair",j,pre[j],post[j],0,0,0);
    }
    emit("complete",cfg.warmup,cfg.publications,cfg.call_floor,cfg.batch,next_token-1,epoch);
    if(cfg.hook[0]) REQUIRE(atomic_load(&hook_seen)&&atomic_load(&hook_released),"required hook missed");
    atomic_store(&running,false);
    if(cfg.hook[0]) pthread_join(hook_thread,NULL);
    if(cfg.producers==2) for(unsigned i=0;i<2;i++) pthread_join(producers[i].thread,NULL);
    for(unsigned i=0;i<cfg.n;i++) pthread_join(workers[i].thread,NULL);
    atomic_store(&trace_done,true); pthread_join(collector_thread,NULL);
    free(desc); free(life); free(requests); free(slot_token); free(pressure_token); free(retire_clock); free(indirection);
    if(data_window) munmap(data_window,data_length);
    if(backing>=0) close(backing);
    munmap(arena,ARENA_BYTES); return 0;
finish_without_run:
    if(!strcmp(cfg.mode,"cost")) {
        clock_pairs(post);
        for(unsigned j=0;j<PAIRS;j++) emit("coordinator-pair",j,pre[j],post[j],0,0,0);
    }
    atomic_store(&running,false); atomic_store_explicit(&ready,true,memory_order_release);
    for(unsigned i=0;i<cfg.n;i++) pthread_join(workers[i].thread,NULL);
    atomic_store(&trace_done,true); pthread_join(collector_thread,NULL); return 79;
}
