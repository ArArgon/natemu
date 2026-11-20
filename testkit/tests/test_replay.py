import json
import random

from testkit.sched import policy, replay
from testkit.sched.policy import Config

CFG = Config(p_cores=8, e_cores=4)


def afternoon():
    """One 300-unit run at 0; twelve small runs (1-3 units of 5-60 s) arriving every 30 s after it."""
    rng = random.Random(3)
    runs = [{'id': 'big', 'gate': False, 'units': [{'ask': 0.0, 'dur': rng.uniform(10, 60), 'cores': 1, 'excl': False}
                                                    for _ in range(300)]}]
    for k in range(12):
        runs.append({'id': f's{k}', 'gate': False, 'units': [
            {'ask': 30.0 * (k + 1), 'dur': rng.uniform(5, 60), 'cores': 1, 'excl': False} for _ in range(rng.randint(1, 3))]})
    return runs


def small_delay(runs, monkeypatch=None):
    out = replay.simulate(runs, CFG); replay.delays(runs, CFG, out)
    return replay.summary(out)['small']['mean']


def test_fair_share_cuts_small_runs_delay_against_first_come_first_served(monkeypatch):
    fair = small_delay(afternoon())
    monkeypatch.setattr(policy, 'order_key', lambda r, share, last, cfg, now: (-r.priority, r.asked, r.id))
    fifo = small_delay(afternoon())
    assert fifo > 300 and fair < fifo / 4, (fair, fifo)       # first come first served: they wait for most of 'big'


def test_load_reads_recorded_runs(tmp_path):
    d = tmp_path / '.natemu' / 'runs' / 'r1'; d.mkdir(parents=True)
    (d / 'run.json').write_text(json.dumps({'args': ['--gate']}))
    (d / 'timings.json').write_text(json.dumps({'units': [
        {'asked': 0, 'granted': 1, 'ended': 4, 'cores': 2, 'core_class': 'P'},
        {'asked': 0, 'granted': 5, 'ended': 6, 'cores': 1, 'core_class': 'X'},
        {'asked': 0, 'granted': None, 'ended': None, 'cores': 1, 'core_class': ''}]}))
    runs = replay.load([tmp_path])
    assert runs == [{'id': 'r1', 'gate': True, 'units': [{'ask': 0, 'dur': 3, 'cores': 2, 'excl': False, 'cls': 'P'},
                                                         {'ask': 0, 'dur': 1, 'cores': 1, 'excl': True, 'cls': 'X'}]}]


def test_a_unit_recorded_on_an_efficiency_core_replays_at_its_performance_core_length():
    from testkit.sched.policy import Config
    run = {'id': 'r', 'gate': False, 'units': [{'ask': 0.0, 'dur': 30.0, 'cores': 1, 'excl': False, 'cls': 'E'}]}
    assert replay.simulate([run], Config(p_cores=1, e_cores=1, e_slowdown=3.0))['r']['turnaround'] == 10.0
    assert replay.simulate([run], Config(p_cores=1, e_cores=1))['r']['turnaround'] == 30.0
