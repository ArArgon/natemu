# Track 03 native handoff: missed inherited HVC PC convention

September 20; author `/root/memory`, independent reviewer `/root`.

The first reviewed native handoff matrix stopped before its first boot readback.
The probe compared HVF's returned PC to the HVC instruction address. It received
`0x4000006c`, the next instruction, while requiring `0x40000068`. No handoff epoch
ran. [INV-03-04 claim 21](../research/investigations/INV-03-04.md) and its findings
review own the raw result and limits.

This was a missed existing premise, not a new platform discovery. Frozen
[host.c](../../experiments/track01/source/host.c) at line 304 explicitly documents
that Hypervisor.framework reports the return PC after HVC and validates the
instruction at PC minus four. The author reused the setup idioms without carrying
that adjacent checkpoint convention into the new fixture. Independent review
checked assembly labels, protocol and synthetic models but shared the same wrong
PC expectation; the synthetic oracle agreed with its author-supplied model and
therefore could not expose the mistaken interface premise.

The narrow correction names instruction and resume addresses separately, compares
actual returned PC to the exact assembly-owned resume label, and includes mutants
that return the instruction address at both checkpoint sites. Version-2 records
also terminate the failed cell's manifest status rather than leaving its initial
`running` value. The original failure record remains unchanged; fresh build-only
preparation is separately retained and reviewed before any new run budget.

For another inherited native interface, inspect the actual producer/consumer
boundary together with the copied setup. A shared assumption in source and
synthetic data is not independent evidence of that API's behavior. This correction
does not justify reopening unrelated accepted source findings or broadening the
finite handoff probe into a general VM implementation.
