import itertools
import math

from hypothesis import given, settings, strategies as st

from testkit.sched.policy import (Config, Grant, Request, State, cost, decide, granted, run_of, shares, unsatisfiable,
                                  GATE, INTERACTIVE)

CFG = Config(p_cores=4, e_cores=2, window_budget=100, min_shared=20)
_ids = itertools.count()


def req(**kw):
    kw.setdefault('id', f'r{next(_ids)}'); kw.setdefault('owner_pid', 1)
    return Request(**kw)


def simulate(reqs, durations, cfg=CFG, horizon=1e6):
    """Advance a fake clock event by event. Returns the list of (request, start, end, class)."""
    pending = sorted(reqs, key=lambda r: r.asked)
    st_ = State()
    now, done, running = 0.0, [], []       # running: (end, Grant)
    while pending or st_.waiting or running:
        while pending and pending[0].asked <= now: st_.waiting.append(pending.pop(0))
        d = decide(st_, cfg, now)
        st_.window_open_since, st_.last_window_closed = d.window_open_since, d.last_window_closed
        for r, cls in d.grants:
            st_.waiting.remove(r)
            g = Grant(r, now, cls); st_.held.append(g); running.append((now + durations[r.id], g)); granted(st_, r, now)
        nxt = [e for e, _ in running] + ([pending[0].asked] if pending else [])
        if d.grants: continue                  # decide again at the same instant
        if not nxt: break                      # only unsatisfiable requests left
        now = min(nxt)
        if now > horizon: raise AssertionError('no progress')
        for e, g in [x for x in running if x[0] <= now]:
            running.remove((e, g)); st_.held.remove(g); done.append((g.request, g.start, e, g.core_class))
            rid = run_of(g.request); st_.served[rid] = st_.served.get(rid, 0.0) + (e - g.start) * cost(g, cfg)
    return done, st_


def overlaps(a, b):
    return a[1] < b[2] and b[1] < a[2]


def check_invariants(done, cfg=CFG):
    for a, b in itertools.combinations(done, 2):
        if overlaps(a, b): assert not (a[3] == 'X' or b[3] == 'X'), (a, b)
    points = sorted({t for x in done for t in (x[1], x[2])})
    for t0, t1 in zip(points, points[1:]):
        mid = (t0 + t1) / 2
        act = [x for x in done if x[1] <= mid < x[2]]
        assert sum(x[0].cores for x in act if x[3] == 'P') <= cfg.p_cores
        assert sum(x[0].cores for x in act if x[3] == 'E') <= cfg.e_cores


def test_exclusive_never_overlaps():
    rs = [req(cores=2, expected=10), req(exclusive=True, expected=5), req(cores=2, expected=10)]
    done, _ = simulate(rs, {r.id: 10 if not r.exclusive else 5 for r in rs})
    check_invariants(done)
    assert len(done) == 3


def test_window_batches_exclusives():
    shared = req(cores=1, expected=30)
    ex = [req(exclusive=True, asked=1, expected=5) for _ in range(3)]
    done, _ = simulate([shared] + ex, {shared.id: 30, **{r.id: 5 for r in ex}})
    starts = sorted(x[1] for x in done if x[3] == 'X')
    assert starts == [30, 35, 40]          # one drain, then back to back


def test_window_budget_closes():
    ex = [req(exclusive=True, expected=60) for _ in range(3)]
    late = req(cores=1, asked=1, expected=1)
    done, _ = simulate(ex + [late], {**{r.id: 60 for r in ex}, late.id: 1})
    by = {x[0].id: x for x in done}
    assert by[late.id][1] == 120            # window closed after its 100 s budget (two tests), shared ran
    assert by[ex[2].id][1] == 121           # no shared work waiting: the next window opens at once


def test_min_shared_interval_after_window():
    ex1 = req(exclusive=True, expected=10)
    sh = [req(cores=4, asked=1, expected=50)]
    ex2 = req(exclusive=True, asked=2, expected=10)
    done, _ = simulate([ex1, ex2] + sh, {ex1.id: 10, ex2.id: 10, sh[0].id: 50})
    by = {x[0].id: x for x in done}
    # the window holds ex1 and ex2 back to back (both queued when it ran); shared runs after it
    assert by[sh[0].id][1] >= by[ex2.id][2] or by[ex2.id][1] >= by[sh[0].id][2]
    check_invariants(done)


