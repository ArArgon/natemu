#!/usr/bin/env python3
"""Finite Q30+Q31+Q32+Q33+Q34 log bound, four CPUs and sixteen repetitions; no VM execution.

This is arithmetic over inspected source paths, not an automated C proof or
runtime evidence. Source hashes make a changed fixture/protocol require recounting.
For two/three CPUs every counted multiplicity is no larger than the four-CPU case.

A single-page access has at most one handled fault: recovery restores active=1
under the writer lock before unlock/ERET, preventing another writer from evicting
its mapping before completion. A genuine denial skips the native instruction.
The ordinary successful read bound is 13: BEGIN/END 2, active markers 4, FAULT 1,
WAIT 1, A/D mutations 4, MAP 1. The write bound is 20: replace MAP by MUTATION
for a trapped table write, then add GATE 3 and ACK 4. Permission upgrades use MAP
instead of MUTATION. Each lock acquisition logs WAIT at most once; each epoch
logs one ACK per CPU. Poll/barrier iterations do not otherwise log.
Group 7's four staged barriers retain sixteen peer reads per writer round
(four per stage) and the same four writer operations. The extra barriers add
no epochs or independent log events; their park ACKs remain charged per epoch.

Cross-page operations cost at most 22: BEGIN/END 2, active 2, WAIT 1, GATE/ACK 7,
two MAPs, eight A/D mutations. All 192 are counted AGAIN, retaining the already
charged scalar bound. They prevalidate/pin both pages and do not rescan roles.

The reachable table-page union is at most 50: 24 pages per reset-built root,
plus SPARE and SPARE+4096. No other fixture value becomes a nonleaf pointer.
The recursive-root case only shortens traversal of the existing graph; it can
change role bits on those pages but cannot introduce data pages. One rescan
emits at most one ROLE per physical page. Conservatively charge 50 to every
write/CMPXCHG outside group 8 and every context/invalidate/setup/initial epoch.
In group 8, variants 0/1/4 change only leaf values (zero ROLE); variant 2 swaps
one PT (at most two ROLE); variant 3 swaps the two three-page VA subtrees (at
most six ROLE). Charge six to EVERY group-8 write, including ordinary data writes.

Each fixed context/invalidate/setup costs at most nine non-ROLE events: one
WAIT, three GATE, four ACK, plus its CONTEXT/MARK. Setup additionally receives
256 MUTATION events, the checked patch ceiling, even for single-entry setup.
The 3,000 direct-event allowance exceeds 1,728 CASE + 192 reset MARK + 64 group-5
MARK + 640 group-8 MARK + 5 publication MARK = 2,629.

This bounds the declared successful paths, not arbitrary defects/repeated faults.
Overflow remains a fatal retained failure and cannot produce host-check PASS.
"""

