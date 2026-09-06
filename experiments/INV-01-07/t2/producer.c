/* M07-T2 proposed timer/event return. Build/synthetic only until independent review. */
#include <dispatch/dispatch.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define N 2000
#define PERIOD UINT64_C(1000000)
extern uint64_t t2_dispatch_deadline(uint64_t, uint64_t *);
static mach_timebase_info_data_t tb;
static const char *mode;
static FILE *output;
static int comma;
static const qos_class_t classes[]={QOS_CLASS_USER_INTERACTIVE,QOS_CLASS_BACKGROUND};
static const char *qualities[]={"user-interactive","background"};
static void require(int ok,const char *what) { if(!ok) { fprintf(stderr,"T2 failure: %s\n",what); _exit(2); } }
static void checked(int rc,const char *what) { require(rc==0,what); }
static uint64_t add(uint64_t a,uint64_t b) { require(a<=UINT64_MAX-b,"addition overflow"); return a+b; }
static uint64_t ns(uint64_t ticks) { __uint128_t n=(__uint128_t)ticks*tb.numer/tb.denom; require(n<=UINT64_MAX,"ns overflow");return (uint64_t)n; }
static uint64_t ticks_ceil(uint64_t n) { __uint128_t t=((__uint128_t)n*tb.denom+tb.numer-1)/tb.numer;require(t<=UINT64_MAX,"tick overflow");return(uint64_t)t; }
struct qos { int set,get,relative; qos_class_t got,current; };
static struct qos quality(int q,int set) {
    struct qos v={0};
    if(set) v.set=pthread_set_qos_class_self_np(classes[q],0);
    v.get=pthread_get_qos_class_np(pthread_self(),&v.got,&v.relative);
    v.current=qos_class_self();
    checked(v.set,"QoS set"); checked(v.get,"QoS get");
    require(v.got==classes[q]&&v.current==classes[q]&&v.relative==0,"QoS mismatch");return v;
}
static void qos_json(struct qos q) { fprintf(output,"[%d,%d,%u,%u,%d]",q.set,q.get,q.got,q.current,q.relative); }
/* Uniform raw rows: index, requested uptime ns, represented deadline ns,
 * opaque dispatch token (zero for non-dispatch), begin tick, wake tick,
 * sender-observed completion tick, dispatch handler count. */