def test_backfill_only_short_known():
    long_ = req(cores=1, expected=100)
    ex = req(exclusive=True, asked=1, expected=5)
    short = req(cores=1, asked=2, expected=10)
    unknown = req(cores=1, asked=3, expected=None)
    toolong = req(cores=1, asked=4, expected=200)
    done, _ = simulate([long_, ex, short, unknown, toolong], {long_.id: 100, ex.id: 5, short.id: 10, unknown.id: 1, toolong.id: 200})
    by = {x[0].id: x for x in done}
    assert by[short.id][1] == 2              # backfilled: ends before the drain horizon (100)
    assert by[unknown.id][1] >= by[ex.id][2] and by[toolong.id][1] >= by[ex.id][2]
    assert by[ex.id][1] == 100


def test_priority_before_age():
    blocker = req(cores=4, expected=10)
    old = req(cores=4, asked=1, priority=GATE, expected=10)
    new = req(cores=4, asked=2, priority=INTERACTIVE, expected=10)
    done, _ = simulate([blocker, old, new], {blocker.id: 10, old.id: 10, new.id: 10})
    by = {x[0].id: x for x in done}
    assert by[new.id][1] == 10 and by[old.id][1] == 20


def test_fifo_large_claim_not_starved():
    first = req(cores=3, expected=10)
    big = req(cores=4, asked=1, perf=True, expected=10)
    smalls = [req(cores=1, asked=2 + i, perf=True, expected=10) for i in range(5)]
    done, _ = simulate([first, big] + smalls, {r.id: 10 for r in [first, big] + smalls})
    by = {x[0].id: x for x in done}
    assert all(by[s.id][1] >= by[big.id][1] for s in smalls)


def test_e_cores_for_non_perf_only():
    p = req(cores=4, perf=True, expected=10)
    e = req(cores=2, asked=1, expected=10)
    perf2 = req(cores=1, asked=2, perf=True, expected=10)
    done, _ = simulate([p, e, perf2], {r.id: 10 for r in (p, e, perf2)})
    by = {x[0].id: x for x in done}
    assert by[e.id][3] == 'E' and by[e.id][1] == 1
    assert by[perf2.id][3] == 'P' and by[perf2.id][1] == 10


def test_unsatisfiable():
    assert unsatisfiable(req(cores=5, perf=True), CFG)
    assert not unsatisfiable(req(cores=4, perf=True), CFG)
    assert unsatisfiable(req(cores=5), CFG)
    assert not unsatisfiable(req(exclusive=True, cores=99), CFG)


workload = st.lists(st.tuples(st.integers(1, 6), st.booleans(), st.booleans(), st.integers(1, 300),
                              st.sampled_from([GATE, INTERACTIVE]), st.integers(0, 500), st.booleans()),
                    min_size=1, max_size=40)


@settings(max_examples=300, deadline=None)
@given(workload)
def test_random_workloads_invariants(w):
    rs, durs = [], {}
    for cores, excl, perf, dur, prio, asked, known in w:
        r = req(cores=cores, exclusive=excl and asked % 5 == 0, perf=perf, priority=prio, asked=float(asked),
                expected=float(dur) if known else None)
        rs.append(r); durs[r.id] = dur
    done, final = simulate(rs, durs)
    check_invariants(done)
    satisfiable = [r for r in rs if not unsatisfiable(r, CFG)]
    assert {x[0].id for x in done} == {r.id for r in satisfiable}           # nothing waits forever
    assert all(unsatisfiable(r, CFG) for r in final.waiting)


def test_external_busy_keeps_window_closed():
    st_ = State(waiting=[req(exclusive=True, expected=5)])
    assert decide(st_, CFG, 0.0, external_busy=True).grants == []
    assert decide(st_, CFG, 0.0).grants[0][1] == 'X'


def test_external_busy_does_not_starve_shared():
    st_ = State(waiting=[req(exclusive=True, expected=5), req(cores=1, expected=None), req(cores=1, expected=30)])
    g = decide(st_, CFG, 0.0, external_busy=True).grants
    assert len(g) == 2 and all(cls != 'X' for _, cls in g)



# --- fair share between runs (rule 7) ------------------------------------------------------------------------------

def test_small_run_is_not_held_back_by_a_large_one():
    """A run of 100 units asked first must not make a 1-unit run asked a second later wait for all of it."""
    big = [req(asked=0, run='big') for _ in range(100)]
    small = req(asked=1, run='small')
    done, _ = simulate(big + [small], {r.id: 10 for r in big + [small]})
    start = next(s for r, s, _, _ in done if r.id == small.id)
    assert start <= 10                              # the first slot that frees up (6 slots, 10 s units)


def test_two_equal_runs_share_the_machine():
    a = [req(asked=0, run='a') for _ in range(30)]
    b = [req(asked=1, run='b') for _ in range(30)]
    done, _ = simulate(a + b, {r.id: 10 for r in a + b})
    end = {k: max(e for r, _, e, _ in done if r.run == k) for k in 'ab'}
    assert abs(end['a'] - end['b']) <= 20           # interleaved: both finish near the end, not one after the other


