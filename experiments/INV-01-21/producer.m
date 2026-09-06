#import <AppKit/AppKit.h>
#include "core.h"
#include <fcntl.h>
#include <inttypes.h>
#include <mach/mach_time.h>
#include <pthread/qos.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#ifndef M21_BUILD
#error producing build identity required
#endif
enum { MAX_ACTORS=17 };
typedef struct {
    m21_record *rows;
    _Atomic uint64_t reserved,dropped;
    m21_witness continuous, clocks[5];
    uint64_t counter,interruptions,errors;
    int id,requested,set_rc,get_rc,got,relative;
    bool demoted;
    _Atomic bool alive;
} actor;
static actor actors[MAX_ACTORS];
static m21_gate callbacks=M21_GATE_INIT;
static uint32_t receipt_order[8*M21_CAP];
static _Atomic uint64_t receipt_count;
static _Atomic bool stopping,demote,invalid,reversal;
static mach_timebase_info_data_t tb;
static unsigned workers,total;
static uint64_t resolutions[5];
static const char *arm,*run_id;
static void require(bool ok,const char *s){if(!ok){fprintf(stderr,"M21: %s\n",s);_exit(2);}}
static void fail(void){atomic_store(&invalid,true);atomic_store(&stopping,true);}
static void check(int rc){if(rc)fail();}
static m21_record *reserve(actor *a) {
    uint64_t i;if(!m21_claim(&a->reserved,&a->dropped,M21_CAP,&i)){fail();return NULL;}
    m21_record *r=&a->rows[i];r->seq=i;r->actor=a->id;return r;
}
static void quality(actor *a,bool change) {
    qos_class_t got=QOS_CLASS_UNSPECIFIED;int rel=0;
    if(change)a->set_rc=pthread_set_qos_class_self_np((qos_class_t)a->requested,0);
    a->get_rc=pthread_get_qos_class_np(pthread_self(),&got,&rel);a->got=(int)got;a->relative=rel;
    if(a->set_rc||a->get_rc||a->got!=a->requested||rel)fail();
}
static void poll_actor(actor *a) {
    if(atomic_load(&demote)&&!a->demoted){a->requested=QOS_CLASS_BACKGROUND;quality(a,true);a->demoted=true;}
}
static uint64_t continuous(actor *a) {
    uint64_t t=mach_continuous_time();m21_observe(&a->continuous,t,a->counter);
    if(a->continuous.error)fail();if(a->continuous.reversals)atomic_store(&reversal,true);return t;
}
/* Fixed sequential order, not a simultaneous clock reading. Notifications do
 * not update ordered-actor witnesses: callbacks can run concurrently. */