struct row { uint64_t id,requested,deadline,token,begin,wake,end,events; };
static void rows_json(struct row *rows,size_t count) {
    fputc('[',output);
    for(size_t i=0;i<count;i++) { struct row *r=&rows[i];fprintf(output,"%s[%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64"]",i?",":"",r->id,r->requested,r->deadline,r->token,r->begin,r->wake,r->end,r->events); }
    fputc(']',output);
}
static void cell(const char *kind,const char *mechanism,int q) {
    fprintf(output,"%s{\"kind\":\"%s\",\"mechanism\":\"%s\",\"qos\":\"%s\"",comma?",\n":"",kind,mechanism,qualities[q]);comma=1;
}
static void wakeup(const char *mechanism,uint64_t target,int q) {
    struct row rows[N]={0};struct qos observed=quality(q,1),callbacks[N]={0};
    for(unsigned i=0;i<N;i++) {
        struct row *r=&rows[i];r->id=i;r->begin=mach_absolute_time();
        r->requested=add(ns(r->begin),target);r->deadline=r->requested;
        if(!strcmp(mechanism,"mach_wait_until")) {
            uint64_t deadline=add(r->begin,ticks_ceil(target));r->deadline=ns(deadline);
            checked(mach_wait_until(deadline),"mach_wait_until");r->wake=mach_absolute_time();
        } else if(!strcmp(mechanism,"nanosleep")) {
            struct timespec wait={(time_t)(target/1000000000),(long)(target%1000000000)};
            int rc;do{rc=nanosleep(&wait,&wait);}while(rc&&errno==EINTR);checked(rc,"nanosleep");r->wake=mach_absolute_time();
        } else {
            dispatch_queue_t queue=dispatch_get_global_queue(classes[q],0);
            dispatch_source_t timer=dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER,0,
                !strcmp(mechanism,"dispatch_source_timer_strict")?DISPATCH_TIMER_STRICT:0,queue);
            dispatch_semaphore_t fired=dispatch_semaphore_create(0),cancelled=dispatch_semaphore_create(0);
            require(timer&&fired&&cancelled,"dispatch allocation");
            __block struct qos actual={0};
            dispatch_source_set_event_handler(timer,^{
                r->wake=mach_absolute_time();r->events++;actual=quality(q,0);
                dispatch_semaphore_signal(fired);
            });
            dispatch_source_set_cancel_handler(timer,^{dispatch_semaphore_signal(cancelled);});
            /* The same public object supplies the opaque request and readable
             * actual deadline. No decoding/casting of dispatch_time_t. */
            r->token=t2_dispatch_deadline(r->requested,&r->deadline);
            require(r->token&&r->token!=DISPATCH_TIME_FOREVER,"dispatch deadline sentinel");
            dispatch_source_set_timer(timer,r->token,DISPATCH_TIME_FOREVER,0);
            dispatch_resume(timer);
            checked((int)dispatch_semaphore_wait(fired,dispatch_time(DISPATCH_TIME_NOW,1000000000)),"dispatch event timeout");
            r->end=mach_absolute_time();
            dispatch_source_cancel(timer);
            checked((int)dispatch_semaphore_wait(cancelled,dispatch_time(DISPATCH_TIME_NOW,1000000000)),"dispatch cancel timeout");
            callbacks[i]=actual;dispatch_release(timer);dispatch_release(fired);dispatch_release(cancelled);
        }
        if(!r->end)r->end=mach_absolute_time();
        if(!strcmp(mode,"dispatch-early")&&i==0)r->wake=(uint64_t)((__uint128_t)(r->deadline-1000)*tb.denom/tb.numer);
    }
    cell("wakeup",mechanism,q);fprintf(output,",\"target_ns\":%"PRIu64",\"qos_record\":",target);qos_json(observed);
    fprintf(output,",\"callback_qos\":[");for(unsigned i=0;i<N;i++){if(i)fputc(',',output);qos_json(callbacks[i]);}fprintf(output,"],\"rows\":");rows_json(rows,N);fputc('}',output);fflush(output);
}
struct cadence_shared { atomic_int ready,go;uint64_t epoch; };
struct cadence_worker { struct cadence_shared *shared;struct row rows[N];struct qos qos;int q,id;uint64_t start,end; };
static void *cadence_thread(void *argument) {
    struct cadence_worker *w=argument;w->qos=quality(w->q,1);
    atomic_fetch_add_explicit(&w->shared->ready,1,memory_order_release);
    while(!atomic_load_explicit(&w->shared->go,memory_order_acquire))sched_yield();
    w->start=mach_absolute_time();
    for(unsigned i=0;i<N;i++) {
        struct row *r=&w->rows[i];r->id=i;r->begin=mach_absolute_time();
        uint64_t deadline=add(w->shared->epoch,ticks_ceil((i+1)*PERIOD));
        r->requested=add(ns(w->shared->epoch),(i+1)*PERIOD);r->deadline=ns(deadline);
        checked(mach_wait_until(deadline),"cadence wait");r->wake=mach_absolute_time();r->end=r->wake;
    }
    w->end=mach_absolute_time();return NULL;
}
static void cadence(int n,int q) {
    struct cadence_shared shared;atomic_init(&shared.ready,0);atomic_init(&shared.go,0);
    struct cadence_worker *workers=calloc((size_t)n,sizeof *workers);require(workers!=NULL,"cadence allocation");
    pthread_t threads[8];
    for(int i=0;i<n;i++){workers[i].shared=&shared;workers[i].id=i;workers[i].q=q;checked(pthread_create(&threads[i],NULL,cadence_thread,&workers[i]),"cadence create");}
    while(atomic_load_explicit(&shared.ready,memory_order_acquire)!=n)sched_yield();
    shared.epoch=add(mach_absolute_time(),ticks_ceil(10000000));
    atomic_store_explicit(&shared.go,1,memory_order_release);
    for(int i=0;i<n;i++)checked(pthread_join(threads[i],NULL),"cadence join");
    if(!strcmp(mode,"cadence-duplicate"))workers[0].rows[1].deadline=workers[0].rows[0].deadline;
    cell("cadence","mach_wait_until",q);fprintf(output,",\"workers\":%d,\"period_ns\":1000000,\"epoch_tick\":%"PRIu64",\"threads\":[",n,shared.epoch);
    for(int i=0;i<n;i++){struct cadence_worker *w=&workers[i];fprintf(output,"%s{\"id\":%d,\"start_tick\":%"PRIu64",\"end_tick\":%"PRIu64",\"qos_record\":",i?",":"",i,w->start,w->end);qos_json(w->qos);fprintf(output,",\"rows\":");rows_json(w->rows,N);fputc('}',output);}
    fprintf(output,"]}");fflush(output);free(workers);
}
/* Only lock-free flag publication in the handler; the running target records
 * receive after return. This includes handler-return/polling work in latency. */
