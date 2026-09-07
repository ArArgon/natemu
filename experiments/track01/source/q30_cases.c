#include "q30.h"
#define VA 0x123456789000UL
#define HIGH 0xffff923456789000UL
#define ALIAS 0x234560000000UL
#define ALT (ALIAS + 0x800000)
#define BIG 0x345600000000UL
#define ROOT0 0x200000UL
#define ROOT1 0x220000UL
#define SPARE 0x240000UL
#define A 0x10000UL
#define B 0x14000UL
#define NX (1UL << 63)
#define PRESENT 7UL
#define SEED(p) (0x5a00000000000000UL | (p))
#ifdef Q30_FIXTURE_CHECK
extern unsigned char *q30_fixture_memory;
extern u64 q30_fixture_cpu(void);
#define MEMORY q30_fixture_memory
#define CPU() q30_fixture_cpu()
#else
#define MEMORY ((unsigned char *)X86_RAM_IPA)
static u64 fixture_cpu(void) { u64 n; __asm__ volatile("mrs %0, tpidr_el1" : "=r"(n)); return n; }
#define CPU() fixture_cpu()
#endif
static struct q30_patch patches[256];
static u64 patch_count, next_page[2], path[2][4];
static u64 raw(u64 gpa) { return *(volatile u64 *)(MEMORY + gpa); }
static u64 pending(u64 gpa) {
    for (u64 i = patch_count; i; --i) if (patches[i - 1].gpa == gpa) return patches[i - 1].value;
    return raw(gpa);
}
static void patch(u64 gpa, u64 value) {
    for (u64 i = 0; i < patch_count; ++i) if (patches[i].gpa == gpa) {
        patches[i].value = value; return;
    }
    q30_check(patch_count < 256, 100);
    patches[patch_count++] = (struct q30_patch){gpa, value};
}
static void map(unsigned root, u64 va, u64 gpa, int large, u64 *locations) {
    u64 table = root ? ROOT1 : ROOT0;
    for (unsigned level = 4; level; --level) {
        u64 slot = table + ((va >> (12 + 9 * (level - 1))) & 511) * 8;
        if (locations) locations[4 - level] = slot;
        if (level == 1 || (large && level == 2)) { patch(slot, gpa | PRESENT | (large ? 128 : 0)); return; }
        u64 entry = pending(slot);
        if (!(entry & 1)) {
            u64 page = next_page[root]; next_page[root] += 4096;
            q30_check(next_page[root] <= (root ? SPARE : ROOT1), 101);
            entry = page | PRESENT; patch(slot, entry);
        }
        q30_check(!(entry & 128), 102);
        table = entry & X86_PA_MASK;
    }
}
static void context(u64 root, u64 cpl, u64 wp, u64 nxe, u64 pge) {
    q30_context((struct x86_context){root, cpl, wp, nxe, pge}, Q30->case_id);
}
static void reset(void) {
    q30_barrier();
    if (CPU() == 0) {
        patch_count = 0; next_page[0] = ROOT0 + 4096; next_page[1] = ROOT1 + 4096;
        for (u64 p = ROOT0; p < 0x250000; p += 8) if (raw(p)) patch(p, 0);
        for (unsigned root = 0; root < 2; ++root) {
            for (u64 i = 0; i < MAX_CPUS; ++i)
                map(root, VA + i * 4096, (root ? B : A) + i * 4096, 0, i ? 0 : path[root]);
            map(root, HIGH, root ? A : B, 0, 0);
            map(root, ALIAS, 0, 1, 0); map(root, ALIAS + 0x200000, 0x200000, 1, 0);
            map(root, ALT, SPARE, 0, 0); map(root, BIG, 0, 1, 0);
            // Actual native accesses at both edges of each x86 canonical range.
            map(root, 0, A, 0, 0); map(root, 0x7ffffffff000UL, B, 0, 0);
            map(root, 0xffff800000000000UL, A, 0, 0); map(root, 0xfffffffffffff000UL, B, 0, 0);
        }
        for (u64 p = A; p < B + MAX_CPUS * 4096; p += 4096) {
            patch(p, SEED(p)); patch(p + 4088, SEED(p + 4088));
        }
        patch(0, SEED(0)); patch(0x1ff000, SEED(0x1ff000));
        patch(0x1ffff8, SEED(0x1ffff8));
        q30_setup_batch(patches, patch_count);
        QLOG(QE_MARK, 4, patch_count, next_page[0], next_page[1]);
    }
    q30_barrier();
    context(ROOT0, 0, 1, 1, 0);
    q30_barrier();
}
static void begin(unsigned group, unsigned variant, unsigned rep) {
    q30_barrier();
    if (CPU() == 0) {
        Q30->case_id = ((u64)group << 32) | ((u64)rep << 16) | variant;
        QLOG(QE_CASE, group, variant, rep, 1);
    }
    q30_barrier();
}
static void end(unsigned group, unsigned variant, unsigned rep) {
    q30_barrier();
    if (CPU() == 0) QLOG(QE_CASE, group, variant, rep, 0);
    q30_barrier();
}
static u64 access(u64 va, unsigned kind, unsigned width, u64 value, u64 compare,
                  u64 status, u64 error) {
    u64 result = q30_access(va, kind, width, value, compare);
    struct q30_request *r = &Q30->cpu[CPU()].request;
    q30_check(r->status == status && r->error == error, 103);
    return result;
}
static void read_value(u64 va, u64 value) {
    q30_check(access(va, Q_LOAD, 8, 0, 0, 0, 0) == value, 104);
}
static void write_entry(u64 gpa, u64 value) {
    access(ALIAS + gpa, Q_STORE, 8, value, 0, 0, 0);
}
static void leader_entry(u64 gpa, u64 value) {
    q30_barrier(); if (CPU() == 0) write_entry(gpa, value); q30_barrier();
}
static u64 locate(u64 va, unsigned level) {
    u64 table = ROOT0;
    for (unsigned at = 4; at; --at) {
        u64 slot = table + ((va >> (12 + 9 * (at - 1))) & 511) * 8;
        if (at == level) return slot;
        table = raw(slot) & X86_PA_MASK;
    }
    q30_check(0, 105); return 0;
}
static void setup_one(u64 gpa, u64 value) {
    q30_barrier(); if (CPU() == 0) q30_setup(gpa, value, 8); q30_barrier();
}

