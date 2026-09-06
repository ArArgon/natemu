# M06-M1 delivered Mach baseline

The [investigation](../../../docs/research/investigations/INV-01-06.md) owns current status; the [research workflow](../../../docs/research/README.md) governs new execution.

Compile-only: `python3 -B build.py --output "$NATEMU_RAW_ROOT/fresh-build"`. Synthetic validation: `python3 -B -m unittest discover -s . -p "test_*.py" -v`. `run.py plan` enumerates the full retained scope; after independent review, `run.py run --build <retained-build> --output <fresh-external-directory>` executes only the declared first tranche.

## Native return under preparation — 2026-09-05

The v3 implementation replaces the previous success-label scaffold. No native
M06-M1 experiment is approved or executed by this author return. The first
proposed execution tranche is **six original positive children**: n=1, local
plain ad-hoc signing, non-JIT, serial server, direct path; load/store × fixed
seeds 60601–60603, with the unchanged ten warmup and 100 checked events each.
Its six children have 180 s summed total ceilings and a 900 s stage timer;
each uses a 5 s no-output-progress and 30 s total watchdog, 64 MiB per
stdout/stderr file, 1 GiB stage threshold and 1 GiB free-space reserve. All
other original positive cells and every negative/control/hold remain explicit
not-run obligations. This stage cannot complete M06-M1 or supply Q19's Mach
composition prerequisite. Independent review of this return precedes any run.

The AOT `m1_access` assembly wrapper places cookie/nonce in x9/x10 and retains
cookie/nonce/sentinel/SP in x19–x22 across a call to one exact exported load or
store label. The descriptor, exact PC and stack bounds precede release arming.
The server acquires arming, validates exception category, address, count/flavor,
SDK-accessor PC/SP and actual delivered registers, and retains all 68 old and
new general-state words. It copies the general state unchanged. The worker
records measured result, preserved registers and exact before/after SP equality.
A second delivery for the same event is terminal. Commit and disarm occur only
after actual send completion, and descriptor fields are not reused earlier.

Main registers all workers before opening admission. Even rounds admit one
rotated worker at a time; odd rounds release every worker before waiting for
all completions. Workers remain alive and disarmed while main joins front
servers. Every forwarding worker then restores its actually installed/saved
owned prior, delivers one extra proof event, and parks until the prior server
joins. It finally restores its initial selected mask, verifies snapshots and
disposes resources on the still-living worker. Server arguments live through
join; exactly one thread consumes each prior queue. Initial task ports are
captured and compared after teardown, without installation on the task.

The build produces and actually inspects separate plain and hardened+allow-jit
binaries. Both local JIT/signing combinations remain capability questions.
The immutable JIT path uses the public approved write callback, cache
invalidation and reader ISB; it is outside the first non-JIT tranche.

### Message/right ownership of the new wrapper

The retained SDK `mach/message.h` defines the public `mach_msg` API,
`MACH_MSG_TYPE_MOVE_SEND_ONCE`, timeout and trailer options. Retained generated
`mach_excServer.c` supplies the state routine demux and reply-header construction;
retained generated headers plus `verify_layout.c` fix the admitted routine's
field layout. The wrapper retains that parser and accepts only routine 2406,
a simple fixed two-code/68-word message, exact native NDR and the requested
format-0/null trailer. The raw request bytes and decoded state are both retained.
A catch's prepared reply record is distinct from a successful IPC send record.

| Owned object/path | Acquisition | Successful disposition | Error disposition |
| --- | --- | --- | --- |
| Front/prior receive right | `mach_port_allocate(RECEIVE)` | One `mod_refs(RECEIVE,-1)` after servers join and living-worker restoration | Entire owned child terminates; no success disposal assertion |
| Inserted handler send right | `mach_port_insert_right(MAKE_SEND)` | One `mod_refs(SEND,-1)` after restoration | Terminal child; no reuse |
| Worker control send right | `mach_thread_self()` | One `mach_port_deallocate` on the living worker after restore | Terminal child |
| Each non-null snapshot tuple | `thread_get_exception_ports` or `task_get_exception_ports` | One deallocation per acquired tuple, including repeated names across separate inspections | Terminal child; never reported as restored/cleaned |
| Received reply send-once right | Successful simple receive, remote disposition checked | Generated reply moves that same right; successful `mach_msg` send is recorded before event metadata reuse | Rejected wire/demux or failed send exits the whole child, without guessing whether a failed transfer consumed a right |
| Forced reply-send-failure seam | Same received owned reply right | Explicitly deallocate only this owned reference, clear remote name, attempt send; outcome remains unreviewed negative evidence | Terminal child, never a positive reply |
| Port set | `mach_port_allocate(PORT_SET)` | One decrement after all receive users join | Terminal child |
| Owned data/JIT mappings | Checked `mmap` | Checked `munmap` after users join | Terminal child |

Null handler tuples are semantically no handler; their inactive behavior/flavor
fields are not interpreted as an active route. Active tuples compare full
mask/port/behavior/flavor. Only EXC_MASK_BAD_ACCESS is modified. Fatal paths
rely on whole-child containment, claim no successful in-process restoration,
and do not attempt ambiguous duplicate right cleanup. This table describes
instrument policy; no server-death recovery guarantee is inferred.