struct event { pthread_mutex_t mutex;pthread_cond_t cond;dispatch_semaphore_t sem;
    atomic_int ready,request,ack,stop;atomic_ullong received;int kind,q;struct qos qos; };
static atomic_int signal_seen;
static void prepare_signal_flag(void) {
    atomic_init(&signal_seen,0);
    require(atomic_is_lock_free(&signal_seen),"handler atomic not lock free");
}
static int take_signal(void) { return atomic_exchange_explicit(&signal_seen,0,memory_order_acquire); }
static void handler(int signal_number) { (void)signal_number;atomic_store_explicit(&signal_seen,1,memory_order_release); }
static void *event_target(void *argument) {
    struct event *e=argument;e->qos=quality(e->q,1);if(e->kind!=0)atomic_store(&e->ready,1);
    int seen=0;
    while(!atomic_load(&e->stop)) {
        if(e->kind==0){checked(pthread_mutex_lock(&e->mutex),"event lock");while(atomic_load(&e->request)==seen&&!atomic_load(&e->stop)){atomic_store(&e->ready,seen+1);checked(pthread_cond_wait(&e->cond,&e->mutex),"event wait");}checked(pthread_mutex_unlock(&e->mutex),"event unlock");}
        else if(e->kind==1)checked((int)dispatch_semaphore_wait(e->sem,DISPATCH_TIME_FOREVER),"event semaphore");
        else { if(!take_signal())continue; }
        if(atomic_load(&e->stop))break;
        int request=atomic_load(&e->request);if(request==seen)continue;
        atomic_store(&e->received,mach_absolute_time());seen=request;atomic_store(&e->ack,seen);
    }
    return NULL;
}
static void event_arm(int kind,int q) {
    static const char *names[]={"pthread_cond_signal","dispatch_semaphore_signal","pthread_kill_SIGUSR1_spinning"};
    struct event e={0};e.kind=kind;e.q=q;struct qos sender=quality(q,1);
    atomic_init(&e.ready,0);atomic_init(&e.request,0);atomic_init(&e.ack,0);atomic_init(&e.stop,0);atomic_store(&signal_seen,0);atomic_init(&e.received,0);
    checked(pthread_mutex_init(&e.mutex,NULL),"event mutex init");checked(pthread_cond_init(&e.cond,NULL),"event cond init");
    e.sem=dispatch_semaphore_create(0);require(e.sem!=NULL,"event semaphore create");
    pthread_t thread;checked(pthread_create(&thread,NULL,event_target,&e),"event create");
    while(!atomic_load(&e.ready))sched_yield();
    struct row rows[N]={0};
    for(int i=0;i<N;i++){
        struct row *r=&rows[i];r->id=(uint64_t)i;
        if(kind==0)while(atomic_load(&e.ready)!=i+1)sched_yield();
        r->begin=mach_absolute_time();if(kind!=0)atomic_store(&e.request,i+1);
        if(kind==0){checked(pthread_mutex_lock(&e.mutex),"send lock");atomic_store(&e.request,i+1);checked(pthread_cond_signal(&e.cond),"send signal");checked(pthread_mutex_unlock(&e.mutex),"send unlock");}
        else if(kind==1)dispatch_semaphore_signal(e.sem);else checked(pthread_kill(thread,SIGUSR1),"pthread_kill");
        while(atomic_load(&e.ack)!=i+1)require(ns(mach_absolute_time()-r->begin)<1000000000,"event acknowledgement timeout");
        r->end=mach_absolute_time();r->wake=atomic_load(&e.received);r->events=1;
        if(!strcmp(mode,"rtt-zero")&&i==0)r->end=r->begin;
    }
    atomic_store(&e.stop,1);
    if(kind==0){checked(pthread_mutex_lock(&e.mutex),"stop lock");checked(pthread_cond_signal(&e.cond),"stop signal");checked(pthread_mutex_unlock(&e.mutex),"stop unlock");}
    else if(kind==1)dispatch_semaphore_signal(e.sem);
    checked(pthread_join(thread,NULL),"event join");
    checked(pthread_mutex_destroy(&e.mutex),"event mutex destroy");checked(pthread_cond_destroy(&e.cond),"event cond destroy");dispatch_release(e.sem);
    cell("event",names[kind],q);fprintf(output,",\"sender_qos\":");qos_json(sender);fprintf(output,",\"target_qos\":");qos_json(e.qos);fprintf(output,",\"rows\":");rows_json(rows,N);fputc('}',output);fflush(output);
}
static void noise_json(void) {
    uint64_t pairs[10000][2];
    for(unsigned i=0;i<10000;i++){pairs[i][0]=mach_absolute_time();pairs[i][1]=mach_absolute_time();}
    fputc('[',output);for(unsigned i=0;i<10000;i++)fprintf(output,"%s[%u,%"PRIu64",%"PRIu64"]",i?",":"",i,pairs[i][0],pairs[i][1]);fputc(']',output);
}
static int within(const char *p,const char *r){size_t n=strlen(r);return !strncmp(p,r,n)&&(p[n]=='/'||p[n]=='\0');}
int main(int argc,char **argv) {
    require(argc==3,"arguments: output mode");mode=argv[2];
    require(!strcmp(mode,"normal")||!strcmp(mode,"dispatch-early")||!strcmp(mode,"rtt-zero")||!strcmp(mode,"cadence-duplicate"),"mode");
    const char *identity=getenv("NATEMU_T2_RUN_ID");require(identity&&strlen(identity)==32,"missing run identity");
    checked(mach_timebase_info(&tb),"timebase");require(tb.numer&&tb.denom,"timebase domain");
    prepare_signal_flag();
    struct sigaction action={0};action.sa_handler=handler;checked(sigemptyset(&action.sa_mask),"signal mask");checked(sigaction(SIGUSR1,&action,NULL),"signal install");
    char root[PATH_MAX],parent[PATH_MAX],resolved[PATH_MAX];
    const char *rawroot=getenv("NATEMU_RAW_ROOT");require(rawroot&&realpath(rawroot,root)&&!strcmp(root,rawroot),"raw root");
    require(strcmp(root,"/")&&!within(root,T2_REPO)&&!within(T2_REPO,root),"root repository overlap");
    require(argv[1][0]=='/'&&strlen(argv[1])<sizeof parent,"output absolute path");strcpy(parent,argv[1]);char *slash=strrchr(parent,'/');require(slash&&slash!=parent,"output parent");*slash=0;
    require(realpath(parent,resolved)&&!strcmp(parent,resolved)&&within(parent,root)&&!within(parent,T2_REPO),"external output");
    int fd=open(argv[1],O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW,0600);require(fd>=0,"exclusive output");output=fdopen(fd,"w");require(output!=NULL,"output stream");
    fprintf(output,"{\"schema\":\"M07-T2-raw-v1\",\"mode\":\"%s\",\"run_id\":\"%s\",\"timebase\":[%u,%u],\"calibration_pre\":",mode,identity,tb.numer,tb.denom);noise_json();fprintf(output,",\"cells\":[\n");
    if(!strcmp(mode,"normal")){
        const char *mechanisms[]={"mach_wait_until","nanosleep","dispatch_source_timer_leeway0","dispatch_source_timer_strict"};const uint64_t targets[]={100000,1000000,10000000};
        for(int m=0;m<4;m++)for(int t=0;t<3;t++)for(int q=0;q<2;q++)wakeup(mechanisms[m],targets[t],q);
        for(int n=2;n<=8;n*=2)for(int q=0;q<2;q++)cadence(n,q);
        for(int m=0;m<3;m++)for(int q=0;q<2;q++)event_arm(m,q);
    }else if(!strcmp(mode,"dispatch-early"))wakeup("dispatch_source_timer_strict",100000,0);
    else if(!strcmp(mode,"rtt-zero"))event_arm(2,0);else cadence(2,0);
    fprintf(output,"],\"calibration_post\":");noise_json();fprintf(output,",\"complete\":true}\n");require(fclose(output)==0,"output close");return 0;
}
