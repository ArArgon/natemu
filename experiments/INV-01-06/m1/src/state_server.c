/* M06-M1, owned state-only unchanged-state retry experiment.
 * Compile/synthetic preparation is not authorization to execute this file.
 * No signal handler, thread_set_state, pointer-state mutation or private API.
 */
#include <errno.h>
#include <mach/mach.h>
#include <mach/arm/thread_status.h>
#include <mach/thread_act.h>
#include <mach/task.h>
#include <libkern/OSCacheControl.h>
#include <pthread.h>
#include <sched.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include "mach_exc.h"
#include "mach_excServer.h"

#define BEHAVIOR (EXCEPTION_STATE | MACH_EXCEPTION_CODES)
#define COUNT ARM_THREAD_STATE64_COUNT
#define SENTINEL UINT64_C(0x1864186418641864)
#define RECEIVE_TIMEOUT_MS 60000
_Static_assert(COUNT == 68, "reviewed state layout");
_Static_assert(THREAD_STATE_NONE == 5, "arm64 decoy flavor consumed by control checker");
_Static_assert(__DARWIN_OPAQUE_ARM_THREAD_STATE64 == 0, "raw PC/SP decoder requires non-opaque arm64");
typedef struct { uint64_t value,cookie,nonce,sentinel,sp_before,sp_after; } Result;
/* AOT wrapper owns all sentinel registers across the exact faulting access.
 * x9/x10 are independent delivered cookie/nonce; x19..x22 are callee-saved
 * and their returned values plus exact SP equality are measured after retry. */
extern void m1_access(void *,void *,uint64_t,uint64_t,uint64_t,Result *);
extern uint64_t m1_load(void *);
extern void m1_store(void *,uint64_t);
extern char m1_load_fault[],m1_store_fault[];
__asm__(".text\n.p2align 2\n.globl _m1_access\n_m1_access:\n"
 "stp x29,x30,[sp,#-80]!\nstp x19,x20,[sp,#16]\nstp x21,x22,[sp,#32]\nstp x23,x24,[sp,#48]\n"
 "mov x16,x0\nmov x0,x1\nmov x1,x2\nmov x19,x3\nmov x20,x4\nmov x24,x5\n"
 "mov x9,x3\nmov x10,x4\nmovz x21,#0x1864\nmovk x21,#0x1864,lsl #16\n"
 "movk x21,#0x1864,lsl #32\nmovk x21,#0x1864,lsl #48\nmov x22,sp\nisb\nblr x16\n"
 "stp x0,x19,[x24]\nstp x20,x21,[x24,#16]\nmov x11,sp\nstp x22,x11,[x24,#32]\n"
 "ldp x23,x24,[sp,#48]\nldp x21,x22,[sp,#32]\nldp x19,x20,[sp,#16]\nldp x29,x30,[sp],#80\nret\n"
 ".p2align 2\n.globl _m1_load\n.globl _m1_load_fault\n_m1_load:\n_m1_load_fault:\nldr x0,[x0]\nret\n"
 ".p2align 2\n.globl _m1_store\n.globl _m1_store_fault\n_m1_store:\n_m1_store_fault:\nstr x1,[x0]\nret\n");

typedef struct {
 mach_msg_type_number_t count;
 exception_mask_t masks[EXC_TYPES_COUNT];
 mach_port_t ports[EXC_TYPES_COUNT];
 exception_behavior_t behaviors[EXC_TYPES_COUNT];
 thread_state_flavor_t flavors[EXC_TYPES_COUNT];
} Snapshot;
typedef struct {
 int id; pthread_t thread; thread_port_t control;
 mach_port_t front,prior;
 Snapshot initial,saved;
 _Atomic int registered,permit,done,phase,proof_done,attempted,exited;
 _Atomic int outstanding,front_seen,prior_seen,front_sent,prior_sent,receive_destroyed;
 int event; uint64_t cookie,nonce,pc,sp_lo,sp_hi;
 uint64_t *page; size_t page_size;
} Worker;
static struct { int n,jit,warmup,checked,hold_worker; unsigned seed;
 const char *access,*topology,*path,*negative,*arm,*hold; } cfg;
static Worker workers[8];
static Snapshot task_initial;
static mach_port_t port_set;
static uint32_t *jit_code;
static _Atomic int ready;
static _Atomic int hold_entered,hold_release,teardown_requested,teardown_deferred,join_attempt[8];
static pthread_mutex_t output_lock=PTHREAD_MUTEX_INITIALIZER;
static _Thread_local Worker *serving;
static _Thread_local const char *serving_role;

