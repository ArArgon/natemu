# Disposable native128 pair qualification

INV-03-04 claim37 preparation only. No native execution is authorized by this file.
Three fixed fresh-process cells: forced torn-pair control (writerCPU0), then
LDP/STP targets with writerCPU0 and writerCPU1. Each has oneVM/two owner-thread
vCPUs/144KiB mapped backing. Reuse handoff25's stage1 ISH WB0xff aliases, exact
SCTLR0x30d01985 and ACTLR2 setup, with MMFR2.AT1 required on both vCPUs.

The aligned16-byte pair starts at IPA0x40010000, initially(0,~0). CPU0 uses
VA0x40010000; CPU1 uses VA0x40020000. Separate128-byte-spaced control words carry
ready/done/ack/abort. Host touches backing only before entry/after both CPUs exit.
No host reads or writes participate in sample loops. The exact data instructions
are `ldp x10,x11,[x24]` and `stp x10,x11,[x24]`, verified in retained disassembly.

In the control, writer STRs low=1, release-publishes phase1, waits for reader ack,
then STRs high=~1. Reader acquire-waits phase1, samples once using tested LDP,
and acknowledges. Required raw pair is(1,~0), detected as torn. This is a positive
oracle control, not a legal atomic-write sample. Only if it qualifies may targets run.

Each target writer performs8,000,000 incrementing complementary STP updates.
Reader uses LDP until writer publishes done. Both run after an initial native
ready handshake; no handshake or host exit surrounds individual pair updates.
Reader reports sample count, changing-low-value count, torn count, first torn
pair and last low value through registers at terminal HVC. Writer reports stores.
A torn target stops; missing progress (<10,000 reads or<1,000 observed changes)
is inconclusive and stops, never triggers a retry. Complement checks use sampled
registers, never reload the data. These finite concurrent execution intervals and
observed interleaving are not a claim of simultaneous physical-core residency.

The pure independent Python oracle checks native setup, lifecycle/API counts,
resumePC/HVC identity and counters/results. No mixed pair observed supports only
the sampled operational mapping/instruction path; it does not expose private
stage2 bits or prove universal atomicity. No performance estimate follows.

Default runner: fresh snapshot, two strict compile jobs, assembly disassembly,
pure oracle models/mutations and hashes. Existing frozen retention helper handles
allocation, bounded commands and process cleanup. No signing/probe execution in
default mode. Later --execute needs separately reviewed budget:3workers sequential,
20s/cell100s signing-excluded execution phase, no VM CPUsetter/inheritedunlimitedCPU.
Keep all failures, stopfirstinvalidcontrol/target, no retries. Exact source snapshots
include this README, probe.c, guest.S, oracle.py, run.py, unchanged entitlement and
frozen retention.py. Use explicit NATEMU_RAW_ROOT outside repo and a fresh --name.
