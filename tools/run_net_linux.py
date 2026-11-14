#!/usr/bin/env python3
"""Stock-Linux networking through the normal CLI (`--net`, user-mode NAT on libslirp). Correctness only; the throughput
lines are recorded, not judged. Each VM gets 180 s; the VM lock is held shared.

    python3 tools/run_net_linux.py prepare --out DIR
    python3 tools/run_net_linux.py run --fexc FEXC --out DIR [--prepared DIR] [--cases C ...] [--vcpus N ...] [--offline] [--transport mmio|pci]
    python3 tools/run_net_linux.py self-check

prepare (run does it into OUT/prepared without --prepared): the Debian 6.12.107 kernel and its unchanged virtio_mmio/virtio_net/net_failover/failover modules, BusyBox and
the signed Debian curl/iperf3 closure staged on November 8 (natemu-raw/io-network-tools-20261108-01), with INIT below.
run starts an HTTP server (a 64 MiB random file) and an iperf3 server on the host's LAN address: the guest cannot
reach the host's loopback, by design (so the host needs a LAN address). Every case brings eth0 up with DHCP and must get 10.0.2.15, router 10.0.2.2 and
DNS 10.0.2.3, then:
  basic    the driver negotiated checksum offloads both ways and EVENT_IDX; ping the gateway; the host file over HTTP, byte-exact (sha256); iperf3 TCP both ways and UDP at 50 Mbit/s
           (loss under 1%); with the internet (default; --offline skips): DNS for deb.debian.org and an HTTPS download
           of Debian's trixie Release file (curl, CA-verified TLS).
  rebind   three times: with HTTP downloads looping (at least 1 MiB received first), unbind and rebind the virtio_net
           driver (a full device reset under traffic) and DHCP again; then fetch the host file byte-exact.
  cancel   with a 60 s receive stream running, SIGINT natemu: it must exit 2 within 10 s with every stop Cancelled.
  reboot   fetch byte-exact, then `reboot -f` with the network up: the run ends (natemu does not reboot) with Power(2),
           exit 2, no hang.
  nodriver virtio_net never loaded (the device stays unconfigured): the guest powers off cleanly.
Not in the default set (several minutes; `--cases stress`):
  stress   iperf3 --bidir with 8 streams for 60 s; 100 parallel streams; 500 short HTTP fetches 64 at a time (every one
           200); UDP floods at 2 Gbit/s both ways (loss allowed); then the host file byte-exact and the gateway ping
           (the network still works). Runs with --perf-log: the backend's drop counters are recorded.
Host-side checks: exit status and stop, no `natemu: network:` (libslirp guest-error) lines, and the run's wall time."""
import argparse
import fcntl
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time

from io_common import PCI_PROBE, ROOT, VM_LOCK, digest, pci_errors, stop_group, write_json
from raw_root import RAW_ROOT
from run_virtio_rng import KVER

KERNEL_DEB = RAW_ROOT / 'io3-block-20261108-01/prerequisites/linux-image-6.12.107+deb13-amd64_6.12.107-1_amd64.deb'
BLOCK_INPUTS = RAW_ROOT / 'io3-block-20261108-01/inputs-01'
TOOLS_ROOTFS = RAW_ROOT / 'io-network-tools-20261108-01/rootfs'
MODULES = {'virtio_mmio': [], 'failover': [], 'net_failover': ['failover'], 'virtio_net': ['net_failover', 'failover']}
APPLETS = ('sh', 'mount', 'modprobe', 'cat', 'grep', 'sed', 'awk', 'ip', 'udhcpc', 'nslookup', 'ping', 'sha256sum', 'ls',
           'sleep', 'kill', 'poweroff', 'reboot', 'nproc', 'echo', 'wc', 'head', 'seq', 'dmesg')
