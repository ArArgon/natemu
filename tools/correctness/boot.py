#!/usr/bin/env python3
"""boot.py OUT NAME EMU VCPUS SUITE PART INITRD: one boot of a correctness initramfs (natemu stub or QEMU), serial to
OUT/NAME.serial, one JSON record appended to OUT/runs.jsonl. Kernel command line `console=ttyS0 mitigations=off nokaslr
ct=SUITE part=PART`. EMU: natemu (the stub: env STUB, RUNTIME, FEXC; compile cache off) or qemu (TCG thread=multi, the
like-for-like CPU model of natemu-raw/bench-20261105-03/scripts/bench.py). The run ends at the guest's poweroff after
CT-END, or at CAP seconds (env CT_CAP, default 3600). A foreign emulator process seen while running is recorded
(`contended`). Exit 0 when CT-END was reached, 1 otherwise. Run it through vm.sh (the VM lock and the idle check)."""
import json, os, re, signal, subprocess, sys, time

RAW = '/Users/liaozping/orca/natemu-raw'
K = f'{RAW}/kernel-census-20261028-01/dl/linux'                     # Debian 6.12.107 bzImage (= the play kernel)
QEMU = '/opt/homebrew/bin/qemu-system-x86_64'
QCPU = 'Haswell,-pcid,-hle,-rtm,-invpcid,-tsc-deadline,-erms,-mca,-mce,+sha-ni,+adx,+clflushopt,+clwb,+smep,+umip'
out, name, emu, n, suite, part, initrd = sys.argv[1:8]
cap = float(os.environ.get('CT_CAP', '3600'))
cmdline = f'console=ttyS0 mitigations=off nokaslr ct={suite} part={part}'
os.makedirs(out, exist_ok=True)
serial = f'{out}/{name}.serial'
if os.path.exists(serial): os.remove(serial)
env = dict(os.environ, NATEMU_COMPILE_CACHE='off'); env.pop('NATEMU_PERF_LOG', None)
if emu == 'natemu':
    cmd = [env['STUB'], '--runtime', env['RUNTIME'], '--fexc', env['FEXC'], '--kernel', K, '--initrd', initrd, '--cmdline', cmdline, '--vcpus', n,
           '--serial', serial]
else:
    cmd = [QEMU, '-nodefaults', '-machine', 'pc,hpet=off,i8042=off,vmport=off', '-accel', 'tcg,thread=multi', '-cpu', QCPU,
           '-m', '2048', '-smp', n, '-kernel', K, '-initrd', initrd, '-append', cmdline, '-display', 'none', '-monitor', 'none',
           '-serial', f'file:{serial}', '-no-reboot']

def foreign(me):
    found = set()
    for x in ('natemu-host', 'natemu', 'qemu-system-x86_64'):
        for q in subprocess.run(['pgrep', '-x', x], capture_output=True, text=True).stdout.split():
            if int(q) != me: found.add(f'{q} {x}')
    return found

t0 = time.monotonic()
p = subprocess.Popen(cmd, env=env, stdout=open(f'{out}/{name}.stdout', 'w'), stderr=open(f'{out}/{name}.stderr', 'w'))
others, last, end_at, begin_at = set(), 0.0, None, None
while time.monotonic() - t0 < cap and p.poll() is None:
    time.sleep(0.5)
    now = time.monotonic()
    if now - last >= 5: others |= foreign(p.pid); last = now
    try: text = open(serial, errors='replace').read()
    except FileNotFoundError: continue
    if begin_at is None and 'CT-BEGIN' in text: begin_at = now
    if end_at is None and 'CT-END' in text: end_at = now
    if end_at is not None and now - end_at > 30: break          # powered off late: stop it
if p.poll() is None:
    p.send_signal(signal.SIGINT)
    try: p.wait(10)
    except subprocess.TimeoutExpired: p.kill(); p.wait()
text = open(serial, errors='replace').read() if os.path.exists(serial) else ''
m = re.search(r'CT-END .*guest_s=([0-9.]+)', text)
rec = dict(name=name, emu=emu, vcpus=int(n), suite=suite, part=part, initrd=initrd, reached=end_at is not None,
           results=text.count('CT-RESULT '), wall_s=round(end_at - begin_at, 1) if end_at and begin_at else None,
           guest_s=float(m.group(1)) if m else None, total_s=round(time.monotonic() - t0, 1), rc=p.returncode,
           contended=sorted(others), cmd=cmd, stderr=open(f'{out}/{name}.stderr').read()[-400:])
open(f'{out}/runs.jsonl', 'a').write(json.dumps(rec) + '\n')
print(json.dumps({k: rec[k] for k in ('name', 'reached', 'results', 'wall_s', 'guest_s', 'total_s', 'rc', 'contended')}))
sys.exit(0 if end_at is not None else 1)
