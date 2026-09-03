# Executor guide

This guide defines the research workflow.

## Workflow

Use this sequence as the superset workflow. Apply
only the conditional gates required by the evidence being considered; an
inapplicable gate must be recorded as `not-applicable` with its reason, never
silently skipped:

Select question → breadth pass → question gate → source gate → method proposal → method gate → experiment code implementation → code gate → collect raw data → result gate → independent replication → adversarial critique (adversarial gate) → synthesis gate.

The gate names are question, source, method, code, result, replication,
adversarial, and synthesis. Which gates apply to which evidence is defined
normatively in the evidence policy's
[promotion and challenge](evidence-policy.md#promotion-and-challenge) section;
when an applicable gate passes is defined in
[gate completion](evidence-policy.md#gate-completion).

The breadth pass must cover product assumptions, adjacent systems, negative and
boundary cases, host/guest matrices, multicore interactions, alternatives,
falsifiers, and measurement failure modes. It identifies what must be
questioned; it does not establish findings. Record the breadth pass in an
auditable form and update the question backlog with every newly surfaced
issue. If none is found, record `no new issues found` explicitly.

At the end of every track, perform and record a second breadth pass from
product, architecture, correctness, and methodology perspectives. Its output,
including disagreements and unresolved implications, remains visible to
synthesis and updates the question backlog.

## Independence

An independent reviewer is a different author working from a separately
recorded review context and able to challenge the evidence and method. The
originating author or originating session may not approve any load-bearing
method, code, result, replication, adversarial, or synthesis review.

Self-review is required at every applicable gate, but it is not a substitute
for required independent review.

Independent replication must independently prepare and review its own
experiment code, then reproduce the claimed result from the approved question
and documented method in its own reviewable execution record.
A platform-sensitive observation is valid for the host and OS it was measured
on and states that applicability. Replication on further hosts widens the
applicability statement and is done incrementally as machines become
available; it is required only before synthesis relies on the finding as
platform-wide.
Adversarial critique actively seeks counterexamples, hidden assumptions,
applicability failures, and code defects. A failed review returns the work to
the appropriate earlier step; it does not create a finding by itself.

Eligible findings may enter candidate synthesis after their applicable gates.
The synthesis gate approves, revises, or rejects the final decision packet and
selects one of the outcomes defined in the [research governance](README.md):
stop, continue targeted research, or authorize planning. None of these steps
authorizes implementation.
