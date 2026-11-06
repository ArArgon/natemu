#include "q34.h"
#define READ(reg) ({ u64 v; __asm__ volatile("mrs %0, " #reg : "=r"(v)); v; })
#define WRITE(reg, v) __asm__ volatile("msr " #reg ", %0" :: "r"((u64)(v)) : "memory")
#define SYNC() __asm__ volatile("dsb ish\nisb" ::: "memory")
#define LOAD(p) __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define STORE(p, v) __atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define S ((volatile struct shared *)SHARED_PA)
#define MEMORY ((unsigned char *)X86_RAM_IPA)
static unsigned char roles_new[X86_RAM_SIZE / 4096];
static unsigned char roles_seen[X86_RAM_SIZE / 4096];
static u64 fault_address[MAX_CPUS];
static unsigned char code_pins[MAX_CPUS][X86_RAM_SIZE / 4096];
extern u64 q30_load64(u64), q30_store64(u64, u64), q30_store8(u64, u64);
extern u64 q30_load8(u64);
extern u64 q30_cas(u64, u64, u64), q30_exec(u64);
extern char q30_load_site[], q30_load_return[], q30_store_site[], q30_store_return[];
extern char q30_byte_site[], q30_byte_return[], q30_cas_load[], q30_cas_store[];
extern char q30_cas_return[], q30_exec_return[];
extern char q30_load8_site[], q30_load8_return[];

