"""Scheduling decisions as a pure function (spec R4): claims, priorities, exclusive windows and backfill.

`decide(state, cfg, now)` says which waiting requests to grant now and how the exclusive window changes. It never
blocks and keeps no hidden state, so it is tested with a fake clock (testkit/tests/test_policy.py). Rules:

1. While an exclusive grant is held, nothing else is granted.
2. Exclusive requests queue by priority, then the fair-share order (7). A window *opens* only on a drained machine; inside it exclusive
   requests run one after another until the queue is empty or the window budget is spent (a single request longer than
   the budget still runs alone). Then the window closes and shared work gets at least `min_shared` seconds before the
   next window may become pending (unless no shared work is waiting).
3. While a window is pending (exclusive work queued, the machine draining), a shared request is granted only if its
   expected duration is known and it ends by the drain horizon: the latest expected end of the shared work running.
4. Shared requests by priority, then the fair-share order (7); strict: one that cannot be granted now blocks those after it of the same or
   lower priority, so a large claim is not starved by small ones.
5. `perf` requests use performance cores only; others use performance cores if free, else efficiency cores.
6. A request no core class can ever hold is unsatisfiable; it is never granted (the caller reports it not runnable).
7. Fair share between runs: within a priority, requests of the run that has had the least machine time so far
   (core-seconds, held grants counted up to now, an exclusive grant as the whole machine) go first, then by age. So
   a run of six thousand units submitted a second earlier no longer holds back a run of one. Shares grow as a decision
   hands out cores, so one run does not take every core that frees at once. A run that has had no grant for
   `starve_after` seconds (since its last grant, or since it asked) goes to the front of its priority, so a stream
   of new small runs cannot starve an old large one, while a large run being served keeps its fair share.
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
    run: str = ''                   # the run it belongs to (fair share); '' = the id's prefix before ':', else its own
    light: bool = False             # waits on another machine (a remote container build): granted at once, as class 'L',
                                    # and never blocks or waits for an exclusive window


@dataclasses.dataclass
class Grant:
    request: Request
    start: float
    core_class: str             # 'P', 'E', 'X' (the whole machine) or 'L' (light: waits on another machine)


@dataclasses.dataclass
class Config:
    p_cores: int
    e_cores: int
    window_budget: float = 600.0
    min_shared: float = 120.0
    starve_after: float = 1800.0


@dataclasses.dataclass
class State:
    held: list = dataclasses.field(default_factory=list)          # [Grant]
    waiting: list = dataclasses.field(default_factory=list)       # [Request]
    window_open_since: float | None = None
    last_window_closed: float | None = None
    served: dict = dataclasses.field(default_factory=dict)       # run -> core-seconds of finished grants
    last_grant: dict = dataclasses.field(default_factory=dict)   # run -> time of its latest grant (rule 7's starvation)


@dataclasses.dataclass
class Decision:
    grants: list                    # [(Request, core_class)]
    window_open_since: float | None
    last_window_closed: float | None


def unsatisfiable(r: Request, cfg: Config) -> bool:
    if r.exclusive: return False
    return r.cores > cfg.p_cores if r.perf else r.cores > max(cfg.p_cores, cfg.e_cores)


def run_of(r: Request) -> str:
    """The request's run: its `run`, else the prefix of an id written `<run>:<unique>` (ids survive every version of
    the pool file), else the id itself."""
    return r.run or (r.id.split(':', 1)[0] if ':' in r.id else r.id)


def cost(g: Grant, cfg: Config) -> float:
    """Cores a grant takes: an exclusive one takes the whole machine."""
    return float(cfg.p_cores + cfg.e_cores) if g.core_class == 'X' else float(g.request.cores)


def shares(state: State, cfg: Config, now: float) -> dict:
    """Core-seconds per run: finished grants, plus each held grant at its expected length (charged when it starts, as
    fair queuing does, so a run that has just been granted many units is not still the cheapest) or its time so far
    when that is longer or the length is unknown."""
    out = dict(state.served)
    for g in state.held:
        t = max(now - g.start, g.request.expected or 0.0)
        out[run_of(g.request)] = out.get(run_of(g.request), 0.0) + max(0.0, t) * cost(g, cfg)
    return out


def order_key(r: Request, share: dict, last: dict, cfg: Config, now: float):
    """Rule 7: priority, then (unless its run is starved: no grant for starve_after) the run's share, then age."""
    starved = now - last.get(run_of(r), r.asked) >= cfg.starve_after
    return (-r.priority, 0 if starved else 1, share.get(run_of(r), 0.0), r.asked, r.id)


def granted(state: State, r: Request, now: float) -> None:
    """Book-keeping a holder of the state does for each grant it applies (rule 7's starvation clock)."""
    state.last_grant[run_of(r)] = now


def window_pending(state: State, cfg: Config, now: float) -> bool:
    """Exclusive work is queued and the machine should drain for it."""
    if state.window_open_since is not None: return False
    if not any(r.exclusive for r in state.waiting): return False
    shared_waiting = any(not r.exclusive and not unsatisfiable(r, cfg) for r in state.waiting)
    return state.last_window_closed is None or not shared_waiting or now - state.last_window_closed >= cfg.min_shared


def decide(state: State, cfg: Config, now: float, external_busy: bool = False) -> Decision:
    """`external_busy`: the old runners' VM lock is held elsewhere, or VMs run outside the framework; no window opens.
    Light requests are granted at once and light grants are invisible to every other rule: they wait on another
    machine, so neither an exclusive window nor shared work has to wait for them."""
    light = [(r, 'L') for r in state.waiting if r.light]
    view = dataclasses.replace(state, held=[g for g in state.held if g.core_class != 'L'],
                               waiting=[r for r in state.waiting if not r.light])
    d = _decide(view, cfg, now, external_busy)
    return Decision(light + d.grants, d.window_open_since, d.last_window_closed)


def _decide(state: State, cfg: Config, now: float, external_busy: bool) -> Decision:
    open_since, last_closed = state.window_open_since, state.last_window_closed
    if any(g.core_class == 'X' for g in state.held):
        return Decision([], open_since, last_closed)
    share = shares(state, cfg, now)
    key = lambda r: order_key(r, share, state.last_grant, cfg, now)
    excl = sorted((r for r in state.waiting if r.exclusive), key=key)
    shared_held = [g for g in state.held if g.core_class != 'X']

    if open_since is not None:                                  # between two exclusive tests of an open window
        if excl and now - open_since < cfg.window_budget and not shared_held and not external_busy:
            return Decision([(excl[0], 'X')], open_since, last_closed)
        open_since, last_closed = None, now                     # queue empty, budget spent or the old lock taken: close
    view = State(state.held, state.waiting, open_since, last_closed, state.served, state.last_grant)
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
    cands = [r for r in state.waiting if not r.exclusive and not unsatisfiable(r, cfg)]
    while cands:
        r = min(cands, key=key); cands.remove(r)            # re-ranked after every grant: shares grow as cores go out
        if blocked_at is not None and r.priority <= blocked_at: break
        if pending and (r.expected is None or now + r.expected > horizon):
            blocked_at = r.priority; continue
        cls = next((c for c in (('P',) if r.perf else ('P', 'E')) if used[c] + r.cores <= cap[c]), None)
        if cls is None:
            blocked_at = r.priority; continue
        used[cls] += r.cores
        share[run_of(r)] = share.get(run_of(r), 0.0) + (r.expected if r.expected is not None else 60.0) * r.cores
        grants.append((r, cls))
    return Decision(grants, open_since, last_closed)