### N4/N5 author return — independent review required

The accepted six-child baseline and its retained producing builds remain
separate from this new return. `run.py` still defaults to that exact baseline.
No control or hold native execution is authorized by the implementation below.

`--stage controls` proposes all 33 original auxiliary children: eight negatives
plus terminal, restoration and unsupported-prior controls, each with three
fixed seeds. Its native child ceilings sum to 990 s and its stage ceiling is
1,800 s. `--stage holds` proposes 96 complete held children: n=2, plain ad-hoc,
non-JIT load, both topologies and both paths, held worker 0, every applicable
point, three fixed seeds, ten warmup and 100 checked events. Its native ceilings
sum to 2,880 s and its stage ceiling is 3,600 s. Both retain 5 s no-progress/
30 s child ceilings, kernel 64 MiB per output file, 1 GiB stage output threshold
and 1 GiB free reserve. Stop on the first unexpected outcome; remaining rows
stay not-run. Neither stage completes the method. The complete schedule still
contains 384 positive, 33 auxiliary and 11,520 applicable held children.

The unsupported-prior control installs an owned, unserved default-behavior
decoy only after inspecting an empty selected thread route. The ordinary
registration guard then inspects and refuses it before any admission or fault.
It never invokes that route or replaces an unknown pre-existing handler.
Receive destruction, reply-right release and unexpected helper mapping retain
actual API/action fields. `controls.py` separately decodes raw message bytes,
checks event/state attribution and demands the specific observed corruption,
repeated fault, rejected PC, owned helper address, failed send or contained
stall. Exit status or a trigger marker alone never qualifies a negative.
Unhandled and receive-death outcomes remain categorical parent observations,
without predicting a downstream signal or claiming successful cleanup.

The held actor releases the trace lock before waiting. For event holds, round
0 admits the selected worker first. The controller issues a real teardown
request; main consumes it while waiting for that unfinished worker and records
that the outstanding event prevents teardown. At n>1 the controller admits
other workers only after hold entry and waits for the selected competitor's
actual fault-attempt record. With independent servers it also requires that
competitor's completed/disarmed event before releasing the held actor. Shared
serial service may queue, and its checker rejects another front receive while
the shared server is held. At n=1 the consumed/deferred teardown request is
the competing operation. No elapsed duration substitutes for the handshake.

Teardown holds occur before initial-route restoration or before disposal of
the retained snapshot/handler/control/mapping resources. The temporary
inspection reference returned by restoration is still released after that
inspection; it is not the retained original/prior reference. Main waits for
the selected teardown hold before recording its actual join attempt, and the
controller releases only after observing that attempt on a still-live worker.
All selected workers, both server topologies and every named point remain in
the full held matrix. `holds.py` verifies the full normal trace plus exact
hold/competitor/progress/restore orders and missing or duplicate records.

Synthetic fixtures now include complete direct/forwarded traces at 1/2/4/8
workers, load/store and both topologies; every applicable held identity and
point; full 110-event warmup/proof samples; and destructive control/hold
mutations. Real dummy children exercise the retained runner's restoration,
negative, held and mismatch consumers. These are fabricated inputs, not
Mach/publication execution or host evidence.

The R1–R5 return raises internal receive timeout to60s so it cannot preempt
the parent's30s total/5s no-progress watchdog. In receive-right destruction,
the receiver waits for the worker's completed action record before emitting
its specific owned-port-death terminal record. Consumers reject unrelated
explicit exits and resource signals; memory-fault signals, the declared parent
watchdog, and the exact owned-port-death API result remain distinct categories.
Other terminal outcomes stay unresolved rather than being counted as controls.

Forwarding must follow front attribution and prior delivery/reply, then precede
the front reply. The restored-prior proof must finish commit/disarm before its
proof marker and initial-route restoration. Send-completion logging may still
follow recipient resumption. Raw PC/SP checks are tied to the asserted
non-opaque arm64 layout; the native build refuses an opaque layout.

The after-receive hold now retains a bounded `received_message` record after
successful receive and owned-port lookup, before envelope/NDR/count/trailer
validation. Its bytes must match the later validated `wire` record, which the
hold consumer requires after release. Other hold points retain their original
positions. These corrections still require independent return acceptance.

Full topology/signing/JIT observations, all remaining held identities/cells,
transport-spoofing preflight fixtures, replication and independent findings
review remain owed. No source or synthetic acceptance proves Q19 composition,
foreign-handler compatibility, broad state completeness or any timing result.

The receive-error correction records the actual attempted receive port, registered
owner/role, Mach return and successful destruction publication in an explicit
`M06-M1-receive-failure-v1` record. The investigation owns its pending review and
failed-run interpretation. Build-only reporter validation (no Mach receive or
fault execution) is `python3 -B test_receive_failure.py --build <retained-build>`;
it uses that build's retained production source and generated MIG inputs.