static void logrow(const char *fmt,...) __attribute__((format(printf,1,2)));
static void logrow(const char *fmt,...) {
 char text[8192]; va_list ap; va_start(ap,fmt);
 int length=vsnprintf(text,sizeof text,fmt,ap); va_end(ap);
 if(length<0||(size_t)length>=sizeof text) _exit(74);
 pthread_mutex_lock(&output_lock);
 if(fputs(text,stdout)<0||fputc('\n',stdout)==EOF||fflush(stdout)) _exit(74);
 pthread_mutex_unlock(&output_lock);
}
static _Noreturn void fail(const char *reason,int code) {
 logrow("{\"kind\":\"terminal\",\"reason\":\"%s\",\"code\":%d}",reason,code);
 fsync(STDOUT_FILENO); _exit(70);
}
#define REQUIRE(test,reason) do { if(!(test)) fail(reason,errno); } while(0)
static void krcheck(kern_return_t kr,const char *reason) { if(kr!=KERN_SUCCESS) fail(reason,kr); }
static bool forward(void) { return !strcmp(cfg.path,"forward"); }
static bool negative(const char *name) { return !strcmp(cfg.negative,name); }
static void trigger(Worker *w,const char *name) {
 logrow("{\"kind\":\"negative_trigger\",\"worker\":%d,\"event\":%d,\"name\":\"%s\"}",w->id,w->event,name);
 REQUIRE(!fsync(STDOUT_FILENO),"trigger durability");
}
static bool teardown_hold(void) {return !strcmp(cfg.hold,"teardown-before-restore")||!strcmp(cfg.hold,"before-right-disposal");}
static void hold_point(Worker *w,const char *name,const char *role) {
 if(strcmp(cfg.hold,name)||w->id!=cfg.hold_worker||(!teardown_hold()&&w->event!=0)) return;
 REQUIRE(!atomic_load(&hold_entered),"hold entered twice");
 logrow("{\"kind\":\"hold_enter\",\"worker\":%d,\"event\":%d,\"point\":\"%s\",\"role\":\"%s\",\"outstanding\":%d}",w->id,w->event,name,role,atomic_load(&w->outstanding));
 atomic_store_explicit(&hold_entered,1,memory_order_release);
 while(!atomic_load_explicit(&hold_release,memory_order_acquire)) sched_yield();
 logrow("{\"kind\":\"hold_exit\",\"worker\":%d,\"event\":%d,\"point\":\"%s\"}",w->id,w->event,name);
}
static void wait_done(Worker *w,int event_number) {
 while(atomic_load_explicit(&w->done,memory_order_acquire)<=event_number) {
  if(atomic_load_explicit(&teardown_requested,memory_order_acquire)&&!atomic_load(&teardown_deferred)) {
   Worker *held=&workers[cfg.hold_worker];
   int armed=atomic_load_explicit(&held->outstanding,memory_order_acquire);
   int done=atomic_load_explicit(&held->done,memory_order_acquire);
   REQUIRE(armed==1&&done==0,"held teardown request not pending");
   logrow("{\"kind\":\"teardown_deferred\",\"worker\":%d,\"outstanding\":%d,\"completed\":%d}",held->id,armed,done);
   atomic_store_explicit(&teardown_deferred,1,memory_order_release);
  }
  sched_yield();
 }
}
static void *hold_controller(void *unused) {
 (void)unused;Worker *held=&workers[cfg.hold_worker];
 while(!atomic_load_explicit(&hold_entered,memory_order_acquire)) sched_yield();
 if(teardown_hold()) {
  while(!atomic_load_explicit(&join_attempt[held->id],memory_order_acquire)) sched_yield();
  REQUIRE(!atomic_load_explicit(&held->exited,memory_order_acquire),"held worker already exited");
  logrow("{\"kind\":\"hold_competitor\",\"worker\":%d,\"operation\":\"join\",\"target\":%d}",held->id,held->id);
 } else {
  logrow("{\"kind\":\"teardown_request\",\"worker\":%d}",held->id);
  atomic_store_explicit(&teardown_requested,1,memory_order_release);
  while(!atomic_load_explicit(&teardown_deferred,memory_order_acquire)) sched_yield();
  if(cfg.n>1) {
   int competitor=(held->id+1)%cfg.n;
   for(int i=0;i<cfg.n;i++) if(i!=held->id) atomic_store_explicit(&workers[i].permit,1,memory_order_release);
   while(!atomic_load_explicit(&workers[competitor].attempted,memory_order_acquire)) sched_yield();
   bool independent=!strcmp(cfg.topology,"perworker");
   if(independent) while(atomic_load_explicit(&workers[competitor].done,memory_order_acquire)<1) sched_yield();
   logrow("{\"kind\":\"hold_competitor\",\"worker\":%d,\"operation\":\"fault\",\"target\":%d,\"completed\":%d,\"independent_server\":%s}",held->id,competitor,atomic_load(&workers[competitor].done),independent?"true":"false");
  } else logrow("{\"kind\":\"hold_competitor\",\"worker\":%d,\"operation\":\"teardown\",\"target\":%d}",held->id,held->id);
 }
 logrow("{\"kind\":\"hold_release\",\"worker\":%d,\"point\":\"%s\"}",held->id,cfg.hold);
 atomic_store_explicit(&hold_release,1,memory_order_release);return NULL;
}
static void words(const natural_t *state,mach_msg_type_number_t count,char *out,size_t size) {
 size_t used=0; out[used++]='[';
 for(unsigned i=0;i<count;i++) {
  int n=snprintf(out+used,size-used,"%s%u",i?",":"",state[i]);
  REQUIRE(n>0&&(size_t)n<size-used,"state trace size"); used+=(size_t)n;
 }
 REQUIRE(used+2<size,"state trace tail"); out[used++]=']';out[used]=0;
}
static void snapshot(Worker *w,Snapshot *s,const char *label,bool task) {
 s->count=EXC_TYPES_COUNT;
 kern_return_t kr=task?task_get_exception_ports(mach_task_self(),EXC_MASK_BAD_ACCESS,s->masks,&s->count,s->ports,s->behaviors,s->flavors):
 thread_get_exception_ports(w->control,EXC_MASK_BAD_ACCESS,s->masks,&s->count,s->ports,s->behaviors,s->flavors);
 krcheck(kr,"snapshot");
 logrow("{\"kind\":\"snapshot\",\"worker\":%d,\"label\":\"%s\",\"count\":%u}",task?-1:w->id,label,s->count);
 for(unsigned i=0;i<s->count;i++) logrow("{\"kind\":\"snapshot_tuple\",\"worker\":%d,\"label\":\"%s\",\"index\":%u,\"mask\":%u,\"port\":%u,\"behavior\":%d,\"flavor\":%d}",task?-1:w->id,label,i,s->masks[i],s->ports[i],s->behaviors[i],s->flavors[i]);
}
static bool same_snapshot(const Snapshot *a,const Snapshot *b) {
 /* Selected mask is one bit. Normalize absent/null rows and row ordering. */
 for(unsigned bit=0;bit<32;bit++) if(EXC_MASK_BAD_ACCESS&(1u<<bit)) {
  mach_port_t ap=0,bp=0; int ab=0,bb=0,af=0,bf=0;
  for(unsigned i=0;i<a->count;i++) if(a->masks[i]&(1u<<bit)) {ap=a->ports[i];ab=a->behaviors[i];af=a->flavors[i];}
  for(unsigned i=0;i<b->count;i++) if(b->masks[i]&(1u<<bit)) {bp=b->ports[i];bb=b->behaviors[i];bf=b->flavors[i];}
  if(ap!=bp||(ap&&(ab!=bb||af!=bf))) return false;
 }
 return true;
}
static void dispose(int id,const char *right,kern_return_t kr) {
 logrow("{\"kind\":\"disposal\",\"worker\":%d,\"right\":\"%s\",\"kr\":%d}",id,right,kr); krcheck(kr,"disposal");
}
static void release_snapshot(int id,Snapshot *s,const char *label) {
 for(unsigned i=0;i<s->count;i++) if(MACH_PORT_VALID(s->ports[i])) {
  char name[80];snprintf(name,sizeof name,"%s-%u",label,i);
  dispose(id,name,mach_port_deallocate(mach_task_self(),s->ports[i]));
 }
}
static void restore(Worker *w,const Snapshot *saved,const char *label) {
 REQUIRE(!atomic_load(&w->outstanding),"restore while armed");
 krcheck(thread_set_exception_ports(w->control,EXC_MASK_BAD_ACCESS,MACH_PORT_NULL,EXCEPTION_DEFAULT,THREAD_STATE_NONE),"clear selected mask");
 for(unsigned i=0;i<saved->count;i++) if(MACH_PORT_VALID(saved->ports[i]))
  krcheck(thread_set_exception_ports(w->control,saved->masks[i]&EXC_MASK_BAD_ACCESS,saved->ports[i],saved->behaviors[i],saved->flavors[i]),"restore tuple");
 Snapshot observed; snapshot(w,&observed,label,false);
 bool equal=same_snapshot(saved,&observed);
 logrow("{\"kind\":\"teardown_restore\",\"worker\":%d,\"label\":\"%s\",\"semantic_equal\":%s,\"alive\":true,\"disarmed\":true}",w->id,label,equal?"true":"false");
 REQUIRE(equal,"restoration mismatch"); release_snapshot(w->id,&observed,label);
}
static mach_port_t owned_port(void) {
 mach_port_t port; krcheck(mach_port_allocate(mach_task_self(),MACH_PORT_RIGHT_RECEIVE,&port),"receive allocation");
 krcheck(mach_port_insert_right(mach_task_self(),port,port,MACH_MSG_TYPE_MAKE_SEND),"insert send"); return port;
}
static void register_worker(Worker *w) {
 if(!strcmp(cfg.arm,"prior-handler-unsupported")) {
  w->control=mach_thread_self();Snapshot before;snapshot(w,&before,"before-decoy",false);
  for(unsigned i=0;i<before.count;i++) if(MACH_PORT_VALID(before.ports[i])) fail("preexisting handler before decoy",0);
  mach_port_t decoy=owned_port();
  krcheck(thread_set_exception_ports(w->control,EXC_MASK_BAD_ACCESS,decoy,EXCEPTION_DEFAULT,THREAD_STATE_NONE),"install owned decoy");
  logrow("{\"kind\":\"decoy_installed\",\"worker\":%d,\"port\":%u,\"mask\":%u,\"behavior\":%d,\"flavor\":%d}",w->id,decoy,EXC_MASK_BAD_ACCESS,EXCEPTION_DEFAULT,THREAD_STATE_NONE);
  /* The ordinary admission check below must refuse this actual owned route.
   * The child exits without faulting or invoking the deliberately unserved port. */
  dispose(w->id,"decoy-control-inspection",mach_port_deallocate(mach_task_self(),w->control));
 }
 w->control=mach_thread_self();snapshot(w,&w->initial,"initial",false);
 for(unsigned i=0;i<w->initial.count;i++) if(MACH_PORT_VALID(w->initial.ports[i])) {
  logrow("{\"kind\":\"prior_handler_unsupported\",\"worker\":%d}",w->id);
  fsync(STDOUT_FILENO); _exit(78); /* fatal child: kernel reclaims owned resources */
 }
 if(forward()) {
  w->prior=owned_port();
  krcheck(thread_set_exception_ports(w->control,EXC_MASK_BAD_ACCESS,w->prior,BEHAVIOR,ARM_THREAD_STATE64),"install prior");
  snapshot(w,&w->saved,"saved-prior",false);
  REQUIRE(w->saved.count==1&&w->saved.ports[0]==w->prior&&w->saved.behaviors[0]==(exception_behavior_t)BEHAVIOR&&w->saved.flavors[0]==ARM_THREAD_STATE64,"captured prior contract");
 }
 w->front=owned_port();
 krcheck(thread_set_exception_ports(w->control,EXC_MASK_BAD_ACCESS,w->front,BEHAVIOR,ARM_THREAD_STATE64),"install front");
 logrow("{\"kind\":\"register\",\"worker\":%d,\"recv_port\":%u,\"prior_recv_port\":%u,\"published\":true}",w->id,w->front,w->prior);
 atomic_store_explicit(&w->registered,1,memory_order_release);
}
static Worker *lookup(mach_port_t port) {
 for(int i=0;i<cfg.n;i++) if(atomic_load_explicit(&workers[i].registered,memory_order_acquire)&&
 (workers[i].front==port||workers[i].prior==port)) return &workers[i];
 return NULL;
}