void q30_cases(u64 id) {
    for (unsigned group = 1; group <= 12; ++group) {
        for (unsigned rep = 0; rep < 16; ++rep) {
            reset();
            u64 leaf = path[0][3], parent = path[0][2];
            if (group == 1) {
                begin(group, 0, rep);
                read_value(VA, SEED(A)); read_value(HIGH, SEED(B));
                read_value(0, SEED(A)); read_value(0x7ffffffff000UL, SEED(B));
                read_value(0xffff800000000000UL, SEED(A)); read_value(0xfffffffffffff000UL, SEED(B));
                read_value(0x7ffffffffff8UL, SEED(B + 4088));
                read_value(0xfffffffffffffff8UL, SEED(B + 4088));
                q30_barrier();
                access(VA + id * 4096, Q_STORE, 8, 0xabc000 + id, 0, 0, 0);
                read_value(ALIAS + A + id * 4096, 0xabc000 + id);
                access(0x800000000000UL, Q_LOAD, 8, 0, 0, X86_GP, 0);
                access(0xffff7ffffffff000UL, Q_LOAD, 8, 0, 0, X86_GP, 0);
                access(0x7ffffffffffcUL, Q_LOAD, 8, 0, 0, X86_GP, 0);
                end(group, 0, rep);
                for (unsigned level = 0; level < 4; ++level) {
                    begin(group, level + 1, rep);
                    u64 saved = raw(path[0][level]);
                    leader_entry(path[0][level], saved & ~1UL);
                    access(VA, Q_LOAD, 8, 0, 0, X86_PF, 0);
                    leader_entry(path[0][level], saved | (1UL << 36));
                    access(VA, Q_LOAD, 8, 0, 0, X86_PF, 9);
                    leader_entry(path[0][level], saved);
                    end(group, level + 1, rep);
                }
            } else if (group == 2) {
                for (unsigned level = 0; level < 4; ++level)
                    for (unsigned mode = 0; mode < 4; ++mode) {
                        unsigned variant = level * 4 + mode;
                        begin(group, variant, rep);
                        u64 cpl = mode / 2 ? 3 : 0, wp = mode & 1;
                        u64 saved = raw(path[0][level]);
                        leader_entry(path[0][level], saved & ~2UL);
                        context(ROOT0, cpl, wp, 1, 0);
                        read_value(VA, SEED(A));
                        int denied = cpl || wp;
                        access(VA, Q_STORE, 8, SEED(A), 0, denied ? X86_PF : 0, denied ? 3 + (cpl ? 4 : 0) : 0);
                        // U/S test is independent of the R/W restriction.
                        context(ROOT0, 0, 1, 1, 0);
                        leader_entry(path[0][level], saved & ~4UL);
                        context(ROOT0, cpl, wp, 1, 0);
                        access(VA, Q_LOAD, 8, 0, 0, cpl ? X86_PF : 0, cpl ? 5 : 0);
                        context(ROOT0, 0, 1, 1, 0);
                        leader_entry(path[0][level], saved);
                        end(group, variant, rep);
                    }
                // Fetch from both namespaces, with NX at every walk level.
                for (unsigned level = 0; level < 4; ++level) {
                    begin(group, 16 + level, rep);
                    leader_entry(leaf, 0x4000 | PRESENT);
                    u64 saved = raw(path[0][level]);
                    q30_check(access(VA, Q_EXEC, 8, 0, 0, 0, 0) == 42, 106);
                    leader_entry(path[0][level], saved | NX);
                    access(VA, Q_EXEC, 8, 0, 0, X86_PF, 17);
                    context(ROOT0, 0, 1, 0, 0);
                    access(VA, Q_LOAD, 8, 0, 0, X86_PF, 9);
                    context(ROOT0, 0, 1, 1, 0);
                    leader_entry(path[0][level], saved);
                    leader_entry(leaf, A | PRESENT);
                    end(group, 16 + level, rep);
                }
                begin(group, 20, rep);
                leader_entry(locate(HIGH, 1), 0x5000 | PRESENT);
                q30_check(access(HIGH, Q_EXEC, 8, 0, 0, 0, 0) == 43, 107);
                end(group, 20, rep);
                begin(group, 21, rep);
                const u64 edges[] = {0, 0x7ffffffffff8UL, 0xffff800000000000UL, 0xfffffffffffffff8UL};
                for (unsigned edge = 0; edge < 4; ++edge) {
                    leader_entry(locate(edges[edge], 1), 0x4000 | PRESENT);
                    q30_check(access(edges[edge], Q_EXEC, 8, 0, 0, 0, 0) == 42, 129);
                }
                end(group, 21, rep);
            } else if (group == 3) {
                for (unsigned level = 0; level < 4; ++level) {
                    begin(group, level, rep);
                    u64 saved = raw(path[0][level]);
                    leader_entry(path[0][level], saved & ~1UL);
                    access(VA, Q_LOAD, 8, 0, 0, X86_PF, 0);
                    leader_entry(path[0][level], saved);
                    read_value(VA, SEED(A)); read_value(VA, SEED(A));
                    leader_entry(path[0][level], saved & ~2UL);
                    access(VA, Q_STORE, 8, SEED(A), 0, X86_PF, 3);
                    leader_entry(path[0][level], saved);
                    access(VA, Q_STORE, 8, SEED(A), 0, 0, 0);
                    leader_entry(path[0][level], saved & ~4UL);
                    context(ROOT0, 3, 1, 1, 0);
                    access(VA, Q_LOAD, 8, 0, 0, X86_PF, 5);
                    // The user-writable alias is outside the restricted PML4 path.
                    leader_entry(path[0][level], saved);
                    read_value(VA, SEED(A));
                    context(ROOT0, 0, 1, 1, 0);
                    leader_entry(leaf, 0x4000 | PRESENT);
                    saved = raw(path[0][level]);
                    leader_entry(path[0][level], saved | NX);
                    access(VA, Q_EXEC, 8, 0, 0, X86_PF, 17);
                    leader_entry(path[0][level], saved);
                    q30_check(access(VA, Q_EXEC, 8, 0, 0, 0, 0) == 42, 108);
                    leader_entry(leaf, A | PRESENT);
                    end(group, level, rep);
                }
            } else if (group == 4 || group == 12) {
                begin(group, 0, rep);
                read_value(VA, SEED(A));
                leader_entry(leaf, B | PRESENT);
                // Eager maintenance is stronger than the architectural pre-INVLPG allowed set.
                read_value(VA, SEED(B));
                q30_invalidate(VA, 1); q30_barrier(); read_value(VA, SEED(B));
                leader_entry(leaf, B | 5); q30_invalidate(VA, 2); q30_barrier();
                access(VA, Q_STORE, 8, 123, 0, X86_PF, 3);
                leader_entry(leaf, 0); q30_invalidate(VA, 3); q30_barrier();
                access(VA, Q_LOAD, 8, 0, 0, X86_PF, 0);
                end(group, 0, rep);
                begin(group, 1, rep);
                setup_one(SPARE + ((VA >> 12) & 511) * 8, B | PRESENT);
                leader_entry(parent, SPARE | PRESENT);
                q30_invalidate(VA, 4); q30_barrier(); read_value(VA, SEED(B));
                end(group, 1, rep);
                begin(group, 2, rep);
                u64 current_leaf = locate(VA, 1);
                leader_entry(current_leaf, 0x4000 | PRESENT);
                q30_check(access(VA, Q_EXEC, 8, 0, 0, 0, 0) == 42, 130);
                leader_entry(current_leaf, 0x5000 | PRESENT);
                q30_invalidate(VA, 14); q30_barrier();
                q30_check(access(VA, Q_EXEC, 8, 0, 0, 0, 0) == 43, 131);
                leader_entry(current_leaf, 0x5000 | PRESENT | NX);
                q30_invalidate(VA, 15); q30_barrier();
                access(VA, Q_EXEC, 8, 0, 0, X86_PF, 17);
                end(group, 2, rep);
            } else if (group == 5) {
                begin(group, 0, rep);
                u64 old_pt = raw(parent) & X86_PA_MASK;
                // Clear every sibling in this PT so no successful old leaf exists.
                for (u64 i = 0; i < MAX_CPUS; ++i) leader_entry(leaf + i * 8, 0);
                access(VA, Q_LOAD, 8, 0, 0, X86_PF, 0);
                leader_entry(parent, SPARE | PRESENT);
                q30_invalidate(VA, 5); q30_barrier();
                leader_entry(SPARE + ((VA >> 12) & 511) * 8, B | PRESENT);
                read_value(VA, SEED(B));
                QLOG(QE_MARK, 5, old_pt, SPARE);
                end(group, 0, rep);
            } else if (group == 6) {
                for (unsigned explicit_invalidate = 0; explicit_invalidate < 2; ++explicit_invalidate) {
                    begin(group, explicit_invalidate, rep);
                    for (unsigned level = 0; level < 4; ++level)
                        leader_entry(path[0][level], raw(path[0][level]) & ~96UL);
                    if (explicit_invalidate) q30_invalidate(VA, 6);
                    read_value(VA, SEED(A)); q30_barrier();
                    for (unsigned level = 0; level < 4; ++level) q30_check(raw(path[0][level]) & 32, 109);
                    q30_check(!(raw(leaf) & 64), 110);
                    q30_barrier();
                    access(VA, Q_STORE, 8, SEED(A), 0, 0, 0); q30_barrier();
                    q30_check(raw(leaf) & 64, 111);
                    end(group, explicit_invalidate, rep);
                }
                begin(group, 2, rep);
                leader_entry(leaf, A | 5); context(ROOT0, 0, 0, 1, 0);
                access(VA, Q_STORE, 8, SEED(A), 0, 0, 0); q30_barrier();
                q30_check(raw(leaf) & 64, 112);
                context(ROOT0, 0, 1, 1, 0);
                end(group, 2, rep);
            } else if (group == 7) {
                begin(group, 0, rep);
                // Warm two guest-writable aliases before this physical page has a table role.
                access(ALIAS + SPARE, Q_STORE, 8, 0, 0, 0, 0);
                access(ALT, Q_STORE, 8, 0, 0, 0, 0);
                setup_one(SPARE + ((VA >> 12) & 511) * 8, A | PRESENT);
                leader_entry(parent, SPARE | PRESENT);
                u64 target = SPARE + ((VA >> 12) & 511) * 8;
                for (u64 alias = 0; alias < 2; ++alias) {
                    // One writer per round, with all peers performing actual native walks.
                    for (u64 writer = 0; writer < Q30->ncpus; ++writer) {
                        // Reserve peer work for each writer operation, including both atomics.
                        // Observed overlap is still required; this rendezvous does not prove it.
                        for (unsigned stage = 0; stage < 4; ++stage) {
                            q30_barrier();
                            if (id == writer) {
                                u64 address = (alias ? ALT : ALIAS + SPARE) + (target - SPARE);
                                if (stage == 0) access(address, Q_STORE, 8, B | PRESENT | 96, 0, 0, 0);
                                else if (stage == 1) access(address, Q_STORE, 1, PRESENT | 96, 0, 0, 0);
                                else {
                                    u64 old = raw(target);
                                    if (stage == 2) {
                                        q30_check(access(address, Q_CMPXCHG, 8, A | PRESENT | 96, old, 0, 0) == old, 113);
                                        q30_check(Q30->cpu[id].request.rax == old && (Q30->cpu[id].request.flags & 64), 114);
                                    } else {
                                        q30_check(access(address, Q_CMPXCHG, 8, B | PRESENT, ~old, 0, 0) == old, 115);
                                        q30_check(Q30->cpu[id].request.rax == old && !(Q30->cpu[id].request.flags & 64), 116);
                                    }
                                }
                            } else {
                                for (unsigned walk = 0; walk < 4; ++walk) {
                                    u64 value = access(VA, Q_LOAD, 8, 0, 0, 0, 0);
                                    q30_check(value == SEED(A) || value == SEED(B), 133);
                                }
                            }
                        }
                        q30_barrier(); read_value(VA, SEED(A));
                    }
                }
                end(group, 0, rep);
                begin(group, 1, rep);
                // Mapping D is separate from the targeted table entry; mismatch is still a write.
                u64 alias_leaf = locate(ALT, 1);
                leader_entry(alias_leaf, SPARE | PRESENT);
                q30_barrier();
                if (id == 0) {
                    u64 old = raw(target);
                    access(ALT + target - SPARE, Q_CMPXCHG, 8, 123, ~old, 0, 0);
                    q30_check(raw(alias_leaf) & 64, 117);
                }
                q30_barrier(); leader_entry(alias_leaf, SPARE | 5);
                u64 flags = Q30->cpu[id].request.flags;
                access(ALT + target - SPARE, Q_CMPXCHG, 8, 123, 0, X86_PF, 3);
                q30_check(Q30->cpu[id].request.rax == 0 && Q30->cpu[id].request.flags == flags, 118);
                end(group, 1, rep);
            } else if (group == 8) {
                for (unsigned variant = 0; variant < 5; ++variant) {
                    begin(group, variant, rep);
                    u64 entry = leaf + (variant == 1 ? id * 8 : 0);
                    u64 address = VA + (variant == 1 ? id * 4096 : 0);
                    u64 first = A, second = B;
                    if (variant == 2) {
                        setup_one(SPARE + ((VA >> 12) & 511) * 8, A | PRESENT);
                        setup_one(SPARE + 4096 + ((VA >> 12) & 511) * 8, B | PRESENT);
                        entry = parent; first = SPARE; second = SPARE + 4096;
                    } else if (variant == 3) {
                        entry = path[0][0];
                        first = raw(path[0][0]) & X86_PA_MASK;
                        second = raw(path[1][0]) & X86_PA_MASK;
                    } else if (variant == 4) {
                        // Restore the original root path after the root-swap case.
                        setup_one(path[0][0], (ROOT0 + 4096) | PRESENT);
                        setup_one(parent, (leaf & ~4095UL) | PRESENT);
                        first = second = A;
                    }
                    QLOG(QE_MARK, 8, 1, variant, q30_timer_count());
                    q30_barrier();
                    // ponytail: global writer lock bounds this finite mechanism;
                    // throughput and finer locks belong to architecture/performance.
                    // No fixture barriers occur inside these competing operation loops.
                    for (unsigned i = 0; i < 32; ++i) {
                        u64 flags = variant == 4 && ((i + id) & 1) ? 5 : PRESENT;
                        write_entry(entry, ((i + id) & 1 ? second : first) | flags | 0x400);
                        u64 value = access(address, Q_LOAD, 8, 0, 0, 0, 0);
                        q30_check(value == SEED(A) || value == SEED(B), 119);
                        u64 table_data = access(ALIAS + entry, Q_LOAD, 8, 0, 0, 0, 0) & ~(96UL | (variant == 4 ? 2UL : 0));
                        u64 expected_flags = variant == 4 ? 5 : PRESENT;
                        q30_check(table_data == (first | expected_flags | 0x400) ||
                                  table_data == (second | expected_flags | 0x400), 132);
                        if (variant == 4) {
                            q30_access(address, Q_STORE, 8, SEED(A), 0);
                            struct q30_request *r = &Q30->cpu[id].request;
                            q30_check((r->status == 0 && r->error == 0) ||
                                      (r->status == X86_PF && r->error == 3), 134);
                        }
                        if (i & 1) access(ALIAS + entry, Q_CMPXCHG, 8, second | PRESENT | 0x400,
                                          first | PRESENT | 0x400, 0, 0);
                    }
                    QLOG(QE_MARK, 8, 0, variant, q30_timer_count());
                    end(group, variant, rep);
                }
            } else if (group == 9) {
                for (unsigned pge = 0; pge < 2; ++pge) {
                    begin(group, pge, rep);
                    leader_entry(leaf, A | PRESENT | 256);
                    leader_entry(path[1][3], B | PRESENT | 256);
                    context(id & 1 ? ROOT1 : ROOT0, 0, 1, 1, pge);
                    read_value(VA, SEED(id & 1 ? B : A));
                    context(id & 1 ? ROOT1 : ROOT0, 0, 1, 1, pge); // Same-CR3 reload.
                    read_value(VA, SEED(id & 1 ? B : A));
                    context(id & 1 ? ROOT0 : ROOT1, 0, 1, 1, pge);
                    read_value(VA, SEED(id & 1 ? A : B));
                    context(ROOT0, 0, 1, 1, 0);
                    end(group, pge, rep);
                }
            } else if (group == 10) {
                begin(group, 0, rep);
                read_value(BIG, SEED(0)); read_value(BIG + 0x1ff000, SEED(0x1ff000));
                q30_barrier();
                access(BIG, Q_STORE, 8, 0x1111222233334444UL, 0, 0, 0);
                access(BIG + 0x1ff000, Q_STORE, 8, 0x5555666677778888UL, 0, 0, 0);
                read_value(BIG, 0x1111222233334444UL);
                read_value(BIG + 0x1ff000, 0x5555666677778888UL);
                q30_barrier();
                access(BIG, Q_STORE, 8, SEED(0), 0, 0, 0);
                access(BIG + 0x1ff000, Q_STORE, 8, SEED(0x1ff000), 0, 0, 0);
                q30_barrier();
                access(BIG + 0x200000, Q_LOAD, 8, 0, 0, X86_PF, 0);
                // An eight-byte native store spanning two 4KiB data pages.
                access(VA + 4092, Q_STORE, 8, 0x1122334455667788UL, 0, 0, 0);
                read_value(VA + 4092, 0x1122334455667788UL);
                q30_barrier();
                u64 before = raw(A + 4088), after = raw(A + 4096);
                leader_entry(leaf + 8, (A + 4096) | 5);
                access(VA + 4092, Q_STORE, 8, 0, 0, X86_PF, 3);
                q30_barrier(); q30_check(raw(A + 4088) == before && raw(A + 4096) == after, 120);
                end(group, 0, rep);
                begin(group, 1, rep);
                u64 big_entry = locate(BIG, 2);
                leader_entry(big_entry, 0); q30_invalidate(BIG, 10); q30_barrier();
                setup_one(SPARE, A | PRESENT); setup_one(SPARE + 511 * 8, B | PRESENT);
                leader_entry(big_entry, SPARE | PRESENT);
                read_value(BIG, SEED(A)); read_value(BIG + 0x1ff000, SEED(B));
                leader_entry(big_entry, 0); q30_invalidate(BIG, 11); q30_barrier();
                leader_entry(big_entry, PRESENT | 128);
                read_value(BIG, SEED(0)); read_value(BIG + 0x1ff000, SEED(0x1ff000));
                leader_entry(big_entry, PRESENT | 128 | (1UL << 13));
                access(BIG, Q_LOAD, 8, 0, 0, X86_PF, 9);
                leader_entry(big_entry, PRESENT | 128);
                u64 pdp = locate(BIG, 3), saved = raw(pdp);
                leader_entry(pdp, PRESENT | 128);
                access(BIG, Q_LOAD, 8, 0, 0, X86_PF, 9);
                leader_entry(pdp, saved);
                end(group, 1, rep);
            } else if (group == 11) {
                begin(group, 0, rep);
                u64 high_parent = locate(HIGH, 2);
                setup_one(SPARE + ((VA >> 12) & 511) * 8, A | PRESENT);
                leader_entry(parent, SPARE | PRESENT); leader_entry(high_parent, SPARE | PRESENT);
                read_value(VA, SEED(A)); read_value(HIGH, SEED(A));
                leader_entry(parent, 0); q30_invalidate(VA, 12); q30_barrier();
                q30_check(Q30->roles[SPARE / 4096] != 0, 121);
                access(ALT, Q_STORE, 8, 0, 0, 0, 0);
                leader_entry(high_parent, 0); q30_invalidate(HIGH, 13); q30_barrier();
                q30_check(Q30->roles[SPARE / 4096] == 0, 122);
                u64 traps = Q30->cpu[id].table_traps;
                access(ALT, Q_STORE, 8, 0x1100, 0, 0, 0);
                q30_barrier(); read_value(ALIAS + SPARE, 0x1100); q30_barrier();
                access(ALIAS + SPARE, Q_STORE, 8, 0x2200, 0, 0, 0);
                q30_barrier(); read_value(ALT, 0x2200); q30_barrier();
                q30_check(Q30->cpu[id].table_traps == traps, 123);
                leader_entry(parent, SPARE | PRESENT);
                access(ALT, Q_STORE, 8, 0, 0, 0, 0);
                q30_check(Q30->cpu[id].table_traps > traps, 124);
                end(group, 0, rep);
                begin(group, 1, rep);
                // A dormant CR3 root must rediscover its complete graph on activation.
                q30_check(Q30->roles[ROOT1 / 4096] == 0, 125);
                q30_barrier();
                context(ROOT1, 0, 1, 1, 0); read_value(VA, SEED(B));
                context(ROOT0, 0, 1, 1, 0); q30_barrier();
                q30_check(Q30->roles[ROOT1 / 4096] == 0, 126);
                // Recursive reference makes ROOT0 serve levels 4,3,2,1.
                u64 recursive = ROOT0 + 510 * 8;
                leader_entry(recursive, ROOT0 | PRESENT);
                q30_check(Q30->roles[ROOT0 / 4096] == 15, 127);
                leader_entry(recursive, 0);
                q30_check(Q30->roles[ROOT0 / 4096] == 8, 128);
                end(group, 1, rep);
            }
        }
        q30_barrier();
        if (id == 0) Q30->completed_groups |= 1UL << (group - 1);
        q30_barrier();
    }
}
