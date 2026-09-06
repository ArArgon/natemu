/* M23-L1 author implementation: no native campaign before separate code review. */
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <pthread/qos.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <time.h>
#include <unistd.h>

#define EPOCHS 20
#define MAX_ACTORS 137
_Static_assert(sizeof(time_t)==sizeof(int64_t),"M23 requires signed 64-bit time_t");
_Static_assert(CLOCK_REALTIME==0,"M23 checker requires the retained Darwin realtime clock identity");
#define RECORD_BYTES 2048
struct actor { int id,role,index,fd;uint64_t seq,wait_calls[EPOCHS],wait_returns[EPOCHS]; };
static struct actor actors[MAX_ACTORS];
static int workers,helpers,cpus,strategy,cell_identity;
static qos_class_t requested_qos;
static const char *mode,*run_identity;
static pthread_mutex_t target=PTHREAD_MUTEX_INITIALIZER,control=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed=PTHREAD_COND_INITIALIZER;
static int epoch=-1,held,released,done=-1,owner_done=-1,freeze_armed;
static unsigned ready,acked,busy_seen;
static int ack_epoch[8],protected_epoch=-1,peer_updates[8];
static atomic_int helper_stop;
static void die(const char *why){dprintf(STDERR_FILENO,"M23 failure: %s\n",why);_exit(2);}
static void require(int ok,const char *why){if(!ok)die(why);}
static void record(struct actor *a,int e,const char *phase,int rc,uint64_t x,uint64_t y,uint64_t z){
    char buffer[RECORD_BYTES];
    int n=snprintf(buffer,sizeof buffer,"{\"run_id\":\"%s\",\"build_id\":\"%s\",\"cell\":%d,\"actor\":%d,\"seq\":%"PRIu64",\"epoch\":%d,\"phase\":\"%s\",\"rc\":%d,\"a\":%"PRIu64",\"b\":%"PRIu64",\"c\":%"PRIu64",\"tick\":%"PRIu64"}\n",run_identity,M23_BUILD,cell_identity,a->id,a->seq++,e,phase,rc,x,y,z,mach_absolute_time());
    require(n>0&&(size_t)n<sizeof buffer,"record overflow");size_t at=0;
    while(at<(size_t)n){ssize_t w=write(a->fd,buffer+at,(size_t)n-at);if(w<0&&errno==EINTR)continue;require(w>0,"trace write");at+=(size_t)w;}
}
static void api(struct actor *a,int e,const char *phase,int rc){record(a,e,phase,rc,0,0,0);require(rc==0,phase);}
static void lock_control(struct actor *a,int e){int rc=pthread_mutex_lock(&control);if(rc)api(a,e,"control_lock_error",rc);}
static void unlock_control(struct actor *a,int e){int rc=pthread_mutex_unlock(&control);if(rc)api(a,e,"control_unlock_error",rc);}
static void notify(struct actor *a,int e){int rc=pthread_cond_broadcast(&changed);if(rc)api(a,e,"control_notify_error",rc);}
static void wait_control(struct actor *a,int e){
    require(e>=0&&e<EPOCHS,"wait epoch");
    uint64_t prior=a->wait_calls[e]++;require(a->wait_calls[e]!=0,"wait counter overflow");
    if(!prior)record(a,e,"wait_call",0,0,0,0);
    int rc=pthread_cond_wait(&changed,&control);
    if(!prior||rc)api(a,e,"wait_return",rc);
    require(++a->wait_returns[e]!=0,"wait return overflow");
}
static void finish_actor(struct actor *a){
    if(a->role!=2)for(int e=0;e<EPOCHS;e++)record(a,e,"wait_summary",0,a->wait_calls[e],a->wait_returns[e],0);
    record(a,-1,"exit",0,0,0,0);
}
static void cpu_sample(struct actor *a,int e){
    size_t cpu=0;int rc=pthread_cpu_number_np(&cpu);
    if(!strcmp(mode,"placement")&&a->role==1&&a->index==1&&e==2)cpu=(size_t)cpus;
    record(a,e,"cpu",rc,cpu,(uint64_t)cpus,0);require(rc==0,"cpu query");
}
static void start_actor(struct actor *a){
    record(a,-1,"start",0,(uint64_t)a->role,(uint64_t)a->index,0);
    int rc=pthread_set_qos_class_self_np(requested_qos,0);api(a,-1,"qos_set",rc);
    qos_class_t actual=0;int priority=0;rc=pthread_get_qos_class_np(pthread_self(),&actual,&priority);
    record(a,-1,"qos_get",rc,actual,(uint64_t)(int64_t)priority,requested_qos);
    require(!rc&&actual==requested_qos&&priority==0,"requested QoS mismatch");cpu_sample(a,-1);
}
static void wrong_wait(struct actor *a,int e){
    /* control is owned and released==1 is already observed. Deliberately call
     * the API anyway, then preserve the real return, not a precall inference. */
    struct timespec now,deadline;int rc=clock_gettime(CLOCK_REALTIME,&now);
    if(rc)api(a,e,"realtime_error",errno);
    require(!rc&&now.tv_sec>=0&&now.tv_nsec>=0&&now.tv_nsec<1000000000,"realtime domain");
    deadline=now;long ns=deadline.tv_nsec+250000000L;
    if(ns>=1000000000L){require(deadline.tv_sec<INT64_MAX,"deadline overflow");deadline.tv_sec++;ns-=1000000000L;}
    deadline.tv_nsec=ns;
    record(a,e,"wrong_wait_clock",0,(uint64_t)now.tv_sec,(uint64_t)now.tv_nsec,CLOCK_REALTIME);
    record(a,e,"wrong_wait_call",0,(uint64_t)released,(uint64_t)deadline.tv_sec,(uint64_t)deadline.tv_nsec);
    rc=pthread_cond_timedwait(&changed,&control,&deadline);
    record(a,e,"wrong_wait_return",rc,0,0,0);
    require(rc==0||rc==ETIMEDOUT,"timed wait error");
}
static void *owner(void *arg){
    struct actor *a=arg;start_actor(a);
    for(int e=0;e<EPOCHS;e++){
        api(a,e,"target_acquired",pthread_mutex_lock(&target));
        lock_control(a,e);epoch=e;ready=acked=busy_seen=0;released=0;held=0;freeze_armed=0;
        record(a,e,"held_publish",0,1,0,0);held=1;notify(a,e);
        while(!released)wait_control(a,e);
        record(a,e,"release_receipt",0,(uint64_t)released,0,0);
        if(!strcmp(mode,"lost-wakeup")&&e==2)wrong_wait(a,e);
        unlock_control(a,e);
        protected_epoch=(!strcmp(mode,"ordering")&&e==2)?e-1:e;
        record(a,e,"data_publish",0,(uint64_t)protected_epoch,0,0);
        api(a,e,"target_unlocked",pthread_mutex_unlock(&target));
        lock_control(a,e);record(a,e,"owner_done_publish",0,1,0,0);owner_done=e;notify(a,e);
        while(done<e)wait_control(a,e);unlock_control(a,e);
    }
    finish_actor(a);return NULL;
}
static void *peer(void *arg){
    struct actor *a=arg;start_actor(a);unsigned bit=1u<<a->index;
    for(int e=0;e<EPOCHS;e++){
        lock_control(a,e);while(epoch!=e||!held)wait_control(a,e);
        record(a,e,"held_observed",0,(uint64_t)epoch,1,0);
        record(a,e,"ready_publish",0,1,0,0);ready|=bit;notify(a,e);
        if(!strcmp(mode,"frozen")&&a->index==1&&e==2){
            record(a,e,"freeze_wait_call",0,0,0,0);freeze_armed=1;
            for(;;)wait_control(a,e); /* only wait releases control on this path */
        }
        unlock_control(a,e);cpu_sample(a,e);
        uint64_t busy=0,yields=0,omitted=0;
        record(a,e,"attempt",0,(uint64_t)strategy,0,0);
        int rc;
        if(strategy==2)rc=pthread_mutex_lock(&target);
        else for(;;){
            rc=pthread_mutex_trylock(&target);if(rc!=EBUSY)break;
            busy++;if(busy==1){record(a,e,"busy_first",EBUSY,1,0,0);lock_control(a,e);busy_seen|=bit;notify(a,e);unlock_control(a,e);}
            if(strategy==1){
                if(!strcmp(mode,"missing-yield")&&a->index==1&&e==2&&omitted==0)omitted++;
                else {int yield_rc=sched_yield();if(yield_rc)api(a,e,"yield_error",errno);yields++;}
            }
        }
        api(a,e,"target_acquired",rc);
        record(a,e,"attempt_summary",0,busy,yields,omitted);
        record(a,e,"data_read",0,(uint64_t)protected_epoch,(uint64_t)peer_updates[a->index],0);
        require(protected_epoch==e&&peer_updates[a->index]==e,"protected data");
        peer_updates[a->index]++;
        record(a,e,"update",0,(uint64_t)peer_updates[a->index],0,0);
        api(a,e,"target_unlocked",pthread_mutex_unlock(&target));
        lock_control(a,e);int value=(!strcmp(mode,"stale-ack")&&a->index==1&&e==2)?e-1:e;
        record(a,e,"ack_publish",0,(uint64_t)value,0,0);ack_epoch[a->index]=value;acked|=bit;notify(a,e);unlock_control(a,e);
    }
    finish_actor(a);return NULL;
}
static void *helper(void *arg){
    struct actor *a=arg;start_actor(a);volatile uint64_t value=(uint64_t)a->id+1;uint64_t count=0;
    while(!atomic_load_explicit(&helper_stop,memory_order_acquire)){value=value*UINT64_C(6364136223846793005)+1;require(++count!=0,"helper count overflow");}
    record(a,-1,"helper_work",0,count,value,0);finish_actor(a);return NULL;
}
static int within(const char *p,const char *root){size_t n=strlen(root);return !strncmp(p,root,n)&&(p[n]=='/'||p[n]==0);}
static int number(const char *p,int low,int high){char *end;errno=0;long n=strtol(p,&end,10);require(!errno&&*p&&!*end&&n>=low&&n<=high,"integer argument");return(int)n;}
int main(int argc,char **argv){
    require(argc==10,"directory N strategy qos H mode run_id seed C");
    const char *cell=getenv("NATEMU_M23_CELL_ID");require(cell!=NULL,"missing cell identity");cell_identity=number(cell,0,278);
    workers=number(argv[2],1,8);require(workers==1||workers==2||workers==4||workers==8,"N");strategy=number(argv[3],0,2);
    int q=number(argv[4],0,1);requested_qos=q?QOS_CLASS_BACKGROUND:QOS_CLASS_USER_INTERACTIVE;
    helpers=number(argv[5],0,128);mode=argv[6];
    require(!strcmp(mode,"normal")||!strcmp(mode,"held")||!strcmp(mode,"missing-yield")||!strcmp(mode,"stale-ack")||!strcmp(mode,"lost-wakeup")||!strcmp(mode,"placement")||!strcmp(mode,"ordering")||!strcmp(mode,"frozen"),"mode");
    run_identity=argv[7];require(strlen(argv[7])==32&&strspn(argv[7],"0123456789abcdef")==32,"run identity");number(argv[8],23001,23003);cpus=number(argv[9],1,64);
    require(helpers==0||helpers==cpus||helpers==2*cpus,"H");int current=0;size_t size=sizeof current;
    require(!sysctlbyname("hw.ncpu",&current,&size,NULL,0)&&size==sizeof current&&current==cpus,"CPU count changed");
    if(strcmp(mode,"normal"))require(workers>=2&&q==1&&helpers==cpus&&strategy==(!strcmp(mode,"missing-yield")?1:2),"control cell");
    char root[PATH_MAX],directory[PATH_MAX];const char *raw=getenv("NATEMU_RAW_ROOT");
    require(raw&&realpath(raw,root)&&!strcmp(raw,root)&&strcmp(root,"/")&&!within(root,M23_REPO)&&!within(M23_REPO,root),"raw root");
    require(realpath(argv[1],directory)&&!strcmp(argv[1],directory)&&within(directory,root)&&strcmp(directory,root),"external directory");
    struct rlimit cap={32*1024*1024,32*1024*1024};require(!setrlimit(RLIMIT_FSIZE,&cap),"file cap");
    int total=workers+helpers+1;require(total<=MAX_ACTORS&&sizeof actors+total*RECORD_BYTES<256*1024*1024,"application buffers");
    for(int i=0;i<total;i++){
        struct actor *a=&actors[i];a->id=i;a->role=i==0?0:i<=workers?1:2;a->index=i==0?0:i<=workers?i-1:i-workers-1;
        char path[PATH_MAX];int n=snprintf(path,sizeof path,"%s/actor-%03d.jsonl",directory,i);require(n>0&&(size_t)n<sizeof path,"trace path");
        a->fd=open(path,O_WRONLY|O_CREAT|O_EXCL|O_APPEND|O_NOFOLLOW,0600);require(a->fd>=0,"trace create");
    }
    atomic_init(&helper_stop,0);struct actor *a=&actors[0];start_actor(a);
    record(a,-1,"config",0,(uint64_t)workers,(uint64_t)helpers,(uint64_t)strategy);
    mach_timebase_info_data_t tb;require(!mach_timebase_info(&tb)&&tb.numer&&tb.denom,"timebase");record(a,-1,"timebase",0,tb.numer,tb.denom,0);
    pthread_t threads[MAX_ACTORS-1];
    for(int i=1;i<total;i++){int rc=pthread_create(&threads[i-1],NULL,i==1?owner:i<=workers?peer:helper,&actors[i]);record(a,-1,"create",rc,(uint64_t)i,0,0);require(!rc,"create");}
    for(int e=0;e<EPOCHS;e++){
        unsigned all=((1u<<workers)-1)&~1u;
        lock_control(a,e);while(epoch!=e||!held||ready!=all)wait_control(a,e);
        record(a,e,"held_observed",0,(uint64_t)epoch,ready,0);unlock_control(a,e);
        struct timespec delay={0,100000000};int rc;do{rc=nanosleep(&delay,&delay);}while(rc&&errno==EINTR);if(rc)api(a,e,"hold_error",errno);
        lock_control(a,e);
        if(!strcmp(mode,"held")&&e==2){record(a,e,"withheld",0,1,0,0);for(;;)wait_control(a,e);}
        /* Exposure is independently checked from the real first-EBUSY record.
         * Owner remains held until peer1 publishes that observation. */
        if(!strcmp(mode,"missing-yield")&&e==2)while(!(busy_seen&2u))wait_control(a,e);
        record(a,e,"release_publish",0,1,0,0);released=1;notify(a,e);
        int saw_owner=0;
        while(owner_done!=e||acked!=all) {
            if(owner_done==e&&!saw_owner){record(a,e,"owner_done_observed",0,1,0,0);saw_owner=1;}
            if(freeze_armed){record(a,e,"freeze_observed",0,1,0,0);freeze_armed=0;}
            wait_control(a,e);
        }
        if(!saw_owner)record(a,e,"owner_done_observed",0,1,0,0);
        for(int i=1;i<workers;i++){record(a,e,"ack_observed",0,(uint64_t)i,(uint64_t)ack_epoch[i],0);require(ack_epoch[i]==e,"stale acknowledgement");}
        record(a,e,"epoch_done",0,(uint64_t)workers,0,0);done=e;held=0;notify(a,e);unlock_control(a,e);
    }
    atomic_store_explicit(&helper_stop,1,memory_order_release);
    for(int i=1;i<total;i++){int rc=pthread_join(threads[i-1],NULL);record(a,-1,"join",rc,(uint64_t)i,0,0);require(!rc,"join");}
    finish_actor(a);
    for(int i=0;i<total;i++)require(!close(actors[i].fd),"trace close");return 0;
}