CASES = ('basic', 'rebind', 'cancel', 'reboot', 'nodriver')
EXTRA_CASES = ('stress',)
BLOB = 64 << 20
DHCP_SCRIPT = '''#!/bin/sh
case "$1" in bound|renew)
  ip addr flush dev $interface; ip addr add $ip/$mask dev $interface; ip route add default via $router
  : > /etc/resolv.conf; for d in $dns; do echo "nameserver $d" >> /etc/resolv.conf; done
  echo "NET-DHCP ip=$ip mask=$mask router=$router dns=$dns";;
esac
'''
INIT = r'''#!/bin/sh
export PATH=/bin:/usr/bin:/usr/sbin
mount -t proc proc /proc; mount -t sysfs sys /sys; mount -t devtmpfs dev /dev
arg() { sed -n "s/.*$1=\([^ ]*\).*/\1/p" /proc/cmdline; }
C=$(arg net_case); H=$(arg net_host); S=$(arg net_sha)
@PCI_PROBE@maybe_probe() { if [ "$(arg net_pci)" = 1 ]; then pci_probe; fi; }
echo "NET-BEGIN case=$C cpus=$(nproc) kernel=$(cat /proc/sys/kernel/osrelease)"
modprobe virtio_mmio; ip link set lo up
if [ $C = nodriver ]; then echo "NET-NO-ETH0 $(ls /sys/class/net | grep -c eth0)"; echo NET-END; poweroff -f; fi
modprobe virtio_net
dhcp() { ip link set eth0 up; udhcpc -i eth0 -n -q -t 5 -T 2 -f -s /etc/dhcp.sh 2>&1 | grep NET-DHCP; }
fetch() { echo "NET-FETCH $1 $(curl -sS --max-time 60 http://$H:8000/blob | sha256sum | cut -d' ' -f1)"; }
dhcp
case $C in
basic)
  echo "NET-FEATURES $(cat /sys/class/net/eth0/device/features)"
  ping -c 3 -W 2 10.0.2.2 >/dev/null && echo NET-PING-GATEWAY-OK
  fetch first
  curl -sS --max-time 60 -o /dev/null -w 'NET-SPEED http %{size_download} %{speed_download}\n' http://$H:8000/blob
  iperf3 -c $H -t 5 -f m 2>&1 | grep -E ' sender|error' | sed 's/^/NET-SPEED tcp-tx /'
  iperf3 -c $H -t 5 -f m -R 2>&1 | grep -E ' sender|error' | sed 's/^/NET-SPEED tcp-rx /'
  iperf3 -c $H -t 3 -f m -u -b 50M 2>&1 | grep -E 'receiver|error' | sed 's/^/NET-UDP /'
  if [ "$(arg net_internet)" = 1 ]; then
    nslookup deb.debian.org 2>&1 | grep -q '^Address.*[0-9]\.[0-9]' && echo NET-DNS-OK
    curl -sS --max-time 30 https://deb.debian.org/debian/dists/trixie/Release -o /tmp/r && grep -m1 '^Codename: trixie' /tmp/r && echo NET-HTTPS-OK
  fi;;
rebind)
  D=$(ls /sys/bus/virtio/drivers/virtio_net | grep '^virtio')
  for i in 1 2 3; do
    (while :; do curl -s --max-time 10 -o /dev/null http://$H:8000/blob; done) & L=$!; sleep 2
    echo "NET-RX-BEFORE-RESET $(cat /sys/class/net/eth0/statistics/rx_bytes)"
    echo $D > /sys/bus/virtio/drivers/virtio_net/unbind && echo "NET-UNBOUND $D"
    ls /sys/class/net | grep -q eth0 || echo NET-ETH0-GONE
    echo $D > /sys/bus/virtio/drivers/virtio_net/bind && echo "NET-REBOUND $D"
    kill $L; dhcp
  done
  fetch rebound;;
reboot)
  fetch first; maybe_probe; echo NET-END; reboot -f;;
stress)
  iperf3 -c $H -t 60 -P 8 --bidir -f m 2>&1 | grep -E 'SUM.*(TX|RX)-C.* (sender|receiver)|error' | sed 's/^/NET-STRESS bidir /'
  iperf3 -c $H -t 5 -P 100 -f m 2>&1 | grep -E 'SUM.* (sender|receiver)|error' | sed 's/^/NET-STRESS p100 /'
  for i in $(seq 500); do printf 'url = "http://%s:8000/small"\noutput = "/dev/null"\n' $H; done > /tmp/urls
  echo "NET-STRESS conns $(curl -s --parallel --parallel-max 64 -K /tmp/urls -w '%{http_code}\n' | grep -c '^200$')"
  iperf3 -c $H -t 5 -u -b 2G -R -f m 2>&1 | grep -E 'receiver|error' | sed 's/^/NET-STRESS udp-rx /'
  iperf3 -c $H -t 5 -u -b 2G -f m 2>&1 | grep -E 'receiver|error' | sed 's/^/NET-STRESS udp-tx /'
  fetch after; ping -c 3 -W 2 10.0.2.2 >/dev/null && echo NET-PING-GATEWAY-OK;;
cancel)
  echo NET-CANCEL-READY; iperf3 -c $H -t 60 -R >/dev/null 2>&1;;
esac
maybe_probe
echo NET-END
poweroff -f
'''.replace('@PCI_PROBE@', PCI_PROBE.replace('@P@', 'NET'))