static void snapshot(m21_record *r,actor *a,bool ordered) {
    r->clocks[0]=mach_absolute_time();r->clocks[1]=ordered?continuous(a):mach_continuous_time();
    const clockid_t ids[3]={CLOCK_MONOTONIC,CLOCK_MONOTONIC_RAW,CLOCK_UPTIME_RAW};
    for(int k=0;k<3;k++){
        struct timespec ts={0};int rc=clock_gettime(ids[k],&ts);
        if(rc||ts.tv_sec<0||ts.tv_nsec<0||ts.tv_nsec>=1000000000){r->status=1;fail();continue;}
        __uint128_t ns=(__uint128_t)ts.tv_sec*1000000000+(uint64_t)ts.tv_nsec;
        if(ns>UINT64_MAX){r->status=1;fail();}else r->clocks[k+2]=(uint64_t)ns;
    }
    if(ordered)for(int k=0;k<5;k++){m21_observe(&a->clocks[k],r->clocks[k],r->seq);if(a->clocks[k].error)fail();}
}
static m21_record *record(actor *a,int source,int value,bool ordered) {
    m21_record *r=reserve(a);if(!r)return NULL;
    r->counter=ordered?a->counter:r->seq;r->source=source;r->value=value;
    if(pthread_threadid_np(NULL,&r->thread_id)){r->status=1;fail();}
    if(ordered){quality(a,false);r->requested=a->requested;r->set_rc=a->set_rc;r->get_rc=a->get_rc;r->got=a->got;r->relative=a->relative;}
    else {qos_class_t got;int relative; r->get_rc=pthread_get_qos_class_np(pthread_self(),&got,&relative);r->got=got;r->relative=relative;if(r->get_rc){r->status=1;fail();}}
    snapshot(r,a,ordered);if(ordered)atomic_store_explicit(&r->committed,1,memory_order_release);return r;
}
static void *worker(void *arg) {
    actor *a=arg;a->requested=QOS_CLASS_USER_INITIATED;quality(a,true);atomic_store(&a->alive,true);
    uint64_t last=continuous(a);record(a,-1,0,true);
    while(!atomic_load(&stopping)){
        if(!m21_inc(&a->counter)){fail();break;}poll_actor(a);
        uint64_t now=continuous(a),delta;
        if(now<last)continue;
        if(!m21_elapsed(now,last,tb.numer,tb.denom,&delta)){fail();break;}
        if(delta>=M21_PERIOD){record(a,-1,0,true);last=now;}
    }
    return NULL;
}
static bool timer_poll(void *arg){actor *a=arg;continuous(a);poll_actor(a);return !atomic_load(&stopping);}
static void *timer(void *arg) {
    actor *a=arg;a->requested=QOS_CLASS_USER_INITIATED;quality(a,true);atomic_store(&a->alive,true);
    continuous(a);
    while(!atomic_load(&stopping)){
        int result=m21_period(nanosleep,timer_poll,a,&a->interruptions,&a->errors);
        if(result<0){fail();break;}
        if(result==0){if(!m21_inc(&a->counter)){fail();break;}record(a,-2,0,true);}
    }
    return NULL;
}
static void notify(unsigned source) {
    int entered=m21_enter(&callbacks);if(entered<0){fail();return;}if(!entered)return;
    @autoreleasepool {
        int value=source==6?(int)NSProcessInfo.processInfo.thermalState:source==7?(int)NSProcessInfo.processInfo.lowPowerModeEnabled:1;
        m21_record *r=record(&actors[workers+1+source],(int)source,value,false);
        if(r)check(m21_publish(&callbacks,r,source*M21_CAP+(uint32_t)r->seq,receipt_order,&receipt_count,8*M21_CAP));
    }
    check(m21_leave(&callbacks));
}
static void emit_notifications(uint64_t *sent) {
    /* These are actual parent receipt candidates, not a chronological merge.
     * Each receipt references a release-committed raw source-local record. */
    while(*sent<atomic_load_explicit(&receipt_count,memory_order_acquire)){
        uint32_t index=receipt_order[*sent];unsigned s=index/M21_CAP;actor *a=&actors[workers+1+s];m21_record *r=&a->rows[index%M21_CAP];
        require(atomic_load_explicit(&r->committed,memory_order_acquire)&&r->receipt_id==*sent,"notification publication");
        printf("{\"event\":\"notification\",\"source\":%u,\"seq\":%"PRIu64",\"receipt\":%"PRIu64",\"value\":%d}\n",s,r->seq,r->receipt_id,r->value);(*sent)++;
    }
    fflush(stdout);
}
static void witness(FILE *f,m21_witness w) {
    fprintf(f,"[ %"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%d ]",w.reads,w.reversals,w.first_before,w.first_after,w.first_before_index,w.first_after_index,w.error);
}
static void serialize(void) {
    FILE *f=fopen("trace.jsonl","wx");require(f!=NULL,"trace create");setvbuf(f,NULL,_IONBF,0);
    fprintf(f,"{\"schema\":\"M21-1-trace-v1\",\"run_id\":\"%s\",\"build_id\":\"%s\",\"n\":%u,\"arm\":\"%s\",\"timebase\":[%u,%u],\"record_size\":%zu,\"backend\":\"nanosleep-relative\",\"clock\":\"API-defined\",\"leeway\":\"not-exposed\",\"resolutions\":[%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64"]}\n",run_id,M21_BUILD,workers,arm,tb.numer,tb.denom,sizeof(m21_record),resolutions[0],resolutions[1],resolutions[2],resolutions[3],resolutions[4]);
#ifdef M21_SERIALIZE_HOOK
    M21_SERIALIZE_HOOK(); /* Synthetic build only; no hook in producing binary. */
