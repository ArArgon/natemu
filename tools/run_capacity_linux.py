#!/usr/bin/env python3
"""Boot unchanged Linux/initramfs with 8 GiB at N1/N8 and verify 4 GiB resident RAM.

Run under the shared VM lock, with no other VM running:
  python3 tools/run_capacity_linux.py --natemu STUB --runtime RUNTIME --fexc FEXC --out NEW_DIR
  python3 tools/run_capacity_linux.py --self-check

Uses bench=shell and serial input; neither the image nor public tests are edited.
stress-ng 0.19.02 one-zero writes/checks every word with both all-one/all-zero
patterns. Unlike lfsr32, it does not skip 96 of every 128 bytes. one-zero ignores
its inner max_ops limit, so --vm-ops 1 finishes one full pass and reports 262144
ops for 4 GiB. An external timeout is failure, never a completed verification.
Observed worker RssAnon >=4 GiB and zero swap prove resident guest memory rather
than reservation. This does not identify particular PFNs or cover address aliasing
with distinct per-address patterns, host swapping, or concurrent memory writers.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT.parent / 'natemu-raw'
KERNEL = RAW / 'kernel-census-20261028-01/dl/linux'
INITRD = RAW / 'bench-20261105-03/out/bench.cpio'
SOURCE = RAW / 'bench-20261105-03/src/stress-ng_0.19.02.orig.tar.xz'
SOURCE_SHA = 'ffe8907154cc3b65cc66cde8663106355f88c948b58169cee1badbde9e0f52bd'
RSS_KB, BOGO_OPS = 4 * 1024 * 1024, 262144
STRESS = ('/usr/bin/stress-ng --vm 1 --vm-bytes 4294967296 --vm-method one-zero '
          '--vm-ops 1 --vm-keep --vm-madvise normal --vm-hang 10 --verify --no-rand-seed --metrics')


def digest(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def guest_script(n):
    return f'''#!/bin/sh
set -u
echo 7 > /proc/sys/kernel/printk
echo
echo CAPACITY-BEGIN cpus=$(nproc) online=$(cat /sys/devices/system/cpu/online) kernel=$(uname -r)
awk '/^MemTotal:/ {{print "CAPACITY-MEMTOTAL kb=" $2}} /^SwapTotal:/ {{print "CAPACITY-SWAPTOTAL kb=" $2}}' /proc/meminfo
cat /proc/swaps
test "$(nproc)" -eq {n} || exit 20
test "$(awk '/^SwapTotal:/ {{print $2}}' /proc/meminfo)" -eq 0 || exit 21
test "$(awk '/^MemTotal:/ {{print $2}}' /proc/meminfo)" -ge 7340032 || exit 22
/usr/bin/stress-ng --version
sha256sum /usr/bin/stress-ng
echo CAPACITY-STRESS-BEGIN
timeout -s KILL 300 {STRESS} > /tmp/capacity-stress.log 2>&1 &
job=$!
while kill -0 "$job" 2>/dev/null; do
  for status in /proc/[0-9]*/status; do
    awk '/^Name:/ {{name=$2}} /^VmRSS:/ {{rss=$2}} /^RssAnon:/ {{anon=$2}} /^VmSwap:/ {{swap=$2}} END {{if (name ~ /^stress-ng/ && anon >= {RSS_KB}) printf "CAPACITY-RSS status=%s rss_kb=%s anon_kb=%s swap_kb=%s\\n", FILENAME,rss,anon,swap}}' "$status" 2>/dev/null
  done
  sleep 1
done
wait "$job"
rc=$?
cat /tmp/capacity-stress.log
echo CAPACITY-STRESS-END rc=$rc
awk '/^SwapTotal:/ {{print "CAPACITY-SWAP-END kb=" $2}}' /proc/meminfo
echo CAPACITY-DMESG-BEGIN
dmesg
echo CAPACITY-DMESG-END
exit "$rc"
'''


def serial_text(path):
    try:
        return path.read_text(errors='replace').replace('\r', '')
    except FileNotFoundError:
        return ''


def judge(text, n):
    problems = []
    def require(ok, reason):
        if not ok:
            problems.append(reason)
    begins = re.findall(r'^CAPACITY-BEGIN cpus=(\d+) online=(\S+) kernel=(\S+)$', text, re.M)
    online = '0' if n == 1 else '0-7'
    require(len(begins) == 1 and begins[0][:2] == (str(n), online), 'wrong/missing online CPU count or mask')
    mem = [int(x) for x in re.findall(r'^CAPACITY-MEMTOTAL kb=(\d+)$', text, re.M)]
    require(len(mem) == 1 and 7 * 1024 * 1024 <= mem[0] <= 8 * 1024 * 1024, 'MemTotal outside 7–8 GiB')
    for marker in ('CAPACITY-SWAPTOTAL', 'CAPACITY-SWAP-END'):
        require(re.findall(r'^' + marker + r' kb=(\d+)$', text, re.M) == ['0'], 'missing or nonzero swap: ' + marker)
    rss = [dict(status=x[0], rss_kb=int(x[1]), anon_kb=int(x[2]), swap_kb=int(x[3])) for x in
           re.findall(r'^CAPACITY-RSS status=(\S+) rss_kb=(\d+) anon_kb=(\d+) swap_kb=(\d+)$', text, re.M)]
    require(bool(rss) and all(x['swap_kb'] == 0 for x in rss) and
            any(x['anon_kb'] >= RSS_KB and x['rss_kb'] >= x['anon_kb'] for x in rss),
            'no observed >=4 GiB anonymous resident worker with zero swap')
    require(re.findall(r'^CAPACITY-STRESS-BEGIN$', text, re.M) == ['CAPACITY-STRESS-BEGIN'], 'stress start missing/duplicate')
    require(re.findall(r'^CAPACITY-STRESS-END rc=(\d+)$', text, re.M) == ['0'], 'stress failed, capped, or incomplete')
    require(re.findall(r'^CAPACITY-END rc=(\d+)$', text, re.M) == ['0'], 'guest script failed/incomplete')
    require(re.findall(r'^CAPACITY-DMESG-END$', text, re.M) == ['CAPACITY-DMESG-END'], 'kernel diagnostic capture incomplete')
    versions = re.findall(r'^stress-ng, version (\S+)', text, re.M)
    require(versions == ['0.19.02'], 'unexpected/missing public stress-ng version')
    binary = re.findall(r'^([a-f0-9]{64})\s+/usr/bin/stress-ng$', text, re.M)
    require(len(binary) == 1, 'missing public stress-ng binary digest')
    metrics = re.findall(r'^stress-ng:\s+metrc:\s+\[\s*\d+\]\s+vm\s+(\d+)\s+', text, re.M)
    require(metrics == [str(BOGO_OPS)], 'not exactly one full 4 GiB double-pattern pass')
    require(len(re.findall(r'\bpassed: 1: vm \(1\)', text)) == 1, 'missing exact passed vm worker')
    require(len(re.findall(r'\bfailed: 0\b', text)) == 1, 'missing zero-failure summary')
    require(len(re.findall(r'\bskipped: 0\b', text)) == 1, 'missing zero-skip summary')
    require('successful run completed' in text, 'public stress-ng completion missing')
    require(not re.search(r'BUG:|WARNING:|Oops:|Kernel panic|Out of memory:|oom-kill:|Killed process|stress-ng:\s+(?:fail|error):', text),
            'kernel/stressor error or OOM diagnostic')
    return problems, dict(memtotal_kb=mem, online=begins, resident_samples=rss, bogo_ops=metrics, stress_sha256=binary)


def run(args, n):
    work = args.out / f'n{n}'
    work.mkdir()
    serial = work / 'serial.txt'
    script = guest_script(n)
    payload = "cat > /tmp/capacity.sh <<'CAPACITY_SCRIPT_EOF'\n" + script + 'CAPACITY_SCRIPT_EOF\n'
    payload += 'sh /tmp/capacity.sh; rc=$?; echo CAPACITY-END rc=$rc; poweroff -f\n'
    # The UART input queue holds4096 bytes; this entire transfer must fit even if the guest pauses.
    assert len(payload.encode()) < 4096
    (work / 'guest.sh').write_text(script)
    (work / 'stdin.txt').write_text(payload)
    cmd = [str(args.natemu.resolve()), '--runtime', str(args.runtime.resolve()), '--fexc', str(args.fexc.resolve()),
           '--kernel', str(KERNEL), '--initrd', str(INITRD), '--ram', str(8 << 30), '--vcpus', str(n),
           '--cmdline', 'console=ttyS0 earlyprintk=serial mitigations=off nokaslr bench=shell',
           '--compile-cache', 'off', '--serial', str(serial.resolve()), '--serial-stdin']
    (work / 'command.json').write_text(json.dumps(cmd, indent=2) + '\n')
    started, timed_out, forced, setup_error = time.monotonic(), False, False, None
    env = dict(os.environ)
    env.pop('NATEMU_PERF_LOG', None)
    with (work / 'stdout.txt').open('w') as stdout, (work / 'stderr.txt').open('w') as stderr:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, env=env)
        try:
            # Wait for the actual shell before handing it commands; boot-time UART resets can discard input.
            deadline = started + 180
            while p.poll() is None and time.monotonic() < deadline and not re.search(r'(?:^|\n)/[^\n]* # ', serial_text(serial)):
                time.sleep(0.1)
            if p.poll() is not None or time.monotonic() >= deadline:
                raise TimeoutError('guest shell did not become ready')
            p.stdin.write(b'stty -echo; echo CAPACITY-SHELL-READY\n'); p.stdin.flush()
            deadline = time.monotonic() + 30
            while p.poll() is None and time.monotonic() < deadline and not re.search(r'^CAPACITY-SHELL-READY$', serial_text(serial), re.M):
                time.sleep(0.1)
            if p.poll() is not None or time.monotonic() >= deadline:
                raise TimeoutError('shell handshake failed')
            p.stdin.write(payload.encode()); p.stdin.flush()
            p.wait(timeout=max(1, 600 - (time.monotonic() - started)))
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            timed_out, setup_error = True, str(exc)
        except (BrokenPipeError, OSError) as exc:
            setup_error = str(exc)
        finally:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    forced = True
                    p.kill(); p.wait()
            p.stdin.close()
    problems, detail = judge(serial_text(serial), n)
    if timed_out or forced or setup_error or p.returncode != 0:
        problems.append(f'transport rc={p.returncode} capped={timed_out} forced={forced} error={setup_error}')
    return dict(vcpus=n, ram_bytes=8 << 30, status='fail' if problems else 'pass', problems=problems,
                oracle=detail, seconds=time.monotonic() - started, process_rc=p.returncode,
                capped=timed_out, forced=forced,
                hashes={f.name: digest(f) for f in work.iterdir() if f.is_file()})


def self_check():
    good = '''CAPACITY-BEGIN cpus=8 online=0-7 kernel=6.12.107
CAPACITY-MEMTOTAL kb=8000000
CAPACITY-SWAPTOTAL kb=0
stress-ng, version 0.19.02
%s  /usr/bin/stress-ng
CAPACITY-STRESS-BEGIN
CAPACITY-RSS status=/proc/123/status rss_kb=4195000 anon_kb=4194304 swap_kb=0
stress-ng: metrc: [123] vm 262144 1.0 1.0 0.0
stress-ng: info: [123] passed: 1: vm (1)
stress-ng: info: [123] failed: 0
stress-ng: info: [123] skipped: 0
stress-ng: info: [123] successful run completed in 1s
CAPACITY-STRESS-END rc=0
CAPACITY-SWAP-END kb=0
CAPACITY-DMESG-END
CAPACITY-END rc=0
''' % ('a' * 64)
    assert not judge(good, 8)[0]
    for before, after in [('anon_kb=4194304', 'anon_kb=3000000'), ('swap_kb=0', 'swap_kb=1'),
                          ('vm 262144', 'vm 1'), ('cpus=8', 'cpus=1'), ('STRESS-END rc=0', 'STRESS-END rc=124'),
                          ('failed: 0', 'failed: 1'), ('skipped: 0', 'skipped: 1')]:
        assert judge(good.replace(before, after), 8)[0], before
    assert judge(good + 'BUG: synthetic negative control\n', 8)[0]
    assert len(guest_script(8).encode()) < 3500
    print('Nine parser controls passed; no VM launched.')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('natemu', 'runtime', 'fexc', 'out'):
        ap.add_argument('--' + name, type=pathlib.Path)
    ap.add_argument('--self-check', action='store_true')
    args = ap.parse_args()
    if args.self_check:
        self_check(); return 0
    if not all(getattr(args, k) for k in ('natemu', 'runtime', 'fexc', 'out')):
        ap.error('--natemu, --runtime, --fexc and --out are required')
    args.out.mkdir(parents=True, exist_ok=False)
    pins = {str(p.resolve()): digest(p) for p in (args.natemu, args.runtime, args.fexc, KERNEL, INITRD, SOURCE, pathlib.Path(__file__))}
    assert pins[str(SOURCE.resolve())] == SOURCE_SHA, 'public source archive differs from reviewed0.19.02'
    report = dict(planned=2, results=[], complete=False, pins=pins, stress_command=STRESS,
                  source_oracle='stress-vm.c:1450-1510,3529-3658,3796;4GiB two-pattern full-word pass,262144 ops',
                  limitations=['No particular PFN assertion or distinct-address pattern.', 'Guest resident evidence does not rule out host swapping.'])
    for n in (1, 8):
        row = run(args, n)
        report['results'].append(row)
        report['complete'] = len(report['results']) == 2
        (args.out / 'capacity-linux.json').write_text(json.dumps(report, indent=2) + '\n')
        print(f'N{n}: {row["status"]}', *row['problems'], sep='\n  ', flush=True)
        if row['forced']:
            break  # Orchestrator checks child cleanup before any subsequent VM.
    return int(not report['complete'] or any(r['status'] != 'pass' for r in report['results']))


if __name__ == '__main__':
    raise SystemExit(main())
