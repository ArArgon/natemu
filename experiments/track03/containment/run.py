#!/usr/bin/env python3
"""Snapshot/build only by default; --execute requires independent run approval."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import select
import shutil
import signal
import socket
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parent
REPO = SOURCE.parents[2]

def child_limits(cpu_limit=True):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if cpu_limit:
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

def stop(child):
    if child is not None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()

def positive_ok(cell):
    rows = [json.loads(line) for line in cell['output'].splitlines() if line.strip()]
    d = {r['op']: r for r in rows if 'op' in r}
    if cell['returncode'] != 0 or cell['timed_out']:
        return False
    required = ['sandbox_before', 'own_pwrite', 'own_pread', 'own_fullfsync',
                'channel_read', 'channel_write', 'hvf_dlopen', 'anonymous_ram',
                'timer_offset_value', 'timer_mask_value', 'guest_exit', 'guest_x0',
                'p_hv_vm_destroy()', 'p_hv_vcpu_destroy(cpu)']
    ok = all(k in d for k in required)
    if ok:
        ok = d['sandbox_before']['rc'] == 0 and d['own_pwrite']['rc'] == d['own_pwrite']['value'] == 16
        ok &= d['own_pread']['rc'] == d['own_pwrite']['value'] and d['own_pread']['value'] == 1
        ok &= d['channel_read']['rc'] == 1 and d['channel_read']['value'] == 1
        ok &= d['channel_write']['rc'] == 1 and cell['channel_reply'] == '42'
        ok &= all(d[k]['rc'] == 0 for k in ['own_fullfsync', 'hvf_dlopen', 'anonymous_ram',
                                           'timer_offset_value', 'timer_mask_value', 'guest_exit', 'guest_x0'])
        ok &= all(r['rc'] == 0 for r in rows if r.get('op', '').startswith('p_hv_'))
        if cell['mode'] != 'control':
            ok &= d.get('sandbox_init', {}).get('rc') == 0 and d.get('sandbox_after', {}).get('rc', 0) > 0
    return bool(ok)

def control_ok(cell):
    d = {r['op']: r for r in map(json.loads, cell['output'].splitlines()) if 'op' in r}
    return (positive_ok(cell) and cell.get('peer_signal_observed') and
            all(d.get(k, {}).get('rc') == 0 for k in
                ['peer_open_rw', 'peer_signal', 'new_loopback_connection']))

def assess(cells):
    """Pure output classifier; missing/invalid controls never become a policy PASS."""
    conclusions = []
    for repeat in range(2):
        group = {c['mode']: c for c in cells if c['repeat'] == repeat}
        if set(group) != {'control', 'before', 'after'}:
            conclusions.append({'repeat': repeat, 'result': 'incomplete'})
            continue
        parsed = {}
        for mode, cell in group.items():
            rows = [json.loads(line) for line in cell['output'].splitlines() if line.strip()]
            parsed[mode] = {r['op']: r for r in rows if 'op' in r}
            if cell['returncode'] != 0 or cell['timed_out']:
                conclusions.append({'repeat': repeat, 'mode': mode, 'result': 'execution_incomplete'})
                continue
            ok = positive_ok(cell)
            conclusions.append({'repeat': repeat, 'mode': mode, 'positive_operations': 'pass' if ok else 'fail'})
        for operation in ['peer_open_rw', 'peer_signal', 'peer_task_for_pid', 'new_loopback_connection']:
            baseline = parsed.get('control', {}).get(operation)
            for mode in ['before', 'after']:
                observed = parsed.get(mode, {}).get(operation)
                if (baseline is None or baseline['rc'] != 0 or
                    (operation == 'peer_signal' and not group['control'].get('peer_signal_observed')) or
                    (operation == 'peer_task_for_pid' and not baseline.get('value'))):
                    verdict = 'baseline_denied_or_missing_no_policy_evidence'
                elif observed is None:
                    verdict = 'missing'
                elif observed['rc'] == 0 or (operation == 'peer_signal' and group[mode].get('peer_signal_observed')):
                    verdict = 'unauthorized_operation_succeeded'
                elif operation == 'peer_task_for_pid' or observed['errno'] in [1, 13]:
                    verdict = 'denied_with_positive_control'
                else:
                    verdict = 'other_failure_not_denial_evidence'
                conclusions.append({'repeat': repeat, 'mode': mode, 'operation': operation, 'result': verdict})
    return conclusions

def launch_ok(cell):
    d = {r['op']: r for r in map(json.loads, cell['output'].splitlines()) if 'op' in r}
    names = ['launch_child_bootstrap', 'launch_child_registered', 'launch_child_exceptions',
                 'launch_child_thread_exceptions']
    return (all(d.get(k, {}).get('rc') == 0 for k in names) and
            (cell['launch_context'] == 'ordinary' or all(d[k]['value'] == 0 for k in names)))

def assess_launch(cells):
    result = []
    if len(cells) != 3:
        return [{'result': 'incomplete_launch_comparison'}]
    baseline = {r['op']: r for r in map(json.loads, cells[0]['output'].splitlines()) if 'op' in r}
    for cell in cells:
        result.append({'mode': cell['mode'], 'context': cell['launch_context'],
                       'positive_operations': 'pass' if positive_ok(cell) and launch_ok(cell) else 'fail'})
        if cell['mode'] == 'control':
            continue
        d = {r['op']: r for r in map(json.loads, cell['output'].splitlines()) if 'op' in r}
        for name in ['peer_open_rw', 'peer_signal', 'new_loopback_connection',
                     'peer_task_for_pid', 'peer_task_read_for_pid']:
            before, after = baseline.get(name), d.get(name)
            capability = name in ['peer_task_for_pid', 'peer_task_read_for_pid']
            if (not control_ok(cells[0]) or not before or before['rc'] != 0 or
                (capability and not before.get('value'))):
                verdict = 'baseline_denied_or_missing_no_policy_evidence'
            elif not after:
                verdict = 'missing'
            elif after['rc'] == 0 or (name == 'peer_signal' and cell.get('peer_signal_observed')):
                verdict = 'unauthorized_operation_succeeded'
            elif name == 'peer_task_for_pid' or after.get('errno') in [1, 13]:
                verdict = 'denied_with_positive_control'
            else:
                verdict = 'other_failure_not_denial_evidence'
            result.append({'mode': cell['mode'], 'context': cell['launch_context'],
                           'operation': name, 'result': verdict})
    return result

SELF_OPS = ['self_task_control', 'self_task_read', 'direct_self_control', 'direct_self_read']

def self_success(cell):
    d = {r['op']: r for r in map(json.loads, cell['output'].splitlines()) if 'op' in r}
    return (d.get('self_task_facility', {}).get('rc') == 0 and
            all(d.get(k, {}).get('rc') == 0 and d[k].get('value') == 1 for k in SELF_OPS))

def assess_filter(cells):
    if len(cells) < 2 or not all(self_success(c) for c in cells[:2]):
        return [{'result': 'baseline_self_denied_or_missing_no_filter_attribution'}]
    if len(cells) != 3:
        return [{'result': 'incomplete_filter_comparison'}]
    if not all(positive_ok(c) and launch_ok(c) for c in cells):
        return [{'result': 'invalid_control_or_compatibility_failure'}]
    d = {r['op']: r for r in map(json.loads, cells[2]['output'].splitlines()) if 'op' in r}
    result = []
    for name in SELF_OPS:
        r = d.get(name)
        if not r:
            verdict = 'missing'
        elif r.get('rc') == 0 or r.get('value'):
            verdict = 'entry_not_denied'
        elif name.endswith('read') and r.get('errno') not in [1, 13]:
            verdict = 'other_failure_not_permission_denial'
        else:
            verdict = 'denied_after_added_filter_with_matched_self_controls'
        result.append({'operation': name, 'result': verdict})
    result.append({'operation': 'self_task_facility',
                   'result': 'pass' if d.get('self_task_facility', {}).get('rc') == 0 else 'fail'})
    return result

READ_OPS = ['self_task_read', 'direct_self_read']
CONTROL_OPS = ['self_task_control', 'direct_self_control']

def read_cell_ok(cell, amended=False):
    if (cell.get('launch_context') != 'sanitized' or
        cell.get('policy') != ('amended' if amended else 'base') or
        cell.get('mode') not in (['before'] if amended else ['control', 'before'])):
        return False
    d = {r['op']: r for r in map(json.loads, cell['output'].splitlines()) if 'op' in r}
    if not (positive_ok(cell) and launch_ok(cell) and
            d.get('self_task_facility', {}).get('rc') == 0):
        return False
    if cell['mode'] == 'control':
        return control_ok(cell) and self_success(cell)
    return (all(d.get(k, {}).get('rc') == 5 and d[k].get('value') == 0 for k in CONTROL_OPS) and
            all((d.get(k, {}).get('rc') == -1 and d[k].get('errno') in [1, 13] and d[k].get('value') == 0)
                if amended else (d.get(k, {}).get('rc') == 0 and d[k].get('value') == 1)
                for k in READ_OPS))

def assess_read_filter(cells):
    if (len(cells) < 2 or [c.get('mode') for c in cells[:2]] != ['control', 'before'] or
        not all(read_cell_ok(c) for c in cells[:2])):
        return [{'result': 'read_baseline_invalid_or_changed_no_filter_attribution'}]
    if len(cells) != 3:
        return [{'result': 'incomplete_read_filter_comparison'}]
    return [{'result': ('read_denied_after_added_rule_control_unchanged'
                        if read_cell_ok(cells[2], amended=True) else
                        'read_filter_or_compatibility_failure_no_attribution')}]

MACH96_SEEDS = [0x13579bdf, 0x2468ace1]

def mach96_cell_ok(cell, amended=False, denied_method=False):
    if (cell.get('launch_context') != 'sanitized' or
        cell.get('policy') != ('amended' if amended else 'base') or
        cell.get('mode') not in (['before'] if amended else ['control', 'before'])):
        return False
    d = {r['op']: r for r in map(json.loads, cell['output'].splitlines()) if 'op' in r}
    if not (positive_ok(cell) and launch_ok(cell) and
            d.get('self_task_facility', {}).get('rc') == 0):
        return False
    if cell['mode'] == 'control' and not control_ok(cell):
        return False
    for i, seed in enumerate(MACH96_SEEDS):
        for phase, rc, value in [('pre', 0, seed), ('call', 53 if amended and denied_method else 5, seed if amended else 0),
                                 ('post', 0, seed ^ 0xffffffff)]:
            row = d.get(f'mach96_{i}_{phase}', {})
            if row.get('rc') != rc or row.get('value') != value:
                return False
    return True

def assess_mach96_filter(cells, denied_method=False):
    if (len(cells) < 2 or [c.get('mode') for c in cells[:2]] != ['control', 'before'] or
        not all(mach96_cell_ok(c) for c in cells[:2])):
        return [{'result': 'mach96_baseline_copyout_missing_no_attribution'}]
    if len(cells) != 3:
        return [{'result': 'incomplete_mach96_comparison'}]
    return [{'result': ('mach96_copyout_suppressed_after_added_rule'
                        if mach96_cell_ok(cells[2], amended=True, denied_method=denied_method) else
                        'mach96_filter_or_compatibility_failure_no_attribution')}]

def iomain_cell_ok(cell, amended=False):
    if (cell.get('launch_context') != 'sanitized' or
        cell.get('policy') != ('amended' if amended else 'base') or
        cell.get('mode') not in (['before'] if amended else ['control', 'before'])):
        return False
    rows = list(map(json.loads, cell['output'].splitlines()))
    d = {r['op']: r for r in rows if 'op' in r}
    names = ['iomain_euid', 'iomain_host', 'iomain_call', 'iomain_right', 'iomain_release', 'iomain_host_release']
    if not all(sum(r.get('op') == name for r in rows) == 1 for name in names):
        return False
    if not (positive_ok(cell) and launch_ok(cell) and
            d.get('self_task_facility', {}).get('rc') == 0 and
            (cell['mode'] != 'control' or control_ok(cell))):
        return False
    if not (d['iomain_euid']['rc'] == 0 and isinstance(d['iomain_euid']['value'], int) and
            d['iomain_euid']['value'] > 0 and d['iomain_host']['rc'] == 0 and d['iomain_host']['value'] == 1 and
            all(d[k]['rc'] == 0 for k in ['iomain_right', 'iomain_release', 'iomain_host_release'])):
        return False
    call, right, release = (d[k] for k in ['iomain_call', 'iomain_right', 'iomain_release'])
    if amended:
        # This is a retained failure observation, NOT an accepted policy-denial code.
        return (isinstance(call['rc'], int) and call['rc'] != 0 and call['value'] == 0 and
                right['value'] == 0 and release['value'] == 0)
    return (call['rc'] == 0 and 0 < call['value'] < 0xffffffff and
            right['value'] == 0x10000 and release['value'] == 1)

def assess_iomain_filter(cells):
    if (len(cells) < 2 or [c.get('mode') for c in cells[:2]] != ['control', 'before'] or
        not all(iomain_cell_ok(c) for c in cells[:2])):
        return [{'result': 'iomain_baseline_invalid_no_attribution'}]
    if len(cells) != 3:
        return [{'result': 'incomplete_iomain_comparison'}]
    result = 'iomain_acquisition_or_compatibility_failure_no_acceptance'
    if iomain_cell_ok(cells[2], amended=True):
        result = 'iomain_absent_after_rule_requires_error_and_findings_review'
    return [{'result': result}]

def self_check():
    rows = [{'op': k, 'rc': 0, 'errno': 0, 'value': 0} for k in
            ['sandbox_before', 'own_fullfsync', 'hvf_dlopen', 'anonymous_ram',
             'timer_offset_value', 'timer_mask_value', 'guest_exit', 'guest_x0',
             'p_hv_vm_destroy()', 'p_hv_vcpu_destroy(cpu)', 'peer_open_rw',
             'peer_signal', 'peer_task_for_pid', 'new_loopback_connection']]
    next(r for r in rows if r['op'] == 'peer_task_for_pid')['value'] = 1
    rows += [{'op': 'own_pwrite', 'rc': 16, 'errno': 0, 'value': 16},
             {'op': 'own_pread', 'rc': 16, 'errno': 0, 'value': 1},
             {'op': 'channel_read', 'rc': 1, 'errno': 0, 'value': 1},
             {'op': 'channel_write', 'rc': 1, 'errno': 0, 'value': 0}]
    cells = []
    for repeat in range(2):
        for mode in ['control', 'before', 'after']:
            sample = copy.deepcopy(rows)
            if mode != 'control':
                for row in sample:
                    if row['op'] in ['peer_open_rw', 'peer_signal', 'peer_task_for_pid', 'new_loopback_connection']:
                        row.update(rc=-1, errno=1)
                sample += [{'op': 'sandbox_init', 'rc': 0}, {'op': 'sandbox_after', 'rc': 1}]
            cells.append({'mode': mode, 'repeat': repeat, 'returncode': 0, 'timed_out': False,
                          'channel_reply': '42', 'peer_signal_observed': mode == 'control',
                          'output': '\n'.join(json.dumps(row) for row in sample)})
    assert control_ok(cells[0])
    for operation in ['own_fullfsync', 'guest_exit']:
        invalid = copy.deepcopy(cells[0])
        parsed = list(map(json.loads, invalid['output'].splitlines()))
        next(r for r in parsed if r['op'] == operation)['rc'] = -1
        invalid['output'] = '\n'.join(map(json.dumps, parsed))
        assert not control_ok(invalid)
    invalid = copy.deepcopy(cells[0])
    invalid['peer_signal_observed'] = False
    assert not control_ok(invalid)
    good = assess(cells)
    assert sum(x.get('positive_operations') == 'pass' for x in good) == 6
    assert sum(x.get('result') == 'denied_with_positive_control' for x in good) == 16
    bad = copy.deepcopy(cells)
    bad[0]['peer_signal_observed'] = False
    assert any(x.get('operation') == 'peer_signal' and x.get('result') == 'baseline_denied_or_missing_no_policy_evidence' for x in assess(bad))
    bad = copy.deepcopy(cells)
    bad[1]['channel_reply'] = '00'
    assert any(x.get('positive_operations') == 'fail' for x in assess(bad))
    bad = copy.deepcopy(cells)
    parsed = [json.loads(line) for line in bad[0]['output'].splitlines()]
    next(x for x in parsed if x['op'] == 'peer_task_for_pid')['rc'] = 5
    bad[0]['output'] = '\n'.join(map(json.dumps, parsed))
    assert any(x.get('operation') == 'peer_task_for_pid' and x.get('result') == 'baseline_denied_or_missing_no_policy_evidence' for x in assess(bad))
    assert all(x['result'] == 'incomplete' for x in assess([]))
    launch = [copy.deepcopy(cells[i]) for i in [0, 1, 1]]
    for i, cell in enumerate(launch):
        cell['launch_context'] = 'sanitized' if i == 2 else 'ordinary'
        extra = [{'op': k, 'rc': 0, 'value': 0} for k in
                 ['launch_child_bootstrap', 'launch_child_registered', 'launch_child_exceptions',
                 'launch_child_thread_exceptions']]
        extra.append({'op': 'peer_task_read_for_pid', 'rc': -1, 'errno': 1, 'value': 0})
        cell['output'] += '\n' + '\n'.join(map(json.dumps, extra))
    assert launch_ok(launch[2])
    assert sum(x.get('positive_operations') == 'pass' for x in assess_launch(launch)) == 3
    assert sum(x.get('operation') == 'peer_task_read_for_pid' and
               x.get('result') == 'baseline_denied_or_missing_no_policy_evidence'
               for x in assess_launch(launch)) == 2
    wrong = copy.deepcopy(launch[2])
    parsed = list(map(json.loads, wrong['output'].splitlines()))
    next(x for x in parsed if x.get('op') == 'launch_child_bootstrap')['value'] = 1
    wrong['output'] = '\n'.join(map(json.dumps, parsed))
    assert not launch_ok(wrong)
    for field in ['launch_child_registered', 'launch_child_exceptions', 'launch_child_thread_exceptions']:
        wrong = copy.deepcopy(launch[2])
        parsed = list(map(json.loads, wrong['output'].splitlines()))
        next(x for x in parsed if x.get('op') == field)['value'] = 1
        wrong['output'] = '\n'.join(map(json.dumps, parsed))
        assert not launch_ok(wrong)
    read_control = copy.deepcopy(launch)
    parsed = list(map(json.loads, read_control[0]['output'].splitlines()))
    next(x for x in parsed if x.get('op') == 'peer_task_read_for_pid').update(rc=0, errno=0, value=1)
    read_control[0]['output'] = '\n'.join(map(json.dumps, parsed))
    assert sum(x.get('operation') == 'peer_task_read_for_pid' and
               x.get('result') == 'denied_with_positive_control' for x in assess_launch(read_control)) == 2
    assert assess_launch([])[0]['result'] == 'incomplete_launch_comparison'
    filtered = copy.deepcopy(launch)
    for i, cell in enumerate(filtered):
        cell['launch_context'] = 'sanitized'
        extra = [{'op': name, 'rc': 0 if i < 2 else -1,
                  'errno': 0 if i < 2 else 1, 'value': 1 if i < 2 else 0} for name in SELF_OPS]
        extra.append({'op': 'self_task_facility', 'rc': 0, 'value': 0})
        cell['output'] += '\n' + '\n'.join(map(json.dumps, extra))
    assert self_success(filtered[1])
    assert sum(x.get('result') == 'denied_after_added_filter_with_matched_self_controls'
               for x in assess_filter(filtered)) == 4
    wrong = copy.deepcopy(filtered)
    parsed = list(map(json.loads, wrong[1]['output'].splitlines()))
    next(x for x in parsed if x.get('op') == 'direct_self_read').update(rc=-1, errno=1, value=0)
    wrong[1]['output'] = '\n'.join(map(json.dumps, parsed))
    assert not self_success(wrong[1])
    assert assess_filter(wrong)[0]['result'] == 'baseline_self_denied_or_missing_no_filter_attribution'
    wrong = copy.deepcopy(filtered)
    parsed = list(map(json.loads, wrong[2]['output'].splitlines()))
    next(x for x in parsed if x.get('op') == 'direct_self_control').update(rc=0, value=1)
    wrong[2]['output'] = '\n'.join(map(json.dumps, parsed))
    assert any(x.get('result') == 'entry_not_denied' for x in assess_filter(wrong))
    read_cells = copy.deepcopy(filtered)
    for i, cell in enumerate(read_cells):
        cell['policy'] = 'amended' if i == 2 else 'base'
    for i in [1, 2]:
        rows = list(map(json.loads, read_cells[i]['output'].splitlines()))
        for row in rows:
            if row.get('op') in CONTROL_OPS:
                row.update(rc=5, errno=0, value=0)
        read_cells[i]['output'] = '\n'.join(map(json.dumps, rows))
    # The launch fixtures used above include ordinary inherited ports; READ uses sanitized launch.
    for cell in read_cells:
        rows = list(map(json.loads, cell['output'].splitlines()))
        for row in rows:
            if row.get('op') in ['launch_child_bootstrap', 'launch_child_registered']:
                row['value'] = 0
        cell['output'] = '\n'.join(map(json.dumps, rows))
    assert all(read_cell_ok(c) for c in read_cells[:2])
    assert assess_read_filter(read_cells)[0]['result'] == 'read_denied_after_added_rule_control_unchanged'
    assert not read_cell_ok(read_cells[0], amended=True)
    assert assess_read_filter(read_cells[:2] + [copy.deepcopy(read_cells[0])])[0]['result'] != 'read_denied_after_added_rule_control_unchanged'
    for index, op, delta in [
        (1, 'self_task_read', dict(rc=-1, errno=1, value=0)),
        (1, 'direct_self_control', dict(rc=0, value=1)),
        (1, 'direct_self_control', dict(rc=4, value=0)),
        (1, 'self_task_facility', dict(rc=5)),
        (1, 'own_pread', dict(value=0)),
        (2, 'direct_self_read', dict(rc=0, value=1)),
        (2, 'direct_self_read', dict(rc=-1, errno=22, value=0)),
        (2, 'direct_self_read', dict(rc=5, errno=1, value=0)),
        (2, 'direct_self_read', dict(rc=-1, errno=1, value=1)),
        (2, 'self_task_control', dict(rc=0, value=1)),
        (2, 'self_task_control', dict(rc=4, value=0)),
        (2, 'self_task_facility', dict(rc=5)),
        (2, 'guest_exit', dict(rc=1))]:
        wrong = copy.deepcopy(read_cells)
        rows = list(map(json.loads, wrong[index]['output'].splitlines()))
        next(r for r in rows if r.get('op') == op).update(delta)
        wrong[index]['output'] = '\n'.join(map(json.dumps, rows))
        assert not read_cell_ok(wrong[index], amended=index == 2)
        assert assess_read_filter(wrong)[0]['result'] != 'read_denied_after_added_rule_control_unchanged'
    assert assess_read_filter(read_cells[:2])[0]['result'] == 'incomplete_read_filter_comparison'
    mach_cells = copy.deepcopy(read_cells)
    for index, cell in enumerate(mach_cells):
        for i, seed in enumerate(MACH96_SEEDS):
            for phase, rc, value in [('pre', 0, seed), ('call', 5, seed if index == 2 else 0),
                                     ('post', 0, seed ^ 0xffffffff)]:
                cell['output'] += '\n' + json.dumps(dict(op=f'mach96_{i}_{phase}', rc=rc, value=value))
    success = 'mach96_copyout_suppressed_after_added_rule'
    assert assess_mach96_filter(mach_cells)[0]['result'] == success
    assert assess_mach96_filter(mach_cells[:2] + [mach_cells[0]])[0]['result'] != success
    for index, op, delta in [
        (0, 'mach96_0_call', dict(value=MACH96_SEEDS[0])),
        (1, 'mach96_1_call', dict(value=MACH96_SEEDS[1])),
        (1, 'mach96_0_pre', dict(value=0)),
        (2, 'mach96_0_call', dict(value=0)),
        (2, 'mach96_1_call', dict(rc=4)),
        (2, 'mach96_1_post', dict(rc=-1)),
        (2, 'self_task_facility', dict(rc=5)),
        (2, 'own_pread', dict(value=0)),
        (2, 'guest_exit', dict(rc=1))]:
        wrong = copy.deepcopy(mach_cells)
        rows = list(map(json.loads, wrong[index]['output'].splitlines()))
        next(r for r in rows if r.get('op') == op).update(delta)
        wrong[index]['output'] = '\n'.join(map(json.dumps, rows))
        assert assess_mach96_filter(wrong)[0]['result'] != success
    # Distinct fixed methods: never silently promote the old exact-5 run.
    assert assess_mach96_filter(mach_cells, denied_method=True)[0]['result'] != success
    denied_cells = copy.deepcopy(mach_cells)
    rows = list(map(json.loads, denied_cells[2]['output'].splitlines()))
    for row in rows:
        if row.get('op') in ['mach96_0_call', 'mach96_1_call']:
            row['rc'] = 53
    denied_cells[2]['output'] = '\n'.join(map(json.dumps, rows))
    assert assess_mach96_filter(denied_cells, denied_method=True)[0]['result'] == success
    assert assess_mach96_filter(denied_cells)[0]['result'] != success
    assert assess_mach96_filter(denied_cells[:2] + [denied_cells[0]], denied_method=True)[0]['result'] != success
    io_cells = copy.deepcopy(read_cells)
    for index, cell in enumerate(io_cells):
        cell['output'] += '\n' + '\n'.join(map(json.dumps, [
            dict(op='iomain_euid', rc=0, value=501),
            dict(op='iomain_host', rc=0, value=1),
            dict(op='iomain_call', rc=0 if index < 2 else 53, value=123 if index < 2 else 0),
            dict(op='iomain_right', rc=0, value=0x10000 if index < 2 else 0),
            dict(op='iomain_release', rc=0, value=1 if index < 2 else 0),
            dict(op='iomain_host_release', rc=0, value=0)]))
    observed = 'iomain_absent_after_rule_requires_error_and_findings_review'
    assert assess_iomain_filter(io_cells)[0]['result'] == observed
    assert assess_iomain_filter(io_cells[:2])[0]['result'] != observed
    assert assess_iomain_filter(io_cells[:2] + [io_cells[0]])[0]['result'] != observed
    for index, op, delta in [
        (0, 'iomain_call', dict(rc=53, value=0)),
        (1, 'iomain_right', dict(rc=0, value=0)),
        (1, 'iomain_release', dict(rc=5)),
        (1, 'iomain_host', dict(value=0)),
        (0, 'iomain_euid', dict(value=0)),
        (2, 'iomain_euid', dict(rc=-1, value=0)),
        (2, 'iomain_call', dict(rc=0, value=123)),
        (2, 'iomain_call', dict(value=0xffffffff)),
        (2, 'iomain_right', dict(value=0x10000)),
        (2, 'iomain_host_release', dict(rc=5)),
        (2, 'self_task_facility', dict(rc=5)),
        (2, 'guest_exit', dict(rc=1)),
        (2, 'own_pread', dict(value=0))]:
        wrong = copy.deepcopy(io_cells)
        rows = list(map(json.loads, wrong[index]['output'].splitlines()))
        next(r for r in rows if r.get('op') == op).update(delta)
        wrong[index]['output'] = '\n'.join(map(json.dumps, rows))
        assert assess_iomain_filter(wrong)[0]['result'] != observed
    for context, policy, mode in [('ordinary', 'amended', 'before'),
                                  ('sanitized', 'base', 'before'),
                                  ('sanitized', 'amended', 'control')]:
        wrong = copy.deepcopy(io_cells)
        wrong[2].update(launch_context=context, policy=policy, mode=mode)
        assert assess_iomain_filter(wrong)[0]['result'] != observed
    wrong = copy.deepcopy(io_cells)
    rows = list(map(json.loads, wrong[2]['output'].splitlines()))
    rows = [r for r in rows if r.get('op') != 'iomain_call']
    wrong[2]['output'] = '\n'.join(map(json.dumps, rows))
    assert assess_iomain_filter(wrong)[0]['result'] != observed
    # An unexpected error can be retained as absence, never promoted to a denial PASS.
    wrong = copy.deepcopy(io_cells)
    rows = list(map(json.loads, wrong[2]['output'].splitlines()))
    next(r for r in rows if r.get('op') == 'iomain_call')['rc'] = -303
    wrong[2]['output'] = '\n'.join(map(json.dumps, rows))
    assert assess_iomain_filter(wrong)[0]['result'] == observed
    print('assessor synthetic self-check passed; no sandbox or VM invoked')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name')
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--control-only', action='store_true', help='One reviewed unrestricted CPU-limit diagnostic only')
    parser.add_argument('--launch-audit', action='store_true', help='Reviewed three-cell launch-only comparison')
    parser.add_argument('--task-filter', action='store_true', help='Three-cell self-acquisition filter attribution only')
    parser.add_argument('--read-filter', action='store_true', help='Three-cell READ-only attribution; CONTROL remains denied')
    parser.add_argument('--mach96-filter', action='store_true', help='Mach-96 handler-copyout attribution only')
    parser.add_argument('--mach96-denied-filter', action='store_true', help='Versioned Mach-96 method: amended KERN_DENIED 53 only')
    parser.add_argument('--iomain-filter', action='store_true', help='MIG205 acquisition contrast; errors require findings review')
    parser.add_argument('--execute', action='store_true', help='Only after reviewed execution authorization')
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0
    if not args.name:
        parser.error('--name is required unless --self-check')
    if sum([args.launch_audit, args.control_only, args.task_filter, args.read_filter, args.mach96_filter, args.mach96_denied_filter, args.iomain_filter]) > 1:
        parser.error('--launch-audit, --task-filter, --read-filter, --mach96-filter, --mach96-denied-filter, --iomain-filter and --control-only are exclusive')
    mach96_mode = args.mach96_filter or args.mach96_denied_filter
    filter_mode = args.task_filter or args.read_filter or mach96_mode or args.iomain_filter
    launch_mode = args.launch_audit or filter_mode
    # Reuse the frozen retention implementation, without editing or running its main.
    frozen = REPO / 'experiments/track01/source/run.py'
    spec = importlib.util.spec_from_file_location('retention', frozen)
    retention = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(retention)
    run = retention.allocate(os.environ.get('NATEMU_RAW_ROOT'), REPO, args.name)
    snap = run / 'source'
    snap.mkdir()
    for name in ['probe.c', 'worker.sb', 'entitlements.plist', 'run.py', 'launch.c', 'worker-task-filter.sb', 'worker-read-filter.sb', 'worker-mach96-filter.sb', 'worker-iomain-filter.sb']:
        shutil.copyfile(SOURCE / name, snap / name)
    shutil.copyfile(frozen, snap / 'retention.py')
    for original, retained in [(SOURCE / 'run.py', snap / 'run.py'), (frozen, snap / 'retention.py')]:
        if original.read_bytes() != retained.read_bytes():
            raise RuntimeError('runner/helper snapshot mismatch')
    record = {'schema': 'natemu.track03.containment.v6', 'runner_sha256': hashlib.sha256((snap / 'run.py').read_bytes()).hexdigest(),
              'retention_sha256': hashlib.sha256((snap / 'retention.py').read_bytes()).hexdigest(),
              'purpose': 'decision-evidence' if args.execute else 'preparation',
              'execute_requested': args.execute, 'control_only': args.control_only, 'launch_audit': args.launch_audit, 'task_filter': args.task_filter, 'read_filter': args.read_filter, 'mach96_filter': args.mach96_filter, 'mach96_denied_filter': args.mach96_denied_filter,
              'iomain_filter': args.iomain_filter, 'iomain_method': 'mig205-observation-v1' if args.iomain_filter else None,
              'mach96_method': ('fixed-denied53-v1' if args.mach96_denied_filter else 'legacy-exact5-v1') if mach96_mode else None,
              'supervisor_uid': os.getuid(), 'supervisor_euid': os.geteuid(),
              'inherited_cpu_limit': list(resource.getrlimit(resource.RLIMIT_CPU)),
              'vm_worker_cpu_limit_setter': 'omitted', 'peer_cpu_limit': [10, 10],
              'build_cpu_limit': [60, 60], 'commands': [], 'cells': [], 'status': 'preparing',
              'budget': {'workers': 3 if launch_mode else (1 if args.control_only else 6), 'peer': 1, 'seconds_per_worker': 20, 'execution_seconds': 100 if launch_mode else (60 if args.control_only else 240),
                         'guest_bytes_per_worker': 16384, 'vcpus_per_worker': 1},
              'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    peer = None
    peer_output = bytearray()
    try:
        retained_spec = importlib.util.spec_from_file_location('retained_helpers', snap / 'retention.py')
        retention = importlib.util.module_from_spec(retained_spec)
        retained_spec.loader.exec_module(retention)
        retention.save(run, record)
        for command in [['sw_vers'], ['uname', '-a'], ['sysctl', 'hw.model'], ['xcrun', 'clang', '--version'],
                        ['xcrun', '--show-sdk-version']]:
            retention.command(run, record, command)
        sdk = retention.command(run, record, ['xcrun', '--show-sdk-path']).strip()
        retention.command(run, record, ['xcrun', 'clang', '-isysroot', sdk, '-std=c11', '-O2',
                                       '-Wall', '-Wextra', '-Werror', snap / 'probe.c', '-lsandbox',
                                       '-o', run / 'probe'])
        if mach96_mode:
            retention.command(run, record, ['xcrun', 'otool', '-tvV', run / 'probe'])
        if launch_mode:
            retention.command(run, record, ['xcrun', 'clang', '-isysroot', sdk, '-std=c11', '-O2',
                                           '-Wall', '-Wextra', '-Werror', snap / 'launch.c', '-o', run / 'launch'])
        if args.iomain_filter:
            retention.command(run, record, [sys.executable, snap / 'run.py', '--self-check'])
        if not args.execute:
            record['status'] = 'built_not_signed_or_executed'
            return 0
        if tuple(record['inherited_cpu_limit']) != (resource.RLIM_INFINITY, resource.RLIM_INFINITY):
            raise RuntimeError('VM worker requires inherited unlimited RLIMIT_CPU; no setter workaround permitted')
        retention.command(run, record, ['codesign', '--force', '--sign', '-', '--entitlements',
                                       snap / 'entitlements.plist', run / 'probe'])
        retention.command(run, record, ['codesign', '--verify', '--verbose=2', run / 'probe'])
        retention.command(run, record, ['codesign', '-d', '--entitlements', ':-', run / 'probe'])
        if launch_mode:
            retention.command(run, record, ['codesign', '--force', '--sign', '-', run / 'launch'])
            retention.command(run, record, ['codesign', '--verify', '--verbose=2', run / 'launch'])
        deadline = time.monotonic() + record['budget']['execution_seconds']
        peer = subprocess.Popen([run / 'probe', 'peer'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, preexec_fn=child_limits)
        if not select.select([peer.stdout], [], [], 5)[0]:
            raise RuntimeError('peer readiness timeout')
        ready = peer.stdout.readline()
        peer_output.extend(ready)
        if int(ready) != peer.pid:
            raise RuntimeError('invalid peer PID')
        record['peer_pid'] = peer.pid
        fixture = run / 'fixtures'
        fixture.mkdir()
        peer_path = fixture / 'peer-disk'
        peer_path.write_bytes(b'peer-marker')
        for repeat in range(1 if args.control_only or launch_mode else 2):
            cases = ([('control', 'sanitized', 'base'), ('before', 'sanitized', 'base'),
                      ('before', 'sanitized', 'amended')] if filter_mode else
                     ([('control', 'ordinary', 'base'), ('before', 'ordinary', 'base'),
                       ('before', 'sanitized', 'base')] if args.launch_audit else
                      [(m, None, 'base') for m in
                       (['control'] if args.control_only else ['control', 'before', 'after'])]))
            for mode, context, policy in cases:
                if time.monotonic() >= deadline:
                    raise TimeoutError('execution budget exceeded')
                stem = f'{repeat}-{mode}' + (f'-{context}' if context else '') + (f'-{policy}' if filter_mode else '')
                own_path = fixture / f'{stem}-own-disk'
                own = os.open(own_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.ftruncate(own, 4096)
                parent_channel, channel = socket.socketpair()
                listener = socket.socket()
                listener.bind(('127.0.0.1', 0))
                listener.listen(1)
                port = listener.getsockname()[1]
                parent_channel.sendall(b'A')
                child = None
                cell = {'mode': mode, 'launch_context': context, 'policy': policy, 'repeat': repeat, 'status': 'running', 'port': port,
                        'own_file': str(own_path), 'peer_file': str(peer_path), 'peer_pid': peer.pid,
                        'stdout': f'{stem}.stdout', 'stderr': f'{stem}.stderr', 'timed_out': False,
                        'cpu_limit': record['inherited_cpu_limit'], 'cpu_limit_setter': 'omitted'}
                record['cells'].append(cell)
                retention.save(run, record)
                try:
                    argv = [run / 'probe', mode, snap / (('worker-iomain-filter.sb' if args.iomain_filter else 'worker-mach96-filter.sb' if mach96_mode else ('worker-read-filter.sb' if args.read_filter else 'worker-task-filter.sb')) if policy == 'amended' else 'worker.sb'), str(own), str(channel.fileno()),
                            peer_path, str(peer.pid), str(port)]
                    if context:
                        argv = [run / 'launch', context] + argv + [context]
                    if filter_mode:
                        argv += ['iomain-filter' if args.iomain_filter else 'mach96-filter' if mach96_mode else 'task-filter']
                    cell['argv'] = list(map(str, argv))
                    child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             pass_fds=(own, channel.fileno()), close_fds=True,
                                             start_new_session=True, preexec_fn=lambda: child_limits(False))
                    channel.close()
                    try:
                        output, errors = child.communicate(timeout=min(20, deadline-time.monotonic()))
                    except subprocess.TimeoutExpired:
                        cell['timed_out'] = True
                        stop(child)
                        output, errors = child.communicate()
                    cell['returncode'] = child.returncode
                    cell['output'] = output.decode('utf-8', errors='replace')
                    (run / cell['stdout']).write_bytes(output)
                    (run / cell['stderr']).write_bytes(errors)
                    parent_channel.settimeout(1)
                    try:
                        cell['channel_reply'] = parent_channel.recv(1).hex()
                    except TimeoutError:
                        cell['channel_reply'] = ''
                    signal_data = b''
                    if select.select([peer.stdout], [], [], 1)[0]:
                        signal_data = peer.stdout.readline()
                        peer_output.extend(signal_data)
                    cell['peer_signal_observed'] = signal_data == b'S\n'
                    cell['status'] = 'collected'
                    if launch_mode and (not launch_ok(cell) or
                        (context == 'ordinary' and not positive_ok(cell))):
                        raise RuntimeError(f'{stem}: invalid ordinary control or launch postcondition; stop')
                    if cell['timed_out'] or (mode == 'control' and not control_ok(cell)) or child.returncode in [2, 3, 24, 25, 26]:
                        raise RuntimeError(f'{stem}: invalid control/profile/environment or timeout; stop')
                    if args.iomain_filter and policy == 'base':
                        if not iomain_cell_ok(cell):
                            record['status'] = 'iomain_inconclusive_before_amendment'
                            record['assessment'] = assess_iomain_filter(record['cells'])
                            return 0
                    if mach96_mode and policy == 'base':
                        if not mach96_cell_ok(cell):
                            record['status'] = 'mach96_inconclusive_before_amendment'
                            record['assessment'] = assess_mach96_filter(record['cells'], denied_method=args.mach96_denied_filter)
                            return 0
                    if args.read_filter and policy == 'base':
                        if not read_cell_ok(cell):
                            record['status'] = 'read_filter_inconclusive_before_amendment'
                            record['assessment'] = assess_read_filter(record['cells'])
                            return 0
                    if args.task_filter and policy == 'base':
                        if not positive_ok(cell):
                            raise RuntimeError(f'{stem}: invalid base positive control; stop')
                        if not self_success(cell):
                            record['status'] = 'task_filter_inconclusive_before_amendment'
                            record['assessment'] = assess_filter(record['cells'])
                            return 0
                    # A restricted HVF failure is a measured candidate limitation;
                    # retain it and still collect the fixed setup-before comparison.
                finally:
                    stop(child)
                    os.close(own)
                    channel.close()
                    parent_channel.close()
                    listener.close()
                    retention.save(run, record)
        if args.iomain_filter:
            record['assessment'] = assess_iomain_filter(record['cells'])
            record['status'] = 'iomain_collected_requires_findings_review'
        elif mach96_mode:
            record['assessment'] = assess_mach96_filter(record['cells'], denied_method=args.mach96_denied_filter)
            record['status'] = 'mach96_collected_requires_findings_review'
        elif args.read_filter:
            record['assessment'] = assess_read_filter(record['cells'])
            record['status'] = 'read_filter_collected_requires_findings_review'
        elif args.task_filter:
            record['assessment'] = assess_filter(record['cells'])
            record['status'] = 'task_filter_collected_requires_findings_review'
        elif args.launch_audit:
            record['assessment'] = assess_launch(record['cells'])
            record['status'] = 'launch_comparison_collected_requires_findings_review'
        elif args.control_only:
            record['assessment'] = [{'mode': 'control', 'positive_operations': 'pass' if control_ok(record['cells'][0]) else 'fail',
                                     'result': 'no_restricted_cells_cpu_limit_diagnostic_only'}]
            record['status'] = 'control_diagnostic_collected_requires_findings_review'
        else:
            record['assessment'] = assess(record['cells'])
            record['status'] = 'matrix_collected_requires_findings_review'
    except BaseException as error:
        record['status'] = 'failed_or_inapplicable'
        record['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        stop(peer)
        if peer is not None:
            peer_output.extend(peer.stdout.read())
            (run / 'peer.stdout').write_bytes(peer_output)
            (run / 'peer.stderr').write_bytes(peer.stderr.read())
        record['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        retention.save(run, record)
        retention.checksums(run)
        print(run)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
