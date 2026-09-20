# Disposable native ownership handoff

INV-03-04 claim 20 preparation, corrected after claim 21’s failed first boot
checkpoint. No handoff result exists; the first run used an incorrect PC oracle. Four fixed cells are native
stage-1 ISH/OSH × register-copy/direct-host payload access. Each fresh process
creates one fresh VM, two owner-thread vCPUs, a 144-KiB anonymous backing allocation
and 64 epochs. The backend is the main thread; vCPU APIs stay on their creator
threads. No CPU resource-limit setter runs in VM workers; inherited CPU limits
must already be unlimited. The frozen Track 01 source is read only for its
retention helper and accepted native idioms, never modified.

A single 4-KiB data page has VA 0x40010000 and 0x40020000 aliases to IPA
0x40010000. Four 64-bit aligned words at offsets 0,128,256,384 occupy distinct
cache lines. The worker reads and records `hw.cachelinesize`, requiring 64 or
128 bytes before mapping; other sizes stop, rather than silently weakening coverage. Both aliases use the same stage-1 WB/SH and execute-never attributes.
The 4-KiB table granule has L0/L1/L2/L3, MAIR0=0xff, T0SZ16, WB table walks,
selected SH0, EPD1 and IPS40. Boot returns actual MAIR/TCR/SCTLR/ACTLR plus both
stage-1 descriptors through registers. These are not stage-2 readbacks.
The code page is disjoint, immutable after initial publication; other identity
pages are NX. Stage-2 mapping is RWX as a disposable fixture, not a product
permission decision. Initial host writes/cache publication precede VM entry.

Every native command starts DSB SY, accesses only its selected alias, increments
an independently maintained native sequence, and ends DSB SY before its specific
HVC. The host validates exception class/immediate and the exact named **resume PC**,
then captures x0–x9. The frozen Track 01 `host.c` checkpoint already documents
that HVF returns the next instruction PC after HVC. Version 2 records separately
name instruction/resume addresses; the oracle explicitly rejects the instruction
address as the returned PC for both boot and ordinary replies. A reply
is a completed participant drain, not an arbitrary host stop or data ready word.
The owner sets only the named assembly resume label. Both CPUs and both aliases
read every release; writers alternate CPU and alias. Concurrent owner dispatch
is used for observations, but actual native overlap is not a claimed oracle.
This tests serialized ownership handoffs, not an ordering litmus.

Each epoch prewarms the previous data, performs host→resident phase1,
resident→host phase2, and a second host→resident round trip phase3 in the same
buffer. Data encode epoch, phase and word offset. A direct transfer uses actual
volatile host backing reads/writes bracketed by compiler-clobber DSB SY, only
while both participants are at validated drains. Copy writes enter solely in
registers and are stored by the selected native participant; copy reads use
actual exported registers. No host payload alias read follows initial ownership
transfer in a copied cell. Host allocation, initial publication and teardown
remain separately stated OS/backing assumptions.

The finite source-job subcase captures old returned bytes and a generation,
advances generation after mutation, rejects stale publication before completion,
and observes new source bytes on release. This is an executable host coordinator
model composed with actual native transfer; it is not a captive shim, complete
translator, byte-decode cache, arbitrary backend callback or native I-cache test.
All payload operations are aligned scalar words; no whole-payload atomicity,
arbitrary byte-copy, MMIO, cache-control or guest UC bus semantics are inferred.

`oracle.py` independently constructs the exact ordered 64-epoch trace and checks
all returned words, native sequence/CPU/epoch/phase/alias, HVC site/syndrome,
stage-1 readbacks, drain coverage, mutation/revocation/completion order, critical
API counts and teardown. Missing/duplicate/extra events, errors and partial runs
fail. Synthetic good models and deliberate mutants test the checker, not hardware.
The producer reports observed data; it does not replace mismatches with expected
values. The oracle's instruction-site identity depends on the reviewed retained
assembly/disassembly; a forged producer is not its threat model.

Preparation (build only; no signing or worker execution):

```sh
NATEMU_RAW_ROOT=/private/tmp/natemu-track03-raw python3 -B experiments/track03/native-handoff/run.py --name handoff-preparation-01
```

The runner atomically refuses reused output names, snapshots its seven inputs
including the frozen retention helper, builds from those inputs, retains failures,
raw native disassembly/section bytes and hashes. Two build jobs have 60-second
ceilings. The helper hashes the final manifest, never embeds a self-hash in it.
A distinct source/code/oracle review and explicit run budget are required before
adding `--execute`. That option signs the fresh disposable worker, then permits
exactly four cells once, 20 seconds each/100 seconds total, no retries. Any error,
timeout or oracle mismatch stops the matrix and retains the partial record.
Decision-evidence findings also require independent fresh-build replication.
No timing, universal coherence or effective stage-2 attribute claim follows from
passing. ISH/OSH differences are correlations requiring diagnosis, not proof of a
cache cause or a remedy. Direct-host transport remains a candidate, not selected
by this method; copied transport is a scoped control with unmeasured cost.

The corrected version-2 runner finalizes a failed cell with its terminal command
status/return code or oracle error; old version-1 failed records remain unchanged.
Correcting preparation does not authorize a retry of the consumed matrix budget.

After the second primary stopped at a boot value comparison without retaining
values, preparation 05 moves unchanged comparisons after BOTH complete boot
records are printed. Passing trace format, all expected values and the oracle
remain unchanged. This diagnostics correction does not authorize execution.

Claim 24’s source-backed configuration sets ITD/SED explicitly: initial SCTLR
request `0x30d00980`, exact enabled readback `0x30d01985`. No mask is used; the
oracle rejects the prior `0x30d01805` value. All other fields, assembly and
protocol remain unchanged. Preparation 06 is build only, not run authorization.