def prepare(out):
    out.mkdir(parents=True)
    tree = out / 'initramfs'
    shutil.copytree(TOOLS_ROOTFS, tree, symlinks=True)     # Debian layout: lib -> usr/lib
    (tree / 'bin').mkdir()
    shutil.copy2(BLOCK_INPUTS / 'bootstrap/bin/busybox', tree / 'bin/busybox')
    for name in APPLETS: (tree / 'bin' / name).symlink_to('busybox')
    for d in ('proc', 'sys', 'dev', 'tmp'): (tree / d).mkdir(exist_ok=True)
    modules, archive = {}, subprocess.check_output(['ar', 'p', str(KERNEL_DEB), 'data.tar.xz'], timeout=60)
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        for member in package:
            name = Path(member.name).name.removesuffix('.ko.xz')
            if name not in MODULES or not member.name.endswith('.ko.xz'): continue
            rel = member.name.split('/' + KVER + '/', 1)[1].removesuffix('.xz')
            data = lzma.decompress(package.extractfile(member).read())
            assert data[:4] == b'\x7fELF' and ('vermagic=' + KVER).encode() in data, rel
            dest = tree / 'usr/lib/modules' / KVER / rel
            dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(data)
            modules[name] = {'path': rel, 'sha256': digest(dest)}
    assert set(modules) == set(MODULES), 'missing stock kernel module'
    # BusyBox modprobe wants every transitive dependency on the module's line.
    (tree / 'usr/lib/modules' / KVER / 'modules.dep').write_text(
        ''.join(modules[m]['path'] + ':' + ''.join(' ' + modules[d]['path'] for d in deps) + '\n' for m, deps in MODULES.items()))
    for path, text in (('etc/dhcp.sh', DHCP_SCRIPT), ('init', INIT)):
        (tree / path).write_text(text); (tree / path).chmod(0o755)
    spec = importlib.util.spec_from_file_location('mkcpio', ROOT / 'tools/play/mkcpio.py')
    mkcpio = importlib.util.module_from_spec(spec); spec.loader.exec_module(mkcpio)
    mkcpio.main(str(tree), str(out / 'initramfs.cpio'))
    shutil.copyfile(BLOCK_INPUTS / 'linux', out / 'linux')
    (out / 'serve').mkdir(); (out / 'serve/blob').write_bytes(os.urandom(BLOB))   # its sha256 is the check
    (out / 'serve/small').write_bytes(os.urandom(4096))
    manifest = {'purpose': 'stock Linux networking check (user-mode NAT); not a performance claim', 'kernel_version': KVER,
                'modules': modules, 'sources': {str(p): digest(p) for p in (KERNEL_DEB, BLOCK_INPUTS / 'linux',
                                                                            BLOCK_INPUTS / 'bootstrap/bin/busybox', Path(__file__))},
                'tools_rootfs': str(TOOLS_ROOTFS),
                'outputs': {n: digest(out / n) for n in ('linux', 'initramfs.cpio', 'serve/blob', 'serve/small')}}
    write_json(out / 'manifest.json', manifest)
    return manifest


