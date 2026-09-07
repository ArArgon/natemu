#include <Hypervisor/Hypervisor.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "q34.h"

static void *ram;
static int backing = -1;
static struct shared *s;
static struct q30_state *q;
static struct q31_state *f;
static struct q32_state *interrupts;
static struct q33_state *code;
static struct q34_state *startup;
static struct q33_host_transaction transactions[MAX_CPUS];
static u64 transfer_pc, transfer_count[MAX_CPUS];
static unsigned ncpus;
static int entry_control, use_gic = 1, initial_tso = 1;
static u64 initial_sctlr = 0x30d00800;
static int entry_seen[MAX_CPUS];
static int checkpoint_seen[MAX_CPUS];
static int q31_checkpoint_seen[MAX_CPUS];
static int q32_checkpoint_seen[MAX_CPUS];
static int q33_checkpoint_seen[MAX_CPUS];
static int finish_seen[MAX_CPUS], final_tso_ok[MAX_CPUS];
static u64 entry;
static hv_vcpu_t vcpus[MAX_CPUS];
static u64 host_exits[MAX_CPUS], timer_exits[MAX_CPUS];
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static unsigned created, finished;
static unsigned checkpoint_arrived;
static int checkpoint_ready, checkpoint_pass;
static unsigned q31_checkpoint_arrived;
static int q31_checkpoint_ready,q31_checkpoint_pass;
static unsigned q32_checkpoint_arrived;
static int q32_checkpoint_ready,q32_checkpoint_pass;
static unsigned q33_checkpoint_arrived;
static int q33_checkpoint_ready,q33_checkpoint_pass;
static atomic_bool timed_out;
static atomic_bool devices_stop;
static atomic_ullong stopped_spi_permit[MAX_CPUS];

