import subprocess
import time

from testkit import contention


def test_busy_foreign_process_is_seen_and_own_group_is_not():
    p = subprocess.Popen(['python3', '-c', 'while True: pass'], start_new_session=True)
    try:
        time.sleep(2.5)                             # let ps's decaying %cpu rise
        assert any(pid == p.pid for pid, _, _ in contention.sample(root=1 << 30))     # not ours: seen
        assert not any(pid == p.pid for pid, _, _ in contention.sample())           # our child: part of the run
    finally:
        p.kill(); p.wait()


def test_summary_orders_by_peak():
    assert contention.summary({'a': 30.0, 'b': 90.0}) == 'b (90%), a (30%)'
