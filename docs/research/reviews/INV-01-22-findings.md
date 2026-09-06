# INV-01-22 — findings review

Reviewer/context: Codex `/root`, independent of matrix author
`/root/mach_native_review`. Date: 2026-09-05. Scope: the four-policy conditional
matrix, definitions, positive toys, six negative witnesses and missing actual
inputs. No experiment, guest admission or selected policy is reviewed here.

Inspected the full submitted matrix and owning investigation/dependencies.
Independently rechecked the primary [Linux 6.12 timekeeping abstractions](https://docs.kernel.org/6.12/timers/timekeeping.html)
and [historical KVM/x86 timekeeping discussion](https://docs.kernel.org/6.12/virt/kvm/x86/timekeeping.html),
and the installed SDK 26.4 `mach/mach_time.h` continuous-clock declarations.
The documentary uses preserve generic versus selected-guest and historical
versus current-behavior boundaries. Mathematical policy assumptions are
explicit and are not attributed to those sources as host guarantees.

Checked each monotonic/common-epoch predicate algebraically, including
0 <= delta S <= delta H, positive fixed a/q and one global E. Recomputed the
four event times (host 5 ms; host 2.5 ms; event E=5; host 9 ms) and counter
ratio 10. Verified all six negative witnesses violate the stated toy
predicate or make the real measurement target unknown. Constant-a rescaling
can recover host elapsed only when that mapping is known; paused/execution
counter examples cannot recover external lease lifetime unaided. Opposing
unit-slope and freeze rows remain alternative contracts, not impossible
simultaneous demands imposed on Linux.

One author return was requested: define the origins of cumulative E and S so
V/P preserve G(H0)=G0. The author added E(H0)=0 and S(H0)=0 within the common
epoch; independently reread and checked all rows against that definition.
This closes the offset ambiguity without changing any toy outcome. The
source author also replaced the now-stale pending-C1 reference with accepted
local claims 27/28; all lifecycle/mapping unknowns remain unchanged.

The actual-input matrix separately retains host/release, n=2/4/8 and each
lifecycle stratum, with no invented guest identity or inferred success.
Its unknowns have existing owners. No complete policy is admitted, ranked or
killed. The precise guest CPU/device/clocksource contract and lifecycle/IO
inputs remain load-bearing work; accepting the matrix cannot close Q22 by
turning all those cells into accepted viability findings.

**Verdict: accept the bounded conditional analysis and its explicit unknowns.**
The algebraic findings are Inferred under the stated assumptions. No
unresolved substantive defect remains in that analysis; actual product
admissibility remains open. No reviewer-authored load-bearing change, native
execution, or new host observation occurred.
