# Executor guide

This guide defines a future workflow only. Organization work does not execute
the workflow. Any execution is conditional on separate research authorization.

## Workflow

When separately authorized, use this sequence as the superset workflow. Apply
only the conditional gates required by the evidence being considered; an
inapplicable gate must be recorded as `not-applicable` with its reason, never
silently skipped:

Select question → breadth pass → question gate → source gate → method proposal → method gate → harness implementation → harness gate → collect raw data → result gate → independent replication → adversarial critique (adversarial gate) → synthesis gate.

The gate names are question, source, method, harness, result, replication,
adversarial, and synthesis. Which gates apply to which evidence, and when an
applicable gate passes, are defined normatively in the
[evidence policy](evidence-policy.md#gate-completion).

The breadth pass must cover product assumptions, adjacent systems, negative and
boundary cases, host/guest matrices, multicore interactions, alternatives,
falsifiers, and harness failure modes. It identifies what must be questioned;
it does not establish findings. Record the breadth pass in an auditable form
and update the question backlog with every newly surfaced issue. If none is
found, record `no new issues found` explicitly.

At the end of every track, perform and record a second breadth pass from
product, architecture, correctness, and methodology perspectives. Its output,
including disagreements and unresolved implications, remains visible to
synthesis and updates the question backlog.

## Independence

An independent reviewer is a different author working from a separately
recorded review context and able to challenge the evidence and method. The
originating author or originating session may not approve any load-bearing
method, harness, result, replication, adversarial, or synthesis review.

Self-review is required at every applicable gate, but it is not a substitute
for required independent review.

Independent replication must independently prepare and review its harness, then
reproduce the claimed result from the approved question and documented method
in its own reviewable execution record. Platform-sensitive load-bearing
observations must be independently replicated across their declared machine/OS
matrix. If that matrix is not completed, the finding retains a narrow
applicability statement and cannot close a platform-wide gate. Adversarial
critique actively seeks counterexamples, hidden assumptions, applicability
failures, and harness defects. A failed review returns the work to the
appropriate earlier step; it does not create a finding by itself.

Eligible findings may enter candidate synthesis after their applicable gates.
The synthesis gate approves, revises, or rejects the final decision packet and
selects one of the outcomes defined in the [research governance](README.md):
stop, continue targeted research, or authorize planning. None of these steps
authorizes implementation.