kern_return_t catch_mach_exception_raise(mach_port_t p,mach_port_t t,mach_port_t k,exception_type_t e,mach_exception_data_t c,mach_msg_type_number_t n) {
 (void)p;(void)t;(void)k;(void)e;(void)c;(void)n;fail("unexpected routine",2405);
}
kern_return_t catch_mach_exception_raise_state_identity(mach_port_t p,mach_port_t t,mach_port_t k,exception_type_t e,mach_exception_data_t c,mach_msg_type_number_t n,int *f,thread_state_t o,mach_msg_type_number_t oc,thread_state_t r,mach_msg_type_number_t *rc) {
 (void)p;(void)t;(void)k;(void)e;(void)c;(void)n;(void)f;(void)o;(void)oc;(void)r;(void)rc;fail("unexpected routine",2407);
}
kern_return_t catch_mach_exception_raise_state(mach_port_t port,exception_type_t exception,const mach_exception_data_t code,mach_msg_type_number_t codes,int *flavor,const thread_state_t old_state,mach_msg_type_number_t count,thread_state_t new_state,mach_msg_type_number_t *new_count) {
 Worker *w=lookup(port);REQUIRE(w&&w==serving,"registered receive identity");
 bool prior=port==w->prior;const char *role=prior?"prior":"front";
 REQUIRE(exception==EXC_BAD_ACCESS&&codes==2&&*flavor==ARM_THREAD_STATE64&&count==COUNT,"state shape");
 REQUIRE(atomic_load_explicit(&w->outstanding,memory_order_acquire)==1,"unarmed fault");
 _Atomic int *seen=prior?&w->prior_seen:&w->front_seen;
 if(atomic_exchange(seen,1)) fail("repeated fault before commit",0);
 arm_thread_state64_t state;memcpy(&state,old_state,sizeof state);
 uint64_t pc=arm_thread_state64_get_pc(state),sp=arm_thread_state64_get_sp(state);
 bool valid=pc==w->pc&&sp>=w->sp_lo&&sp<w->sp_hi&&state.__x[9]==w->cookie&&state.__x[10]==w->nonce&&
 state.__x[19]==w->cookie&&state.__x[20]==w->nonce&&state.__x[21]==SENTINEL&&state.__x[22]==sp&&
 code[0]==KERN_PROTECTION_FAILURE&&(uint64_t)code[1]==(uintptr_t)w->page;
 char old_words[1600];words(old_state,count,old_words,sizeof old_words);
 logrow("{\"kind\":\"receive\",\"worker\":%d,\"event\":%d,\"role\":\"%s\",\"port\":%u,\"routine_id\":2406,\"exception\":%d,\"codeCnt\":%u,\"code\":[%lld,%llu],\"flavor\":%d,\"old_stateCnt\":%u,\"state\":%s}",w->id,w->event,role,port,exception,codes,(long long)code[0],(unsigned long long)code[1],*flavor,count,old_words);
 logrow("{\"kind\":\"identity\",\"worker\":%d,\"event\":%d,\"role\":\"%s\",\"pc\":%llu,\"sp\":%llu,\"cookie\":%llu,\"nonce\":%llu,\"valid\":%s}",w->id,w->event,role,(unsigned long long)pc,(unsigned long long)sp,(unsigned long long)state.__x[9],(unsigned long long)state.__x[10],valid?"true":"false");
 REQUIRE(valid,"identity mismatch");
 if(prior||!forward()) hold_point(w,"server-before-repair",role);
 if(negative("server-stall")&&!prior) {trigger(w,cfg.negative);for(;;) pause();}
 if(negative("prior-server-failure")&&prior) {trigger(w,cfg.negative);fail("prior server failure",0);}
 if(!strcmp(cfg.arm,"terminal")) {trigger(w,"terminal");return KERN_FAILURE;}
 if(!prior&&forward()) {
  hold_point(w,"front-before-forward",role);
  REQUIRE(w->saved.ports[0]==w->prior&&w->prior!=w->front,"one-hop owned chain");
  int f=*flavor; natural_t returned[COUNT];mach_msg_type_number_t returned_count=COUNT;
  mach_exception_data_type_t c[2]={code[0],code[1]};
  kern_return_t kr=mach_exception_raise_state(w->saved.ports[0],exception,c,2,&f,old_state,count,returned,&returned_count);
  REQUIRE(kr==KERN_SUCCESS&&f==*flavor&&returned_count==count&&!memcmp(returned,old_state,sizeof returned),"forward reply failure");
  char result[1600];words(returned,returned_count,result,sizeof result);
  logrow("{\"kind\":\"forward_call\",\"worker\":%d,\"event\":%d,\"prior_kr\":%d,\"prior_flavor\":%d,\"prior_stateCnt\":%u,\"state\":%s}",w->id,w->event,kr,f,returned_count,result);
 } else {
  bool skip=negative("skipped-repair-nominal-success");
  if(skip) trigger(w,cfg.negative);
  errno=0;int result=skip?0:mprotect(w->page,w->page_size,PROT_READ|PROT_WRITE);int error=errno;
  logrow("{\"kind\":\"repair\",\"worker\":%d,\"event\":%d,\"role\":\"%s\",\"address\":%llu,\"length\":%zu,\"mprotect_result\":%d,\"errno\":%d,\"skipped\":%s}",w->id,w->event,role,(unsigned long long)(uintptr_t)w->page,w->page_size,result,error,skip?"true":"false");
  REQUIRE(result==0,"repair failed");
  if(negative("corrupted-repair-result")) {trigger(w,cfg.negative);*w->page^=1;}
 }
 if(prior) hold_point(w,"prior-before-reply",role);
 else hold_point(w,"server-before-reply",role);
 REQUIRE(*new_count>=count,"reply capacity");*new_count=count;
 memcpy(new_state,old_state,count*sizeof(natural_t));
 char reply[1600];words(new_state,*new_count,reply,sizeof reply);
 logrow("{\"kind\":\"reply\",\"worker\":%d,\"event\":%d,\"role\":\"%s\",\"kr\":0,\"flavor\":%d,\"new_stateCnt\":%u,\"state\":%s}",w->id,w->event,role,*flavor,*new_count,reply);
 return KERN_SUCCESS;
}

