# Checkpoint C inline segment checks on M3 — November 22, 2026

This experiment ran `compat-tls-natemu` on one Apple M3 in three alternating A/B rounds. A uses the
per-access resident check at natemu `cd1e0427` with FEX `10e9e8c`. B uses the inline dword MOV check at
natemu `0a8aceba` with FEX `b031814`. Each build ran the same warmed user loop at 50,000 iterations and
800,000 accesses per sample, with three timed samples for each segment in each round. All final-word
checks passed.

The four commit ids are Codex's working commits and the fork commits they pinned; none is on a branch. A is the
build with the checked integer reads, immediate stores and ADD plus the two reserved dispatcher tags, B the inline
dword MOV check before it was combined with the XCHG, multiply/divide and BMI checks. The fork's `m2-checkpoint-c`
branch holds A's compiler as `9eab6430`; B's compiler change is its `25a5e35e`.

Times are nanoseconds per access, including the loop's common work. The medians are taken over the
three round medians. The ranges show their spread.

| Segment | A median | A range | B median | B range | Speedup |
| --- | ---: | ---: | ---: | ---: | ---: |
| Flat DS | 0.223 | 0.218–0.229 | 0.218 | 0.218–0.229 | Within the observed spread |
| FS | 183.218 | 182.788–183.810 | 0.842 | 0.825–0.843 | 217.7× |
| GS | 183.386 | 182.333–183.652 | 0.825 | 0.824–0.827 | 222.2× |

The inline path removes the resident transition from each eligible FS or GS access in this dword MOV
loop. Whole-workload and M4 measurements remain separate from this experiment. The six raw framework
records are retained locally under `natemu-checkpoint-c-inline-mov-wload/.natemu/m3/`.

Both builds were replaced on November 23, 2026 by the generic operand check ([the plan](m2-checkpoint-c-plan.md#generated-code-one-generic-check-and-two-identity-bits)),
whose inline sequence is three instructions shorter than B's and applies to every form; its numbers will come from the
bench case `compat-tls-natemu`.
