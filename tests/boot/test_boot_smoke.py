"""The `startup` boots of tests/boot/run_boot_smoke.py (case `boot-startup`): the script's fields are read from serial
without the kernel's echo of its command line, and the judge refuses what a boot must not show."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)

import run_boot_smoke as smoke


def test_cpu_lists_are_linux_s_form():
    assert smoke.cpu_list(range(4)) == '0-3'
    assert smoke.cpu_list({0, 2, 3}) == '0,2-3'
    assert smoke.cpu_list({0, 1, 3}) == '0-1,3'
    assert smoke.cpu_list({0}) == '0'


def test_the_command_line_echo_gives_no_field():
    echo = f'[    0.000000] Command line: {smoke.STARTUP_CMDLINE}\n[    0.1] Kernel command line: {smoke.STARTUP_CMDLINE}\n'
    assert smoke.startup_fields(echo) == {}
    assert smoke.startup_fields(echo + 'natemu-cpus=2 natemu-online=0-1\n[    2.7] x\nnatemu-off-1=refused\n') == \
        {'cpus': '2', 'online': '0-1', 'off-1': 'refused'}


def fields(n, mailbox):
    f = {'cpus': str(n), 'online': smoke.cpu_list(range(n)), 'cpus-end': str(n), 'script': 'done',
         'warm-reset': str((n - 1) * (1 if mailbox else 2))}
    for c in range(1, n):
        f[f'off-{c}'] = 'refused' if mailbox else smoke.cpu_list(set(range(n)) - {c})
        f[f'on-{c}'] = smoke.cpu_list(range(n))
    return f


def report(n, waiting='halted'):
    return {'stop': 'poweroff', 'vcpus': [{'stop': 'Power(1)'}] + [{'stop': 'Cancelled', 'waiting': waiting}] * (n - 1)}


def test_the_judge_passes_a_good_boot_and_refuses_each_wrong_field():
    for n in (2, 4):
        for mailbox in (True, False):
            assert smoke.startup_judge(n, mailbox, report(n), fields(n, mailbox)) == []
            for k in fields(n, mailbox):
                bad = dict(fields(n, mailbox), **{k: 'x'})
                assert smoke.startup_judge(n, mailbox, report(n), bad), k
    # Offlining with a mailbox, or not without one; a CPU missing; a shutdown that ends otherwise or leaves an AP waiting.
    assert smoke.startup_judge(2, True, report(2), fields(2, False))
    assert smoke.startup_judge(2, False, report(2), fields(2, True))
    assert smoke.startup_judge(4, False, report(4), dict(fields(4, False), cpus='3'))
    assert smoke.startup_judge(2, False, dict(report(2), stop='cancelled'), fields(2, False))
    assert smoke.startup_judge(2, False, report(2, waiting='sipi'), fields(2, False))
