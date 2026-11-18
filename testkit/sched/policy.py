"""Scheduling decisions as a pure function (spec R4): claims, priorities, exclusive windows and backfill.

`decide(state, cfg, now)` says which waiting requests to grant now and how the exclusive window changes. It never
blocks and keeps no hidden state, so it is tested with a fake clock (testkit/tests/test_policy.py). Rules:

1. While an exclusive grant is held, nothing else is granted.
2. Exclusive requests queue by priority, then age. A window *opens* only on a drained machine; inside it exclusive
   requests run one after another until the queue is empty or the window budget is spent (a single request longer than
   the budget still runs alone). Then the window closes and shared work gets at least `min_shared` seconds before the
   next window may become pending (unless no shared work is waiting).
3. While a window is pending (exclusive work queued, the machine draining), a shared request is granted only if its
   expected duration is known and it ends by the drain horizon: the latest expected end of the shared work running.
4. Shared requests by priority, then age; strict: one that cannot be granted now blocks those after it of the same or
   lower priority, so a large claim is not starved by small ones.
5. `perf` requests use performance cores only; others use performance cores if free, else efficiency cores.
6. A request no core class can ever hold is unsatisfiable; it is never granted (the caller reports it not runnable).
"""
import dataclasses
import math

INTERACTIVE, GATE = 10, 0


@dataclasses.dataclass
class Request:
    id: str
    owner_pid: int
    cores: int = 1
    exclusive: bool = False
    perf: bool = False
    priority: int = INTERACTIVE
    asked: float = 0.0
    expected: float | None = None
    label: str = ''
    worktree: str = ''


@dataclasses.dataclass
class Grant:
    request: Request
    start: float
    core_class: str             # 'P', 'E' or 'X' (the whole machine)


@dataclasses.dataclass
class Config:
    p_cores: int
    e_cores: int
    window_budget: float = 600.0
    min_shared: float = 120.0


@dataclasses.dataclass
class State:
    held: list = dataclasses.field(default_factory=list)          # [Grant]
    waiting: list = dataclasses.field(default_factory=list)       # [Request]
    window_open_since: float | None = None
    last_window_closed: float | None = None


@dataclasses.dataclass
class Decision:
    grants: list                    # [(Request, core_class)]
    window_open_since: float | None
    last_window_closed: float | None


def unsatisfiable(r: Request, cfg: Config) -> bool:
    if r.exclusive: return False
    return r.cores > cfg.p_cores if r.perf else r.cores > max(cfg.p_cores, cfg.e_cores)


def _order(r: Request):
    return (-r.priority, r.asked, r.id)


def window_pending(state: State, cfg: Config, now: float) -> bool:
    """Exclusive work is queued and the machine should drain for it."""
    if state.window_open_since is not None: return False
    if not any(r.exclusive for r in state.waiting): return False
    shared_waiting = any(not r.exclusive and not unsatisfiable(r, cfg) for r in state.waiting)
    return state.last_window_closed is None or not shared_waiting or now - state.last_window_closed >= cfg.min_shared


def decide(state: State, cfg: Config, now: float, external_busy: bool = False) -> Decision:
    """`external_busy`: the old runners' VM lock is held elsewhere, or VMs run outside the framework; no window opens."""
    open_since, last_closed = state.window_open_since, state.last_window_closed
    if any(g.core_class == 'X' for g in state.held):
        return Decision([], open_since, last_closed)
    excl = sorted((r for r in state.waiting if r.exclusive), key=_order)
    shared_held = [g for g in state.held if g.core_class != 'X']

    if open_since is not None:                                  # between two exclusive tests of an open window
        if excl and now - open_since < cfg.window_budget and not shared_held:
            return Decision([(excl[0], 'X')], open_since, last_closed)
        open_since, last_closed = None, now                     # queue empty or budget spent: close
    view = State(state.held, state.waiting, open_since, last_closed)
    pending = window_pending(view, cfg, now)
    if pending and not shared_held and not external_busy:       # drained: open a window
        return Decision([(excl[0], 'X')], now, last_closed)
    if pending and external_busy and not shared_held:
        pending = False             # the window waits for something outside the pool; do not starve shared work meanwhile

    horizon = max((g.start + (g.request.expected if g.request.expected is not None else math.inf) for g in shared_held),
                  default=now)
    used = {'P': sum(g.request.cores for g in shared_held if g.core_class == 'P'),
            'E': sum(g.request.cores for g in shared_held if g.core_class == 'E')}
    cap = {'P': cfg.p_cores, 'E': cfg.e_cores}
    grants, blocked_at = [], None
    for r in sorted((r for r in state.waiting if not r.exclusive and not unsatisfiable(r, cfg)), key=_order):
        if blocked_at is not None and r.priority <= blocked_at: break
        if pending and (r.expected is None or now + r.expected > horizon):
            blocked_at = r.priority; continue
        cls = next((c for c in (('P',) if r.perf else ('P', 'E')) if used[c] + r.cores <= cap[c]), None)
        if cls is None:
            blocked_at = r.priority; continue
        used[cls] += r.cores
        grants.append((r, cls))
    return Decision(grants, open_since, last_closed)
