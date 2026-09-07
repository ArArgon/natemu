// Native synthetic validation of fixture expectations, not VM/protocol evidence.
// clang -DQ30_FIXTURE_CHECK -pthread check_cases.c q30_cases.c x86.c -o /tmp/check-cases
#ifdef Q34_CASES_CHECK
#define Q33_CASES_CHECK
#endif
#ifdef Q33_CASES_CHECK
#define Q32_CASES_CHECK
#endif
#ifdef Q32_CASES_CHECK
#define Q31_CASES_CHECK
#endif
#include "q34.h"
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sched.h>
#include <time.h>
struct q30_state *q30_fixture_state;
struct q31_state *q31_fixture_state;
struct shared *q31_fixture_shared;
unsigned char *q30_fixture_memory;
struct q34_state *q34_fixture_state;
#ifdef Q34_CASES_CHECK
static int q34_wrong_import_root;
#endif
static _Thread_local u64 current_cpu;
static pthread_mutex_t model = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t rendezvous = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t condition = PTHREAD_COND_INITIALIZER;
static unsigned arrived, generation;
static u64 accesses, changes, max_patches;
#ifdef Q33_CASES_CHECK
static int q33_drop_fetch_gpa;
#endif
static void model_change(void) {
#ifdef Q33_CASES_CHECK
    // Model change identity only; this is not the native gate/ACK protocol.
    __atomic_add_fetch(&Q30->epoch,1,__ATOMIC_RELEASE);
#endif
}
static u64 faults_at[MAX_CPUS];
#ifdef Q32_CASES_CHECK
struct q32_state *q32_fixture_state;
struct q33_state *q33_fixture_state;
extern char q32_hold_begin[],q32_hold_end[],q32_wait_begin[],q32_wait_after[],q32_wait_end[];
// Fixed model-only label order; tentative C globals may be linker-sorted.
__asm__(".section __DATA,__data\n"
        ".globl _q32_hold_begin,_q32_hold_end,_q32_wait_begin,_q32_wait_after,_q32_wait_end\n"
        "_q32_hold_begin: .space 16\n_q32_hold_end: .space 16\n"
        "_q32_wait_begin: .space 16\n_q32_wait_after: .space 16\n_q32_wait_end: .space 16\n"
        ".section __TEXT,__text\n");