static _Noreturn void receive_failure(mach_port_t port,mach_msg_return_t mr) {
 Worker *w=lookup(port);
 bool owned=negative("receive-right-destruction")&&w==&workers[0]&&port==w->front&&
            (mr==MACH_RCV_PORT_DIED||mr==MACH_RCV_INVALID_NAME||mr==MACH_RCV_PORT_CHANGED);
 /* Published XNU maps THREAD_RESTART on an invalidated receive waitq to
  * PORT_CHANGED despite the SDK's obsolete-error comment. Preserve attribution
  * before termination; this never attributes a later process signal's cause. */
 if(owned) while(!atomic_load_explicit(&w->receive_destroyed,memory_order_acquire)) sched_yield();
 int destroyed=w?atomic_load_explicit(&w->receive_destroyed,memory_order_acquire):0;
 logrow("{\"kind\":\"receive_failure\",\"schema\":\"M06-M1-receive-failure-v1\",\"worker\":%d,\"port\":%u,\"role\":\"%s\",\"code\":%d,\"destruction_observed\":%s}",
        w?w->id:-1,port,w?(port==w->front?"front":"prior"):"unknown",mr,destroyed?"true":"false");
 fail(owned?"owned receive right invalidated":"receive failure",mr);
}

/* Keep MIG's generated parser. This wrapper validates only the admitted fixed
 * routine envelope and owns the received reply right until successful send.
 * Successful MOVE_SEND_ONCE consumes it. Failed send/rejected request exits
 * the whole owned child; no ambiguous right is reused or deallocated twice. */