static void hvcheck(hv_return_t rc, const char *what) {
    if (rc != HV_SUCCESS) {
        fprintf(stderr, "{\"api\":\"%s\",\"error\":%u}\n", what, rc);
        exit(2);
    }
}
#define HV(expr) hvcheck((expr), #expr)
static void die(const char *what) { perror(what); exit(2); }
static int sparse_write(int fd,const void *data,size_t size){
    static const unsigned char zero[65536];
    if(ftruncate(fd,0)||ftruncate(fd,(off_t)size))return 0;
    for(size_t offset=0;offset<size;offset+=sizeof(zero)){
        size_t length=size-offset<sizeof(zero)?size-offset:sizeof(zero);
        const unsigned char *bytes=(const unsigned char *)data+offset;
        if(!memcmp(bytes,zero,length))continue;
        for(size_t done=0;done<length;){
            ssize_t n=pwrite(fd,bytes+done,length-done,(off_t)(offset+done));
            if(n<0&&errno==EINTR)continue;
            if(n<=0)return 0;
            done+=(size_t)n;
        }
    }
    return fsync(fd)==0;
}
static void dump_file(const char *name, const void *data, size_t size) {
    int fd = open(name, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) die(name);
    if(!sparse_write(fd,data,size)||close(fd))die(name);
}
static void initialize_x86(unsigned char *memory) {
    memset(memory, 0, X86_RAM_SIZE);
    for (u64 gpa = 0; gpa < X86_RAM_SIZE; gpa += 4096) {
        *(u64 *)(memory + gpa) = 0x5a00000000000000UL | gpa;
        *(u64 *)(memory + gpa + 4088) = 0x5a00000000000000UL | gpa | 0xff8;
    }
    memset(memory + 0x200000, 0, 0x50000);
    const u32 code42[] = {0xd2800540, 0xd65f03c0};
    const u32 code43[] = {0xd2800560, 0xd65f03c0};
    // Four immutable 8-byte payloads cover each code page's first and last pair.
    memcpy(memory + 0x4000, code42, sizeof(code42));
    memcpy(memory + 0x4ff8, code42, sizeof(code42));
    memcpy(memory + 0x5000, code43, sizeof(code43));
    memcpy(memory + 0x5ff8, code43, sizeof(code43));
}
static int q30_integrity(u64 last_kind) {
    if (q->magic != Q30_MAGIC || q->version != Q30_VERSION || q->ncpus != ncpus ||
        q->failure || q->completed_groups != 0xfff || !q->records ||
        q->records > Q30_LOG_CAPACITY || !q->epoch || q->gate || q->lock ||
        q->owner >= ncpus) return 0;
    u64 inflight[MAX_CPUS] = {0}, completed[MAX_CPUS] = {0};
    for (u64 i = 0; i < q->records; ++i) {
        const struct q30_record *r = &q->log[i];
        if (r->sequence != i + 1 || r->cpu >= ncpus ||
            r->kind < QE_CONTEXT || r->kind > last_kind || r->epoch > q->epoch) return 0;
        if (r->kind == QE_BEGIN) {
            if (inflight[r->cpu]) return 0;
            inflight[r->cpu] = r->sequence;
        } else if (r->kind == QE_END) {
            if (!inflight[r->cpu] || r->data[0] != inflight[r->cpu]) return 0;
            inflight[r->cpu] = 0;
            completed[r->cpu]++;
        }
    }
    for (unsigned i = 0; i < ncpus; ++i) {
        const struct q30_cpu *c = &q->cpu[i];
        if (c->started != 1 || c->done != 1 || c->active || c->waiting ||
            c->request.inflight || c->ack != q->epoch || !c->operations ||
            c->operations != completed[i] || inflight[i] ||
            c->timer_end <= c->timer_start) return 0;
    }
    return 1;
}
static int q31_integrity(void) {
    u64 expected=Q31_VARIANTS*Q31_REPETITIONS;
    if(f->magic!=Q31_MAGIC || f->version!=Q31_VERSION || f->ncpus!=ncpus ||
       f->failure || f->phase!=2 || f->cases!=expected ||
       !f->checkpoint_records || f->checkpoint_records>=q->records ||
       !f->checkpoint_epoch || f->checkpoint_epoch>q->epoch) return 0;
    u64 cases[MAX_CPUS]={0},attempts[MAX_CPUS]={0},deliveries[MAX_CPUS]={0};
    u64 snapshots[MAX_CPUS]={0},chunks[MAX_CPUS]={0};
    for(u64 i=f->checkpoint_records;i<q->records;i++) {
        const struct q30_record *r=&q->log[i];
        if(r->kind!=QE_Q31)continue;
        u64 cpu=r->cpu,kind=r->data[0];
        if(cpu>=ncpus||kind<Q31_CASE||kind>Q31_END)return 0;
        if(kind==Q31_ATTEMPT&&r->data[1]!=++attempts[cpu])return 0;
        if(kind==Q31_DELIVER)deliveries[cpu]++;
        if(kind==Q31_END)cases[cpu]++;
        if(kind==Q31_STATE) {
            if(r->data[1]!=snapshots[cpu]+1||r->data[4]!=chunks[cpu])return 0;
            if(++chunks[cpu]==7){chunks[cpu]=0;snapshots[cpu]++;}
        }
    }
    for(unsigned i=0;i<ncpus;i++) {
        const struct q31_cpu *c=&f->cpu[i];
        if(c->started!=1||c->done!=1||c->cases!=expected||c->depth||
           c->timer_end<=c->timer_start||c->timer_start<q->cpu[i].timer_end||
           s->cpu[i].timer_count<c->timer_end||!c->commits||!c->deliveries||
           c->cases!=cases[i]||c->attempts!=attempts[i]||c->deliveries!=deliveries[i]||
           c->snapshots!=snapshots[i]||chunks[i])return 0;
    }
    for(u64 i=0;i<X86_RAM_SIZE/4096;i++)if(f->code_roles[i]||f->code_users[i])return 0;
    return 1;
}
static int q32_integrity(void){
    if(interrupts->magic!=Q32_MAGIC||interrupts->version!=Q32_VERSION||interrupts->ncpus!=ncpus||
       interrupts->phase!=2||interrupts->failure||interrupts->cases!=Q32_VARIANTS*Q32_REPETITIONS||
       interrupts->checkpoint_records<=f->checkpoint_records||interrupts->checkpoint_records>=q->records||
       interrupts->checkpoint_epoch>q->epoch)return 0;
    u64 ends[MAX_CPUS]={0},native[MAX_CPUS]={0},deliveries[MAX_CPUS]={0};
    for(u64 index=interrupts->checkpoint_records;index<q->records;index++){
        const struct q30_record *r=&q->log[index];if(r->kind!=QE_Q32)continue;
        if(r->cpu>=ncpus||r->data[0]<Q32_CASE||r->data[0]>Q32_END)return 0;
        if(r->data[0]==Q32_END)ends[r->cpu]++;
        if(r->data[0]==Q32_NATIVE)native[r->cpu]++;
        if(r->data[0]==Q32_DISPATCH&&!r->data[5])deliveries[r->cpu]++;
    }
    for(unsigned i=0;i<ncpus;i++){
        const struct q32_cpu *c=&interrupts->cpu[i];
        if(c->started!=1||c->done!=1||c->cases!=Q32_VARIANTS*Q32_REPETITIONS||c->cases!=ends[i]||
           c->native_count!=native[i]||c->guest_count!=deliveries[i]||!c->native_count||!c->guest_count||
           c->halted||c->ready||c->hook_stage||c->timer_end<=c->timer_start||
           c->spi_sent!=c->spi_deasserted||c->spi_sent!=c->spi_request||
           c->diagnostic_ack!=c->diagnostic_request)return 0;
        for(unsigned word=0;word<4;word++)if(c->irr[word]||c->isr[word])return 0;
        for(unsigned source=0;source<3;source++)for(unsigned sender=0;sender<ncpus;sender++)
            if(c->emitted[source][sender]!=c->accepted[source][sender])return 0;
    }
    return 1;
}
static int q33_integrity(void){
    int ok=code->magic==Q33_MAGIC&&code->version==Q33_VERSION&&code->ncpus==ncpus&&
            code->phase==2&&!code->failure&&code->cases==Q33_VARIANTS*Q33_REPETITIONS&&
            code->allocated<=Q33_OBJECTS&&code->pool_bytes<=Q33_POOL_END-Q33_POOL;
    for(unsigned i=0;i<ncpus;i++)ok&=code->cpu[i].done&&code->cpu[i].cases==code->cases&&
            !code->cpu[i].depth&&!code->cpu[i].transport_active&&!transactions[i].active;
    return ok;
}
static int dump_ram(void) {
    if (!ram || backing < 0) return 0;
    if(!sparse_write(backing,ram,RAM_SIZE)){perror("RAM dump");return 0;}return 1;
}
static void dump_on_exit(void) {
    if (ram) fprintf(stderr, "{\"atexit_partial_ram_dump\":%s}\n", dump_ram() ? "true" : "false");
}
// Caller holds lock. Both the real watchdog and the synthetic check use this
// wakeup, so a timeout cannot leave a stopped vCPU waiting for missing peers.
static void timeout_wakeup(void) {
    atomic_store(&timed_out, true);
    pthread_cond_broadcast(&changed);
}
static int checkpoint_wait(void) {
    while (!checkpoint_ready && !atomic_load(&timed_out)) pthread_cond_wait(&changed, &lock);
    return checkpoint_ready && checkpoint_pass && !atomic_load(&timed_out);
}
static int q31_checkpoint_wait(void){
    while(!q31_checkpoint_ready&&!atomic_load(&timed_out))pthread_cond_wait(&changed,&lock);
    return q31_checkpoint_ready&&q31_checkpoint_pass&&!atomic_load(&timed_out);
}
static int q32_checkpoint_wait(void){
    while(!q32_checkpoint_ready&&!atomic_load(&timed_out))pthread_cond_wait(&changed,&lock);
    return q32_checkpoint_ready&&q32_checkpoint_pass&&!atomic_load(&timed_out);
}
static int q33_checkpoint_wait(void){
    while(!q33_checkpoint_ready&&!atomic_load(&timed_out))pthread_cond_wait(&changed,&lock);
    return q33_checkpoint_ready&&q33_checkpoint_pass&&!atomic_load(&timed_out);
}
static void *checkpoint_test_waiter(void *arg) {
    int *state = arg;
    pthread_mutex_lock(&lock);
    state[0] = 1;
    pthread_cond_broadcast(&changed);
    state[1] = state[2]==3 ? q33_checkpoint_wait() : state[2]==2 ? q32_checkpoint_wait() : state[2] ? q31_checkpoint_wait() : checkpoint_wait();
    pthread_mutex_unlock(&lock);
    return NULL;
}
static int response_registers(hv_vcpu_t v,const struct q33_packet *p,unsigned *written,hv_return_t *rc,
                              hv_return_t (*setter)(hv_vcpu_t,hv_reg_t,uint64_t)){
    *written=0;*rc=HV_SUCCESS;
    while(*written<16){
        *rc=setter(v,(hv_reg_t)(HV_REG_X0+*written),p->word[*written]);
        if(*rc!=HV_SUCCESS)return 0;
        ++*written;
    }
    return 1;
}
static unsigned test_set_calls,test_set_fail;
static hv_return_t test_setter(hv_vcpu_t v,hv_reg_t reg,uint64_t value){
    (void)v;(void)reg;(void)value;return test_set_calls++==test_set_fail?HV_ERROR:HV_SUCCESS;
}
static int transport_self_check(void){
    struct q33_packet packet={{0}};unsigned written;hv_return_t rc;
    for(test_set_fail=0;test_set_fail<=16;test_set_fail++){
        test_set_calls=0;int ready=response_registers(0,&packet,&written,&rc,test_setter);
        if(ready!=(test_set_fail==16)||written!=test_set_fail||
           test_set_calls!=(test_set_fail==16?16:test_set_fail+1))return 0;
    }
    struct q33_host_transaction t={0};
    struct q33_request request={.rip=0x1000e020,.cr3=0x250000,.page=0x28e000,.generation=1,
        .source=0x70548,.operand=7,.kind=1,.length=80,.identity=1,.cpu=0};
    packet=(struct q33_packet){{QT_BEGIN,1,0,0,0,0,0,0,96,1,0,QF_DELAY,10,2,0,0}};
    if(!q33_respond(&t,0,&packet))return 0;
    for(unsigned offset=0;offset<96;offset+=32){
        packet.word[0]=QT_REQUEST;packet.word[2]=offset;packet.word[3]=32;
        memcpy(&packet.word[4],(const char *)&request+offset,32);
        if(!q33_respond(&t,0,&packet))return 0;
    }
    packet.word[0]=QT_PULL;packet.word[2]=0;packet.word[3]=32;packet.word[8]=80;
    if(!q33_respond(&t,0,&packet)||packet.word[10]!=QT_WAIT||t.offered||t.acknowledged)return 0;
    packet.word[0]=QT_CANCEL;
    if(!q33_respond(&t,0,&packet)||t.active||t.acknowledged||packet.word[10]!=QT_DONE)return 0;
    packet.word[0]=QT_ACK;if(q33_respond(&t,0,&packet))return 0;
    return 1;
}
static int checkpoint_self_check(void) {
    if(!transport_self_check())return 1;
    for(int stage=0;stage<4;stage++){
        int state[3] = {0, -1,stage};pthread_t waiter;atomic_store(&timed_out,false);
        if (pthread_create(&waiter, NULL, checkpoint_test_waiter, state)) die("self-check thread");
        pthread_mutex_lock(&lock);
        while (!state[0]) pthread_cond_wait(&changed, &lock);
        timeout_wakeup();pthread_mutex_unlock(&lock);
        if (pthread_join(waiter, NULL)) die("self-check join");
        if (state[1] != 0 || checkpoint_ready || q31_checkpoint_ready || q32_checkpoint_ready || q33_checkpoint_ready) return 1;
    }
    char path[]="/private/tmp/natemu-sparse-XXXXXX";int fd=mkstemp(path);
    if(fd<0)die("sparse self-check");unlink(path);
    unsigned char expected[131089]={0},actual[sizeof(expected)];
    expected[17]=3;expected[65540]=9;
    for(unsigned test=0;test<2;test++){
        if(test)memset(expected,0,sizeof(expected));
        if(!sparse_write(fd,expected,sizeof(expected)))die("sparse write self-check");
        struct stat st;if(fstat(fd,&st)||st.st_size!=(off_t)sizeof(expected))die("sparse size self-check");
        size_t offset=0;
        while(offset<sizeof(actual)){
            ssize_t n=pread(fd,actual+offset,sizeof(actual)-offset,(off_t)offset);
            if(n<0&&errno==EINTR)continue;
            if(n<=0)die("sparse read self-check");offset+=(size_t)n;
        }
        if(memcmp(expected,actual,sizeof(actual)))return 1;
    }
    close(fd);
    puts("self-check PASS: register setter failures at all16positions, WAIT cancellation, all four checkpoint timeouts and sparse dump exact bytes/length including trailing/all zero; no VM");
    return 0;
}
static int checkpoint(hv_vcpu_t v, unsigned id, u64 pc) {
    uint64_t cpsr, actlr, tcr, ttbr0, ttbr1;
    HV(hv_vcpu_get_reg(v, HV_REG_CPSR, &cpsr));
    HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_ACTLR_EL1, &actlr));
    HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_TCR_EL1, &tcr));
    HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_TTBR0_EL1, &ttbr0));
    HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_TTBR1_EL1, &ttbr1));
    // Hypervisor.framework reports the return PC after HVC, as at initial entry.
    int valid = !(pc&3) && pc >= RAM_BASE + 4 && pc <= RAM_BASE + 0x200000 &&
                *(u32 *)((char *)ram + pc - RAM_BASE - 4) == 0xd4000662 &&
                (actlr & 2) && !(cpsr & 128);
    printf("{\"cpu\":%u,\"checkpoint_instruction_pc\":%lu,\"checkpoint_resume_pc\":%lu,"
           "\"checkpoint_cpsr\":%llu,\"checkpoint_actlr\":%llu,\"checkpoint_tcr\":%llu,"
           "\"checkpoint_ttbr0\":%llu,\"checkpoint_ttbr1\":%llu,\"checkpoint_register_check\":%s}\n",
           id, pc - 4, pc, cpsr, actlr, tcr, ttbr0, ttbr1, valid ? "true" : "false");
    pthread_mutex_lock(&lock);
    checkpoint_seen[id] = valid ? 1 : -1;
    ++checkpoint_arrived;
    if (checkpoint_arrived == ncpus && !atomic_load(&timed_out)) {
        // All vCPUs remain stopped while I/O proceeds; leave the mutex available
        // so the watchdog can wake other checkpoint waiters on failure/timeout.
        pthread_mutex_unlock(&lock);
        f->checkpoint_records=q->records;f->checkpoint_epoch=q->epoch;
        dump_file("q30-checkpoint.bin", ram, RAM_SIZE);
        int ok = q30_integrity(QE_MARK) && f->magic==Q31_MAGIC &&
                 f->version==Q31_VERSION && f->ncpus==ncpus && !f->phase && !f->failure && !f->cases;
        for (unsigned i = 0; i < ncpus; ++i)
            ok &= checkpoint_seen[i] == 1 && entry_seen[i] && host_exits[i] == 2 &&
                  !s->cpu[i].done && !s->cpu[i].failure && !s->cpu[i].tso_end;
        printf("{\"q30_checkpoint_dump_complete\":true,\"checkpoint_ram_bytes\":%lu,"
               "\"checkpoint_records\":%lu,\"checkpoint_epoch\":%lu,"
               "\"q30_checkpoint_host_checks_pass\":%s,\"independent_oracle\":\"pending\"}\n",
               RAM_SIZE, q->records, q->epoch, ok ? "true" : "false");
        pthread_mutex_lock(&lock);
        checkpoint_pass = ok;
        checkpoint_ready = 1;
        pthread_cond_broadcast(&changed);
    }
    int resume = checkpoint_wait();
    pthread_mutex_unlock(&lock);
    if (resume) HV(hv_vcpu_set_reg(v, HV_REG_PC, pc));
    return resume;
}
static void spi_initialize(void);
static int q31_checkpoint_host(hv_vcpu_t v,unsigned id,u64 pc){
    uint64_t cpsr,actlr,tcr,ttbr0,ttbr1;
    HV(hv_vcpu_get_reg(v,HV_REG_CPSR,&cpsr));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_ACTLR_EL1,&actlr));
    HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_TCR_EL1,&tcr));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_TTBR0_EL1,&ttbr0));
    HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_TTBR1_EL1,&ttbr1));
    int valid=!(pc&3)&&pc>=RAM_BASE+4&&pc<=RAM_BASE+0x200000&&
        *(u32 *)((char *)ram+pc-RAM_BASE-4)==0xd4000682&&(actlr&2)&&!(cpsr&128);
    printf("{\"cpu\":%u,\"q31_checkpoint_instruction_pc\":%lu,\"q31_checkpoint_resume_pc\":%lu,"
           "\"q31_checkpoint_cpsr\":%llu,\"q31_checkpoint_actlr\":%llu,\"q31_checkpoint_tcr\":%llu,"
           "\"q31_checkpoint_ttbr0\":%llu,\"q31_checkpoint_ttbr1\":%llu,\"q31_checkpoint_register_check\":%s}\n",
           id,pc-4,pc,cpsr,actlr,tcr,ttbr0,ttbr1,valid?"true":"false");
    pthread_mutex_lock(&lock);q31_checkpoint_seen[id]=valid?1:-1;++q31_checkpoint_arrived;
    if(q31_checkpoint_arrived==ncpus&&!atomic_load(&timed_out)){
        pthread_mutex_unlock(&lock);
        /* Every vCPU has run with its final affinity and is now stopped. */
        if(use_gic)spi_initialize();
        interrupts->checkpoint_records=q->records;interrupts->checkpoint_epoch=q->epoch;
        dump_file("q31-checkpoint.bin",ram,RAM_SIZE);
        int ok=q30_integrity(QE_Q31)&&q31_integrity()&&interrupts->magic==Q32_MAGIC&&
            interrupts->version==Q32_VERSION&&interrupts->ncpus==ncpus&&!interrupts->phase&&!interrupts->failure;
        for(unsigned i=0;i<ncpus;i++)ok&=q31_checkpoint_seen[i]==1&&host_exits[i]==3&&
            !s->cpu[i].done&&!s->cpu[i].tso_end&&!s->cpu[i].failure;
        printf("{\"q31_checkpoint_dump_complete\":true,\"q31_checkpoint_ram_bytes\":%lu,"
               "\"q31_checkpoint_records\":%lu,\"q31_checkpoint_epoch\":%lu,"
               "\"q31_checkpoint_host_checks_pass\":%s,\"independent_oracle\":\"pending\"}\n",
               RAM_SIZE,q->records,q->epoch,ok?"true":"false");
        pthread_mutex_lock(&lock);q31_checkpoint_pass=ok;q31_checkpoint_ready=1;pthread_cond_broadcast(&changed);
    }
    int resume=q31_checkpoint_wait();
    pthread_mutex_unlock(&lock);if(resume)HV(hv_vcpu_set_reg(v,HV_REG_PC,pc));return resume;
}
static int q32_checkpoint_host(hv_vcpu_t v,unsigned id,u64 pc){
    uint64_t cpsr,actlr,tcr,ttbr0,ttbr1;
    HV(hv_vcpu_get_reg(v,HV_REG_CPSR,&cpsr));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_ACTLR_EL1,&actlr));
    HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_TCR_EL1,&tcr));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_TTBR0_EL1,&ttbr0));
    HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_TTBR1_EL1,&ttbr1));
    int valid=!(pc&3)&&pc>=RAM_BASE+4&&pc<=RAM_BASE+0x200000&&
        *(u32 *)((char *)ram+pc-RAM_BASE-4)==0xd40006a2&&(actlr&2)&&!(cpsr&128);
    printf("{\"cpu\":%u,\"q32_checkpoint_instruction_pc\":%lu,\"q32_checkpoint_resume_pc\":%lu,"
           "\"q32_checkpoint_cpsr\":%llu,\"q32_checkpoint_actlr\":%llu,\"q32_checkpoint_tcr\":%llu,"
           "\"q32_checkpoint_ttbr0\":%llu,\"q32_checkpoint_ttbr1\":%llu,\"q32_checkpoint_register_check\":%s}\n",
           id,pc-4,pc,cpsr,actlr,tcr,ttbr0,ttbr1,valid?"true":"false");
    pthread_mutex_lock(&lock);q32_checkpoint_seen[id]=valid?1:-1;++q32_checkpoint_arrived;
    if(q32_checkpoint_arrived==ncpus&&!atomic_load(&timed_out)){
        pthread_mutex_unlock(&lock);
        code->checkpoint_records=q->records;code->checkpoint_epoch=q->epoch;
        dump_file("q32-checkpoint.bin",ram,RAM_SIZE);
        int ok=q30_integrity(QE_Q32)&&q31_integrity()&&q32_integrity()&&code->magic==Q33_MAGIC&&
            code->version==Q33_VERSION&&code->ncpus==ncpus&&!code->phase&&!code->failure;
        for(unsigned i=0;i<ncpus;i++)ok&=q32_checkpoint_seen[i]==1&&host_exits[i]==4&&
            !s->cpu[i].done&&!s->cpu[i].tso_end&&!s->cpu[i].failure;
        printf("{\"q32_checkpoint_dump_complete\":true,\"q32_checkpoint_ram_bytes\":%lu,"
               "\"q32_checkpoint_records\":%lu,\"q32_checkpoint_epoch\":%lu,"
               "\"q32_checkpoint_host_checks_pass\":%s,\"independent_oracle\":\"pending\"}\n",
               RAM_SIZE,q->records,q->epoch,ok?"true":"false");
        pthread_mutex_lock(&lock);q32_checkpoint_pass=ok;q32_checkpoint_ready=1;pthread_cond_broadcast(&changed);
    }
    int resume=q32_checkpoint_wait();
    pthread_mutex_unlock(&lock);if(resume)HV(hv_vcpu_set_reg(v,HV_REG_PC,pc));return resume;
}
static int q33_checkpoint_host(hv_vcpu_t v,unsigned id,u64 pc){
    uint64_t cpsr,actlr,tcr,ttbr0,ttbr1;
    HV(hv_vcpu_get_reg(v,HV_REG_CPSR,&cpsr));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_ACTLR_EL1,&actlr));
    HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_TCR_EL1,&tcr));HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_TTBR0_EL1,&ttbr0));
    HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_TTBR1_EL1,&ttbr1));
    int valid=!(pc&3)&&pc>=RAM_BASE+4&&pc<=RAM_BASE+0x200000&&
        *(u32 *)((char *)ram+pc-RAM_BASE-4)==0xd40006e2&&(actlr&2)&&!(cpsr&128);
    printf("{\"cpu\":%u,\"q33_checkpoint_instruction_pc\":%lu,\"q33_checkpoint_resume_pc\":%lu,"
           "\"q33_checkpoint_cpsr\":%llu,\"q33_checkpoint_actlr\":%llu,\"q33_checkpoint_tcr\":%llu,"
           "\"q33_checkpoint_ttbr0\":%llu,\"q33_checkpoint_ttbr1\":%llu,\"q33_checkpoint_register_check\":%s}\n",
           id,pc-4,pc,cpsr,actlr,tcr,ttbr0,ttbr1,valid?"true":"false");
    pthread_mutex_lock(&lock);q33_checkpoint_seen[id]=valid?1:-1;++q33_checkpoint_arrived;
    if(q33_checkpoint_arrived==ncpus&&!atomic_load(&timed_out)){
        pthread_mutex_unlock(&lock);
        startup->checkpoint_records=q->records;startup->checkpoint_epoch=q->epoch;
        dump_file("q33-checkpoint.bin",ram,RAM_SIZE);
        int ok=q30_integrity(QE_Q33)&&q31_integrity()&&q32_integrity()&&q33_integrity()&&
            startup->magic==Q34_MAGIC&&startup->version==Q34_VERSION&&startup->ncpus==ncpus&&!startup->phase&&!startup->failure;
        for(unsigned i=0;i<ncpus;i++)ok&=q33_checkpoint_seen[i]==1&&host_exits[i]==5+transfer_count[i]&&
            !s->cpu[i].done&&!s->cpu[i].tso_end&&!s->cpu[i].failure;
        printf("{\"q33_checkpoint_dump_complete\":true,\"q33_checkpoint_ram_bytes\":%lu,"
               "\"q33_checkpoint_records\":%lu,\"q33_checkpoint_epoch\":%lu,"
               "\"q33_checkpoint_host_checks_pass\":%s,\"independent_oracle\":\"pending\"}\n",
               RAM_SIZE,q->records,q->epoch,ok?"true":"false");
        pthread_mutex_lock(&lock);q33_checkpoint_pass=ok;q33_checkpoint_ready=1;pthread_cond_broadcast(&changed);
    }
    int resume=q33_checkpoint_wait();
    pthread_mutex_unlock(&lock);if(resume)HV(hv_vcpu_set_reg(v,HV_REG_PC,pc));return resume;
}
static int stopped_source_readback(hv_vcpu_t v,unsigned id,unsigned source,u64 txn,const char *stage){
    uint64_t pending,active,ctl,cval,cpsr;
    u64 intid=source==Q32_TIMER?s->timer_intid:source==Q32_PEER?Q32_SGI:interrupts->spi_base+id;
    if(source<Q32_TIMER||source>Q32_HOST)return 0;
    // Owning thread, stopped vCPU. Sequential reads are not an atomic GIC snapshot.
    if(source==Q32_HOST){
        HV(hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)(0x200+4*(intid/32)),&pending));
        HV(hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)(0x300+4*(intid/32)),&active));
    }else{
        HV(hv_gic_get_redistributor_reg(v,HV_GIC_REDISTRIBUTOR_REG_GICR_ISPENDR0,&pending));
        HV(hv_gic_get_redistributor_reg(v,HV_GIC_REDISTRIBUTOR_REG_GICR_ISACTIVER0,&active));
    }
    HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_CNTP_CTL_EL0,&ctl));
    HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_CNTP_CVAL_EL0,&cval));
    HV(hv_vcpu_get_reg(v,HV_REG_CPSR,&cpsr));
    u64 bit=1UL<<(intid%32);
    printf("{\"q33_stopped_source\":\"%s\",\"cpu\":%u,\"transaction\":%lu,\"source\":%u,\"intid\":%lu,"
           "\"pending_word\":%llu,\"active_word\":%llu,\"pending\":%s,\"active\":%s,"
           "\"timer_ctl\":%llu,\"timer_cval\":%llu,\"cpsr\":%llu}\n",
           stage,id,txn,source,intid,pending,active,pending&bit?"true":"false",active&bit?"true":"false",ctl,cval,cpsr);
    return !!(pending&bit);
}
static int transfer_host(hv_vcpu_t v,unsigned id,u64 pc){
    struct q33_packet request={{0}},response={{0}};unsigned gets=0,sets=0;
    hv_return_t rc=HV_SUCCESS;int accepted=0;
    if(pc==transfer_pc+4&&q32_checkpoint_seen[id]==1){
        for(;gets<16;gets++){
            uint64_t value=0;rc=hv_vcpu_get_reg(v,(hv_reg_t)(HV_REG_X0+gets),&value);
            if(rc!=HV_SUCCESS)break;request.word[gets]=value;
        }
        response=request;
        if(gets==16){
            accepted=q33_respond(&transactions[id],id,&response);
            if(accepted&&request.word[12]==6&&request.word[13]<3&&response.word[10]==QT_WAIT){
                unsigned source=(unsigned)request.word[13]+1;
                stopped_source_readback(v,id,source,request.word[1],"before");
                if(source==Q32_HOST){
                    u64 sequence=__atomic_load_n(&interrupts->cpu[id].spi_request,__ATOMIC_ACQUIRE);
                    printf("{\"q33_spi_permit\":true,\"cpu\":%u,\"transaction\":%lu,\"sequence\":%lu}\n",id,request.word[1],sequence);
                    atomic_store_explicit(&stopped_spi_permit[id],sequence,memory_order_release);
                }
                uint64_t cval,ctl;HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_CNTP_CVAL_EL0,&cval));
                HV(hv_vcpu_get_sys_reg(v,HV_SYS_REG_CNTP_CTL_EL0,&ctl));
                u64 before,after;__asm__ volatile("mrs %0,cntvct_el0":"=r"(before));
                struct timespec delay={0,5000000};nanosleep(&delay,NULL);
                __asm__ volatile("mrs %0,cntvct_el0":"=r"(after));
                stopped_source_readback(v,id,source,request.word[1],"after");
                printf("{\"q33_host_delay\":true,\"cpu\":%u,\"transaction\":%lu,\"before\":%lu,\"after\":%lu,\"timer_cval\":%llu,\"timer_ctl\":%llu}\n",id,request.word[1],before,after,cval,ctl);
            }
            if(accepted){
                response_registers(v,&response,&sets,&rc,hv_vcpu_set_reg);
            }
        }
    }
    int valid=accepted&&gets==16&&sets==16&&rc==HV_SUCCESS;
    flockfile(stdout);
    printf("{\"q33_transfer\":true,\"cpu\":%u,\"instruction_pc\":%lu,\"resume_pc\":%lu,\"request\":[",id,pc-4,pc);
    for(unsigned i=0;i<16;i++)printf("%s%lu",i?",":"",request.word[i]);
    printf("],\"response\":[");for(unsigned i=0;i<16;i++)printf("%s%lu",i?",":"",response.word[i]);
    printf("],\"accepted\":%s,\"register_checks_pass\":%s,\"get_count\":%u,\"set_count\":%u,\"api_status\":%u}\n",
           accepted?"true":"false",valid?"true":"false",gets,sets,rc);
    if(valid&&request.word[0]==QT_REQUEST&&transactions[id].received==sizeof(struct q33_request)){
        struct q33_host_transaction *t=&transactions[id];const u64 *words=(const u64 *)&t->request;
        printf("{\"q33_produced\":true,\"cpu\":%u,\"transaction\":%lu,\"request\":[",id,t->transaction);
        for(unsigned i=0;i<12;i++)printf("%s%lu",i?",":"",words[i]);
        printf("],\"bytes_hex\":\"");const unsigned char *bytes=(const unsigned char *)t->code;
        for(u64 i=0;i<t->request.length;i++)printf("%02x",bytes[i]);puts("\"}");
    }
    funlockfile(stdout);
    if(!valid)return 0;
    transfer_count[id]++;
    HV(hv_vcpu_set_reg(v,HV_REG_PC,pc));return 1;
}
static void distributor_bits(u64 offset,u64 clear,u64 set){
    uint64_t value;HV(hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)offset,&value));
    HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)offset,(value&~clear)|set));
}
static void spi_readback(unsigned cpu,const char *stage,u64 sequence){
    u64 intid=interrupts->spi_base+cpu;
    const u64 offsets[]={0,0x80+4*(intid/32),0x100+4*(intid/32),
        0x200+4*(intid/32),0x300+4*(intid/32),0x400+4*(intid/4),
        0xc00+4*(intid/16),0x6000+8*intid};
    uint64_t value[8];
    for(unsigned i=0;i<8;i++)
        HV(hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)offsets[i],&value[i]));
    /* Sequential register reads are diagnostics, not an atomic GIC snapshot. */
    printf("{\"q32_spi_readback\":\"%s\",\"cpu\":%u,\"intid\":%lu,\"sequence\":%lu,"
           "\"ctlr\":%llu,\"group\":%llu,\"enable\":%llu,\"pending\":%llu,"
           "\"active\":%llu,\"priority\":%llu,\"config\":%llu,\"route\":%llu}\n",
           stage,cpu,intid,sequence,value[0],value[1],value[2],value[3],
           value[4],value[5],value[6],value[7]);
}
static void spi_initialize(void){
    for(unsigned cpu=0;cpu<ncpus;cpu++){
        u64 spi=interrupts->spi_base+cpu;
        distributor_bits(0x80+4*(spi/32),0,1UL<<(spi%32));
        distributor_bits(0x400+4*(spi/4),255UL<<(8*(spi%4)),128UL<<(8*(spi%4)));
        HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)(0x6000+8*spi),cpu));
        HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)(0x100+4*(spi/32)),1UL<<(spi%32)));
    }
    for(unsigned cpu=0;cpu<ncpus;cpu++)spi_readback(cpu,"initialized",0);
}
static void *device_worker(void *unused){
    (void)unused;struct timespec interval={0,100000};
    while(!atomic_load(&devices_stop)&&!atomic_load(&timed_out)){
        if(__atomic_load_n(&startup->phase,__ATOMIC_ACQUIRE)==1){
            u64 request=__atomic_load_n(&startup->diagnostic_request,__ATOMIC_ACQUIRE);
            if(request>__atomic_load_n(&startup->diagnostic_ack,__ATOMIC_ACQUIRE)){
                struct q34_mailbox *m=(struct q34_mailbox *)startup->memory;
                if(m->command!=1||request!=startup->serial||startup->diagnostic_cpu!=startup->publisher||
                   startup->diagnostic_reason<Q34_WRONG_ID||startup->diagnostic_reason>Q34_BOOT_ONCE){
                    fprintf(stderr,"invalid Q34 startup diagnostic request\n");exit(2);
                }
                printf("{\"q34_diagnostic_stop\":true,\"cpu\":%lu,\"case\":%lu,\"reason\":%lu,\"request_sequence\":%lu,\"command\":%u,\"apic_id\":%u,\"vector\":%lu}\n",
                       startup->diagnostic_cpu,request,startup->diagnostic_reason,startup->request_sequence,m->command,m->apic_id,m->vector);
                __atomic_store_n(&startup->diagnostic_ack,request,__ATOMIC_RELEASE);
            }
        }
        if(__atomic_load_n(&interrupts->phase,__ATOMIC_ACQUIRE)==1||
           __atomic_load_n(&code->phase,__ATOMIC_ACQUIRE)==1||
           __atomic_load_n(&startup->phase,__ATOMIC_ACQUIRE)==1){
            for(unsigned cpu=0;cpu<ncpus;cpu++){
                struct q32_cpu *c=&interrupts->cpu[cpu];u64 intid=interrupts->spi_base+cpu;
                u64 diagnostic=__atomic_load_n(&c->diagnostic_request,__ATOMIC_ACQUIRE);
                if(diagnostic>c->diagnostic_ack){
                    const struct q31_arch *a=&f->cpu[cpu].arch;
                    if(!c->halted||(a->flags&512)||!(c->irr[1]&2)||c->diagnostic_case!=interrupts->cases+1){
                        fprintf(stderr,"invalid masked-HLT diagnostic request\n");exit(2);
                    }
                    printf("{\"q32_diagnostic_stop\":\"masked-halt\",\"cpu\":%u,\"sequence\":%lu,\"case\":%lu,"
                           "\"flags\":%lu,\"rip\":%lu,\"halted\":%lu,\"irr_word1\":%lu,\"native_count\":%lu}\n",
                           cpu,diagnostic,c->diagnostic_case,a->flags,a->rip,c->halted,c->irr[1],c->native_count);
                    __atomic_store_n(&c->diagnostic_ack,diagnostic,__ATOMIC_RELEASE);
                }
                u64 deassert=__atomic_load_n(&c->spi_deassert,__ATOMIC_ACQUIRE);
                if(deassert>c->spi_deasserted){
                    HV(hv_gic_set_spi((uint32_t)intid,false));
                    printf("{\"q32_spi_action\":\"deassert\",\"cpu\":%u,\"intid\":%lu,\"sequence\":%lu}\n",cpu,intid,deassert);
                    __atomic_store_n(&c->spi_deasserted,deassert,__ATOMIC_RELEASE);
                }
                u64 request=__atomic_load_n(&c->spi_request,__ATOMIC_ACQUIRE);
                // Host-internal permission opens only after the stopped owner's
                // before-readback. Deassertion above remains independently live.
                if(__atomic_load_n(&code->phase,__ATOMIC_ACQUIRE)==1 &&
                   __atomic_load_n(&code->cpu[cpu].group,__ATOMIC_ACQUIRE)==6 &&
                   __atomic_load_n(&code->cpu[cpu].phase,__ATOMIC_ACQUIRE)==3 &&
                   atomic_load_explicit(&stopped_spi_permit[cpu],memory_order_acquire)<request)continue;
                if(request>c->spi_sent&&c->spi_deasserted==c->spi_sent&&__atomic_load_n(&c->ready,__ATOMIC_ACQUIRE)){
                    /* GIC reconfiguration requires a disabled, inactive interrupt.
                     * The prior line is already low; watchdog bounds an old active IRQ. */
                    u64 bit=1UL<<(intid%32);uint64_t active;
                    HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)(0x180+4*(intid/32)),bit));
                    HV(hv_gic_get_distributor_reg((hv_gic_distributor_reg_t)(0x300+4*(intid/32)),&active));
                    if(active&bit)continue;
                    HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)(0x280+4*(intid/32)),bit));
                    u64 shift=2*(intid%16),edge=c->spi_level?0:2;
                    distributor_bits(0xc00+4*(intid/16),3UL<<shift,edge<<shift);
                    HV(hv_gic_set_distributor_reg((hv_gic_distributor_reg_t)(0x100+4*(intid/32)),bit));
                    spi_readback(cpu,"pre-assert",request);
                    __atomic_store_n(&c->spi_sent,request,__ATOMIC_RELEASE);
                    printf("{\"q32_spi_action\":\"assert\",\"cpu\":%u,\"intid\":%lu,\"sequence\":%lu,\"level_triggered\":%s}\n",
                           cpu,intid,request,c->spi_level?"true":"false");
                    HV(hv_gic_set_spi((uint32_t)intid,true));
                    spi_readback(cpu,"post-assert",request);
                }
            }
        }
        nanosleep(&interval,NULL);
    }
    return NULL;
}
static void *watchdog(void *unused) {
    (void)unused;
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 20;
    pthread_mutex_lock(&lock);
    while (finished < ncpus) {
        if (pthread_cond_timedwait(&changed, &lock, &deadline) == ETIMEDOUT) {
            timeout_wakeup();
            if (created == ncpus) HV(hv_vcpus_exit(vcpus, ncpus));
            if(use_gic)for(unsigned cpu=0;cpu<ncpus;cpu++)
                spi_readback(cpu,"timeout",__atomic_load_n(&interrupts->cpu[cpu].spi_sent,__ATOMIC_ACQUIRE));
            fprintf(stderr, "{\"watchdog_timeout\":true,\"created\":%u}\n", created);
            break;
        }
    }
    pthread_mutex_unlock(&lock);
    return NULL;
}
static void *cpu(void *arg) {
    unsigned id = (unsigned)(uintptr_t)arg;
    hv_vcpu_exit_t *out;
    HV(hv_vcpu_create(&vcpus[id], &out, NULL));
    hv_vcpu_t v = vcpus[id];
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_MPIDR_EL1, (1UL << 31) | id));
    if (use_gic) {
    hv_ipa_t redist;
    HV(hv_gic_get_redistributor_base(v, &redist));
    s->cpu[id].redistributor = redist;
    s->cpu[id].timer_intid = s->timer_intid;
    HV(hv_gic_set_redistributor_reg(v, HV_GIC_REDISTRIBUTOR_REG_GICR_IGROUPR0, 0xffffffff));
    HV(hv_gic_set_redistributor_reg(v, HV_GIC_REDISTRIBUTOR_REG_GICR_ISENABLER0,
                                  (1UL << s->timer_intid) | 2 | (1UL<<Q32_SGI)));
    for (unsigned i = 0; i < 8; ++i)
        HV(hv_gic_set_redistributor_reg(v, (hv_gic_redistributor_reg_t)(0x10400 + i * 4), 0x80808080));
    }
    HV(hv_vcpu_set_reg(v, HV_REG_PC, entry));
    HV(hv_vcpu_set_reg(v, HV_REG_X0, id));
    HV(hv_vcpu_set_reg(v, HV_REG_CPSR, 0x3c5)); // EL1h, DAIF masked.
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_SCTLR_EL1, initial_sctlr));
    HV(hv_vcpu_set_sys_reg(v, HV_SYS_REG_ACTLR_EL1, initial_tso ? 2 : 0));
    uint64_t pfr0, sctlr, actlr;
    HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_ID_AA64PFR0_EL1, &pfr0));
    HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_SCTLR_EL1, &sctlr));
    HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_ACTLR_EL1, &actlr));
    printf("{\"cpu\":%u,\"initial_pfr0\":%llu,\"initial_sctlr\":%llu,\"initial_actlr\":%llu}\n",
           id, pfr0, sctlr, actlr);
    if (!!(actlr & 2) != initial_tso) { fprintf(stderr, "TSO readback failed\n"); exit(2); }
    pthread_mutex_lock(&lock);
    ++created;
    pthread_cond_broadcast(&changed);
    while (created < ncpus && !atomic_load(&timed_out)) pthread_cond_wait(&changed, &lock);
    pthread_mutex_unlock(&lock);
    for (unsigned iteration = 0; iteration < 30000 && !atomic_load(&timed_out); ++iteration) {
        uint64_t before_ticks;
        HV(hv_vcpu_get_exec_time(v, &before_ticks));
        printf("{\"cpu\":%u,\"enter_run\":%u,\"exec_ticks_before\":%llu}\n", id, iteration, before_ticks);
        HV(hv_vcpu_run(v));
        host_exits[id]++;
        if (out->reason == HV_EXIT_REASON_CANCELED) {
            uint64_t pc, esr, elr, far, sp, cpsr, exec_time;
            HV(hv_vcpu_get_reg(v, HV_REG_PC, &pc));
            HV(hv_vcpu_get_reg(v, HV_REG_CPSR, &cpsr));
            HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_ESR_EL1, &esr));
            HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_ELR_EL1, &elr));
            HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_FAR_EL1, &far));
            HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_SP_EL1, &sp));
            HV(hv_vcpu_get_exec_time(v, &exec_time));
            printf("{\"cpu\":%u,\"canceled_pc\":%llu,\"esr\":%llu,\"elr\":%llu,"
                   "\"far\":%llu,\"sp\":%llu,\"cpsr\":%llu,\"exec_ticks\":%llu}\n",
                   id, pc, esr, elr, far, sp, cpsr, exec_time);
            continue;
        }
        if (out->reason == HV_EXIT_REASON_VTIMER_ACTIVATED) {
            ++timer_exits[id];
            fprintf(stderr, "{\"cpu\":%u,\"unexpected_virtual_timer_exit\":true}\n", id);
            break;
        }
        uint64_t pc;
        HV(hv_vcpu_get_reg(v, HV_REG_PC, &pc));
        flockfile(stdout);
        printf("{\"cpu\":%u,\"exit_reason\":%u,\"syndrome\":%llu,\"pc\":%lu,\"ipa\":%llu}\n",
               id, out->reason, out->exception.syndrome, (u64)pc, out->exception.physical_address);
        funlockfile(stdout);
        if (out->reason == HV_EXIT_REASON_EXCEPTION && (out->exception.syndrome >> 26) == 0x16 &&
            (out->exception.syndrome & 65535) == 0x32 && !entry_seen[id] && host_exits[id] == 1) {
            entry_seen[id] = 1;
            if (entry_control) break;
            HV(hv_vcpu_set_reg(v, HV_REG_PC, entry + 4));
            continue;
        }
        if (out->reason == HV_EXIT_REASON_EXCEPTION && (out->exception.syndrome >> 26) == 0x16 &&
            (out->exception.syndrome & 65535) == 0x33 && entry_seen[id] &&
            !checkpoint_seen[id] && host_exits[id] == 2) {
            if (!checkpoint(v, id, pc)) break;
            continue;
        }
        if (out->reason == HV_EXIT_REASON_EXCEPTION && (out->exception.syndrome >> 26) == 0x16 &&
            (out->exception.syndrome & 65535) == 0x34 && checkpoint_seen[id] == 1 &&
            !q31_checkpoint_seen[id] && host_exits[id] == 3) {
            if(!q31_checkpoint_host(v,id,pc))break;
            continue;
        }
        if(out->reason==HV_EXIT_REASON_EXCEPTION&&(out->exception.syndrome>>26)==0x16&&
           (out->exception.syndrome&65535)==0x35&&q31_checkpoint_seen[id]==1&&
           !q32_checkpoint_seen[id]&&host_exits[id]==4){
            if(!q32_checkpoint_host(v,id,pc))break;continue;
        }
        if(out->reason==HV_EXIT_REASON_EXCEPTION&&(out->exception.syndrome>>26)==0x16&&
           (out->exception.syndrome&65535)==0x36&&q32_checkpoint_seen[id]==1){
            if(!transfer_host(v,id,pc))break;continue;
        }
        if(out->reason==HV_EXIT_REASON_EXCEPTION&&(out->exception.syndrome>>26)==0x16&&
           (out->exception.syndrome&65535)==0x37&&q32_checkpoint_seen[id]==1&&
           !q33_checkpoint_seen[id]&&host_exits[id]==5+transfer_count[id]){
            if(!q33_checkpoint_host(v,id,pc))break;continue;
        }
        if (out->reason == HV_EXIT_REASON_EXCEPTION && (out->exception.syndrome >> 26) == 0x16 &&
            (out->exception.syndrome & 65535) == 0x30 && checkpoint_seen[id] == 1 &&
            q33_checkpoint_seen[id] == 1 && host_exits[id] == 6+transfer_count[id] && s->cpu[id].done &&
            q->cpu[id].done && f->cpu[id].done && interrupts->cpu[id].done && code->cpu[id].done) {
            finish_seen[id] = 1;
            break;
        }
        s->cpu[id].failure = s->cpu[id].failure ? s->cpu[id].failure : 0x2000;
        break;
    }
    uint64_t final_pc, final_cpsr, final_actlr, final_exec;
    HV(hv_vcpu_get_reg(v, HV_REG_PC, &final_pc));
    HV(hv_vcpu_get_reg(v, HV_REG_CPSR, &final_cpsr));
    HV(hv_vcpu_get_sys_reg(v, HV_SYS_REG_ACTLR_EL1, &final_actlr));
    HV(hv_vcpu_get_exec_time(v, &final_exec));
    final_tso_ok[id] = !!(final_actlr & 2) == initial_tso;
    printf("{\"cpu\":%u,\"final_pc\":%llu,\"final_cpsr\":%llu,\"final_actlr\":%llu,\"final_exec_ticks\":%llu}\n",
           id, final_pc, final_cpsr, final_actlr, final_exec);
    pthread_mutex_lock(&lock);
    ++finished;
    pthread_cond_broadcast(&changed);
    // GIC topology remains intact until every vCPU has stopped running.
    while (finished < ncpus) pthread_cond_wait(&changed, &lock);
    pthread_mutex_unlock(&lock);
    HV(hv_vcpu_destroy(v));
    return NULL;
}
static u64 parse(const char *text, int base) {
    char *end;
    errno = 0;
    unsigned long v = strtoul(text, &end, base);
    if (errno || !*text || *end || *text == '-') { fprintf(stderr, "invalid number\n"); exit(2); }
    return v;
}
int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc == 2 && !strcmp(argv[1], "--self-check")) return checkpoint_self_check();
    if (argc < 3 || argc > 5) { fprintf(stderr, "host guest.bin entry-hex [vcpus] [gic|plain|plain-no-tso|plain-sctlr]\n"); return 2; }
    entry = parse(argv[2], 0);
    u64 count = argc >= 4 ? parse(argv[3], 10) : 2;
    if (argc == 5) {
        if (strcmp(argv[4], "gic") && strcmp(argv[4], "plain") && strcmp(argv[4], "plain-no-tso") &&
            strcmp(argv[4], "plain-sctlr")) return 2;
        entry_control = 1;
        use_gic = !strcmp(argv[4], "gic");
        initial_tso = strcmp(argv[4], "plain-no-tso") != 0;
        if (!strcmp(argv[4], "plain-sctlr")) initial_sctlr = 0x30900180;
    }
    if (count < 2 || count > MAX_CPUS || entry < RAM_BASE || entry >= RAM_BASE + 0x200000 || entry % 4)
        return 2;
    ncpus = (unsigned)count;
    int input = open(argv[1], O_RDONLY);
    struct stat st;
    if (input < 0 || fstat(input, &st)) die("guest input");
    if (st.st_size <= 0 || st.st_size > 0x200000 || entry >= RAM_BASE + (u64)st.st_size) return 2;
    backing = open("ram.bin", O_RDWR | O_CREAT | O_EXCL, 0600);
    if (backing < 0 || ftruncate(backing, RAM_SIZE)) die("ram backing");
    HV(hv_vm_allocate(&ram, RAM_SIZE, HV_ALLOCATE_DEFAULT));
    if (atexit(dump_on_exit)) die("atexit");
    memset(ram, 0, RAM_SIZE);
    for (size_t offset = 0; offset < (size_t)st.st_size;) {
        ssize_t got = read(input, (char *)ram + offset, (size_t)st.st_size - offset);
        if (got < 0 && errno == EINTR) continue;
        if (got <= 0) die("read guest");
        offset += (size_t)got;
    }
    close(input);
    unsigned transfer_sites=0;
    for(size_t off=0;off+4<=(size_t)st.st_size;off+=4)if(*(u32 *)((char *)ram+off)==0xd40006c2){
        transfer_pc=RAM_BASE+off;transfer_sites++;
    }
    if(transfer_sites!=1){fprintf(stderr,"Q33 transfer helper must be unique\n");return 2;}

    s = (struct shared *)((char *)ram + SHARED_PA - RAM_BASE);
    s->magic = MAGIC; s->version = 1; s->ncpus = ncpus;
    q = (struct q30_state *)((char *)ram + Q30_PA - RAM_BASE);
    q->magic = Q30_MAGIC; q->version = Q30_VERSION; q->ncpus = ncpus;
    f = (struct q31_state *)((char *)ram + Q31_PA - RAM_BASE);
    f->magic=Q31_MAGIC;f->version=Q31_VERSION;f->ncpus=ncpus;
    interrupts=(struct q32_state *)((char *)ram+Q32_PA-RAM_BASE);
    interrupts->magic=Q32_MAGIC;interrupts->version=Q32_VERSION;interrupts->ncpus=ncpus;
    code=(struct q33_state *)((char *)ram+Q33_PA-RAM_BASE);
    code->magic=Q33_MAGIC;code->version=Q33_VERSION;code->ncpus=ncpus;
    startup=(struct q34_state *)((char *)ram+Q34_PA-RAM_BASE);
    startup->magic=Q34_MAGIC;startup->version=Q34_VERSION;startup->ncpus=ncpus;
    interrupts->sgi=Q32_SGI;interrupts->eoimode=0;
    unsigned char *x86 = (unsigned char *)ram + X86_RAM_IPA - RAM_BASE;
    initialize_x86(x86);
    dump_file("x86-initial.bin", x86, X86_RAM_SIZE);
    __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(s->counter_hz));
    uint32_t max_ipa, default_ipa;
    bool el2;
    hv_ipa_granule_t default_granule, granule;
    HV(hv_vm_config_get_max_ipa_size(&max_ipa));
    HV(hv_vm_config_get_default_ipa_size(&default_ipa));
    HV(hv_vm_config_get_default_ipa_granule(&default_granule));
    HV(hv_vm_config_get_el2_supported(&el2));
    hv_vm_config_t config = hv_vm_config_create();
    bool default_el2, selected_el2;
    HV(hv_vm_config_get_el2_enabled(config, &default_el2));
    HV(hv_vm_config_set_el2_enabled(config, false));
    HV(hv_vm_config_get_el2_enabled(config, &selected_el2));
    printf("{\"default_el2_enabled\":%s,\"selected_el2_enabled\":%s}\n",
           default_el2 ? "true" : "false", selected_el2 ? "true" : "false");
    HV(hv_vm_config_set_ipa_size(config, 36));
    HV(hv_vm_config_get_ipa_granule(config, &granule));
    printf("{\"format\":7,\"stage\":\"%s\",\"q30_state_version\":%lu,\"q31_state_version\":%lu,\"q32_state_version\":%lu,\"q33_state_version\":%lu,\"q34_state_version\":%lu,\"ram_bytes\":%lu,"
           "\"x86_ram_bytes\":%lu,\"ncpus\":%u,\"max_ipa_bits\":%u,"
           "\"default_ipa_bits\":%u,\"selected_ipa_bits\":36,\"default_ipa_granule_enum\":%u,"
           "\"selected_ipa_granule_enum\":%u,\"host_page_size\":%d,\"el2_supported\":%s,"
           "\"counter_hz\":%lu,\"placement\":\"uncontrolled\"}\n",
           entry_control ? "entry-control" : "q34startup", Q30_VERSION, Q31_VERSION, Q32_VERSION, Q33_VERSION, Q34_VERSION, RAM_SIZE, X86_RAM_SIZE, ncpus, max_ipa, default_ipa,
           default_granule, granule, getpagesize(), el2 ? "true" : "false", s->counter_hz);
    HV(hv_vm_create(config));
    os_release(config);
    HV(hv_vm_map(ram, RAM_BASE, RAM_SIZE, HV_MEMORY_READ | HV_MEMORY_WRITE | HV_MEMORY_EXEC));
    printf("{\"entry_control\":%s,\"gic_created\":%s}\n", entry_control ? "true" : "false", use_gic ? "true" : "false");
    if (use_gic) {
    hv_gic_config_t gic = hv_gic_config_create();
    HV(hv_gic_config_set_distributor_base(gic, GICD_PA));
    HV(hv_gic_config_set_redistributor_base(gic, GICR_PA));
    HV(hv_gic_create(gic));
    os_release(gic);
    uint32_t intid;
    HV(hv_gic_get_intid(HV_GIC_INT_EL1_PHYSICAL_TIMER, &intid));
    if (intid >= 32 || intid < 16) return 2;
    s->timer_intid = intid;
    HV(hv_gic_set_distributor_reg(HV_GIC_DISTRIBUTOR_REG_GICD_CTLR, 0x12));
    uint32_t spi_base,spi_count;HV(hv_gic_get_spi_interrupt_range(&spi_base,&spi_count));
    if(spi_base<32||spi_count<ncpus||spi_base+ncpus>1020)return 2;
    interrupts->spi_base=spi_base;interrupts->spi_count=spi_count;
    printf("{\"q32_spi_base\":%u,\"q32_spi_count\":%u,\"q32_sgi\":%u,\"q32_eoimode\":0,\"q32_native_group\":1}\n",spi_base,spi_count,Q32_SGI);
    }
    pthread_t threads[MAX_CPUS], watcher,devices;
    if (pthread_create(&watcher, NULL, watchdog, NULL)) die("watchdog thread");
    if (pthread_create(&devices,NULL,device_worker,NULL))die("device thread");
    for (unsigned i = 0; i < ncpus; ++i)
        if (pthread_create(&threads[i], NULL, cpu, (void *)(uintptr_t)i)) die("vcpu thread");
    for (unsigned i = 0; i < ncpus; ++i) pthread_join(threads[i], NULL);
    atomic_store(&devices_stop,true);pthread_join(devices,NULL);
    pthread_join(watcher, NULL);
    int pass = !atomic_load(&timed_out);
    for (unsigned i = 0; i < ncpus; ++i) {
        struct cpu_state *c = &s->cpu[i];
        if (entry_control) {
            pass &= entry_seen[i] && host_exits[i] == 1 && final_tso_ok[i];
            printf("{\"cpu\":%u,\"entry_hvc_seen\":%s,\"host_exits\":%lu}\n",
                   i, entry_seen[i] ? "true" : "false", host_exits[i]);
            continue;
        }
        int ok = entry_seen[i] && checkpoint_seen[i] == 1 && checkpoint_pass &&
                 q31_checkpoint_seen[i] == 1 && q31_checkpoint_pass &&
                 q32_checkpoint_seen[i] == 1 && q32_checkpoint_pass &&
                 q33_checkpoint_seen[i] == 1 && q33_checkpoint_pass &&
                 finish_seen[i] && host_exits[i] == 6+transfer_count[i] && !timer_exits[i] &&
                 final_tso_ok[i] && c->done == 1 && !c->failure && (c->tso_start & 2) && (c->tso_end & 2) &&
                 c->sync_count == 1 && c->sgi_count == 1 && c->timer_count >= 3 &&
                 c->alias_value == 0x13579bdf2468ace0UL && (c->sctlr & 1) && !(c->tcr & (3UL << 14)) &&
                 !(c->waker_after & 6);
        pass &= ok;
        printf("{\"cpu\":%u,\"foundation_check\":%s,\"done\":%lu,\"failure\":%lu,\"tso_start\":%lu,"
               "\"tso_end\":%lu,\"sctlr\":%lu,\"tcr\":%lu,\"mmfr0\":%lu,\"alias_value\":%lu,"
               "\"sync_count\":%lu,\"timer_count\":%lu,\"sgi_count\":%lu,\"esr\":%lu,\"far\":%lu,"
               "\"elr\":%lu,\"trace_count\":%lu,\"ticks_start\":%lu,\"ticks_end\":%lu,"
               "\"host_exits\":%lu,\"virtual_timer_exits\":%lu,\"physical_timer_intid\":%lu,"
               "\"waker_before\":%lu,\"waker_after\":%lu}\n",
               i, ok ? "true" : "false", c->done, c->failure, c->tso_start, c->tso_end, c->sctlr, c->tcr,
               c->mmfr0, c->alias_value, c->sync_count, c->timer_count, c->sgi_count, c->esr, c->far,
               c->elr, c->trace_count, c->ticks_start, c->ticks_end, host_exits[i], timer_exits[i], c->timer_intid,
               c->waker_before, c->waker_after);
        const struct q30_cpu *p = &q->cpu[i];
        printf("{\"cpu\":%u,\"q30_started\":%lu,\"q30_done\":%lu,\"operations\":%lu,"
               "\"faults\":%lu,\"table_traps\":%lu,\"guest_faults\":%lu,\"ack\":%lu,"
               "\"timer_start\":%lu,\"timer_end\":%lu}\n", i, p->started, p->done,
               p->operations, p->faults, p->table_traps, p->guest_faults, p->ack,
               p->timer_start, p->timer_end);
    }
    if (!entry_control) {
        int integrity = q30_integrity(QE_Q34);
        pass &= integrity;
        printf("{\"q30_log_integrity\":%s,\"failure\":%lu,\"records\":%lu,\"epoch\":%lu,"
               "\"completed_groups\":%lu,\"independent_oracle\":\"pending\"}\n",
               integrity ? "true" : "false", q->failure, q->records, q->epoch, q->completed_groups);
        int complete=q31_integrity();pass&=complete;
        printf("{\"q31_log_integrity\":%s,\"q31_phase\":%lu,\"q31_cases\":%lu,\"q31_failure\":%lu,"
               "\"independent_oracle\":\"pending\"}\n",complete?"true":"false",f->phase,f->cases,f->failure);
        for(unsigned i=0;i<ncpus;i++) {
            const struct q31_cpu *c=&f->cpu[i];
            printf("{\"cpu\":%u,\"q31_started\":%lu,\"q31_done\":%lu,\"q31_cases\":%lu,"
                   "\"q31_attempts\":%lu,\"q31_deliveries\":%lu,\"q31_commits\":%lu,"
                   "\"q31_code_writes\":%lu,\"q31_snapshots\":%lu,\"q31_timer_start\":%lu,\"q31_timer_end\":%lu}\n",
                   i,c->started,c->done,c->cases,c->attempts,c->deliveries,c->commits,c->code_writes,
                   c->snapshots,c->timer_start,c->timer_end);
        }
        int irq_complete=q32_integrity();pass&=irq_complete;
        printf("{\"q32_log_integrity\":%s,\"q32_phase\":%lu,\"q32_cases\":%lu,\"q32_failure\":%lu,\"independent_oracle\":\"pending\"}\n",
               irq_complete?"true":"false",interrupts->phase,interrupts->cases,interrupts->failure);
        for(unsigned i=0;i<ncpus;i++){
            const struct q32_cpu *c=&interrupts->cpu[i];
            printf("{\"cpu\":%u,\"q32_started\":%lu,\"q32_done\":%lu,\"q32_cases\":%lu,\"q32_native_count\":%lu,"
                   "\"q32_guest_count\":%lu,\"q32_boundaries\":%lu,\"q32_timer_start\":%lu,\"q32_timer_end\":%lu}\n",
                   i,c->started,c->done,c->cases,c->native_count,c->guest_count,c->boundaries,c->timer_start,c->timer_end);
        }
    }
    if(!entry_control){
        int ok=q33_integrity();
        pass&=ok;printf("{\"q33_basic_integrity\":%s,\"cases\":%lu,\"allocated\":%lu,\"failure\":%lu,\"independent_oracle\":\"pending\"}\n",ok?"true":"false",code->cases,code->allocated,code->failure);
    }
    for(unsigned i=0;i<ncpus;i++){
        const struct q33_cpu *c=&code->cpu[i];
        printf("{\"cpu\":%u,\"q33_started\":%lu,\"q33_done\":%lu,\"q33_cases\":%lu,\"q33_requests\":%lu,\"q33_chunks\":%lu,\"q33_entries\":%lu,\"q33_effects\":%lu,\"q33_timer_start\":%lu,\"q33_timer_end\":%lu}\n",
               i,c->started,c->done,c->cases,c->requests,c->chunks,c->entries,c->effects,c->timer_start,c->timer_end);
    }
    if(!entry_control){
        int ok=startup->magic==Q34_MAGIC&&startup->version==Q34_VERSION&&startup->ncpus==ncpus&&
            startup->phase==2&&!startup->failure&&startup->cases==Q34_VARIANTS*Q34_REPETITIONS&&
            !startup->lock_owner&&startup->diagnostic_ack==startup->diagnostic_request;
        for(unsigned i=0;i<ncpus;i++){
            const struct q34_cpu *c=&startup->cpu[i];
            ok&=c->cases==startup->cases&&!c->failures&&c->continuations==c->ready;
            printf("{\"cpu\":%u,\"q34_cases\":%lu,\"q34_ready\":%lu,\"q34_continuations\":%lu,\"q34_failure\":%lu,\"q34_cr3\":%lu,\"q34_rsp\":%lu}\n",
                   i,c->cases,c->ready,c->continuations,c->failures,c->arch.cr3,c->arch.gpr[QR_RSP]);
        }
        pass&=ok;printf("{\"q34_basic_integrity\":%s,\"cases\":%lu,\"failure\":%lu,\"independent_oracle\":\"pending\"}\n",ok?"true":"false",startup->cases,startup->failure);
    }
    dump_file("q34.bin",startup,sizeof(*startup));
    dump_file("q33.bin",code,sizeof(*code));
    dump_file("shared.bin", s, sizeof(*s));
    dump_file("q30.bin", q, sizeof(*q));
    dump_file("q31.bin", f, sizeof(*f));
    dump_file("q32.bin",interrupts,sizeof(*interrupts));
    int dump_ok = dump_ram();
    pass &= dump_ok;
    printf("{\"stopped_ram_dump_complete\":%s}\n", dump_ok ? "true" : "false");
    HV(hv_vm_unmap(RAM_BASE, RAM_SIZE));
    HV(hv_vm_destroy());
    HV(hv_vm_deallocate(ram, RAM_SIZE));
    ram = NULL;
    close(backing);
    printf("{\"%s\":%s,\"timing_qualified\":false}\n",
           entry_control ? "diagnostic_entry_pass" : "q34_host_checks_pass", pass ? "true" : "false");
    return pass ? 0 : 1;
}