#endif
    for(unsigned i=0;i<total;i++){
        actor *a=&actors[i];uint64_t count=atomic_load(&a->reserved);if(count>M21_CAP)count=M21_CAP;
        for(uint64_t k=0;k<count;k++){
            m21_record *r=&a->rows[k];require(atomic_load_explicit(&r->committed,memory_order_acquire)==1,"uncommitted after drain");
            fprintf(f,"{\"row\":[%u,%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%"PRIu64",%d,%d,%d,%d,%d,%d,%d,%d,%"PRIu64",%"PRIu64"]}\n",i,r->seq,r->counter,r->clocks[0],r->clocks[1],r->clocks[2],r->clocks[3],r->clocks[4],r->requested,r->set_rc,r->get_rc,r->got,r->relative,r->status,r->source,r->value,r->thread_id,r->receipt_id);
        }
        fprintf(f,"{\"actor\":%u,\"reserved\":%"PRIu64",\"dropped\":%"PRIu64",\"demoted\":%s,\"interruptions\":%"PRIu64",\"errors\":%"PRIu64",\"continuous\":",i,atomic_load(&a->reserved),atomic_load(&a->dropped),a->demoted?"true":"false",a->interruptions,a->errors);witness(f,a->continuous);
        fprintf(f,",\"clocks\":[");for(int j=0;j<5;j++){if(j)fputc(',',f);witness(f,a->clocks[j]);}fprintf(f,"]}\n");
    }
    fprintf(f,"{\"complete\":true,\"invalid\":%s,\"reversal\":%s,\"drained\":true,\"joined\":%u}\n",atomic_load(&invalid)?"true":"false",atomic_load(&reversal)?"true":"false",workers+1);
    require(!ferror(f),"trace write");require(fclose(f)==0,"trace close");
}
int main(int argc,char **argv) { @autoreleasepool {
    require(argc==5,"usage n arm seed run_id");workers=(unsigned)strtoul(argv[1],NULL,10);arm=argv[2];uint64_t seed=strtoull(argv[3],NULL,10);run_id=argv[4];
    require(workers==1||workers==2||workers==4||workers==8,"N");require(strspn(run_id,"0123456789abcdef")==32&&strlen(run_id)==32,"run identity");
    require(!strcmp(arm,"T1")||!strcmp(arm,"T2")||!strcmp(arm,"T3a")||!strcmp(arm,"T3b")||!strcmp(arm,"T4")||!strcmp(arm,"T5")||!strcmp(arm,"C1")||!strcmp(arm,"C2"),"arm");
    require(seed>=21001&&seed<=21003,"seed");total=workers+9;require(mach_timebase_info(&tb)==0&&tb.numer&&tb.denom,"timebase");
    actor main_actor={0};main_actor.requested=QOS_CLASS_USER_INITIATED;quality(&main_actor,true);require(!atomic_load(&invalid),"main QoS");
    printf("{\"event\":\"main-qos\",\"requested\":%d,\"set\":%d,\"get\":%d,\"got\":%d,\"relative\":%d}\n",main_actor.requested,main_actor.set_rc,main_actor.get_rc,main_actor.got,main_actor.relative);fflush(stdout);
    resolutions[0]=resolutions[1]=((uint64_t)tb.numer+tb.denom-1)/tb.denom;
    const clockid_t resolution_ids[3]={CLOCK_MONOTONIC,CLOCK_MONOTONIC_RAW,CLOCK_UPTIME_RAW};
    for(unsigned k=0;k<3;k++){struct timespec ts;require(clock_getres(resolution_ids[k],&ts)==0&&ts.tv_sec>=0&&ts.tv_nsec>=0&&ts.tv_nsec<1000000000,"resolution");resolutions[k+2]=(uint64_t)ts.tv_sec*1000000000+(uint64_t)ts.tv_nsec;}
    uint64_t owned=(uint64_t)total*M21_CAP*sizeof(m21_record)+(workers+1)*1024*1024+sizeof(actors)+2*1024*1024;
    require(owned<=128*1024*1024,"application allocation bound");
    for(unsigned i=0;i<total;i++){
        actors[i].id=(int)i;actors[i].rows=calloc(M21_CAP,sizeof(m21_record));require(actors[i].rows!=NULL,"records");
        for(unsigned j=0;j<M21_CAP;j++)atomic_init(&actors[i].rows[j].committed,0);
    }
    NSNotificationCenter *workspace=NSWorkspace.sharedWorkspace.notificationCenter,*defaults=NSNotificationCenter.defaultCenter;
    NSArray<NSString *> *names=@[NSWorkspaceWillSleepNotification,NSWorkspaceDidWakeNotification,NSWorkspaceScreensDidSleepNotification,NSWorkspaceScreensDidWakeNotification,NSWorkspaceSessionDidResignActiveNotification,NSWorkspaceSessionDidBecomeActiveNotification,NSProcessInfoThermalStateDidChangeNotification,NSProcessInfoPowerStateDidChangeNotification];
    /* Tokens and captured process-lifetime context are intentionally retained
     * until process exit. Removal is not used as a callback lifetime barrier. */
    static id tokens[8];for(unsigned s=0;s<8;s++){unsigned source=s;tokens[s]=[(s<6?workspace:defaults) addObserverForName:names[s] object:nil queue:nil usingBlock:^(NSNotification *note){(void)note;notify(source);}];require(tokens[s]!=nil,"observer registration");}
    NSRunningApplication *frontmost=NSWorkspace.sharedWorkspace.frontmostApplication;
    printf("{\"event\":\"registered\",\"sources\":[0,1,2,3,4,5,6,7],\"owned_bytes\":%"PRIu64",\"thermal\":%ld,\"low_power\":%d,\"frontmost_self\":%d}\n",owned,(long)NSProcessInfo.processInfo.thermalState,NSProcessInfo.processInfo.lowPowerModeEnabled,frontmost?(frontmost.processIdentifier==getpid()):-1);fflush(stdout);
    pthread_t threads[9];pthread_attr_t attr;require(!pthread_attr_init(&attr)&&!pthread_attr_setstacksize(&attr,1024*1024),"thread attributes");
    unsigned order[8];for(unsigned i=0;i<workers;i++)order[i]=i;
    for(unsigned i=workers;i>1;i--){seed=seed*UINT64_C(6364136223846793005)+1;unsigned j=(unsigned)(seed%i),v=order[i-1];order[i-1]=order[j];order[j]=v;}
    for(unsigned i=0;i<workers;i++)require(!pthread_create(&threads[order[i]],&attr,worker,&actors[order[i]]),"worker create");
    require(!pthread_create(&threads[workers],&attr,timer,&actors[workers]),"timer create");require(!pthread_attr_destroy(&attr),"thread attr destroy");
    int flags=fcntl(STDIN_FILENO,F_GETFL);require(flags>=0&&fcntl(STDIN_FILENO,F_SETFL,flags|O_NONBLOCK)==0,"command pipe");
    bool armed=false;uint64_t sent=0;char command[32];size_t used=0;id activity=nil;
    while(!atomic_load(&stopping)){
        emit_notifications(&sent);bool ready=atomic_load(&actors[workers].reserved)>0;
        for(unsigned i=0;i<=workers;i++)ready=ready&&atomic_load(&actors[i].alive);
        if(!armed&&ready){armed=true;puts("{\"event\":\"armed\"}");fflush(stdout);}
        if(atomic_load(&reversal)){puts("{\"event\":\"reversal\"}");fflush(stdout);atomic_store(&stopping,true);break;}
        char c;ssize_t n;while((n=read(STDIN_FILENO,&c,1))==1){
            if(c!='\n'){require(used<sizeof(command)-1,"command length");command[used++]=c;continue;}command[used]=0;used=0;
            if(!strcmp(command,"stop"))atomic_store(&stopping,true);
            else if(!strcmp(command,"demote")&&!strcmp(arm,"T5")&&!atomic_load(&demote)){atomic_store(&demote,true);puts("{\"event\":\"demote-published\"}");}
            else if(!strcmp(command,"activity")&&!strcmp(arm,"C1")&&!activity){activity=[NSProcessInfo.processInfo beginActivityWithOptions:NSActivityUserInitiated|NSActivityIdleSystemSleepDisabled reason:@"M21-1 bounded control"];require(activity!=nil,"activity token");puts("{\"event\":\"activity-begin\",\"token\":true}");}
            else require(false,"unexpected command");fflush(stdout);
        }
        if(n==0){fail();break;}if(n<0&&errno!=EAGAIN&&errno!=EWOULDBLOCK){fail();break;}
        @autoreleasepool {[[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.005]];}
    }
    require(!m21_close(&callbacks),"close admission");
    for(unsigned s=0;s<8;s++)[(s<6?workspace:defaults) removeObserver:tokens[s]];
    puts("{\"event\":\"admission-closed\",\"removed\":[0,1,2,3,4,5,6,7]}");fflush(stdout);
    atomic_store(&stopping,true);require(!m21_drain(&callbacks),"callback drain");
    for(unsigned i=0;i<=workers;i++)require(!pthread_join(threads[i],NULL),"join");
    if(activity){[NSProcessInfo.processInfo endActivity:activity];puts("{\"event\":\"activity-end\"}");}
    emit_notifications(&sent);serialize();puts("{\"event\":\"complete\"}");fflush(stdout);
    return atomic_load(&invalid)?2:0;
} }
