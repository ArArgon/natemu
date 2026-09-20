# Track 03 retrospective: detailed research displaced the decision

Recorded at the owner's request. Author: `/root`.
This is a separate recurrence after the correction documented in
[architectural framing drift](track03-architecture-drift.md); that earlier record
is preserved as written.

## Systematic mistake

**The failure recurred after the owner's correction.** The assistant restored
the comparison's framing, then let a provisional TCG choice displace the
unfinished comparison again. Detailed work continued, but its connection to the
decision that Track 03 needed to finish was not enforced. Prioritization and
stopping criteria were a major reason the track remained unfinished; missing
Intel hardware was a later dependency of particular tests, not the underlying
cause of the stalled generator comparison.

## Recurrence: detailed research displaced the decision

The systematic mistake was allowing the next discoverable technical uncertainty
to determine the next task. A bounded individual pass was treated as enough
discipline, even when a succession of those passes no longer answered why one
generator should be selected. Calling the choice provisional did not prevent
work allocation and progress reporting from treating it as settled.

Three controls failed together:

- **Selection had no decisive comparison criterion.** The assistant did not
  establish what matched evidence would favor TCG, FEX or Box64 before committing
  most subsequent attention to one combination. Keeping a chosen QEMU frontend
  with TCG explained that pairing, but did not justify choosing it over the
  other coherent frontend/generator combinations.
- **Follow-up work had no effective architectural stopping rule.** Register
  allocation and recovery work expanded into exact floating-point status,
  state-image formats, faults during event delivery, gather progress and rare
  instruction interactions. Some findings were useful or necessary. The error
  was failing to establish which remaining uncertainty could change the design,
  which belonged to implementation conformance, and when to return to selection.
- **Local acceptance displaced completion accountability.** Reviews could
  validate a source fact or conditional interface without resolving the parent
  comparison. The assistant accumulated those results and reported increasingly
  settled direction while FEX's pilot remained unbuilt and Box64 remained at
  source inspection. Uneven investigation was never evidence of inferiority.

The recorded sequence locates the recurrence on September 20, after the framing
correction. In [INV-03-02](../research/investigations/INV-03-02.md), claim29 kept
all three candidates and selected a FEX pilot to gather information; claim35
reached static preparation without a C++ build or native result. Claim46 then
selected the TCG/QEMU combination as a provisional planning policy. The
[Return35 review](../research/reviews/INV-03-02-findings.md#return-35--claim46-component-choice-decision-review)
explicitly identified the circular selection rationale. Qualifying the language
made the policy's limits clearer, but did not complete the comparative work.
Subsequent helper and numerical investigations deepened that selected combination.
The records establish this sequence, not an exact wall-clock pivot or a measured
fraction of effort wasted.

The same mistake later affected blocker classification. An Intel-compatible
virtual CPU profile chosen during research became a reason to insist on physical
Intel observations of ENTER fault effects and MOV SS/ICEBP debug reporting.
Distinguishing AMD/KVM observations from Intel evidence was appropriate; declaring
the whole track blocked on Intel hardware was not adequately justified. The
assistant had not established why those exact ambiguities invalidated an
architectural choice or required Linux compatibility, rather than calling for a
documented semantic policy or later validation. The owner's requirement was
x86-64 Linux compatibility, not an exact physical Intel replica. The later
AMD/KVM run supplied useful exploratory results without resolving that distinction.

**Correction:** before another follow-up, state the decision it can change and
the consequence of each plausible outcome. If neither outcome changes component
selection or the required interface, retain the uncertainty with its proper
implementation/validation owner instead of automatically blocking architecture
closure. If it can invalidate the design, explain that dependency and investigate
it. Apply the same criterion and burden of evidence to every generator. This does
not permit ignoring known incompatibilities or declaring unanswered behavior
correct. It requires a reason for the depth and priority of the work.

The immediate unfinished obligation is still comparative justification of the
generator choice. More isolated TCG audits cannot discharge it. The owner should
receive progress in terms of decisions resolved and consequential uncertainties
remaining, with provisional policy clearly separated from demonstrated preference.

## Documentation handling

The first attempt to record this recurrence inserted it into the earlier
retrospective and revised that record's assessment of the cleanup. That conflated
a shared failure pattern with a shared incident and changed the earlier account.
At the owner's request, those edits were reverted and this separate record was
created. Related incidents should be linked; recurrence is evidence that a prior
correction was insufficient, not a reason to rewrite its history.

## Status

The mistake is documented, not resolved by documentation. Comparative justification
and reassessment of architecture blockers remain technical work in the owning
investigations. This record changes no source finding or acceptance verdict.