static void serve(mach_port_t port,int events) {
 union { union __RequestUnion__catch_mach_exc_subsystem request;
         unsigned char bytes[sizeof(union __RequestUnion__catch_mach_exc_subsystem)+MAX_TRAILER_SIZE]; } in;
 union __ReplyUnion__catch_mach_exc_subsystem out;
 for(int i=0;i<events;i++) {
  memset(&in,0,sizeof in);memset(&out,0,sizeof out);
  mach_msg_header_t *head=(void *)&in,*reply=(void *)&out;
  /* The parent's 30s total/5s no-progress watchdog owns containment, including
   * an idle peer receiver while only the deliberately stalled worker is admitted. */
  mach_msg_return_t mr=mach_msg(head,MACH_RCV_MSG|MACH_RCV_TIMEOUT|MACH_RCV_TRAILER_TYPE(MACH_MSG_TRAILER_FORMAT_0)|MACH_RCV_TRAILER_ELEMENTS(MACH_RCV_TRAILER_NULL),0,sizeof in,port,RECEIVE_TIMEOUT_MS,MACH_PORT_NULL);
  if(mr!=MACH_MSG_SUCCESS) receive_failure(port,mr);
  Worker *w=lookup(head->msgh_local_port);REQUIRE(w,"unregistered wire port");
  int outstanding=atomic_load_explicit(&w->outstanding,memory_order_acquire);
  serving=w;serving_role=head->msgh_local_port==w->prior?"prior":"front";
  REQUIRE(head->msgh_size>=sizeof *head&&head->msgh_size<=sizeof in,"receive buffer bounds");
  char raw[sizeof in.bytes*2+1];
  for(unsigned b=0;b<head->msgh_size;b++) snprintf(raw+2*b,3,"%02x",in.bytes[b]);
  if(outstanding==1&&!strcmp(cfg.hold,"server-after-receive")&&w->id==cfg.hold_worker&&w->event==0&&!strcmp(serving_role,"front")) {
   logrow("{\"kind\":\"received_message\",\"worker\":%d,\"event\":%d,\"role\":\"front\",\"size\":%u,\"raw\":\"%s\"}",w->id,w->event,head->msgh_size,raw);
   hold_point(w,"server-after-receive",serving_role);
  }
  REQUIRE(outstanding==1,"wire before admission");
  typedef __Request__mach_exception_raise_state_t Request;
  size_t size=offsetof(Request,old_state)+COUNT*sizeof(natural_t);
  REQUIRE(!(head->msgh_bits&MACH_MSGH_BITS_COMPLEX)&&head->msgh_id==2406&&head->msgh_size==size&&
   MACH_MSGH_BITS_REMOTE(head->msgh_bits)==MACH_MSG_TYPE_MOVE_SEND_ONCE&&MACH_PORT_VALID(head->msgh_remote_port),"wire envelope");
  Request *request=(void *)head;
  REQUIRE(!memcmp(&request->NDR,&NDR_record,sizeof NDR_record)&&request->codeCnt==2&&request->old_stateCnt==COUNT,"wire NDR/count");
  mach_msg_trailer_t trailer;memcpy(&trailer,in.bytes+round_msg(head->msgh_size),sizeof trailer);
  REQUIRE(trailer.msgh_trailer_type==MACH_MSG_TRAILER_FORMAT_0&&trailer.msgh_trailer_size==sizeof trailer,"wire trailer");
  logrow("{\"kind\":\"wire\",\"worker\":%d,\"event\":%d,\"role\":\"%s\",\"size\":%u,\"bits\":%u,\"ndr_exact\":true,\"trailer_size\":%u,\"reply_right\":%u,\"raw\":\"%s\"}",w->id,w->event,serving_role,head->msgh_size,head->msgh_bits,trailer.msgh_trailer_size,head->msgh_remote_port,raw);
  REQUIRE(mach_exc_server(head,reply),"generated demux rejection");
  if(negative("reply-send-failure")) {
   trigger(w,cfg.negative);
   mach_port_t dropped=reply->msgh_remote_port;
   kern_return_t dropped_kr=mach_port_deallocate(mach_task_self(),dropped);
   logrow("{\"kind\":\"negative_action\",\"worker\":%d,\"event\":%d,\"action\":\"drop-reply-right\",\"port\":%u,\"kr\":%d}",w->id,w->event,dropped,dropped_kr);
   krcheck(dropped_kr,"drop owned reply right");
   reply->msgh_remote_port=MACH_PORT_NULL;
  }
  mr=mach_msg(reply,MACH_SEND_MSG|MACH_SEND_TIMEOUT,reply->msgh_size,0,MACH_PORT_NULL,1000,MACH_PORT_NULL);
  logrow("{\"kind\":\"send\",\"worker\":%d,\"event\":%d,\"role\":\"%s\",\"kr\":%d,\"disposition\":\"move-send-once\"}",w->id,w->event,serving_role,mr);
  if(mr!=MACH_MSG_SUCCESS) fail("reply send failure",mr);
  atomic_store_explicit(!strcmp(serving_role,"prior")?&w->prior_sent:&w->front_sent,1,memory_order_release);
 }
}
typedef struct { mach_port_t port;int count; } Server;
static void *server_thread(void *arg) {Server *s=arg;serve(s->port,s->count);return NULL;}
static void event(Worker *w,int number,bool proof) {
 w->event=number;w->nonce=(uint64_t)number+1;
 w->pc=cfg.jit?(uintptr_t)(jit_code+(!strcmp(cfg.access,"store")?4:0)):
  (uintptr_t)(!strcmp(cfg.access,"store")?m1_store_fault:m1_load_fault);
 atomic_store(&w->front_seen,0);atomic_store(&w->prior_seen,0);atomic_store(&w->front_sent,0);atomic_store(&w->prior_sent,0);
 *w->page=UINT64_C(0x1111111111111111);
 uint64_t expected=!strcmp(cfg.access,"load")?*w->page:UINT64_C(0x2222222222222222)^w->nonce;
 REQUIRE(mprotect(w->page,w->page_size,PROT_NONE)==0,"arm protection");
 if(negative("wrong-event-descriptor")) {trigger(w,cfg.negative);w->pc+=4;}
 logrow("{\"kind\":\"arm\",\"worker\":%d,\"event\":%d,\"warmup\":%s,\"proof\":%s,\"address\":%llu,\"page_size\":%zu,\"access\":\"%s\",\"cookie\":%llu,\"nonce\":%llu,\"expected_pc\":%llu,\"sp_lo\":%llu,\"sp_hi\":%llu,\"expected_data\":%llu}",w->id,number,number<cfg.warmup?"true":"false",proof?"true":"false",(unsigned long long)(uintptr_t)w->page,w->page_size,cfg.access,(unsigned long long)w->cookie,(unsigned long long)w->nonce,(unsigned long long)w->pc,(unsigned long long)w->sp_lo,(unsigned long long)w->sp_hi,(unsigned long long)expected);
 atomic_store_explicit(&w->outstanding,1,memory_order_release);
 hold_point(w,"worker-armed-before-fault","worker");
 if(*cfg.hold) {
  logrow("{\"kind\":\"fault_attempt\",\"worker\":%d,\"event\":%d}",w->id,number);
  atomic_store_explicit(&w->attempted,number+1,memory_order_release);
 }
 if(negative("receive-right-destruction")) {
  trigger(w,cfg.negative);kern_return_t destroyed=mach_port_mod_refs(mach_task_self(),w->front,MACH_PORT_RIGHT_RECEIVE,-1);
  logrow("{\"kind\":\"negative_action\",\"worker\":%d,\"event\":%d,\"action\":\"destroy-receive\",\"port\":%u,\"kr\":%d}",w->id,w->event,w->front,destroyed);
  krcheck(destroyed,"destroy receive");
  atomic_store_explicit(&w->receive_destroyed,1,memory_order_release);
 }
 if(negative("unexpected-helper-fault")) {
  uint64_t *other=mmap(NULL,w->page_size,PROT_NONE,MAP_PRIVATE|MAP_ANON,-1,0);
  REQUIRE(other!=MAP_FAILED,"helper owned mapping");trigger(w,cfg.negative);
  logrow("{\"kind\":\"negative_action\",\"worker\":%d,\"event\":%d,\"action\":\"helper-access\",\"address\":%llu,\"length\":%zu,\"protection\":0}",w->id,w->event,(unsigned long long)(uintptr_t)other,w->page_size);
  volatile uint64_t value=*other;(void)value;
 }
 void *fn=cfg.jit?(void *)(jit_code+(!strcmp(cfg.access,"store")?4:0)):
  (!strcmp(cfg.access,"store")?(void *)m1_store:(void *)m1_load);
 Result r={0};m1_access(fn,w->page,expected,w->cookie,w->nonce,&r);
 bool data=(!strcmp(cfg.access,"load")?r.value:*w->page)==expected;
 bool sentinels=r.cookie==w->cookie&&r.nonce==w->nonce&&r.sentinel==SENTINEL;
 bool sp=r.sp_before==r.sp_after&&r.sp_before>=w->sp_lo&&r.sp_before<w->sp_hi;
 logrow("{\"kind\":\"resume\",\"worker\":%d,\"event\":%d,\"data_ok\":%s,\"sentinels_ok\":%s,\"sp_ok\":%s,\"data\":%llu,\"cookie\":%llu,\"nonce\":%llu,\"sentinel\":%llu,\"sp_before\":%llu,\"sp_after\":%llu}",w->id,number,data?"true":"false",sentinels?"true":"false",sp?"true":"false",(unsigned long long)(!strcmp(cfg.access,"load")?r.value:*w->page),(unsigned long long)r.cookie,(unsigned long long)r.nonce,(unsigned long long)r.sentinel,(unsigned long long)r.sp_before,(unsigned long long)r.sp_after);
 REQUIRE(data&&sentinels&&sp,"resumed state/data mismatch");
 /* A reply can resume the worker before mach_msg returns on its server.
  * Wait for explicit successful send completion before reusing event fields. */
 while(!atomic_load_explicit(proof?&w->prior_sent:&w->front_sent,memory_order_acquire)) sched_yield();
 if(forward()) while(!atomic_load_explicit(&w->prior_sent,memory_order_acquire)) sched_yield();
 hold_point(w,"worker-resumed-before-disarm","worker");
 logrow("{\"kind\":\"commit\",\"worker\":%d,\"event\":%d}",w->id,number);
 atomic_store_explicit(&w->outstanding,0,memory_order_release);
 logrow("{\"kind\":\"disarm\",\"worker\":%d,\"event\":%d}",w->id,number);
}
static void *worker_thread(void *arg) {
 Worker *w=arg;w->control=MACH_PORT_NULL;w->page_size=(size_t)getpagesize();
 w->page=mmap(NULL,w->page_size,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANON,-1,0);REQUIRE(w->page!=MAP_FAILED,"worker page");
 w->sp_hi=(uintptr_t)pthread_get_stackaddr_np(pthread_self());w->sp_lo=w->sp_hi-pthread_get_stacksize_np(pthread_self());
 w->cookie=UINT64_C(0x9a5f000000000000)|(uint64_t)(cfg.seed+(unsigned)w->id*0x1000193u);
 register_worker(w);while(!atomic_load_explicit(&ready,memory_order_acquire)) sched_yield();
 int total=cfg.warmup+cfg.checked;
 for(int e=0;e<total;e++) {while(atomic_load_explicit(&w->permit,memory_order_acquire)<=e) sched_yield();event(w,e,false);atomic_store_explicit(&w->done,e+1,memory_order_release);}
 logrow("{\"kind\":\"worker_parked\",\"worker\":%d,\"completed\":%d,\"disarmed\":true}",w->id,total);
 while(atomic_load_explicit(&w->phase,memory_order_acquire)<1) sched_yield();
 if(forward()) {
  restore(w,&w->saved,"restored-prior");event(w,total,true);
  logrow("{\"kind\":\"restoration_proof\",\"worker\":%d,\"event\":%d}",w->id,total);
 }
 atomic_store_explicit(&w->proof_done,1,memory_order_release);
 while(atomic_load_explicit(&w->phase,memory_order_acquire)<2) sched_yield();
 hold_point(w,"teardown-before-restore","worker");
 restore(w,&w->initial,"restored-initial");
 hold_point(w,"before-right-disposal","worker");
 release_snapshot(w->id,&w->initial,"initial");if(forward()) release_snapshot(w->id,&w->saved,"saved-prior");
 dispose(w->id,"send",mach_port_mod_refs(mach_task_self(),w->front,MACH_PORT_RIGHT_SEND,-1));
 dispose(w->id,"receive",mach_port_mod_refs(mach_task_self(),w->front,MACH_PORT_RIGHT_RECEIVE,-1));
 if(forward()) {dispose(w->id,"prior_send",mach_port_mod_refs(mach_task_self(),w->prior,MACH_PORT_RIGHT_SEND,-1));dispose(w->id,"prior_receive",mach_port_mod_refs(mach_task_self(),w->prior,MACH_PORT_RIGHT_RECEIVE,-1));}
 dispose(w->id,"thread_self",mach_port_deallocate(mach_task_self(),w->control));
 dispose(w->id,"data_page",munmap(w->page,w->page_size));
 logrow("{\"kind\":\"worker_exit\",\"worker\":%d}",w->id);atomic_store_explicit(&w->exited,1,memory_order_release);return NULL;
}
static int write_jit(void *unused) {
 (void)unused;jit_code[0]=0xf9400000;jit_code[1]=0xd65f03c0;jit_code[4]=0xf9000001;jit_code[5]=0xd65f03c0;return 0;
}
PTHREAD_JIT_WRITE_ALLOW_CALLBACKS_NP(write_jit);
static unsigned number(const char *arg) {char *end;unsigned long value=strtoul(arg,&end,10);REQUIRE(*arg&&!*end&&value<=100000,"numeric argument");return (unsigned)value;}
int main(int argc,char **argv) {
 cfg.n=1;cfg.warmup=10;cfg.checked=100;cfg.seed=60601;cfg.access="load";cfg.topology="serial";cfg.path="direct";cfg.negative="";cfg.arm="";cfg.hold="";
 for(int i=1;i<argc;i+=2) {
  REQUIRE(i+1<argc,"argument value");const char *k=argv[i],*v=argv[i+1];
  if(!strcmp(k,"--n")) cfg.n=(int)number(v);else if(!strcmp(k,"--jit")) cfg.jit=(int)number(v);
  else if(!strcmp(k,"--warmup")) cfg.warmup=(int)number(v);else if(!strcmp(k,"--checked")) cfg.checked=(int)number(v);else if(!strcmp(k,"--seed")) cfg.seed=number(v);
  else if(!strcmp(k,"--access")) cfg.access=v;else if(!strcmp(k,"--topology")) cfg.topology=v;else if(!strcmp(k,"--path")) cfg.path=v;
  else if(!strcmp(k,"--negative")) cfg.negative=v;else if(!strcmp(k,"--arm")) cfg.arm=v;
  else if(!strcmp(k,"--hold")) cfg.hold=v;else if(!strcmp(k,"--hold_worker")) cfg.hold_worker=(int)number(v);else fail("unsupported argument",64);
 }
 REQUIRE((cfg.n==1||cfg.n==2||cfg.n==4||cfg.n==8)&&(cfg.jit==0||cfg.jit==1)&&cfg.checked>0,"configuration");
 REQUIRE(!strcmp(cfg.access,"load")||!strcmp(cfg.access,"store"),"access");
 REQUIRE(!strcmp(cfg.topology,"serial")||!strcmp(cfg.topology,"perworker"),"topology");
 REQUIRE(!strcmp(cfg.path,"direct")||!strcmp(cfg.path,"forward"),"path");
 REQUIRE(cfg.hold_worker>=0&&cfg.hold_worker<cfg.n,"held worker");
 if(*cfg.hold) {
  const char *points[]={"worker-armed-before-fault","server-after-receive","server-before-repair","server-before-reply","front-before-forward","prior-before-reply","worker-resumed-before-disarm","teardown-before-restore","before-right-disposal"};
  bool known=false;for(unsigned i=0;i<sizeof points/sizeof points[0];i++) if(!strcmp(cfg.hold,points[i])) known=true;
  REQUIRE(known&&!*cfg.negative&&!*cfg.arm,"hold configuration");
  REQUIRE(forward()||(strcmp(cfg.hold,"front-before-forward")&&strcmp(cfg.hold,"prior-before-reply")),"hold requires prior");
 }
 logrow("{\"kind\":\"meta\",\"schema\":\"M06-M1-trace-v3\",\"investigation\":\"INV-01-06-M1\",\"n\":%d,\"jit\":%d,\"access\":\"%s\",\"topology\":\"%s\",\"path\":\"%s\",\"seed\":%u,\"warmup\":%d,\"checked\":%d,\"negative\":\"%s\",\"arm\":\"%s\",\"routine_id\":2406}",cfg.n,cfg.jit,cfg.access,cfg.topology,cfg.path,cfg.seed,cfg.warmup,cfg.checked,cfg.negative,cfg.arm);
 if(*cfg.hold) logrow("{\"kind\":\"hold_configuration\",\"point\":\"%s\",\"worker\":%d,\"controller_threads\":1}",cfg.hold,cfg.hold_worker);
 snapshot(NULL,&task_initial,"task-initial",true);
 if(cfg.jit) {
  jit_code=mmap(NULL,(size_t)getpagesize(),PROT_READ|PROT_WRITE|PROT_EXEC,MAP_PRIVATE|MAP_ANON|MAP_JIT,-1,0);
  if(jit_code==MAP_FAILED) {logrow("{\"kind\":\"unavailable\",\"api\":\"MAP_JIT\",\"errno\":%d}",errno);return 78;}
  REQUIRE(pthread_jit_write_with_callback_np(write_jit,NULL)==0,"JIT write callback");sys_icache_invalidate(jit_code,32);
 }
 for(int i=0;i<cfg.n;i++) {workers[i].id=i;krcheck(pthread_create(&workers[i].thread,NULL,worker_thread,&workers[i]),"worker creation");}
 for(int i=0;i<cfg.n;i++) while(!atomic_load_explicit(&workers[i].registered,memory_order_acquire)) sched_yield();
 int total=cfg.warmup+cfg.checked;pthread_t fronts[8],priors[8];Server front_args[8],prior_args[8];
 bool serial=!strcmp(cfg.topology,"serial");
 if(serial) {krcheck(mach_port_allocate(mach_task_self(),MACH_PORT_RIGHT_PORT_SET,&port_set),"port set");for(int i=0;i<cfg.n;i++) krcheck(mach_port_move_member(mach_task_self(),workers[i].front,port_set),"port set membership");}
 for(int i=0;i<(serial?1:cfg.n);i++) {front_args[i]=(Server){serial?port_set:workers[i].front,total*(serial?cfg.n:1)+(negative("skipped-repair-nominal-success")?1:0)};krcheck(pthread_create(&fronts[i],NULL,server_thread,&front_args[i]),"front creation");}
 if(forward()) for(int i=0;i<cfg.n;i++) {prior_args[i]=(Server){workers[i].prior,total+1};krcheck(pthread_create(&priors[i],NULL,server_thread,&prior_args[i]),"prior creation");}
 pthread_t controller;if(*cfg.hold) krcheck(pthread_create(&controller,NULL,hold_controller,NULL),"hold controller creation");
 atomic_store_explicit(&ready,1,memory_order_release);
 /* Both deterministic admissions within every child: even rounds rotate
  * workers; odd rounds release all, then await every real completed marker. */
 for(int e=0;e<total;e++) {
  logrow("{\"kind\":\"admission_schedule\",\"event\":%d,\"mode\":\"%s\"}",e,e%2?"together":"rotated");
  for(int i=0;i<cfg.n;i++) {int id=(i+e+((*cfg.hold&&e==0)?cfg.hold_worker:0))%cfg.n;atomic_store_explicit(&workers[id].permit,e+1,memory_order_release);if(!(e%2)) wait_done(&workers[id],e);}
  for(int i=0;i<cfg.n;i++) wait_done(&workers[i],e);
 }
 for(int i=0;i<(serial?1:cfg.n);i++) krcheck(pthread_join(fronts[i],NULL),"front join");
 for(int i=0;i<cfg.n;i++) atomic_store_explicit(&workers[i].phase,1,memory_order_release);
 for(int i=0;i<cfg.n;i++) while(!atomic_load_explicit(&workers[i].proof_done,memory_order_acquire)) sched_yield();
 if(forward()) for(int i=0;i<cfg.n;i++) krcheck(pthread_join(priors[i],NULL),"prior join");
 for(int i=0;i<cfg.n;i++) atomic_store_explicit(&workers[i].phase,2,memory_order_release);
 for(int i=0;i<cfg.n;i++) {
  if(teardown_hold()&&i==cfg.hold_worker) while(!atomic_load_explicit(&hold_entered,memory_order_acquire)) sched_yield();
  if(*cfg.hold) {logrow("{\"kind\":\"join_attempt\",\"worker\":%d}",i);atomic_store_explicit(&join_attempt[i],1,memory_order_release);}
  krcheck(pthread_join(workers[i].thread,NULL),"worker join");
 }
 if(*cfg.hold) krcheck(pthread_join(controller,NULL),"controller join");
 if(serial) dispose(-1,"port_set",mach_port_mod_refs(mach_task_self(),port_set,MACH_PORT_RIGHT_PORT_SET,-1));
 Snapshot final;snapshot(NULL,&final,"task-final",true);REQUIRE(same_snapshot(&task_initial,&final),"task ports changed");
 release_snapshot(-1,&final,"task-final");release_snapshot(-1,&task_initial,"task-initial");
 if(cfg.jit) dispose(-1,"jit_page",munmap(jit_code,(size_t)getpagesize()));
 logrow("{\"kind\":\"summary\",\"events_checked\":%d,\"events_warmup\":%d,\"commits_total\":%d,\"proof_events\":%d,\"zero_unclassified\":true,\"status\":\"completed\"}",cfg.n*cfg.checked,cfg.n*cfg.warmup,cfg.n*(total+(forward()?1:0)),forward()?cfg.n:0);
 return 0;
}