def test_a_stream_of_new_runs_cannot_starve_an_old_one():
    cfg = Config(p_cores=1, e_cores=0, window_budget=100, min_shared=20, starve_after=30)
    old = [req(asked=0, run='old') for _ in range(3)]
    stream = [req(asked=1 + 5 * k, run=f'new{k}') for k in range(40)]
    done, _ = simulate(old + stream, {r.id: 10 for r in old + stream}, cfg)
    starts = sorted(s for r, s, _, _ in done if r.run == 'old')
    assert starts[1] <= 40                          # waited past starve_after (30 s): first in line after that


def test_exclusive_time_counts_as_the_whole_machine():
    g = Grant(req(exclusive=True, run='x'), 0.0, 'X')
    assert shares(State(held=[g]), CFG, 10.0)['x'] == 10.0 * (CFG.p_cores + CFG.e_cores)
    h = Grant(req(run='y', expected=50.0), 0.0, 'P')
    assert shares(State(held=[h]), CFG, 10.0)['y'] == 50.0          # charged at its expected length from the start


def test_fair_share_keeps_the_invariants_on_random_runs():
    import random
    rng = random.Random(7)
    reqs = [req(asked=rng.uniform(0, 200), run=f'run{rng.randint(0, 5)}', cores=rng.randint(1, 3),
                exclusive=rng.random() < 0.1, priority=rng.choice([GATE, INTERACTIVE])) for _ in range(150)]
    done, _ = simulate(reqs, {r.id: rng.uniform(1, 40) for r in reqs})
    assert len(done) == len(reqs)
    check_invariants(done)



def test_a_long_run_keeps_its_fair_share_after_starve_after():
    """Review: starvation by request age put every unit of a run older than starve_after first, ending fair share.
    Starvation is per run, since its last grant: a large run being served stays behind a newcomer."""
    cfg = Config(p_cores=4, e_cores=0, window_budget=100, min_shared=20, starve_after=30)
    big = [req(asked=0, run='big') for _ in range(200)]
    late = req(asked=300, run='late')
    done, _ = simulate(big + [late], {**{r.id: 10 for r in big}, late.id: 10}, cfg)
    start = next(s for r, s, _, _ in done if r.id == late.id)
    assert start - late.asked <= 10                       # the next free slot, not behind the big run's backlog


def test_cores_that_free_together_are_shared_between_runs():
    """Review: one decision used to give every free core to the run with the lowest share."""
    a = [req(asked=0, run='a', expected=10) for _ in range(10)]
    b = [req(asked=0, run='b', expected=10) for _ in range(10)]
    d = decide(State(waiting=a + b), CFG, 0)
    got = [r.run for r, _ in d.grants]
    assert len(got) == CFG.p_cores + CFG.e_cores and abs(got.count('a') - got.count('b')) <= 1


def test_an_open_window_closes_while_the_old_lock_is_held():
    """Review: a grant given back inside an open window was granted again at once, blocking shared work."""
    x = [req(exclusive=True, run='x') for _ in range(2)]
    s = req(run='s')
    st = State(waiting=x + [s], window_open_since=0.0)
    d = decide(st, CFG, 1.0, external_busy=True)
    assert d.window_open_since is None and [r.id for r, _ in d.grants] == [s.id]


def test_the_run_survives_in_the_request_id():
    """Older pool code drops the `runs` map; ids written `<run>:<unique>` keep the run."""
    assert run_of(Request(id='20261118-x:abc', owner_pid=1)) == '20261118-x'
    assert run_of(Request(id='abc', owner_pid=1)) == 'abc'


def test_light_grants_neither_block_nor_wait_for_an_exclusive_window():
    """A preparation waiting on the x86 builder (light) must not keep the machine from draining, and is granted even
    while an exclusive test runs (seen: two remote image builds held every exclusive test and all shared work for
    15 minutes)."""
    st_ = State(held=[Grant(req(light=True, label='prepare'), 0.0, 'L')], waiting=[req(exclusive=True, expected=10)])
    d = decide(st_, CFG, 100.0)
    assert [c for _, c in d.grants] == ['X']                      # the window opens despite the light grant
    st_.waiting = [req(light=True)]
    st_.held.append(Grant(req(exclusive=True), 100.0, 'X'))
    d = decide(st_, CFG, 101.0)
    assert [c for _, c in d.grants] == ['L']                      # granted while the exclusive test runs
    st_ = State(held=[Grant(req(light=True), 0.0, 'L') for _ in range(8)], waiting=[req(cores=4, perf=True)])
    assert [c for _, c in decide(st_, CFG, 1.0).grants] == ['P']  # light grants take no cores