import hashlib
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    for name, expected in {
        "q30.c": "7b95366e395090d01357214a57ad2dd67bdffed412605ce58a5e612b03140cd6",
        "q30_cases.c": "b0a346ee6500bbe2922670235aa1c953be7824ffd0ff0d03ba41d8abf773ed94",
        "q31.c": "9a8a43253524ef494bb9305012b3013e3c27e65ef9e391c526a60c0c34bcf3a7",
        "q32.c": "b9b63a17e26ea57a75f33a45b945823dbbe08baca18fc72a7c4e1e30517cf8a4",
        "q32.h": "d5114ffcc652d7ea1c766f41a00a45a9f22cfe81004915abc07edac63179493d",
        "q33.c": "99b60780bcda9870fcfdfc0ed31990aca443d3f718ee7ce25ac9d67a8c916ea8",
        "q33.h": "b1d9be46a2243b954a52a5edfaea94927580288cb520e3eac5bb7da400976dd0",
        "guest.c": "14a85e00edf9f5fd56b29f4ec3eb3363cbd7df00444c3faa2ac0b97243498f5e",
        "shared.h": "44a31d2d5ea1155f767a8cd507258e2f66dea4844d827da75e39bf05958f96d7",
        "q30.h": "851c04b65ade913fc3efeec1499fe4e74ed18bb722f478d82695cb9bd4da9918",
        "q34.c": "c858daa3b9a9cc78936061dd431c45b44eac923af19129ff44a6e54a115dc0b6",
        "q34.h": "3f8326940cf8317178bc4bb645cdff64ad8f0e35a609c90de03702e7a28011c5",
    }.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, (
            f"{name} changed: inspect paths and recount before updating this bound")

    # Per repetition at four CPUs: LOAD, STORE, CMPXCHG, EXEC, context,
    # invalidate, setup. Reset is attributed to its owning group.
    groups = [
        (80, 16, 0, 0, 4, 0, 1),
        (144, 133, 0, 52, 292, 0, 1),
        (80, 72, 0, 32, 36, 0, 1),
        (20, 11, 0, 12, 4, 24, 2),
        (8, 6, 0, 0, 4, 4, 1),
        (8, 21, 0, 0, 12, 4, 1),
        (416, 27, 21, 0, 4, 0, 2),
        (1280, 768, 320, 0, 4, 0, 5),
        (24, 4, 0, 0, 36, 0, 1),
        (48, 33, 0, 0, 4, 8, 3),
        (20, 23, 0, 0, 12, 8, 2),
        (20, 11, 0, 12, 4, 24, 2),
    ]
    loads, stores, cases, executes, contexts, invalidates, setups = (
        sum(column) * 16 for column in zip(*groups))
    assert (loads, stores, cases, executes) == (34368, 18000, 5456, 1728)
    assert (contexts, invalidates, setups) == (6656, 1152, 352)
    writes = stores + cases
    group8_writes = (groups[7][1] + groups[7][2]) * 16
    fixed_epochs = contexts + invalidates + setups + 1
    access_events = (loads + executes) * 13 + writes * 20 + 192 * 22
    role_events = (writes - group8_writes + fixed_epochs) * 50 + group8_writes * 6
    fixed_events = fixed_epochs * 9 + setups * 256 + 3000
    bound = access_events + role_events + fixed_events
    assert bound == 1924051
    # Per-CPU Q31 successful-path ceilings in declared variant order. Count
    # the source branches, not a measured schedule: a delivery costs <=10
    # accesses, IRET <=7, and a failed delivery adds only its completed prefix.
    # Group 6/2 allows all four first attempts to fail and restart, even though
    # a peer makes only two revocations. Its 109 bound includes observations,
    # peer writes, repairs and the final mapping. Group 7 counts eight byte
    # pairs plus one failed pair and one delivery/repair/IRET. Group 9/9 counts
    # initial gate repairs, failed GP descriptor read, PF delivery/repair/IRET,
    # then the separate GP attempt/delivery/IRET. Group 11 uses the larger peer
    # fault path rather than CPU 0's writer path.
    q31_accesses = [
        [1], [20, 21], [21, 21, 20, 20], [26, 28, 17], [23, 24],
        [28, 28, 109], [36, 1, 36], [21],
        [19, 20, 24, 21, 19, 37, 20, 16, 17, 48, 46], [18], [34], [2, 4],
    ]
    # Excludes per-access MARK/STEP and CODE (charged below). An attempt is
    # 8 events, commit 7, successful delivery 15, IRET 8, failed delivery 8,
    # REP progress 7 per byte; END adds 1. Code CPUID also has its MARK5.
    # For example: four failed/restarted split rounds cost 4*(16+15+8+7)+1;
    # nested handler case 9/10 costs 4*8+2*15+2*8+2*7+7+1.
    q31_other = [
        [16], [47, 47], [47]*4, [47, 47, 32], [62, 62],
        [47, 47, 185], [103, 16, 103], [47],
        [32]*5+[78, 33, 33, 33, 71, 100], [62], [93], [25, 41],
    ]
    # Charge a context transition in EVERY fixture even if root/CPL already
    # match. User fault delivery/return adds 3; early GP/PF/retry adds 5;
    # terminating DF contexts add 2. Each mapping() and serialize_code()
    # invokes one invalidate. Four split retries allow 4+4+1 invalidates.
    q31_contexts = [
        [1], [1, 4], [4, 4, 1, 1], [1]*3, [1]*2, [1]*3, [1]*3, [4],
        [1, 4, 1, 1, 1, 1, 3, 3, 3, 6, 4], [1], [1], [1, 1],
    ]
    q31_invalidates = [
        [0], [2, 2], [2]*4, [2, 2, 0], [2, 2], [2, 2, 9],
        [2, 1, 2], [2], [1]*5+[2, 2, 1, 1, 3, 4], [1], [3], [0, 0],
    ]
    for table in (q31_accesses, q31_other, q31_contexts, q31_invalidates):
        assert list(map(len, table)) == [1, 2, 4, 3, 2, 3, 3, 1, 11, 1, 1, 2]
    cpu_cases = 34 * 16 * 4
    q31_operations = sum(map(sum, q31_accesses)) * 16 * 4
    assert q31_operations == 54144
    # Q30 <=20 plus code generation and Q31 MARK/STEP. FETCH8's extra
    # prevalidation WAIT remains below this ceiling. All split accesses are
    # double charged another 24 each below, retaining the scalar charge.
    q31_access_events = q31_operations * 23
    q31_fixed = (sum(map(sum, q31_other)) * 64 +
                 (sum(map(sum, q31_contexts)) +
                  sum(map(sum, q31_invalidates))) * 64 * 9 +
                 cpu_cases * (96 + 9) + 34 * 16 * 2)
    # Only initial activation changes table roles: <=24 old pages retire and
    # four new roots add 16. Later fixtures change leaves/CPL, not reachability.
    # 8,192 retains the prior conservative ROLE/code pin/role epoch allowance.
    extra_code_events = 8192
    split_events = 12 * 4 * 16 * 24
    bound += q31_access_events + q31_fixed + extra_code_events + split_events
    assert bound == 3614355
    # Q32 cases include every CPU's 17 fixture accesses. Each completed
    # ordinary IRQ uses at most 24 accesses: 8 delivery, 4 handler, 2 JMP-byte
    # fetches, 1 EOI, 2 restored-register reads and 7 IRET/descriptor reads.
    # CLI/STI privilege faults use 10-entry/7-return accesses. Three IRQs bound
    # priority cases. Group 10 includes 16 initialization bytes, eight byte
    # pairs, final eight-byte observations and the one repaired failed pair.
    # Group 12 charges each CPU its own target path plus one remote-controller
    # store; code roles/pins/epochs are separate below. Four-CPU broadcast
    # charges every role the largest role's one extra opcode/mapping write.
    q32_accesses = [
        [41]*3, [43]*3, [41, 34, 34], [41, 41, 41, 41, 42, 61],
        [89, 89, 65], [41, 41], [41, 41, 41, 17], [42, 42, 61],
        [60, 61, 35], [102, 82, 42], [42, 42], [59, 60],
    ]
    assert list(map(len, q32_accesses)) == [3, 3, 3, 6, 3, 2, 4, 3, 3, 3, 2, 2]
    q32_operations = sum(map(sum, q32_accesses)) * 64
    assert q32_operations == 116672
    # 512 non-access events per CPU-case covers <=3 IRQ deliveries including
    # handler attempts, explicit STI/NOP/JMP/EOI and snapshots, all boundary
    # checks, REP's eight progress snapshots, the fault/retry alternatives,
    # sources/mailbox/control records and END. MARK/STEP already belong to the
    # 23-access charge. Eight context changes and eight invalidates exceed the
    # finite user-fault, nested/early-delivery and code-serialization paths.
    # Fixture setup retains the proven <=96 patches and one nine-event epoch.
    # Only leaves/CPL change; the same four roots remain reachable throughout.
    q32_fixed = 37 * 64 * (512 + 8*9 + 8*9 + 96 + 9)
    # Empty SGI/SPI acks have an explicit per-source/per-CPU 4096 cap. Empty
    # periodic timers do not emit Q30/Q31/Q32 records. 8192 covers standalone
    # code-role/pin epochs; 4096 covers global CASE/phase/control margin.
    q32_empty = 4 * 2 * 4096
    bound += q32_operations * 23 + q32_fixed + q32_empty + 8192 + 4096
    assert bound == 8144915
    # Format-2 Q32 group 7/2 retains up to eight real HLT trials. The first
    # trial is already charged above; 16 repetitions * four CPUs * seven
    # additional trials = 448. Every early arrival is fully serviced before
    # an actual EB FD branch returns to HLT, and the eighth early arrival
    # fails after service. Each additional episode costs <=24 IRQ accesses
    # plus two branch FETCH8 accesses. There are no new contexts, invalidates,
    # code roles or table topology changes: these are CPL0 ordinary stack,
    # handler-data and immutable source-byte accesses through existing roots.
    #
    # 192 additional nonaccess records per episode exceeds: HLT attempt/commit
    # and shadow 16; two dispatch boundaries/snapshots 16; delivery 15;
    # handler MOV, handler JMP, EOI and retry JMP 4*15; restored-state snapshot
    # 7; IRET 8; DISPATCH/EOI 2; EMIT/NATIVE/ACCEPT 3; HALT 3; CONTROL15 1.
    # Their sum is 131. Per-access MARK/STEP and all native recovery events
    # remain charged in 23. The extra empty-ack ceiling is unchanged globally.
    # One CONTROL15 for each first trial is charged separately; successful
    # first trials do not silently consume the old fixed-event allowance.
    assert "#define Q32_HALT_TRIALS 8" in (root / "q32.h").read_text()
    assert "#define Q32_VERSION 2UL" in (root / "q32.h").read_text()
    q32_extra_trials = 16 * 4 * 7
    q32_trial_events = q32_extra_trials * (26 * 23 + 192) + 16 * 4
    assert q32_trial_events == 353984
    bound += q32_trial_events
    assert bound == 8498899
    # Q33 adds phase admission and exported helpers; only Q33 calls the new
    # expected-generation pin and second handler MOV. Retain this prefix bound.
    # The native successful FETCH prevalidation now also stores walk.gpa into
    # its result. This changes no operation, epoch or record multiplicity.
    #
    # Q33 per-CPU access ceilings, including the 20-access prepared fixture.
    # An ADD source probe reads EIGHT bytes (instruction plus branch dependency),
    # a MOV probe three. Production probes before/after transfer; entry probes
    # once more. All target-rotated groups charge each CPU both its target and
    # actor work, including every remap-cleanup iteration in group 5/1.
    # Groups 1/2/4 have no epoch writer after their prepare barrier: read-only
    # initial mappings install into empty slots, code/pool links do not mutate
    # guest mappings. Group 3's three writes/four productions finish before
    # its entry barrier: CPU0 <=199 accesses, peers <=160, including all links.
    # Groups 5/6/7/9 serialize targets; actor mutations finish before each fresh
    # entry. Group 5's explicit stale validation/pin attempt is counted once.
    # Group 6 charges three complete IRQs, second handler MOV, handler rewrites,
    # rejected production and fresh production/entry. Group 9 includes old
    # pinned MOV, PF repair/IRET/retry, CPUID, rejection and fresh ADD.
    # Group 8 allows ALL SIXTEEN entry attempts for BOTH generated instructions
    # because peer mapping changes can intervene. No rejected attempt has a
    # guest effect; charge 16*8 ADD and 16*3 LOAD probes, both productions,
    # actual LOAD PF/repair/IRET/retry and optional source-fetch PF/repair/IRET.
    # Group 10's quota/cancel/delay paths make no peer mapping changes.
    q33_accesses = [
        [44, 44], [44, 44], [208, 208], [44, 44], [96, 96, 64],
        [160, 160, 160], [64, 64, 64, 64, 64, 80],
        [256, 288, 288], [80, 112], [48, 64, 40],
    ]
    assert list(map(len, q33_accesses)) == [2, 2, 2, 2, 3, 3, 6, 3, 2, 3]
    q33_operations = sum(map(sum, q33_accesses)) * 64
    assert q33_operations == 191488
    # Nonaccess primitives: source_read ATTEMPT+STATE=8; successful production
    # with n chunks=29+7n; generated entry=28 (including source probe, PIN,
    # UNPIN and state snapshots); linked JMP=16. The largest 4288-byte object
    # costs 967 production records, then 28 entry and 8 END records. Chain
    # cases charge up to four productions, fifteen entries and ten JMPs.
    # Group 8's thirty rejected entry attempts add <=13 records apiece:
    # source snapshot8, PIN/UNPIN2, REJECT1 and two lock WAITs.
    # 1536 per CPU-case also covers pin/unpin lock WAITs, three IRQ snapshots,
    # mailbox/control events, malformed transfer/cancel and fixed fault state.
    # Group 6 includes four full copied-WAIT packet records per target.
    # Context/invalidate/setup protocols and per-access MARK/STEP/CODE remain
    # separate: sixteen contexts and sixteen invalidates per CPU-case exceed
    # the finite remap, fault-return, handler and serialization paths. Setup
    # retains <=96 patches. Leaves change, but the same four roots stay live.
    q33_cpu_cases = 28 * 64
    q33_fixed = q33_cpu_cases * (1536 + 16*9 + 16*9 + 96 + 9)
    # A code-role epoch costs <=10 events including generation. Each CPU-case
    # has two; group 5/1 adds two per target. This is <=37120, plus 896 global
    # CASE records and four initial CONTROL records, below 65536. PIN/UNPIN
    # and their WAITs belong to 1536 above. Empty SGI/SPI acknowledgements keep
    # the same cumulative cap already charged to the Q32 prefix.
    bound += q33_operations * 23 + q33_fixed + 65536
    assert bound == 16425427
    # Reservations persist even for rejected responses; quota refusal makes
    # no reservation. Shared publication/chain groups reserve only on CPU0.
    # By group at four CPUs and sixteen repetitions:
    pool_objects = [80, 128, 128, 128, 512, 384, 768, 384, 256, 192]
    assert sum(pool_objects) == 2960 < 4096
    # Every object is 80 bytes except 64 160-byte group2 objects, 64 4288-byte
    # group2 objects, and 32 160-byte chain replacements. Allocation rounds
    # (length+83)&~63, including the non-line-aligned entry and tail margin.
    pool_bytes = (2960 - 160) * 128 + 96 * 192 + 64 * 4352
    assert pool_bytes == 655360 < 3 * 1024 * 1024
    assert "#define Q33_OBJECTS 4096" in (root / "q33.h").read_text()
    # Q34 source-indexed startup plus exact context import. Six positive
    # variants, sixteen repetitions, three APs: 288 successful AP paths.
    # Every AP is runtime-limited to128 STEP records. One STEP has nine STATE
    # chunks and at most three guest-memory operands, each with at most five
    # startup-walk A/D records and one data record: 1+9+3*6=28. This includes
    # CALL/return, far-transfer descriptors, and the explicit post-import JMP.
    q34_cases, q34_cpu_cases, q34_aps = 8*16, 8*16*4, 6*16*3
    q34_startup = q34_aps * 128 * 28
    # 1024 per global case covers initialization (<128 physical writes),
    # mailbox/control/diagnostic/lock records, nine initial state chunks/AP,
    # CASE/END, import/projection controls and seven final Q31 chunks/CPU.
    q34_direct = q34_cases * 1024
    # project() checks <=1024 differing8-byte words. Its successful input
    # address union is substantially smaller: prefix has64 page-edge sentinels
    # plus four Q33 temporary source words in300000..320000; initial startup
    # writes fit256 aligned words even counting both words for each unaligned
    # field; the fixed bootstrap/trampoline/kernel walk touches <=32 table
    # slots (indices0/1/510/511); shared CALL/PUSH stack uses <=16 words;
    # four private native fault/IRQ stacks use <=8 words each. Mailbox and lock
    # slots are already in initial setup. Sum<512; 1024 guard also rejects an
    # unexpected widened producer before its writes can exceed the budget.
    q34_projection = q34_cases * 1024
    # runtime_setup:512 identity PTEs,20 patches/CPU,one replacement parent.
    q34_runtime_setup = q34_cases * (512 + 4*20 + 1)
    q34_old_fixture_setup = q34_cpu_cases * 96
    # q32_reuse_fixture does17 Q31 native accesses/CPUcase. Publisher performs
    # one MOV read plus24 IRQ delivery/handler/JMP/EOI/IRET accesses per AP.
    # Continuation256: import through repaired MOV43; wrapper initial fetch1,
    # three8-byte source probes24, up to16 entry probes128, IRQ24 =220.
    # The spare36 covers recipe/control reads without claiming a smaller retry
    # count from timing. All accesses are single-page (8-byte aligned frames,
    # page-local instruction bytes/descriptors); charge23 each inclMARK/STEP.
    q34_operations = q34_cpu_cases*17 + q34_aps*(256+25)
    assert q34_operations == 89632
    # Per AP1024 fixed records covers19 source-probe ATTEMPT/BEFORE groups,
    # 16 pin/reject/WAIT paths, actual MOV/ADD/STI/NOP retirements, PF/IRQ state,
    # ten80-byte transport exchanges (three payload chunks), source request,
    # cache publication/entry/effect and import snapshots. Publisher128 covers
    # its one MOV and ordinary IRQ state/retirement/native/APIC records.
    q34_metadata = q34_aps * (1024+128)
    # Per globalcase <=41 epochs: old fixture setup4/context4, imports3,
    # projection<=4 batches (charge5), runtime4, two maps+invalidates perAP12,
    # publisher invalidates3, code-role toggles6. Charge64. Each costs9 protocol
    # records plus32 ROLE records, exceeding the20-page union of four old
    # Q31 roots(16) and shared startup root/PDPT/PD/PT(4). Source startup A/D
    # accesses use separate backing, hence no hidden Q30 epochs. Pin/unpin
    # have no epoch; WAITs are already charged in the fixed/access allowance.
    q34_epochs = q34_cases * 64 * (9+32)
    bound += (q34_startup + q34_direct + q34_projection + q34_runtime_setup +
              q34_old_fixture_setup + q34_operations*23 + q34_metadata + q34_epochs)
    assert bound == 20574003
    assert sum(pool_objects) + q34_aps == 3248 < 4096
    assert pool_bytes + q34_aps*128 == 692224 < 3*1024*1024
    assert '#define Q34_PROJECT_MAX 1024UL' in (root/'q34.h').read_text()
    capacity = 20971520
    ram_bytes = 1984 * 1024 * 1024
    assert f"#define Q30_LOG_CAPACITY {capacity}" in (root / "q30.h").read_text()
    assert "#define RAM_SIZE 0x7c000000UL" in (root / "shared.h").read_text()
    assert bound < capacity
    assert 16 * 1024 * 1024 + 6024 + capacity * 96 <= ram_bytes < 2 * 1024 ** 3
    print(f"PASS static cumulative Q30/Q31/Q32/Q33/Q34 budget: {bound:,} <= {capacity:,} records; "
          f"{capacity - bound:,} spare; 1984 MiB RAM / 2 GiB file ceiling; no VM evidence")


if __name__ == "__main__":
    main()