def lan_address():
    """The host's address on its default route (what the guest reaches through slirp)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(('192.0.2.1', 9))    # no packet is sent
        return s.getsockname()[0]


def judge(case, text, stderr, code, elapsed, blob_sha, internet, pci=None):
    """PCI: the vCPU count when the run used --transport pci (adds the PCI probe checks), else None."""
    reasons = []
    def require(ok, reason):
        if not ok: reasons.append(reason)
    def lines(tag): return re.findall(rf'^{tag} ?(.*)$', text, re.M)
    require(lines('NET-BEGIN') and lines('NET-BEGIN')[0].startswith(f'case={case} '), 'missing/wrong NET-BEGIN')
    dhcp = lines('NET-DHCP')
    require('natemu: network:' not in stderr, 'libslirp reported a guest error')
    stops = re.findall(r'^natemu: (\S+)', stderr, re.M)
    if case == 'cancel':
        require(dhcp and all(d == 'ip=10.0.2.15 mask=24 router=10.0.2.2 dns=10.0.2.3' for d in dhcp), f'DHCP lease {dhcp}')
        require(code == 2 and stops and all(s == 'Cancelled' for s in stops), f'cancel: exit {code}, stops {stops}')
        require(elapsed < 10, f'cancel took {elapsed:.1f} s')
        return reasons
    if case == 'nodriver':
        require(code == 0 and stops == ['Power(1)'], f'exit {code}, stops {stops}')
        require(lines('NET-NO-ETH0') == ['0'] and lines('NET-END') == [''] and not dhcp, 'nodriver: eth0 exists or no end')
        return reasons
    require(dhcp and all(d == 'ip=10.0.2.15 mask=24 router=10.0.2.2 dns=10.0.2.3' for d in dhcp), f'DHCP lease {dhcp}')
    if case == 'reboot':
        require(code == 2 and stops == ['Power(2)'], f'reboot: exit {code}, stops {stops}')
        require(lines('NET-FETCH') == [f'first {blob_sha}'] and lines('NET-END') == [''], 'reboot: fetch or end')
        if pci: reasons += pci_errors(text, 'NET', pci, ('net',))
        return reasons
    require(code == 0 and stops == ['Power(1)'], f'exit {code}, stops {stops}')
    require(lines('NET-END') == [''], 'missing NET-END')
    if pci and case != 'cancel': reasons += pci_errors(text, 'NET', pci, ('net',))
    if case == 'basic':
        features = lines('NET-FEATURES')
        # Bit n is character n: CSUM (0) and GUEST_CSUM (1) offloads, EVENT_IDX (29), VERSION_1 (32).
        require(len(features) == 1 and len(features[0]) >= 33 and all(features[0][b] == '1' for b in (0, 1, 29, 32)),
                f'negotiated features {features}')
        require(lines('NET-FETCH') == [f'first {blob_sha}'], 'HTTP file not byte-exact')
        require(lines('NET-PING-GATEWAY-OK') == [''], 'gateway ping')
        speeds = lines('NET-SPEED')
        require(len(speeds) == 3 and all('Mbits/sec' in s or s.startswith('http ') for s in speeds) and
                not any('error' in s for s in speeds), f'throughput lines {speeds}')
        udp = re.findall(r'\((\d+(?:\.\d+)?)%\)', ' '.join(lines('NET-UDP')))
        require(len(udp) == 1 and float(udp[0]) < 1, f'UDP loss {udp}')
        if internet:
            require(lines('NET-DNS-OK') == [''], 'DNS')
            require(lines('NET-HTTPS-OK') == [''], 'HTTPS')
    if case == 'rebind':
        before = lines('NET-RX-BEFORE-RESET')
        require(len(before) == 3 and all(b.isdigit() and int(b) >= 1 << 20 for b in before), f'no receive stream before a reset {before}')
        require(len(lines('NET-UNBOUND')) == 3 and len(lines('NET-REBOUND')) == 3, 'unbind/rebind')
        require(lines('NET-ETH0-GONE') == [''] * 3, 'eth0 survived unbind')
        require(len(dhcp) == 4, 'a DHCP lease after every rebind')
        require(lines('NET-FETCH') == [f'rebound {blob_sha}'], 'HTTP file after rebind not byte-exact')
    if case == 'stress':
        stress = lines('NET-STRESS')
        require(not any('error' in x for x in stress), f'stress errors {stress}')
        require(sum(x.startswith('bidir ') for x in stress) == 4, 'bidir: TX and RX sender/receiver sums')
        require(sum(x.startswith('p100 ') for x in stress) == 2, 'p100: sender/receiver sums')
        require('conns 500' in stress, 'not every short connection got 200')
        require(sum(x.startswith('udp-') for x in stress) == 2, 'UDP floods did not complete')
        require(lines('NET-FETCH') == [f'after {blob_sha}'], 'HTTP file after stress not byte-exact')
        require(lines('NET-PING-GATEWAY-OK') == [''], 'gateway ping after stress')
    return reasons


def launch(args, case, vcpus, host, blob_sha, work):
    cmdline = (f'console=ttyS0 mitigations=off nokaslr net_case={case} net_host={host} net_sha={blob_sha} '
               f'net_internet={0 if args.offline else 1}' + (' net_pci=1' if args.transport == 'pci' else ''))
    command = [str(args.natemu), '--runtime', str(args.runtime), '--fexc', str(args.fexc), '--kernel', str(args.prepared / 'linux'),
               '--initrd', str(args.prepared / 'initramfs.cpio'), '--net', '--vcpus', str(vcpus), '--ram', '1G',
               '--compile-cache', 'off', '--serial', str(work / 'serial.log'), '--cmdline', cmdline]
    if args.transport == 'pci': command += ['--virtio-transport', 'pci']
    if case == 'stress': command[1:1] = ['--perf-log', str(work / 'perf.log')]
    limit = 400 if case == 'stress' else 180
    write_json(work / 'command.json', {'command': command})
    env = dict(os.environ); env.pop('NATEMU_PERF_LOG', None)
    started, timeout, signalled = time.monotonic(), False, None
    with (work / 'stdout.log').open('wb') as out, (work / 'stderr.log').open('wb') as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, start_new_session=True)
        try:
            while process.poll() is None:
                if time.monotonic() - started > limit: timeout = True; stop_group(process); break
                serial = work / 'serial.log'
                if case == 'cancel' and signalled is None and serial.exists() and 'NET-CANCEL-READY' in serial.read_text(errors='replace'):
                    time.sleep(2); process.send_signal(signal.SIGINT); signalled = time.monotonic()
                time.sleep(0.1)
        except BaseException: stop_group(process); raise
    return process.returncode, timeout, time.monotonic() - (signalled or started)


def run(args):
    args.out.mkdir(parents=True)
    if args.prepared is None:
        args.prepared = args.out / 'prepared'; prepare(args.prepared)
    manifest = json.loads((args.prepared / 'manifest.json').read_text())
    for name, sha in manifest['outputs'].items(): assert digest(args.prepared / name) == sha, name
    blob_sha, host = digest(args.prepared / 'serve/blob'), args.host or lan_address()
    # http.server's default listen backlog (5) would refuse the stress case's 64 parallel connections.
    serve = ('import http.server as h, sys; h.ThreadingHTTPServer.request_queue_size = 512; '
             'h.ThreadingHTTPServer((sys.argv[1], 8000), h.SimpleHTTPRequestHandler).serve_forever()')
    servers = [subprocess.Popen([sys.executable, '-c', serve, host], cwd=args.prepared / 'serve',
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True),
               subprocess.Popen(['iperf3', '-s', '-B', host], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)]
    results = []
    try:
        time.sleep(1)
        # A stale server on the port would serve other bytes: require ours, alive and serving this blob.
        import urllib.request
        with urllib.request.urlopen(f'http://{host}:8000/blob', timeout=5) as r:
            assert all(s.poll() is None for s in servers) and int(r.headers['Content-Length']) == BLOB, 'host servers'
        with open(args.lock, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            for case in args.cases:
                for vcpus in args.vcpus:
                    work = args.out / f'{case}-n{vcpus}'; work.mkdir()
                    code, timeout, elapsed = launch(args, case, vcpus, host, blob_sha, work)
                    text = (work / 'serial.log').read_text(errors='replace')
                    stderr = (work / 'stderr.log').read_text(errors='replace')
                    reasons = ['timeout'] if timeout else judge(case, text, stderr, code, elapsed, blob_sha, not args.offline,
                                                                 vcpus if args.transport == 'pci' else None)
                    speeds = re.findall(r'^NET-(?:SPEED|STRESS|UDP) (.*)$', text, re.M)
                    perf = work / 'perf.log'
                    drops = re.findall(r'^net (\{.*\})$', perf.read_text(errors='replace'), re.M) if perf.exists() else []
                    results.append({'case': case, 'vcpus': vcpus, 'exit': code, 'seconds': round(elapsed, 2),
                                    'pass': not reasons, 'reasons': reasons, 'speeds': speeds,
                                    'drops': json.loads(drops[-1]) if drops else None})
                    print(f"{case} n={vcpus}: {'PASS' if not reasons else 'FAIL ' + '; '.join(reasons)} ({elapsed:.1f} s)"
                          + (f" drops {results[-1]['drops']}" if results[-1]['drops'] else ''), flush=True)
    finally:
        for s in servers: s.terminate(); s.wait()
    write_json(args.out / 'results.json', {'host': host, 'offline': args.offline, 'transport': args.transport, 'prepared': str(args.prepared),
                                            'natemu': {str(args.natemu): digest(args.natemu)}, 'results': results})
    return 0 if all(r['pass'] for r in results) else 1


def self_check():
    sha = 'ab' * 32
    lease = 'NET-DHCP ip=10.0.2.15 mask=24 router=10.0.2.2 dns=10.0.2.3\n'
    FEAT = '1101010000000000100000000000010010000000000000000000000000000000'
    good = {
        'basic': (f'NET-BEGIN case=basic cpus=2 kernel={KVER}\n{lease}NET-FEATURES {FEAT}\nNET-PING-GATEWAY-OK\nNET-FETCH first {sha}\n'
                  'NET-SPEED http 67108864 160000000\nNET-SPEED tcp-tx [  5] 0.00-5.01 sec 459 MBytes 768 Mbits/sec 0 sender\n'
                  'NET-SPEED tcp-rx [  5] 0.00-5.01 sec 794 MBytes 1331 Mbits/sec 0 sender\n'
                  'NET-UDP [  5] 0.00-3.00 sec 71.6 MBytes 200 Mbits/sec 0.019 ms 0/51388 (0%) receiver\n'
                  'NET-DNS-OK\nNET-HTTPS-OK\nNET-END\n', 'natemu: Power(1)\n', 0, 5.0),
        'rebind': (f'NET-BEGIN case=rebind cpus=2 kernel={KVER}\n{lease}'
                   + 'NET-RX-BEFORE-RESET 123456789\nNET-UNBOUND virtio0\nNET-ETH0-GONE\nNET-REBOUND virtio0\n' * 3
                   + f'{lease}' * 3 + f'NET-FETCH rebound {sha}\nNET-END\n', 'natemu: Power(1)\n', 0, 5.0),
        'cancel': (f'NET-BEGIN case=cancel cpus=2 kernel={KVER}\n{lease}NET-CANCEL-READY\n', 'natemu: Cancelled\nnatemu: Cancelled\n', 2, 0.5),
        'reboot': (f'NET-BEGIN case=reboot cpus=2 kernel={KVER}\n{lease}NET-FETCH first {sha}\nNET-END\n', 'natemu: Power(2)\n', 2, 5.0),
        'nodriver': (f'NET-BEGIN case=nodriver cpus=2 kernel={KVER}\nNET-NO-ETH0 0\nNET-END\n', 'natemu: Power(1)\n', 0, 3.0),
        'stress': (f'NET-BEGIN case=stress cpus=2 kernel={KVER}\n{lease}'
                   'NET-STRESS bidir [SUM][TX-C] 0.00-60.00 sec 1 MBytes 1 Mbits/sec 0 sender\n'
                   'NET-STRESS bidir [SUM][TX-C] 0.00-60.00 sec 1 MBytes 1 Mbits/sec receiver\n'
                   'NET-STRESS bidir [SUM][RX-C] 0.00-60.00 sec 1 MBytes 1 Mbits/sec 0 sender\n'
                   'NET-STRESS bidir [SUM][RX-C] 0.00-60.00 sec 1 MBytes 1 Mbits/sec receiver\n'
                   'NET-STRESS p100 [SUM] 0.00-5.00 sec 1 MBytes 1 Mbits/sec 0 sender\n'
                   'NET-STRESS p100 [SUM] 0.00-5.00 sec 1 MBytes 1 Mbits/sec receiver\nNET-STRESS conns 500\n'
                   'NET-STRESS udp-rx [  5] 0.00-5.00 sec 1 MBytes 1 Mbits/sec 0.1 ms 5/100 (5%) receiver\n'
                   'NET-STRESS udp-tx [  5] 0.00-5.00 sec 1 MBytes 1 Mbits/sec 0.1 ms 5/100 (5%) receiver\n'
                   f'NET-FETCH after {sha}\nNET-PING-GATEWAY-OK\nNET-END\n', 'natemu: Power(1)\n', 0, 100.0),
    }
    for case, (text, err, code, t) in good.items():
        assert judge(case, text, err, code, t, sha, True) == [], (case, judge(case, text, err, code, t, sha, True))
    mutations = [('basic', 'ip=10.0.2.15', 'ip=10.0.2.16'), ('basic', f'first {sha}', 'first ' + 'cd' * 32),
                 ('basic', '(0%)', '(5%)'), ('basic', 'NET-HTTPS-OK\n', ''), ('basic', 'NET-DNS-OK\n', ''),
                 ('basic', 'NET-PING-GATEWAY-OK\n', ''), ('basic', 'NET-FEATURES 11', 'NET-FEATURES 01'),
                 ('basic', 'NET-FEATURES 11', 'NET-FEATURES 10'), ('basic', 'NET-END\n', ''), ('basic', 'Mbits/sec 0 sender', 'error'),
                 ('rebind', 'NET-ETH0-GONE\n', ''), ('rebind', f'rebound {sha}', 'rebound ' + 'cd' * 32),
                 ('rebind', 'NET-REBOUND virtio0\n', ''), ('rebind', 'RESET 123456789', 'RESET 4096'),
                 ('reboot', f'first {sha}', 'first ' + 'cd' * 32), ('nodriver', 'NET-NO-ETH0 0', 'NET-NO-ETH0 1'),
                 ('stress', 'conns 500', 'conns 499'), ('stress', f'after {sha}', 'after ' + 'cd' * 32),
                 ('stress', 'NET-STRESS udp-tx', 'NET-X udp-tx'), ('stress', '0 sender', 'error'),
                 ('stress', 'NET-PING-GATEWAY-OK\n', ''), ('stress', 'NET-STRESS bidir [SUM][RX-C]', 'NET-X'), ('basic', 'case=basic', 'case=rebind')]
    for case, old, new in mutations:
        text, err, code, t = good[case]
        assert old in text, (case, old)
        assert judge(case, text.replace(old, new, 1), err, code, t, sha, True), (case, old)
    text, err, code, t = good['basic']
    assert judge('basic', text, err + 'natemu: network: bad\n', code, t, sha, True)
    assert judge('basic', text, 'natemu: Halted\n', 2, t, sha, True)
    assert judge('basic', text.replace('NET-HTTPS-OK\n', ''), err, code, t, sha, False) == []
    text, err, code, t = good['reboot']
    assert judge('reboot', text, 'natemu: Power(1)\n', 0, t, sha, True)
    text, err, code, t = good['nodriver']
    assert judge('nodriver', text, 'natemu: Halted\n', 2, t, sha, True)
    text, err, code, t = good['cancel']
    assert judge('cancel', text, err, code, 12.0, sha, True)
    assert judge('cancel', text, 'natemu: Power(1)\n', 0, t, sha, True)
    text, err, code, t = good['basic']
    probe = ('NET-PCI-BEGIN\nNET-PCI-DEV 0000:00:00.0 0x8086 0x1237 0x060000\nNET-PCI-DEV 0000:00:02.0 0x1af4 0x1041 0x020000\n'
             'NET-PCI-IRQ  24:          0          0  PCI-MSIX-0000:00:02.0   0-edge      virtio0-config\n'
             'NET-PCI-IRQ  25:          2          0  PCI-MSIX-0000:00:02.0   1-edge      virtio0-input.0\n'
             'NET-PCI-DMESG [    1.9] virtio-pci 0000:00:02.0: enabling device (0000 -> 0002)\nNET-PCI-END\n')
    assert judge('basic', text.replace('NET-END\n', probe + 'NET-END\n'), err, code, t, sha, True, 2) == []
    assert judge('basic', text, err, code, t, sha, True, 2)                                    # no probe
    for old, new in (('0x1041', '0x1042'), ('  2          0  PCI', '  0          0  PCI'), ('IRQ  25:          2', 'IRQ  25:          x'),
                     ('virtio-pci', "can't claim BAR 0: virtio-pci")):
        assert judge('basic', text.replace('NET-END\n', probe.replace(old, new) + 'NET-END\n'), err, code, t, sha, True, 2), old
    print('self-check: ok')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('prepare').add_argument('--out', type=Path, required=True)
    r = sub.add_parser('run')
    r.add_argument('--prepared', type=Path, help='a prepare output [default: prepare into OUT/prepared]')
    r.add_argument('--fexc', type=Path, required=True)
    r.add_argument('--out', type=Path, required=True)
    r.add_argument('--natemu', type=Path, default=ROOT / 'target/release/natemu')
    r.add_argument('--runtime', type=Path, default=ROOT / 'target/aarch64-unknown-none/release/natemu-runtime')
    r.add_argument('--cases', nargs='+', choices=CASES + EXTRA_CASES, default=list(CASES))
    r.add_argument('--vcpus', nargs='+', type=int, default=[2])
    r.add_argument('--host', help='the host address the guest connects to [default: the default route\'s]')
    r.add_argument('--offline', action='store_true', help='skip the DNS and HTTPS checks that need the internet')
    r.add_argument('--lock', type=Path, default=VM_LOCK)
    r.add_argument('--transport', choices=('mmio', 'pci'), default='mmio', help='virtio transport (--virtio-transport); pci adds a guest probe and its checks')
    sub.add_parser('self-check')
    args = parser.parse_args()
    if args.command == 'prepare': print(json.dumps(prepare(args.out)['outputs'], indent=2)); return 0
    if args.command == 'run': return run(args)
    return self_check()


if __name__ == '__main__':
    sys.exit(main())
