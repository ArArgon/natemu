# Executor guide

This guide defines a future workflow only. Organization work does not execute
the workflow. Any execution is conditional on separate research authorization.

## Workflow

When separately authorized, follow this sequence without skipping a gate:

Select question → breadth pass → question gate → source review → method proposal → method gate → harness implementation → harness gate → collect raw data → result review → independent replication → adversarial critique → synthesis review.

The breadth pass must cover product assumptions, adjacent systems, negative and
boundary cases, host/guest matrices, multicore interactions, alternatives,
falsifiers, and harness failure modes. It identifies what must be questioned;
it does not establish findings.

The end of every track must include perspectives from product, architecture,
correctness, and methodology. Their disagreements and unresolved implications
remain visible to synthesis.

## Independence

An independent reviewer is a different author working from a separately
recorded review context and able to challenge the evidence and method. The
originating author or originating session may not approve any load-bearing
method, harness, result, replication, adversarial, or synthesis review.

Independent replication must reproduce the claimed result from the approved
question and documented method, using its own reviewable execution record.
Adversarial critique actively seeks counterexamples, hidden assumptions,
applicability failures, and harness defects. A failed review returns the work to
the appropriate earlier step; it does not create a finding by itself.

Only the synthesis review selects one of the outcomes defined in the
[research governance](README.md): stop, continue targeted research, or
authorize planning. None of these steps authorizes implementation.
