# Architecture options

This track is a roadmap only. It defines how separately authorized research
must compare the architecture families that survive
[Feasibility](01-feasibility.md); it records no findings and makes no design
decisions. Work on this track runs only under separate research authorization
and follows the [research governance](../README.md), the
[evidence policy](../evidence-policy.md), and the
[executor guide](../executor-guide.md).

## Comparison dimensions

A surviving option is an architecture family that Feasibility did not kill
and whose rejection this track has not accepted; "option" here and in the
later tracks means such a family. Every surviving option must be evaluated
on every dimension below. Each dimension becomes questions in the
[question backlog](../question-backlog.md), resolved with labelled evidence
through the applicable gates (see
[gate completion](../evidence-policy.md#gate-completion)):

- **Translator approach.** Which translation or interpretation strategy the
  option uses, and what evidence bounds its correctness and cost.
- **Host/guest partitioning.** How responsibility is split between host
  facilities and emulated components, and which platform facts from
  Feasibility each split depends on.
- **MMU strategy.** How guest address translation, permissions, and
  invalidation are represented, and what the candidate mechanisms cost.
- **Code-cache strategy.** How translated code is stored, indexed, shared,
  and invalidated, including under self-modifying and newly mapped code.
- **Interrupt and device models.** How asynchronous events and the scoped
  device set from [Scope and success](00-scope-and-success.md) are delivered,
  and at what latency budget.
- **Multicore implications.** Whether and how the option supports SMP
  execution, cross-vCPU events, and the guest memory model, per the
  correctness surface in [Correctness](02-correctness.md).
- **Dependencies and licensing.** Which external components the option
  requires and whether each is license-compatible with intended distribution.
- **Maintenance and portability.** What sustained effort the option demands
  and how it survives host platform evolution across the supported envelope.
- **Correctness burden.** How much of the correctness surface the option must
  implement itself versus inherit, and how that burden is verified.
- **Failure modes and falsifiers.** How the option can fail, which
  observations would falsify its viability, and how each falsifier is tested
  before the option is preferred.

## Required output

The track's output is a recommendation of at most one option family for
synthesis, plus a rejected-option record giving, for every rejected option,
the reasons, the evidence relied on, and the falsifiers that would reopen the
rejection. The comparison stays at the option-family level: planning owns
implementation details, and no component design, API choice, or code
structure belongs in this track.

## Exit criteria

The track is complete when every surviving option has an accepted evaluation
on every dimension, the recommendation and rejected-option record have passed
independent review, the end-of-track breadth pass is recorded, and all
surfaced questions are filed in the question backlog. The output feeds
[Research synthesis](06-research-synthesis.md); it authorizes nothing by
itself.
