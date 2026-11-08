#!/usr/bin/env python3
"""Run immutable public-test manifest rows under the project's shared VM lock."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


def classify(text, case, capped=False):
    begins = re.findall(r'^CT-BEGIN suite=public part=(\S+) cpus=(\d+) kernel=(\S+)\r?$', text, re.M)
    starts = re.findall(r'^PUB-START id=(\S+) cap=(\d+)\r?$', text, re.M)
    results = re.findall(r'^PUB-RESULT id=(\S+) rc=(\d+)\r?$', text, re.M)
    ends = re.findall(r'^CT-END suite=public part=(\S+) rc=(\d+) guest_s=(\S+)\r?$', text, re.M)
    if capped:
        return 'host-cap'
    if len(begins) != 1 or len(starts) != 1 or len(results) != 1 or len(ends) != 1:
        return 'incomplete'
    if starts[0][0] != case['id'] or results[0][0] != case['id']:
        return 'identity-error'
    rc = int(results[0][1])
    if rc != int(ends[0][1]) or ends[0][0] != str(case['index']) + '/1' or begins[0][0] != ends[0][0]:
        return 'identity-error'
    if rc == 124:
        return 'guest-cap'
    if rc == 137:
        return 'killed-or-cap'
    failures = [s for s in text.splitlines() if re.match(r'^\s*(?:\[[^\]]+\]\s*)?not ok\b', s)
                and not re.search(r'#\s*(SKIP|TODO)\b', s, re.I)]
    if failures or re.search(r'BUG:|WARNING: CPU:|Kernel panic', text):
        return 'failure'
    if case['family'] == 'ltp':
        if rc & 1:
            return 'failure'
        if rc & 2:
            return 'broken'
        if rc & 4:
            return 'warning'
        if rc == 32:
            return 'skip'
    elif rc == 4:
        return 'skip'
    if rc:
        return 'failure'
    return 'completed-needs-oracle-review'


def selftest():
    c = dict(id='sample', index=0, family='tap')
    good = 'CT-BEGIN suite=public part=0/1 cpus=1 kernel=test\nPUB-START id=sample cap=3\nPUB-RESULT id=sample rc=0\nCT-END suite=public part=0/1 rc=0 guest_s=1\n'
    assert classify(good, c) == 'completed-needs-oracle-review'
    assert classify(good, c, True) == 'host-cap'
    assert classify(good + good, c) == 'incomplete'
    assert classify(good.replace('id=sample', 'id=other'), c) == 'identity-error'
    assert classify(good.replace('rc=0', 'rc=4'), c) == 'skip'
    assert classify(good.replace('rc=0', 'rc=124'), c) == 'guest-cap'
    assert classify('not ok 1 - broken\n' + good, c) == 'failure'
    assert classify(good.split('CT-END')[0], c) == 'incomplete'
    assert classify(good.replace('rc=0', 'rc=2'), dict(c, family='ltp')) == 'broken'
    assert classify(good.replace('rc=0', 'rc=137'), c) == 'killed-or-cap'
    assert classify('not ok 1 # SKIP unavailable\n' + good, c) == 'completed-needs-oracle-review'
    print('11 result-classification checks passed')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def campaign_identity(out):
    return {name: digest(out / name) for name in ('manifest.json', 'queue.json')}


def one(out, manifest, row):
    # Do not overlap another owner's VM even if its launcher ignores our lock.
    for name in ('natemu-host', 'natemu', 'qemu-system-x86_64'):
        if subprocess.run(['pgrep', '-x', name], capture_output=True).returncode == 0:
            return 75
    prefix = out / 'runs' / row['name']
    assert not Path(str(prefix) + '.json').exists(), 'attempt already recorded'
    identity = campaign_identity(out)
    for path, expected in manifest['pins'].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected, path
    case = manifest['cases'][row['index']]
    serial = str(prefix) + '.serial'
    assert not Path(serial).exists(), 'refusing to overwrite output'
    cmdline = f'console=ttyS0 mitigations=off nokaslr ct=public part={row["index"]}/1 pubcap={row["guest_cap"]}'
    kernel = case['kernel']
    env = {k: v for k, v in os.environ.items() if not k.startswith('NATEMU_')}
    env['NATEMU_COMPILE_CACHE'] = 'off'
    if row['engine'] == 'natemu':
        b = manifest['candidate']
        cmd = [b['natemu'], '--runtime', b['natemu-runtime'], '--fexc', b['fexc'],
               '--kernel', kernel, '--initrd', case['image'], '--cmdline', cmdline,
               '--vcpus', str(row['vcpus']), '--serial', serial]
    else:
        cmd = [manifest['qemu'], '-nodefaults', '-machine', 'pc,hpet=off,i8042=off,vmport=off',
               '-accel', 'tcg,thread=multi', '-cpu', manifest['qcpu'], '-m', '2048',
               '-smp', str(row['vcpus']), '-kernel', kernel, '-initrd', case['image'],
               '-append', cmdline, '-display', 'none', '-monitor', 'none',
               '-serial', 'file:' + serial, '-no-reboot']
    began = time.monotonic()
    capped = False
    end_at = None
    forced_after_end = False
    overlaps = set()
    last_overlap_check = 0
    with open(str(prefix) + '.stdout', 'xb') as stdout, open(str(prefix) + '.stderr', 'xb') as stderr:
        p = subprocess.Popen(cmd, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            while p.poll() is None:
                now = time.monotonic()
                text = Path(serial).read_text(errors='replace') if Path(serial).exists() else ''
                if re.search(r'^CT-END suite=public ', text, re.M) and end_at is None:
                    end_at = now
                if end_at is not None and now - end_at > 5:
                    forced_after_end = True
                    break
                if now - began >= row['host_cap']:
                    capped = True
                    break
                if now - last_overlap_check > 5:
                    for procname in ('natemu-host', 'natemu', 'qemu-system-x86_64'):
                        for pid in subprocess.run(['pgrep', '-x', procname], capture_output=True, text=True).stdout.split():
                            pgid = subprocess.run(['ps', '-o', 'pgid=', '-p', pid], capture_output=True, text=True).stdout.strip()
                            if pgid and pgid != str(p.pid):
                                overlaps.add(pid + ':' + procname)
                    last_overlap_check = now
                time.sleep(0.25)
        finally:
            # Includes fexc workers; never target unrelated VMs.
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
    text = Path(serial).read_text(errors='replace') if Path(serial).exists() else ''
    result = dict(row, command=cmd, process_rc=p.returncode,
                  seconds=round(time.monotonic() - began, 3), host_capped=capped,
                  forced_after_end=forced_after_end, overlaps=sorted(overlaps),
                  campaign_identity=identity, classification=classify(text, case, capped))
    if p.returncode != 0 and not capped and not forced_after_end and result['classification'] == 'completed-needs-oracle-review':
        result['classification'] = 'host-exit-after-results'
    if campaign_identity(out) != identity or any(digest(p) != h for p, h in manifest['pins'].items()):
        result['classification'] = 'inputs-changed'
    if not re.search(r'^PUB-START id=' + re.escape(case['id']) + r' cap=' + str(row['guest_cap']) + r'\r?$', text, re.M) or not re.search(r'^CT-BEGIN .* cpus=' + str(row['vcpus']) + r' ', text, re.M):
        if result['classification'] == 'completed-needs-oracle-review':
            result['classification'] = 'identity-error'
    result['outputs'] = {str(p): digest(p) for p in (Path(str(prefix) + s) for s in ('.serial', '.stdout', '.stderr')) if p.exists()}
    Path(str(prefix) + '.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)
    return 0


def main():
    a = argparse.ArgumentParser()
    a.add_argument('out', nargs='?')
    a.add_argument('--one')
    a.add_argument('--selftest', action='store_true')
    args = a.parse_args()
    if args.selftest:
        selftest()
        return 0
    out = Path(args.out).resolve()
    m = json.loads((out / 'manifest.json').read_text())
    queue = json.loads((out / 'queue.json').read_text())
    assert len({r['name'] for r in queue}) == len(queue), 'duplicate names'
    if args.one:
        return one(out, m, next(r for r in queue if r['name'] == args.one))
    for row in queue:
        p = out / 'runs' / (row['name'] + '.json')
        if p.exists():
            old = json.loads(p.read_text())
            assert all(old[k] == v for k, v in row.items()), 'changed result identity on resume'
            assert old['campaign_identity'] == campaign_identity(out), 'changed campaign on resume'
            if old['classification'] != 'runner-error':
                required = {str(out / 'runs' / (row['name'] + ext)) for ext in ('.stdout', '.stderr')}
                if old['classification'] == 'completed-needs-oracle-review':
                    required.add(str(out / 'runs' / (row['name'] + '.serial')))
                assert required <= old['outputs'].keys(), 'missing output hashes on resume'
            assert all(digest(f) == h for f, h in old.get('outputs', {}).items()), 'changed output on resume'
            assert all(digest(f) == h for f, h in m['pins'].items()), 'changed input on resume'
            continue  # Resume by immutable attempt ID, never replace a result.
        print('RUN', row['name'], flush=True)
        cmd = ['/usr/bin/lockf', '-k', m['lock'], sys.executable,
               str(Path(__file__).resolve()), str(out), '--one', row['name']]
        with open(out / 'runs' / (row['name'] + '.runner.log'), 'ab') as log:
            for _ in range(120):
                r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
                if r.returncode != 75:
                    break
                time.sleep(1)
        if p.exists():
            print('RESULT', row['name'], json.loads(p.read_text())['classification'], flush=True)
        else:
            p.write_text(json.dumps(dict(row, classification='runner-error', runner_rc=r.returncode,
                                        campaign_identity=campaign_identity(out)), indent=2) + '\n')
            print('NO RESULT', row['name'], 'runner rc', r.returncode, flush=True)
    return int(any(json.loads((out / 'runs' / (r['name'] + '.json')).read_text())['classification'] == 'runner-error' for r in queue))


if __name__ == '__main__':
    sys.exit(main())