static u64 id(void) { return READ(tpidr_el1); }
static struct q30_cpu *self(void) { return &Q30->cpu[id()]; }
u64 q30_fault_address(void) { return fault_address[id()]; }
static void zero(void *address, u64 bytes) {
    volatile unsigned char *p = address;
    while (bytes--) *p++ = 0;
}
void q30_check(int ok, u64 reason) {
    if (ok) return;
    Q30->failure = reason;
    S->cpu[id()].failure = 0x300000 + reason;
    SYNC();
    __asm__ volatile("hvc #0x31");
    for (;;) __asm__ volatile("wfe");
}
u64 q30_record(u64 kind, const u64 data[8]) {
    u64 index = __atomic_fetch_add(&Q30->records, 1, __ATOMIC_RELAXED);
    q30_check(index < Q30_LOG_CAPACITY, 1);
    struct q30_record *r = &Q30->log[index];
    r->kind = kind; r->cpu = id(); r->epoch = LOAD(&Q30->epoch);
    for (unsigned i = 0; i < 8; ++i) r->data[i] = data[i];
    STORE(&r->sequence, index + 1);
    return index + 1;
}
static void park(u64 where) {
    struct q30_cpu *c = self();
    while (LOAD(&Q30->gate) && LOAD(&Q30->owner) != id()) {
        q30_check(!LOAD(&c->active), 2);
        u64 epoch = LOAD(&Q30->epoch);
        if (LOAD(&Q30->gate) == 2 && LOAD(&c->ack) != epoch) {
            SYNC();
            // A delayed observer must not acknowledge a later epoch using an
            // earlier flush's synchronization. This unacknowledged epoch cannot
            // close while this CPU still owes its acknowledgement.
            if (LOAD(&Q30->epoch) != epoch || LOAD(&Q30->gate) != 2) continue;
            QLOG(QE_ACK, epoch, where, LOAD(&c->waiting), c->request.inflight,
                 c->request.operation, c->request.faults);
            c->acknowledgements++;
            STORE(&c->ack, epoch);
        }
        __asm__ volatile("yield" ::: "memory");
    }
}
void q30_poll(void) { park(1); }
u64 q30_timer_count(void) { return S->cpu[id()].timer_count; }
static void lock_enter(void) {
    q30_check(!LOAD(&self()->active), 3);
    int recorded = 0;
    for (;;) {
        park(2);
        if (!__atomic_exchange_n(&Q30->lock, 1, __ATOMIC_ACQUIRE)) break;
        STORE(&self()->waiting, 1);
        if (!recorded) {
            QLOG(QE_WAIT, self()->request.operation, self()->request.inflight,
                 self()->request.faults, S->cpu[id()].timer_count);
            recorded = 1;
        }
        __asm__ volatile("yield" ::: "memory");
    }
    STORE(&self()->waiting, 0);
}
static void lock_leave(void) { STORE(&Q30->lock, 0); }
static void active_enter(void) {
    for (;;) {
        park(3);
        STORE(&self()->active, 1);
        // Store/load ordering is necessary even under TSO (Dekker handshake).
        __asm__ volatile("dmb ish" ::: "memory");
        if (!LOAD(&Q30->gate)) {
            QLOG(QE_MARK, 6, 1, self()->request.operation);
            return;
        }
        STORE(&self()->active, 0);
    }
}
static void active_leave(void) {
    __asm__ volatile("dsb ish" ::: "memory");
    if (LOAD(&self()->active)) QLOG(QE_MARK, 6, 0, self()->request.operation);
    STORE(&self()->active, 0);
}
static u64 *pool(u64 cpu) {
    return (u64 *)(Q30_SHADOW_PA + cpu * Q30_SHADOW_PAGES * 4096);
}
static u64 *allocate_shadow(void) {
    u64 page = self()->shadow_used++;
    q30_check(page < Q30_SHADOW_PAGES, 4);
    u64 *p = pool(id()) + page * 512;
    zero(p, 4096);
    return p;
}
static u64 arm_va(u64 gva) { return (gva >> 63) ? gva : gva | (1UL << 47); }
static u64 *shadow_slot(u64 va) {
    u64 *table = pool(id()) + ((va >> 63) ? 512 : 0);
    for (unsigned shift = 39; shift > 12; shift -= 9) {
        u64 *slot = &table[(va >> shift) & 511];
        if (!*slot) {
            u64 *next = allocate_shadow();
            __asm__ volatile("dsb ishst" ::: "memory");
            *slot = (u64)next | 3;
        }
        q30_check((*slot & 3) == 3, 5);
        table = (u64 *)(*slot & 0x0000fffffffff000UL);
    }
    return &table[(va >> 12) & 511];
}
static void begin_change(void) {
    q30_check(LOAD(&Q30->lock) && !LOAD(&self()->active), 6);
    STORE(&Q30->owner, id());
    __atomic_add_fetch(&Q30->epoch, 1, __ATOMIC_RELEASE);
    self()->epochs++;
    QLOG(QE_GATE, 1, id());
    STORE(&Q30->gate, 1);
    __asm__ volatile("dmb ish" ::: "memory");
    for (u64 cpu = 0; cpu < S->ncpus; ++cpu)
        while (LOAD(&Q30->cpu[cpu].active)) __asm__ volatile("yield" ::: "memory");
    SYNC();
    q32_hook(Q32_TRACKING);
}
static void evict_shadows(void) {
    for (u64 cpu = 0; cpu < S->ncpus; ++cpu) {
        // The accepted foundation is in L0[0]; only guest namespaces are removed.
        zero(pool(cpu) + 256, 256 * 8);
        zero(pool(cpu) + 512, 4096);
    }
    __asm__ volatile("dsb ishst\ntlbi vmalle1is\ndsb ish\nisb" ::: "memory");
    for (u64 cpu = 0; cpu < S->ncpus; ++cpu) Q30->cpu[cpu].shadow_used = 2;
}
static void roles_visit(u64 table, unsigned level, u64 nxe) {
    if (table > X86_RAM_SIZE - 4096 || table & 4095) return;
    unsigned page = (unsigned)(table >> 12), visit = 1U << (level - 1 + 4 * nxe);
    roles_new[page] |= 1U << (level - 1);
    if (roles_seen[page] & visit) return;
    roles_seen[page] |= visit;
    if (level == 1) return;
    for (unsigned i = 0; i < 512; ++i) {
        u64 entry = ((u64 *)(MEMORY + table))[i];
        if (!(entry & 1) || (entry & 0x000ffff000000000UL) || (!nxe && (entry >> 63)) ||
            (entry & 128)) continue;
        roles_visit(entry & X86_PA_MASK, level - 1, nxe);
    }
}
static void rescan_roles(void) {
    zero(roles_new, sizeof(roles_new)); zero(roles_seen, sizeof(roles_seen));
    for (u64 cpu = 0; cpu < S->ncpus; ++cpu)
        roles_visit(Q30->cpu[cpu].context.cr3 & X86_PA_MASK, 4, Q30->cpu[cpu].context.nxe);
    for (u64 i = 0; i < X86_RAM_SIZE / 4096; ++i) {
        if (Q30->roles[i] != roles_new[i]) QLOG(QE_ROLE, i * 4096, Q30->roles[i], roles_new[i]);
        Q30->roles[i] = roles_new[i];
    }
}
static void end_change(int roles_changed) {
    if (roles_changed) rescan_roles();
    SYNC();
    QLOG(QE_GATE, 2, id());
    STORE(&Q30->gate, 2);
    QLOG(QE_ACK, LOAD(&Q30->epoch), 0, 0);
    self()->acknowledgements++;
    STORE(&self()->ack, LOAD(&Q30->epoch));
    for (u64 cpu = 0; cpu < S->ncpus; ++cpu)
        while (LOAD(&Q30->cpu[cpu].ack) != LOAD(&Q30->epoch)) __asm__ volatile("yield" ::: "memory");
    QLOG(QE_GATE, 0, id());
    STORE(&Q30->gate, 0);
}
static unsigned code_page(u64 page) {
    q30_check(!(page & 4095) && page < X86_RAM_SIZE, 22);
    return (unsigned)(page >> 12);
}
static void code_generation_bump(unsigned page, u64 operation) {
    q30_check(LOAD(&Q30->gate) == 1 && LOAD(&Q30->owner) == id() &&
              Q31->code_generation[page] != ~0UL, 23);
    u64 old = Q31->code_generation[page];
    STORE(&Q31->code_generation[page], old + 1);
    QLOG(QE_Q31, Q31_CODE, QC_GENERATION, page * 4096UL, old, old + 1,
         operation, old + 1, Q31->code_users[page]);
}
void q30_code_track(u64 address, int enabled) {
    unsigned page = code_page(address);
    q30_check(enabled == 0 || enabled == 1, 24);
    lock_enter(); begin_change(); evict_shadows();
    unsigned old = Q31->code_roles[page];
    q30_check(old != (unsigned)enabled && (!old || !Q31->code_users[page]), 25);
    Q31->code_roles[page] = (unsigned char)enabled;
    QLOG(QE_Q31, Q31_CODE, QC_ROLE, address, old, enabled, 0,
         Q31->code_generation[page], Q31->code_users[page]);
    if (enabled) code_generation_bump(page, 0);
    end_change(0); lock_leave();
}
u64 q31_code_generation(u64 address) {
    return LOAD(&Q31->code_generation[code_page(address)]);
}
int q30_code_pin(u64 address, u64 expected_generation) {
    unsigned page = code_page(address);
    lock_enter();
    int ok = Q31->code_roles[page] && Q31->code_generation[page] == expected_generation;
    if (ok) {
        q30_check(!code_pins[id()][page], 26);
        code_pins[id()][page] = 1;
        u64 old = Q31->code_users[page]++;
        QLOG(QE_Q31, Q31_CODE, QC_PIN, address, old, old + 1, 0,
             expected_generation, old + 1);
    }
    lock_leave();
    return ok;
}
void q30_code_unpin(u64 address) {
    unsigned page = code_page(address);
    lock_enter();
    q30_check(code_pins[id()][page] && Q31->code_users[page], 27);
    code_pins[id()][page] = 0;
    u64 old = Q31->code_users[page]--;
    QLOG(QE_Q31, Q31_CODE, QC_UNPIN, address, old, old - 1, 0,
         Q31->code_generation[page], old - 1);
    lock_leave();
}
static void mutation(u64 gpa, u64 value, unsigned width, u64 cause, u64 mask) {
    q30_check((width == 1 || width == 8) && gpa <= X86_RAM_SIZE - width &&
              (width == 1 || !(gpa & 7)), 7);
    u64 old = width == 1 ? MEMORY[gpa] : *(u64 *)(MEMORY + gpa);
    if (width == 1) { value &= 255; MEMORY[gpa] = (unsigned char)value; }
    else *(u64 *)(MEMORY + gpa) = value;
    QLOG(QE_MUTATION, gpa, old, value, width, cause, self()->request.operation, mask);
    // A page may contain both guest code and paging structures. Even an A/D
    // update changes its bytes; every tracked mutation invalidates dispatch.
    if (Q31->code_roles[gpa >> 12]) {
        code_generation_bump((unsigned)(gpa >> 12), self()->request.operation);
        if (cause == QM_GUEST || cause == QM_CODE) {
            Q31->cpu[id()].code_writes++;
            q32_hook(Q32_CODE_WRITE);
        }
    }
}
static int accessed_dirty_code(const struct x86_walk *walk, unsigned access) {
    for (u64 i = 0; i < walk->count; ++i) {
        u64 mask = 32 | ((access == X86_WRITE && i + 1 == walk->count) ? 64 : 0);
        u64 address = walk->entry_gpa[i];
        if (Q31->code_roles[address >> 12] && (*(u64 *)(MEMORY + address) & mask) != mask)
            return 1;
    }
    return 0;
}
static void accessed_dirty(struct x86_walk *walk, unsigned access) {
    for (u64 i = 0; i < walk->count; ++i) {
        u64 mask = 32 | ((access == X86_WRITE && i + 1 == walk->count) ? 64 : 0);
        u64 address = walk->entry_gpa[i], old = *(u64 *)(MEMORY + address);
        if ((old & mask) != mask) mutation(address, old | mask, 8, QM_AD, mask);
        walk->entry_value[i] = old | mask;
    }
}
static unsigned access_kind(u64 kind) {
    return kind == Q_EXEC || kind == Q_FETCH8 ? X86_EXECUTE : kind == Q_LOAD ? X86_READ : X86_WRITE;
}
static void decode(u64 gva, unsigned access, struct x86_walk *walk) {
    x86_walk(MEMORY, &self()->context, gva, access, walk);
    q30_check(walk->status < 256, 0x100 + walk->status);
}
static void install(u64 gva, struct x86_walk *walk) {
    u64 *slot = shadow_slot(arm_va(gva));
    int writable = ((walk->rights & X86_RIGHT_WRITE) ||
                    (!self()->context.cpl && !self()->context.wp)) &&
                   (walk->entry_value[walk->count - 1] & 64) &&
                   !Q30->roles[walk->gpa >> 12] && !Q31->code_roles[walk->gpa >> 12];
    u64 descriptor = (X86_RAM_IPA + (walk->gpa & ~4095UL)) | 0xf03 | (1UL << 54);
    if (!writable) descriptor |= 128;
    if (!(walk->rights & X86_RIGHT_EXECUTE)) descriptor |= 1UL << 53;
    q30_check(!*slot, 8); // Valid-entry replacement requires the global break/flush path.
    *slot = descriptor;
    __asm__ volatile("dsb ishst\nisb" ::: "memory");
    QLOG(QE_MAP, gva & ~4095UL, walk->gpa & ~4095UL, (u64)slot, descriptor,
         walk->page_size, walk->rights, writable, Q30->roles[walk->gpa >> 12]);
}
void q30_barrier(void) {
    u64 generation = LOAD(&Q30->barrier_generation);
    if (__atomic_add_fetch(&Q30->barrier_arrived, 1, __ATOMIC_ACQ_REL) == S->ncpus) {
        STORE(&Q30->barrier_arrived, 0);
        STORE(&Q30->barrier_generation, generation + 1);
    } else while (LOAD(&Q30->barrier_generation) == generation) {
        park(4);
        __asm__ volatile("yield" ::: "memory");
    }
    park(4);
}
void q30_context(struct x86_context context, u64 reason) {
    q30_check(!(context.cr3 & ~X86_PA_MASK) && context.cr3 < X86_RAM_SIZE &&
              (context.cpl == 0 || context.cpl == 3) && context.wp <= 1 &&
              context.nxe <= 1 && context.pge <= 1, 9);
    lock_enter(); begin_change(); evict_shadows();
    self()->context = context;
    QLOG(QE_CONTEXT, context.cr3, context.cpl, context.wp, context.nxe, context.pge, reason);
    end_change(1); lock_leave();
}
void q30_invalidate(u64 address, u64 reason) {
    lock_enter(); begin_change(); evict_shadows();
    QLOG(QE_MARK, 1, address, reason);
    end_change(1); lock_leave();
}
void q30_setup(u64 gpa, u64 value, unsigned width) {
    // Fixture setup uses the same quiescence/eviction boundary, but is explicitly
    // tagged; it cannot count as an intercepted translated guest table write.
    lock_enter(); begin_change(); evict_shadows();
    mutation(gpa, value, width, QM_SETUP, 0);
    end_change(1); lock_leave();
}
void q30_setup_batch(const struct q30_patch *patches, u64 count) {
    q30_check(count <= 256, 20);
    lock_enter(); begin_change(); evict_shadows();
    for (u64 i = 0; i < count; ++i) mutation(patches[i].gpa, patches[i].value, 8, QM_SETUP, 0);
    end_change(1); lock_leave();
}
static u64 resume_pc(u64 kind, u64 width) {
    if (kind == Q_LOAD || kind == Q_FETCH8)
        return width == 1 ? (u64)q30_load8_return : (u64)q30_load_return;
    if (kind == Q_EXEC) return (u64)q30_exec_return;
    if (kind == Q_CMPXCHG) return (u64)q30_cas_return;
    return width == 1 ? (u64)q30_byte_return : (u64)q30_store_return;
}
int q30_exception(u64 *frame, u64 esr, u64 far) {
    struct q30_cpu *c = self();
    struct q30_request *r = &c->request;
    if (!c->started || !r->inflight) return 0;
    u64 pc = frame[32], ec = esr >> 26;
    int known = ((r->kind == Q_LOAD || r->kind == Q_FETCH8) &&
                 pc == (r->width == 1 ? (u64)q30_load8_site : (u64)q30_load_site)) ||
                (r->kind == Q_STORE && pc == (r->width == 1 ? (u64)q30_byte_site : (u64)q30_store_site)) ||
                (r->kind == Q_CMPXCHG && (pc == (u64)q30_cas_load || pc == (u64)q30_cas_store)) ||
                (r->kind == Q_EXEC && pc == arm_va(r->gva));
    if (!known || ec != (r->kind == Q_EXEC ? 0x21UL : 0x25UL)) return 0;
    if (ec == 0x25) q30_check(((esr >> 6) & 1) ==
                              (r->kind == Q_STORE || pc == (u64)q30_cas_store), 28);
    q30_check(((esr & 63) >= 4 && (esr & 63) <= 7) ||
              ((esr & 63) >= 12 && (esr & 63) <= 15), 17);
    q30_check(far >= arm_va(r->gva) && far - arm_va(r->gva) < r->width, 10);
    if (Q31->phase == 1 || Q32->phase == 1 || Q33->phase == 1 || Q34->phase == 1)
        q30_check(q31_validate_fault(id(), pc), 29);
    c->faults++; r->faults++;
    QLOG(QE_FAULT, r->operation, pc, far, esr);
    active_leave();
    lock_enter();
    q32_hook(Q32_REPAIR);
    struct x86_walk walk;
    unsigned access = access_kind(r->kind);
    decode(r->gva, access, &walk);
    if (walk.status) {
        r->status = walk.status; r->error = walk.error; c->guest_faults++;
        frame[32] = resume_pc(r->kind, r->width);
        lock_leave();
        return 1;
    }
    r->gpa = walk.gpa;
    int table_tracked = Q30->roles[walk.gpa >> 12] != 0;
    int tracked = access == X86_WRITE && (table_tracked || Q31->code_roles[walk.gpa >> 12]);
    u64 *slot = shadow_slot(arm_va(r->gva));
    if (tracked || *slot || accessed_dirty_code(&walk, access)) {
        begin_change();
        // Peers are now quiescent; the writer lock still owns the sole decision.
        decode(r->gva, access, &walk);
        q30_check(!walk.status, 11);
        evict_shadows();
        accessed_dirty(&walk, access);
        if (tracked) {
            q30_check((r->width == 1 || !(walk.gpa & 7)) &&
                      (walk.gpa & 4095) + r->width <= 4096, 12);
            u64 old = r->width == 1 ? MEMORY[walk.gpa] : *(u64 *)(MEMORY + walk.gpa);
            u64 value = r->kind == Q_CMPXCHG && old != r->compare ? old : r->value;
            mutation(walk.gpa, value, (unsigned)r->width, table_tracked ? QM_GUEST : QM_CODE, 0);
            if (table_tracked) c->table_traps++;
            if (r->kind == Q_CMPXCHG) frame[3] = old;
            frame[32] = resume_pc(r->kind, r->width);
            end_change(table_tracked);
        } else {
            install(r->gva, &walk);
            end_change(0);
        }
    } else {
        accessed_dirty(&walk, access);
        install(r->gva, &walk);
    }
    // Lock ownership excludes a new writer while restoring the active handshake.
    STORE(&c->active, 1);
    __asm__ volatile("dmb ish" ::: "memory");
    QLOG(QE_MARK, 6, 1, r->operation);
    lock_leave();
    return 1;
}
u64 q30_access(u64 gva, unsigned kind, unsigned width, u64 value, u64 compare) {
    struct q30_cpu *c = self();
    struct q30_request *r = &c->request;
    q30_check(!r->inflight && kind <= Q_FETCH8 &&
              (width == 8 || ((kind == Q_LOAD || kind == Q_STORE || kind == Q_FETCH8) && width == 1)) &&
              (kind != Q_FETCH8 || width == 1), 13);
    q30_check(kind != Q_CMPXCHG || !(gva & 7), 18);
    r->gva = gva; r->kind = kind; r->width = width; r->value = value; r->compare = compare;
    r->status = r->error = r->faults = 0; r->gpa = ~0UL; r->inflight = 1;
    fault_address[id()] = gva;
    if (kind == Q_CMPXCHG) r->rax = compare;
    r->operation = QLOG(QE_BEGIN, gva, kind, width, value, compare, r->rax, r->flags, Q30->case_id);
    u64 result = 0;
    if (gva > ~0UL - (width - 1) || !x86_canonical(gva) || !x86_canonical(gva + width - 1)) {
        r->status = X86_GP; c->guest_faults++;
    } else {
        // Cross-page scalar accesses are prevalidated/pinned as one native
        // operation. No partial store is claimed on a genuine guest denial.
        if ((gva & 4095) + width > 4096) {
            lock_enter();
            struct x86_walk first, last;
            unsigned access = access_kind(kind);
            decode(gva, access, &first); decode(gva + width - 1, access, &last);
            struct x86_walk *bad = first.status ? &first : last.status ? &last : 0;
            if (bad) {
                r->status = bad->status; r->error = bad->error; c->guest_faults++;
                fault_address[id()] = first.status ? gva : (gva + width - 1) & ~4095UL;
            }
            else {
                q30_check(!(access == X86_WRITE && (Q30->roles[first.gpa >> 12] || Q30->roles[last.gpa >> 12] ||
                           Q31->code_roles[first.gpa >> 12] || Q31->code_roles[last.gpa >> 12])), 14);
                begin_change(); evict_shadows();
                accessed_dirty(&first, access); accessed_dirty(&last, access);
                install(gva, &first); install(gva + width - 1, &last);
                end_change(0);
                STORE(&c->active, 1);
                __asm__ volatile("dmb ish" ::: "memory");
                QLOG(QE_MARK, 6, 1, r->operation);
            }
            lock_leave();
        } else if (kind == Q_FETCH8) {
            // Arm reads the guest opcode byte as data. Check x86 execute rights
            // even when a readable shadow is cached, then pin that decision
            // through the native byte load under the same writer protocol.
            lock_enter();
            struct x86_walk walk;
            decode(gva, X86_EXECUTE, &walk);
            if (walk.status) { r->status = walk.status; r->error = walk.error; c->guest_faults++; }
            else {
                // Cached readable mappings may avoid the native fault handler;
                // retain the execute walk's physical byte address on this path too.
                r->gpa = walk.gpa;
                STORE(&c->active, 1);
                __asm__ volatile("dmb ish" ::: "memory");
                QLOG(QE_MARK, 6, 1, r->operation);
            }
            lock_leave();
        } else active_enter();
        if (!r->status) {
            u64 address = arm_va(gva);
            if (kind == Q_LOAD || kind == Q_FETCH8) result = width == 1 ? q30_load8(address) : q30_load64(address);
            else if (kind == Q_STORE) result = width == 1 ? q30_store8(address, value) : q30_store64(address, value);
            else if (kind == Q_CMPXCHG) result = q30_cas(address, value, compare);
            else result = q30_exec(address);
            if (r->status) result = 0;
            if (!r->status && kind == Q_CMPXCHG) {
                r->flags = x86_cmp_flags(compare, result, r->flags);
                if (compare != result) r->rax = result;
            }
        }
    }
    QLOG(QE_END, r->operation, result, r->status, r->error, r->gpa, r->rax, r->flags, r->faults);
    c->operations++;
    active_leave(); r->inflight = 0;
    return result;
}
void q30_bootstrap(u64 shim_l1) {
    u64 cpu = id();
    q30_check(Q30->magic == Q30_MAGIC && Q30->version == Q30_VERSION && Q30->ncpus == S->ncpus, 15);
    struct q30_cpu *c = &Q30->cpu[cpu];
    zero(pool(cpu), Q30_SHADOW_PAGES * 4096);
    pool(cpu)[0] = shim_l1 | 3; c->shadow_used = 2;
    c->context = (struct x86_context){0x200000, 0, 1, 1, 0};
    c->request.rax = 0; c->request.flags = 0x202;
    // Two 48-bit ranges, 4KiB TG0/TG1, inner-shareable WB walks, 36-bit PA.
    u64 tcr = 16UL | (1UL << 8) | (1UL << 10) | (3UL << 12) | (16UL << 16) |
              (1UL << 24) | (1UL << 26) | (3UL << 28) | (2UL << 30) | (1UL << 32);
    WRITE(ttbr0_el1, (u64)pool(cpu) | ((cpu + 1) << 48));
    WRITE(ttbr1_el1, (u64)(pool(cpu) + 512));
    WRITE(tcr_el1, tcr);
}
void q30_run(u64 cpu, u64 shim_l1) {
    struct q30_cpu *c = &Q30->cpu[cpu];
    q30_check(pool(cpu)[0] == (shim_l1 | 3), 19);
    u64 saved_daif = READ(daif);
    __asm__ volatile("msr daifset, #2" ::: "memory");
    volatile unsigned char *destination = (volatile unsigned char *)&c->foundation;
    const volatile unsigned char *source = (const volatile unsigned char *)&S->cpu[cpu];
    for (u64 i = 0; i < sizeof(c->foundation); ++i) destination[i] = source[i];
    c->timer_start = S->cpu[cpu].timer_count;
    WRITE(daif, saved_daif);
    q30_check(c->foundation.trace_count <= 32, 21);
    STORE(&c->started, 1);
    q30_barrier();
    // Immutable fixture code only. This does not test Q33 host publication.
    if (cpu == 0) {
        u64 stride = 4UL << ((READ(ctr_el0) >> 16) & 15);
        for (u64 p = X86_RAM_IPA + 0x4000; p < X86_RAM_IPA + 0x6000; p += stride)
            __asm__ volatile("dc cvau, %0" :: "r"(p) : "memory");
        __asm__ volatile("dsb ish" ::: "memory");
        QLOG(QE_MARK, 2, X86_RAM_IPA + 0x4000, 8192, stride);
    }
    q30_barrier();
    __asm__ volatile("ic ialluis\ndsb ish\nisb" ::: "memory");
    QLOG(QE_MARK, 3);
    q30_barrier();
    if (cpu == 0) { lock_enter(); begin_change(); evict_shadows(); end_change(1); lock_leave(); }
    q30_barrier();
    q30_cases(cpu);
    while (S->cpu[cpu].timer_count <= c->timer_start) {
        park(1);
        __asm__ volatile("yield" ::: "memory");
    }
    q30_barrier();
    c->timer_end = S->cpu[cpu].timer_count;
    c->final_tcr = READ(tcr_el1); c->final_ttbr0 = READ(ttbr0_el1); c->final_ttbr1 = READ(ttbr1_el1);
    q30_check(c->timer_end > c->timer_start && (READ(actlr_el1) & 2), 16);
    STORE(&c->done, 1);
    q30_barrier();
}