static _Thread_local u64 model_daif,model_pc,model_cval;
static _Thread_local int delivering;
static unsigned q32_model_early_wait;
static u64 q32_model_halt_case;
static _Thread_local u64 q32_model_wait_case,q32_model_wait_trial;
static u64 native_pending[MAX_CPUS][3];
void q32_fixture_poll(void);
static u64 model_clock(void) {
    struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);
    return (u64)t.tv_sec*1000000000UL+(u64)t.tv_nsec;
}
u64 q32_fixture_read(const char *name) {
    if(!strcmp(name,"tpidr_el1"))return current_cpu;
    if(!strcmp(name,"mpidr_el1"))return current_cpu;
    if(!strcmp(name,"icc_ctlr_el1"))return 0;
    if(!strcmp(name,"ctr_el0"))return 0x00040004; // Model line sizes, not host evidence.
    if(!strcmp(name,"daif"))return model_daif;
    if(!strcmp(name,"cntp_cval_el0"))return model_cval;
    if(!strcmp(name,"cntvct_el0")){q32_fixture_poll();return model_clock();}
    fprintf(stderr,"unknown synthetic register read: %s\n",name);exit(1);
}
void q32_fixture_write(const char *name,u64 value) {
    if(!strcmp(name,"daif")){model_daif=value;return;}
    if(!strcmp(name,"cntp_tval_el0")){model_cval=model_clock()+value;return;}
    if(!strcmp(name,"cntp_ctl_el0")){
        if(value&1)__atomic_store_n(&native_pending[current_cpu][0],1,__ATOMIC_RELEASE);
        return;
    }
    if(!strcmp(name,"icc_sgi1r_el1")){
        for(u64 target=0;target<Q30->ncpus;target++)
            if(value&(1UL<<40)?target!=current_cpu:!!(value&(1UL<<target)))
                __atomic_store_n(&native_pending[target][1],1,__ATOMIC_RELEASE);
        return;
    }
    fprintf(stderr,"unknown synthetic register write: %s\n",name);exit(1);
}
void q32_fixture_poll(void) {
    if(!Q32||delivering)return;
    if(Q32->phase!=1&&(!Q33||Q33->phase!=1)
#ifdef Q34_CASES_CHECK
       &&(!Q34||Q34->phase!=1)
#endif
      )return;
    struct q32_cpu *c=&Q32->cpu[current_cpu];
    if(c->diagnostic_request>c->diagnostic_ack){
        q30_check(c->halted&&!(Q31->cpu[current_cpu].arch.flags&512)&&
                  (c->irr[1]&2)&&c->diagnostic_case==Q32->cases+1,911);
        __atomic_store_n(&c->diagnostic_ack,c->diagnostic_request,__ATOMIC_RELEASE);
    }
    if(c->ready&&c->spi_request>c->spi_sent){
        c->spi_sent=c->spi_request;
        __atomic_store_n(&native_pending[current_cpu][2],1,__ATOMIC_RELEASE);
    }
    if(c->spi_deassert>c->spi_deasserted)c->spi_deasserted=c->spi_deassert;
    if(model_daif&128)return;
    delivering=1;
    for(unsigned source=0;source<3;source++)
        if(__atomic_exchange_n(&native_pending[current_cpu][source],0,__ATOMIC_ACQUIRE)){
            u64 saved=model_daif;model_daif|=128;
            u64 intid=source==0?q31_fixture_shared->timer_intid:source==1?Q32_SGI:Q32->spi_base+current_cpu;
            q32_irq(intid,model_pc?model_pc:1,saved|5);
            model_daif=saved;
        }
    delivering=0;
    if(c->spi_deassert>c->spi_deasserted)c->spi_deasserted=c->spi_deassert;
}
// Synthetic scheduling only: these labels and notifications cannot establish
// a hardware IRQ route, ELR, wait entry, timing, or native paging protocol.
static u64 model_hold(u64 *ready,u64 *ack,u64 wanted,u64 deadline,u64 site) {
    model_pc=site;__atomic_store_n(ready,1,__ATOMIC_RELEASE);model_daif&=~128UL;
    while(__atomic_load_n(ack,__ATOMIC_ACQUIRE)<wanted&&model_clock()<deadline){
        q32_fixture_poll();sched_yield();
    }
    model_daif|=128;model_pc=0;
    return __atomic_load_n(ack,__ATOMIC_ACQUIRE)>=wanted;
}
u64 q32_hold(u64 *ready,u64 *ack,u64 wanted,u64 deadline) {
    return model_hold(ready,ack,wanted,deadline,(u64)q32_hold_begin+4);
}
u64 q32_wait(u64 *ready,u64 *ack,u64 wanted,u64 deadline) {
    u64 site=(u64)q32_wait_after;
    u64 serial=__atomic_load_n(&q32_model_halt_case,__ATOMIC_ACQUIRE);
    if(q32_model_early_wait&&Q32->phase==1&&serial){
        if(q32_model_wait_case!=serial){q32_model_wait_case=serial;q32_model_wait_trial=0;}
        // Select only the modeled native PC. Actual model notification,
        // acceptance, guest frame/service/IRET and retry code still execute.
        if(++q32_model_wait_trial<=q32_model_early_wait)site-=4;
    }
    return model_hold(ready,ack,wanted,deadline,site);
}
#endif
#ifdef Q31_CASES_CHECK
static u64 group_id, group_accesses[13], group_contexts[13], group_setups[13];
static u64 group_events[13], group_patches[13], group_invalidates[13];
static unsigned char code_pins[MAX_CPUS][X86_RAM_SIZE / 4096];
#endif
u64 q30_fixture_cpu(void) { return current_cpu; }
#ifdef Q34_CASES_CHECK
void q34_fixture_poll(void) {
    // Synthetic external observer; native host evidence comes from host.c.
    u64 request=__atomic_load_n(&Q34->diagnostic_request,__ATOMIC_ACQUIRE);
    if(request>__atomic_load_n(&Q34->diagnostic_ack,__ATOMIC_ACQUIRE)){
        const struct q34_mailbox *m=(const struct q34_mailbox *)Q34->memory;
        q30_check(request==Q34->serial&&m->command==1&&
                  Q34->diagnostic_reason>=Q34_WRONG_ID&&
                  Q34->diagnostic_reason<=Q34_BOOT_ONCE,929);
        __atomic_store_n(&Q34->diagnostic_ack,request,__ATOMIC_RELEASE);
    }
    q32_fixture_poll();sched_yield();
}
#endif

