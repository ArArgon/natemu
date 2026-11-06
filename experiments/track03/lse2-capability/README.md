# Captive LSE2 feature API read

Disposable INV-03-04 claim34 method. Default is retained build-only; execution
requires separate pre-execution review and a named budget. The sole worker creates
one default configuration, queries its MMFR2 once, creates one default VM and one
vCPU using that configuration, queries that vCPU's MMFR2 once, then destroys the
vCPU/VM and releases the configuration. All vCPU APIs run on their creating thread.
There are no guest mappings, guest instructions, register writes or hv_vcpu_run.

Both full64-bit values and all API results are retained. AT bits35:32 equal1 in
both matching successful reads means LSE2 is enumerated;0 means not enumerated;
other values, read errors or disagreement remain inconclusive. Lifecycle failure
cannot qualify. No automatic retry, guest-MRS fallback or expanded sample follows.
Enumeration does not itself establish effective inner/outer WB memory attributes
or measured128-bit atomicity. Claim25's handoff is separate bounded evidence.

The runner copies five inputs (this file, probe.c, run.py, existing entitlement,
and unchanged frozen retention helper), builds from that fresh external snapshot,
and retains all outcomes/checksums with the existing helper. It never overwrites
an existing name. Strict one-job compilation has a60-second ceiling. Optional
execution: one worker,20-second worker timeout,30-second signing/execution phase;
no VM-worker CPU-limit setter, inherited CPU limits must be unlimited. Existing
helper kills/reaps process groups on failure. No signing or Hypervisor API call
occurs in default build-only mode. Example preparation:

`NATEMU_RAW_ROOT=/private/tmp/natemu-track03-raw python3 -B experiments/track03/lse2-capability/run.py --name lse2-api-read-preparation-01`

Add `--execute` only under a separately reviewed named budget. The copied
entitlement is unchanged from the native handoff fixture.