#ifdef Q32_CASES_CHECK
static void q32_failure_trace(u64 reason) {
    // Retain the forced exhaustion at the real post-service assertion. This
    // incomplete synthetic trace cannot be accepted as a completed VM/model.
    const char *path=getenv("Q32_TRACE_OUTPUT");
    if(reason!=0x3235||q32_model_early_wait!=8||!path)return;
    FILE *out=fopen(path,"wx");if(!out){perror("partial Q32 model trace");return;}
    struct q32_cpu *c=&Q32->cpu[current_cpu];struct q31_cpu *g=&Q31->cpu[current_cpu];
    fprintf(out,"{\"format\":\"natemu-q32-model-partial-v1\",\"ncpus\":%lu,\"q32_version\":%lu,\"failure_reason\":%lu,\"failing_cpu\":%lu,\"case_serial\":%lu,\"timer_intid\":%lu,\"spi_base\":%lu,\"symbols\":{\"_start\":1,\"_text_end\":%lu,\"_q32_hold_begin\":%lu,\"_q32_hold_end\":%lu,\"_q32_wait_begin\":%lu,\"_q32_wait_end\":%lu,\"_q32_wait_after\":%lu},\"final_cpu\":{\"native_count\":%lu,\"guest_count\":%lu,\"halted\":%lu,\"depth\":%lu,\"arch_words\":[",
            Q30->ncpus,Q32->version,reason,current_cpu,q32_model_halt_case,q31_fixture_shared->timer_intid,Q32->spi_base,~0UL,
            (u64)q32_hold_begin,(u64)q32_hold_end,(u64)q32_wait_begin,(u64)q32_wait_end,(u64)q32_wait_after,
            c->native_count,c->guest_count,c->halted,g->depth);
    const u64 *words=(const u64 *)&g->arch;
    for(unsigned i=0;i<sizeof(g->arch)/8;i++)fprintf(out,"%s%lu",i?",":"",words[i]);
    fputs("],\"irr_isr_words\":[",out);
    for(unsigned i=0;i<8;i++)fprintf(out,"%s%lu",i?",":"",i<4?c->irr[i]:c->isr[i-4]);
    fputs("]}}\n",out);
    for(u64 i=0;i<Q30->records;i++){
        const struct q30_record *r=&Q30->log[i];
        fprintf(out,"[%lu,%lu,%lu,%lu,[",r->sequence,r->kind,r->cpu,r->epoch);
        for(unsigned j=0;j<8;j++)fprintf(out,"%s%lu",j?",":"",r->data[j]);
        fputs("]]\n",out);
    }
    int error=ferror(out);if(fclose(out)||error)fprintf(stderr,"partial model trace write failed\n");
}
#endif
void q30_check(int ok, u64 reason) {
    if (ok) return;
#ifdef Q32_CASES_CHECK
    q32_failure_trace(reason);
#endif
    fprintf(stderr, "FAIL reason=%lu cpu=%lu case=%lx status=%lu error=%lu\n", reason,
            current_cpu, Q30->case_id, Q30->cpu[current_cpu].request.status,
            Q30->cpu[current_cpu].request.error);
#ifdef Q32_CASES_CHECK
    fprintf(stderr,"Q32 model group=%lu completed=%lu stage=%lu ELR=%lx\n",
            group_id,Q32->cpu[current_cpu].cases,Q32->cpu[current_cpu].stage,Q32->cpu[current_cpu].last_elr);
#endif
    exit(1);
}
void q30_barrier(void) {
    pthread_mutex_lock(&rendezvous);
    unsigned old = generation;
    if (++arrived == Q30->ncpus) {
        arrived = 0; ++generation; pthread_cond_broadcast(&condition);
    } else while (generation == old) pthread_cond_wait(&condition, &rendezvous);
    pthread_mutex_unlock(&rendezvous);
}
void q30_poll(void) {
#ifdef Q32_CASES_CHECK
    q32_fixture_poll(); sched_yield();
#endif
}
u64 q30_timer_count(void) {
#ifdef Q31_CASES_CHECK
    return ++q31_fixture_shared->cpu[current_cpu].timer_count;
#else
    return 0;
#endif
} // Synthetic progress only, never native timer/interrupt evidence.
u64 q30_record(u64 kind, const u64 data[8]) {
#ifdef Q32_CASES_CHECK
    if(kind==QE_Q32&&data[0]==Q32_CASE&&data[4])
        __atomic_store_n(&q32_model_halt_case,data[1]==7&&data[2]==2?data[5]:0,__ATOMIC_RELEASE);
#endif
#ifdef Q31_CASES_CHECK
    if ((kind == QE_Q31 && data[0] == Q31_CASE && data[4])
#ifdef Q32_CASES_CHECK
        || (kind == QE_Q32 && data[0] == Q32_CASE && data[4])
#endif
#ifdef Q33_CASES_CHECK
        || (kind == QE_Q33 && data[0] == Q33_CASE && data[4])
#endif
       )
        __atomic_store_n(&group_id, data[1], __ATOMIC_RELEASE);
#ifdef Q34_CASES_CHECK
    if(kind==Q34_RECORD_KIND&&data[0]==Q34_CASE&&data[3])
        __atomic_store_n(&group_id,data[1]+1,__ATOMIC_RELEASE);
#endif
    __atomic_add_fetch(&group_events[__atomic_load_n(&group_id, __ATOMIC_ACQUIRE)], 1, __ATOMIC_RELAXED);
    u64 sequence = __atomic_add_fetch(&Q30->records, 1, __ATOMIC_RELAXED);
    q30_check(sequence <= Q30_LOG_CAPACITY, 907);
    Q30->log[sequence - 1] = (struct q30_record){.sequence=sequence,.kind=kind,.cpu=current_cpu};
    memcpy(Q30->log[sequence - 1].data, data, sizeof(Q30->log[0].data));
    return sequence;
#else
    (void)kind; (void)data;
    return __atomic_add_fetch(&Q30->records, 1, __ATOMIC_RELAXED);
#endif
}
static unsigned char seen[X86_RAM_SIZE / 4096];
static unsigned char new_roles[X86_RAM_SIZE / 4096];
static void visit(u64 table, unsigned level, u64 nxe) {
    if (table >= X86_RAM_SIZE || table & 4095) return;
    unsigned bit = 1U << (level - 1 + 4 * nxe), page = (unsigned)(table >> 12);
    new_roles[page] |= 1U << (level - 1);
    if (seen[page] & bit) return;
    seen[page] |= bit;
    if (level == 1) return;
    for (unsigned i = 0; i < 512; ++i) {
        u64 value = *(u64 *)(q30_fixture_memory + table + i * 8);
        if (!(value & 1) || value & 0x000ffff000000000UL || (!nxe && value >> 63) || value & 128) continue;
        visit(value & X86_PA_MASK, level - 1, nxe);
    }
}
static void roles(void) {
    memset(new_roles, 0, sizeof(new_roles)); memset(seen, 0, sizeof(seen));
    for (u64 i = 0; i < Q30->ncpus; ++i) visit(Q30->cpu[i].context.cr3, 4, Q30->cpu[i].context.nxe);
    for (unsigned i = 0; i < sizeof(new_roles); ++i) Q30->roles[i] = new_roles[i];
}
void q30_context(struct x86_context c, u64 reason) {
    (void)reason; pthread_mutex_lock(&model);
#ifdef Q34_CASES_CHECK
    // Inject the original continuity defect at the actual context seam.
    // This negative control cannot establish native paging behavior.
    if(q34_wrong_import_root&&Q34->phase==1&&c.cr3==0x30b000)
        c.cr3=0x250000+current_cpu*0x4000;
#endif
    Q30->cpu[current_cpu].context = c;
#ifdef Q31_CASES_CHECK
    ++group_contexts[group_id];
    QLOG(QE_CONTEXT, c.cr3, c.cpl, c.wp, c.nxe, c.pge, reason);
#endif
    roles(); ++changes; model_change();
#ifdef Q32_CASES_CHECK
    q32_hook(Q32_TRACKING);
#endif
    pthread_mutex_unlock(&model);
}
void q30_invalidate(u64 va, u64 reason) {
    (void)va; (void)reason; model_change();
#ifdef Q31_CASES_CHECK
    __atomic_add_fetch(&group_invalidates[group_id], 1, __ATOMIC_RELAXED);
#ifdef Q32_CASES_CHECK
    q32_hook(Q32_TRACKING);
#endif
#endif
}
void q30_setup_batch(const struct q30_patch *p, u64 count) {
    pthread_mutex_lock(&model);
    if (count > max_patches) max_patches = count;
#ifdef Q31_CASES_CHECK
    ++group_setups[group_id]; group_patches[group_id] += count;
#endif
    for (u64 i = 0; i < count; ++i) {
#ifdef Q34_CASES_CHECK
        u64 old;memcpy(&old,q30_fixture_memory+p[i].gpa,8);
        // Model-only data provenance, not native quiescence/epoch evidence.
        QLOG(QE_MUTATION,p[i].gpa,old,p[i].value,8,QM_SETUP,Q30->cpu[current_cpu].request.operation,0);
#endif
        memcpy(q30_fixture_memory + p[i].gpa, &p[i].value, 8);
    }
    roles(); ++changes; model_change();
#ifdef Q32_CASES_CHECK
    q32_hook(Q32_TRACKING);
#endif
    pthread_mutex_unlock(&model);
}
void q30_setup(u64 gpa, u64 value, unsigned width) {
    q30_check(width == 8, 900);
    q30_setup_batch(&(struct q30_patch){gpa, value}, 1);
}
u64 q30_fault_address(void) { return faults_at[current_cpu]; }
u64 q30_access(u64 va, unsigned kind, unsigned width, u64 value, u64 compare) {
    pthread_mutex_lock(&model);
    struct q30_cpu *c = &Q30->cpu[current_cpu];
    struct q30_request *r = &c->request;
    unsigned access = kind == Q_LOAD ? X86_READ : kind == Q_EXEC || kind == Q_FETCH8 ? X86_EXECUTE : X86_WRITE;
    r->gva = va; r->kind = kind; r->width = width; r->value = value; r->compare = compare;
    r->operation = accesses + 1; r->gpa = ~0UL; r->faults = 0;
#ifdef Q31_CASES_CHECK
    r->inflight = 1;
    if (kind == Q_CMPXCHG) r->rax = compare;
    r->operation = QLOG(QE_BEGIN, va, kind, width, value, compare, r->rax, r->flags, Q30->case_id);
    // Exercise the producer's actual metadata predicate for every helper call.
    // These C symbol addresses are synthetic sites, not observed Arm exceptions.
    extern char q30_load_site[], q30_load8_site[], q30_store_site[], q30_byte_site[], q30_cas_load[];
    u64 pc = kind == Q_EXEC ? (va >> 63 ? va : va | (1UL << 47)) :
             kind == Q_CMPXCHG ? (u64)q30_cas_load :
             kind == Q_STORE ? (u64)(width == 1 ? q30_byte_site : q30_store_site) :
             (u64)(width == 1 ? q30_load8_site : q30_load_site);
    q30_check(q31_validate_fault(current_cpu, pc) && !q31_validate_fault(current_cpu, 1), 908);
#endif
#ifdef Q32_CASES_CHECK
    q32_hook(Q32_REPAIR);
    if (kind == Q_STORE) q32_hook(Q32_TRACKING);
#endif
    struct x86_walk first, last;
    x86_walk(q30_fixture_memory, &c->context, va, access, &first);
    x86_walk(q30_fixture_memory, &c->context, va + width - 1, access, &last);
    struct x86_walk *bad = first.status ? &first : &last;
    r->status = bad->status; r->error = bad->error; r->rax = kind == Q_CMPXCHG ? compare : r->rax;
    faults_at[current_cpu] = first.status ? va : bad->status ? (va + width - 1) & ~4095UL : va;
    u64 result = 0;
    if (!r->status) {
        r->gpa = first.gpa;
        for (unsigned side = 0; side < 2; ++side) {
            struct x86_walk *walk = side ? &last : &first;
            for (u64 i = 0; i < walk->count; ++i) {
                u64 *entry=(u64 *)(q30_fixture_memory+walk->entry_gpa[i]);
                u64 bits=32|((access==X86_WRITE&&i+1==walk->count)?64UL:0);
#ifdef Q34_CASES_CHECK
                if((*entry|bits)!=*entry)
                    QLOG(QE_MUTATION,walk->entry_gpa[i],*entry,*entry|bits,8,QM_AD,r->operation,bits);
#endif
                *entry|=bits;
            }
        }
        unsigned char bytes[8] = {0};
        for (unsigned i = 0; i < width; ++i) {
            u64 pa = (va & 4095) + i < 4096 ? first.gpa + i : (last.gpa & ~4095UL) + ((va + i) & 4095);
            bytes[i] = q30_fixture_memory[pa];
        }
        memcpy(&result, bytes, width);
        if (kind == Q_EXEC) result = (result >> 5) & 0xffff;
        if (access == X86_WRITE) {
            if (Q30->roles[first.gpa >> 12]) ++c->table_traps;
            u64 store = kind == Q_CMPXCHG && compare != result ? result : value;
            memcpy(bytes, &store, width);
            for (unsigned i = 0; i < width; ++i) {
                u64 pa = (va & 4095) + i < 4096 ? first.gpa + i : (last.gpa & ~4095UL) + ((va + i) & 4095);
                q30_fixture_memory[pa] = bytes[i];
            }
#ifdef Q31_CASES_CHECK
            if (Q31->code_roles[first.gpa >> 12]) {
                u64 page = first.gpa >> 12, old = Q31->code_generation[page]++;
                ++Q31->cpu[current_cpu].code_writes;
                QLOG(QE_Q31, Q31_CODE, QC_GENERATION, page * 4096, old, old + 1,
                     r->operation, old + 1, Q31->code_users[page]);
#ifdef Q32_CASES_CHECK
                q32_hook(Q32_CODE_WRITE);
#endif
            }
#endif
            if (kind == Q_CMPXCHG) {
                r->flags = x86_cmp_flags(compare, result, r->flags);
                if (compare != result) r->rax = result;
            } else result = value;
            roles(); ++changes; model_change();
        }
    }
    ++accesses;
#ifdef Q33_CASES_CHECK
    // Reproduce the retained native omission on the second successful source
    // byte, preserving its byte/status. This is an injected result-contract
    // failure, not a model of the hardware shadow cache.
    if(q33_drop_fetch_gpa && !r->status && kind==Q_FETCH8 &&
       Q33->cpu[current_cpu].group==1 && !Q33->cpu[current_cpu].variant &&
       !Q33->cpu[current_cpu].repetition && va==0x1000e021+current_cpu*0x20000)
        r->gpa=~0UL;
#endif
#ifdef Q31_CASES_CHECK
    ++group_accesses[group_id];
    QLOG(QE_END, r->operation, result, r->status, r->error, r->gpa, r->rax, r->flags, 0);
    r->inflight = 0;
#endif
    pthread_mutex_unlock(&model);
    return result;
}
#ifdef Q31_CASES_CHECK
void q30_code_track(u64 page, int enabled) {
    pthread_mutex_lock(&model);
    q30_check(!(page & 4095) && page < X86_RAM_SIZE &&
              Q31->code_roles[page >> 12] != enabled && !Q31->code_users[page >> 12], 902);
    Q31->code_roles[page >> 12] = (unsigned char)enabled; model_change();
    u64 old = Q31->code_generation[page >> 12];
    QLOG(QE_Q31, Q31_CODE, QC_ROLE, page, !enabled, enabled, 0, old, 0);
    if (enabled) {
        ++Q31->code_generation[page >> 12];
        QLOG(QE_Q31, Q31_CODE, QC_GENERATION, page, old, old + 1, 0, old + 1, 0);
    }
    pthread_mutex_unlock(&model);
}
u64 q31_code_generation(u64 page) {
    pthread_mutex_lock(&model);
    u64 generation = Q31->code_generation[page >> 12];
    pthread_mutex_unlock(&model);
    return generation;
}
int q30_code_pin(u64 page, u64 generation) {
    pthread_mutex_lock(&model);
    unsigned slot = (unsigned)(page >> 12);
    int ok = Q31->code_roles[slot] && Q31->code_generation[slot] == generation;
    if (ok) {
        q30_check(!code_pins[current_cpu][slot], 903);
        code_pins[current_cpu][slot] = 1;
        u64 old = Q31->code_users[slot]++;
        QLOG(QE_Q31, Q31_CODE, QC_PIN, page, old, old + 1, 0, generation, old + 1);
    }
    pthread_mutex_unlock(&model);
    return ok;
}
void q30_code_unpin(u64 page) {
    pthread_mutex_lock(&model);
    unsigned slot = (unsigned)(page >> 12);
    q30_check(code_pins[current_cpu][slot] && Q31->code_users[slot], 904);
    code_pins[current_cpu][slot] = 0;
    u64 old = Q31->code_users[slot]--;
    QLOG(QE_Q31, Q31_CODE, QC_UNPIN, page, old, old - 1, 0, Q31->code_generation[slot], old - 1);
    pthread_mutex_unlock(&model);
}
#endif
#ifdef Q33_CASES_CHECK
static unsigned char *q33_model_code;
static struct q33_host_transaction q33_model_host[MAX_CPUS];
static FILE *q33_model_transfers;
static int q33_model_first=1;
static pthread_mutex_t q33_model_output=PTHREAD_MUTEX_INITIALIZER;
static void model_words(FILE *out,const u64 *words,unsigned count) {
    fputc('[',out);
    for(unsigned i=0;i<count;i++)fprintf(out,"%s%lu",i?",":"",words[i]);
    fputc(']',out);
}
void q33_transfer(struct q33_packet *p) {
    struct q33_packet request=*p;
    struct q33_host_transaction *t=&q33_model_host[current_cpu];
    int accepted=q33_respond(t,(unsigned)current_cpu,p);
    if(q33_model_transfers){
        pthread_mutex_lock(&q33_model_output);
        fprintf(q33_model_transfers,"%s{\"cpu\":%lu,\"request\":",q33_model_first?"":",\n",current_cpu);
        q33_model_first=0;model_words(q33_model_transfers,request.word,16);
        fputs(",\"response\":",q33_model_transfers);model_words(q33_model_transfers,p->word,16);
        fprintf(q33_model_transfers,",\"accepted\":%s",accepted?"true":"false");
        if(accepted&&request.word[0]==QT_REQUEST&&t->received==sizeof(t->request)&&p->word[10]==QT_OK){
            fputs(",\"produced_hex\":\"",q33_model_transfers);
            const unsigned char *bytes=(const unsigned char *)t->code;
            for(u64 i=0;i<t->request.length;i++)fprintf(q33_model_transfers,"%02x",bytes[i]);
            fputc('"',q33_model_transfers);
        }
        fputc('}',q33_model_transfers);
        q30_check(!ferror(q33_model_transfers),922);
        pthread_mutex_unlock(&q33_model_output);
    }
    q30_check(accepted,923);
    if(request.word[12]==6&&p->word[10]==QT_WAIT){
        struct timespec delay={0,5000000};nanosleep(&delay,NULL);
    }
    // Model the helper's reentry unmask and queued notification, not its native
    // instruction interval or register preservation. The VM oracle proves those.
    model_daif&=~128UL;q32_fixture_poll();
}
static void model_binary(const char *variable,const void *data,size_t bytes) {
    const char *path=getenv(variable);if(!path)return;
    FILE *out=fopen(path,"wx");
    if(!out){perror(variable);exit(1);}
    q30_check(fwrite(data,1,bytes,out)==bytes,924);
    q30_check(fclose(out)==0,925);
}
unsigned char *q33_fixture_code(u64 address) {
    q30_check(address>=Q33_POOL&&address<Q33_POOL_END,920);
    return q33_model_code+(address-Q33_POOL);
}
u64 q33_fixture_execute(struct q33_block *b,u64 input) {
    u32 expected[Q33_MAX_BYTES/4];
    struct q33_request r={.source=b->source,.kind=b->kind,.length=b->length};
    q30_check(q33_emit(&r,expected)==b->length &&
              b->address+b->length<=Q33_POOL_END &&
              !memcmp(q33_fixture_code(b->address),expected,b->length),921);
    // Synthetic callback composition only. The independent Python oracle
    // decodes emitted words; actual native execution is reserved for the VM.
    typedef u64 (*callback)(struct q33_block *,u64,u64);
    u64 value=((callback)b->enter)(b,b->address,input);
    value+=b->kind==1?b->operand:0;
    return ((callback)b->finish)(b,b->address,value);
}
#endif
static void *worker(void *arg) {
    current_cpu = (u64)arg;
#ifdef Q34_CASES_CHECK
    q34_run(current_cpu);
#elif defined(Q33_CASES_CHECK)
    q33_run(current_cpu);
#elif defined(Q32_CASES_CHECK)
    q32_run(current_cpu);
#elif defined(Q31_CASES_CHECK)
    q31_run(current_cpu);
#else
    q30_cases(current_cpu);
#endif
    return NULL;
}
int main(int argc, char **argv) {
    q30_fixture_state = calloc(1, sizeof(*Q30)); q30_fixture_memory = calloc(1, X86_RAM_SIZE);
    if (!Q30 || !q30_fixture_memory) return 1;
    Q30->ncpus = argc == 2 ? strtoul(argv[1], NULL, 10) : 2;
    if (Q30->ncpus < 2 || Q30->ncpus > MAX_CPUS) return 2;
    q31_fixture_state = calloc(1, sizeof(*Q31));
    q31_fixture_shared = calloc(1, sizeof(*q31_fixture_shared));
    if (!Q31 || !q31_fixture_shared) return 1;
    Q31->magic = Q31_MAGIC; Q31->version = Q31_VERSION; Q31->ncpus = Q30->ncpus;
    q31_fixture_shared->ncpus = Q30->ncpus;
    for (u64 p = 0; p < X86_RAM_SIZE; p += 4096) {
        *(u64 *)(q30_fixture_memory + p) = 0x5a00000000000000UL | p;
        *(u64 *)(q30_fixture_memory + p + 4088) = 0x5a00000000000000UL | (p + 4088);
    }
    memset(q30_fixture_memory + 0x200000, 0, 0x50000);
#ifdef Q31_CASES_CHECK
    Q30->completed_groups = 0xfff;
    for (u64 i = 0; i < Q30->ncpus; ++i) Q30->cpu[i].started = Q30->cpu[i].done = 1;
#endif
    *(u64 *)(q30_fixture_memory + 0x4000) = 0xd65f03c0d2800540UL;
    *(u64 *)(q30_fixture_memory + 0x5000) = 0xd65f03c0d2800560UL;
    *(u64 *)(q30_fixture_memory + 0x4ff8) = 0xd65f03c0d2800540UL;
    *(u64 *)(q30_fixture_memory + 0x5ff8) = 0xd65f03c0d2800560UL;
#ifdef Q32_CASES_CHECK
    const char *early=getenv("Q32_MODEL_EARLY_WAIT");
    if(early){
        char *end;unsigned long count=strtoul(early,&end,10);
        if(!*early||*end||count>8)return 2;
        q32_model_early_wait=(unsigned)count;
    }
    q32_fixture_state = calloc(1, sizeof(*Q32));
    q33_fixture_state = calloc(1, sizeof(*Q33));
    q34_fixture_state=calloc(1,sizeof(*Q34));
    if (!Q32 || !Q33 || !Q34) return 1;
    Q34->magic=Q34_MAGIC;Q34->version=Q34_VERSION;Q34->ncpus=Q30->ncpus;
    Q32->magic=Q32_MAGIC;Q32->version=Q32_VERSION;Q32->ncpus=Q30->ncpus;
    Q32->spi_base=64;Q32->spi_count=Q30->ncpus;Q32->sgi=Q32_SGI;
    Q31->phase=2;Q31->cases=Q31_VARIANTS*Q31_REPETITIONS;
    for(u64 i=0;i<Q30->ncpus;i++)Q31->cpu[i].started=Q31->cpu[i].done=1;
    q31_fixture_shared->counter_hz=1000000000;q31_fixture_shared->timer_intid=30;
#endif
#ifdef Q33_CASES_CHECK
    q33_drop_fetch_gpa=getenv("Q33_MODEL_DROP_FETCH_GPA")!=NULL;
    Q33->magic=Q33_MAGIC;Q33->version=Q33_VERSION;Q33->ncpus=Q30->ncpus;
    Q32->phase=2;Q32->cases=Q32_REPETITIONS*Q32_VARIANTS;
    for(u64 i=0;i<Q30->ncpus;i++)Q32->cpu[i].started=Q32->cpu[i].done=1;
    q33_model_code=calloc(1,Q33_POOL_END-Q33_POOL);
    if(!q33_model_code)return 1;
    const char *host_path=getenv("Q33_MODEL_HOST_OUTPUT");
    if(host_path){
        q33_model_transfers=fopen(host_path,"wx");
        if(!q33_model_transfers){perror("fresh Q33 model host trace");return 1;}
        fputs("{\"format\":\"natemu-q33-model-host-v1\",\"transfers\":[\n",q33_model_transfers);
    }
#endif
#ifdef Q34_CASES_CHECK
    q34_wrong_import_root=getenv("Q34_MODEL_WRONG_IMPORT_ROOT")!=NULL;
    Q33->phase=2;Q33->cases=Q33_VARIANTS*Q33_REPETITIONS;
    for(u64 i=0;i<Q30->ncpus;i++)Q33->cpu[i].started=Q33->cpu[i].done=1;
#endif
#ifdef Q34_CASES_CHECK
    model_binary("Q34_MODEL_INITIAL_MEMORY_OUTPUT",q30_fixture_memory,X86_RAM_SIZE);
#endif
    pthread_t threads[MAX_CPUS];
    for (u64 i = 0; i < Q30->ncpus; ++i) {
        Q30->cpu[i].context = (struct x86_context){0x200000, 0, 1, 1, 0};
        Q30->cpu[i].request.flags = 0x202;
    }
    for (u64 i = 0; i < Q30->ncpus; ++i) if (pthread_create(&threads[i], NULL, worker, (void *)i)) return 2;
    for (u64 i = 0; i < Q30->ncpus; ++i) if (pthread_join(threads[i], NULL)) return 2;
    q30_check(Q30->completed_groups == 0xfff, 901);
    printf("PASS synthetic fixture expectations: %lu CPUs, %lu accesses, %lu changes, max %lu setup patches; no VM/protocol evidence\n",
           Q30->ncpus, accesses, changes, max_patches);
#ifdef Q31_CASES_CHECK
#ifdef Q34_CASES_CHECK
    q30_check(Q34->phase==2&&!Q34->failure&&Q34->cases==Q34_VARIANTS*Q34_REPETITIONS,930);
    for(u64 i=0;i<Q30->ncpus;i++)q30_check(Q34->cpu[i].cases==Q34->cases&&!q33_model_host[i].active,931);
#elif defined(Q33_CASES_CHECK)
    q30_check(Q33->phase==2 && !Q33->failure,926);
    for(u64 i=0;i<Q30->ncpus;i++)q30_check(Q33->cpu[i].done&&!q33_model_host[i].active,927);
#elif defined(Q32_CASES_CHECK)
    q30_check(Q32->phase==2 && !Q32->failure, 909);
    for(u64 i=0;i<Q30->ncpus;i++)q30_check(Q32->cpu[i].done,910);
#else
    q30_check(Q31->phase == 2 && !Q31->failure && Q31->cases == Q31_VARIANTS * Q31_REPETITIONS, 905);
    for (u64 i = 0; i < Q30->ncpus; ++i)
        q30_check(Q31->cpu[i].done && Q31->cpu[i].cases == Q31_VARIANTS * Q31_REPETITIONS, 906);
#endif
    for (unsigned group = 0; group <= 12; ++group)
        printf("group %u: %lu accesses, %lu contexts, %lu invalidates, %lu setups, %lu patches, %lu direct events\n",
               group, group_accesses[group], group_contexts[group], group_invalidates[group],
               group_setups[group], group_patches[group], group_events[group]);
#ifdef Q34_CASES_CHECK
    const char *trace_path=getenv("Q34_TRACE_OUTPUT");
#elif defined(Q33_CASES_CHECK)
    const char *trace_path = getenv("Q33_TRACE_OUTPUT");
#elif defined(Q32_CASES_CHECK)
    const char *trace_path = getenv("Q32_TRACE_OUTPUT");
#else
    const char *trace_path = getenv("Q31_TRACE_OUTPUT");
#endif
    if (trace_path) {
        FILE *trace = fopen(trace_path, "wx");
        if (!trace) { perror("fresh model trace"); return 1; }
#ifdef Q32_CASES_CHECK
#ifdef Q34_CASES_CHECK
        const char *model_format="natemu-q34-model-trace-v1";
#elif defined(Q33_CASES_CHECK)
        const char *model_format="natemu-q33-model-trace-v1";
#else
        const char *model_format="natemu-q32-model-trace-v1";
#endif
        fprintf(trace,"{\"format\":\"%s\",\"ncpus\":%lu,\"q32_version\":%lu,\"timer_intid\":%lu,\"spi_base\":%lu,\"symbols\":{\"_start\":1,\"_text_end\":%lu,\"_q32_hold_begin\":%lu,\"_q32_hold_end\":%lu,\"_q32_wait_begin\":%lu,\"_q32_wait_end\":%lu,\"_q32_wait_after\":%lu},\"initial_q31_counts\":[",
                model_format,Q30->ncpus,Q32->version,q31_fixture_shared->timer_intid,Q32->spi_base,~0UL,
                (u64)q32_hold_begin,(u64)q32_hold_end,(u64)q32_wait_begin,(u64)q32_wait_end,(u64)q32_wait_after);
        for(u64 i=0;i<Q30->ncpus;i++)fprintf(trace,"%s{\"attempts\":0,\"deliveries\":0,\"commits\":0,\"snapshots\":0}",i?",":"");
#ifdef Q33_CASES_CHECK
        fputs("],\"initial_q32_states\":null}\n",trace);
#else
        fputs("]}\n",trace);
#endif
#else
        fprintf(trace, "{\"format\":\"natemu-q31-model-trace-v1\",\"ncpus\":%lu}\n", Q30->ncpus);
#endif
        for (u64 i = 0; i < Q30->records; ++i) {
            const struct q30_record *r = &Q30->log[i];
            fprintf(trace, "[%lu,%lu,%lu,%lu,[", r->sequence, r->kind, r->cpu, r->epoch);
            for (unsigned word = 0; word < 8; ++word)
                fprintf(trace, "%s%lu", word ? "," : "", r->data[word]);
            fputs("]]\n", trace);
        }
        if (ferror(trace) || fclose(trace)) return 1;
        printf("Model-only metadata/access trace: %s; no paging/native-fault protocol evidence\n", trace_path);
    }
#endif
#ifdef Q33_CASES_CHECK
    if(q33_model_transfers){
        fputs("\n]}\n",q33_model_transfers);
        q30_check(!ferror(q33_model_transfers)&&fclose(q33_model_transfers)==0,928);
    }
    model_binary("Q33_MODEL_POOL_OUTPUT",q33_model_code,Q33_POOL_END-Q33_POOL);
    model_binary("Q33_MODEL_STATE_OUTPUT",Q33,sizeof(*Q33));
#ifdef Q34_CASES_CHECK
    model_binary("Q34_MODEL_STATE_OUTPUT",Q34,sizeof(*Q34));
    model_binary("Q34_MODEL_MEMORY_OUTPUT",q30_fixture_memory,X86_RAM_SIZE);
#endif
    free(q33_model_code);
#endif
#ifdef Q32_CASES_CHECK
    free(Q32); free(Q33); free(Q34);
#endif
    free(Q30); free(q30_fixture_memory); free(Q31); free(q31_fixture_shared);
    return 0;
}
