#!/usr/bin/env python3
"""Stock-Linux networking through the normal CLI (`--net`, user-mode NAT on libslirp). Correctness only; the throughput
lines are recorded, not judged. Each VM gets 180 s; the VM lock is held shared.

    python3 tools/run_net_linux.py prepare --out DIR [--transport mmio|pci]
    python3 tools/run_net_linux.py run --fexc FEXC --out DIR [--prepared DIR] [--cases C ...] [--vcpus N ...] [--offline] [--transport mmio|pci]
    python3 tools/run_net_linux.py self-check

prepare (run does it into OUT/prepared without --prepared): the Debian 6.12.107 kernel and its unchanged virtio_mmio/virtio_net/net_failover/failover modules, BusyBox and
the signed Debian curl/iperf3 closure staged on November 8 (natemu-raw/io-network-tools-20261108-01), with INIT below.
The forwarding and loopback cases use localhost only. Legacy outbound cases start HTTP (a 64 MiB random file)
and iperf3 servers on the host's LAN address; only those cases need a LAN address. Every configured-driver case
brings eth0 up with DHCP and must get 10.0.2.15, router 10.0.2.2 and
DNS 10.0.2.3, then:
  forward  TCP localhost:15201 -> guest:5201 with host loopback disabled, before and after driver reset/DHCP.
  loopback-off  exact host HTTP controls before/after, a forwarded TCP session, then guest HTTP to 10.0.2.2:8000 must fail.
  loopback-on   opt-in permits TCP and UDP forwards, both before and after reset/DHCP with actual receiver bytes/datagrams;
           guest HTTP must fetch the exact blob, with host HTTP controls before/after. Neither loopback case needs LAN or Internet.
  basic    the driver negotiated checksum offloads both ways and EVENT_IDX; ping the gateway; the host file over HTTP, byte-exact (sha256); iperf3 TCP both ways and UDP at 50 Mbit/s
           (loss under 1%); with the internet (default; --offline skips): DNS for deb.debian.org and an HTTPS download
           of Debian's trixie Release file (curl, CA-verified TLS).
  rebind   three times: with HTTP downloads looping (at least 1 MiB received first), unbind and rebind the virtio_net
           driver (a full device reset under traffic) and DHCP again; then fetch the host file byte-exact.
  cancel   with a TCP forward installed and at least 1 MiB of receive progress from a live 60 s iperf3 stream, SIGINT natemu:
           it must exit 2 within 10 s with every stop Cancelled; the forward listener must be released. Host loopback stays off.
  reboot   fetch byte-exact, then `reboot -f` with the network up: the run ends (natemu does not reboot) with Power(2),
           exit 2, no hang.
  nodriver virtio_net never loaded (the device stays unconfigured): the guest powers off cleanly.
Not in the default set (several minutes; `--cases stress`):
  stress   iperf3 --bidir with 8 streams for 60 s; 100 parallel streams; 500 short HTTP fetches 64 at a time (every one
           200); UDP floods at 2 Gbit/s both ways (loss allowed); then the host file byte-exact and the gateway ping
           (the network still works). Runs with --perf-log: the backend's drop counters are recorded.
Host-side checks: exit status and stop, no `natemu: network:` (libslirp guest-error) lines, and the run's wall time."""
import argparse
import copy
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
import tempfile

from io_common import PCI_PROBE, ROOT, VM_LOCK, digest, patch, pci_errors, pci_self_test, stop_group, write_json
from raw_root import RAW_ROOT
from run_virtio_rng import KVER

KERNEL_DEB = RAW_ROOT / 'io3-block-20261108-01/prerequisites/linux-image-6.12.107+deb13-amd64_6.12.107-1_amd64.deb'
BLOCK_INPUTS = RAW_ROOT / 'io3-block-20261108-01/inputs-01'
TOOLS_ROOTFS = RAW_ROOT / 'io-network-tools-20261108-01/rootfs'
MODULES = {'virtio_mmio': [], 'failover': [], 'net_failover': ['failover'], 'virtio_net': ['net_failover', 'failover']}
APPLETS = ('sh', 'mount', 'modprobe', 'cat', 'grep', 'sed', 'awk', 'ip', 'udhcpc', 'nslookup', 'ping', 'sha256sum', 'ls',
           'sleep', 'kill', 'poweroff', 'reboot', 'nproc', 'echo', 'wc', 'head', 'seq', 'cut', 'timeout')
CASES = ('forward', 'loopback-off', 'loopback-on', 'basic', 'rebind', 'cancel', 'reboot', 'nodriver')
EXTRA_CASES = ('stress',)
LOCAL_CASES = ('forward', 'loopback-off', 'loopback-on', 'nodriver')
FORWARD_PORT = 15201
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
# Keep kernel warnings/errors on the console; info (including ACPI shutdown) must not split phase JSON.
echo 5 > /proc/sys/kernel/printk
arg() { sed -n "s/.*$1=\([^ ]*\).*/\1/p" /proc/cmdline; }
C=$(arg net_case); H=$(arg net_host); S=$(arg net_sha)
echo "NET-BEGIN case=$C cpus=$(nproc) kernel=$(cat /proc/sys/kernel/osrelease)"
modprobe virtio_mmio; ip link set lo up
if [ $C = nodriver ]; then echo "NET-NO-ETH0 $(ls /sys/class/net | grep -c eth0)"; echo NET-END; poweroff -f; fi
modprobe virtio_net
dhcp() { ip link set eth0 up; udhcpc -i eth0 -n -q -t 5 -T 2 -f -s /etc/dhcp.sh 2>&1 | grep NET-DHCP; }
fetch() { echo "NET-FETCH $1 $(curl -sS --max-time 60 http://$H:8000/blob | sha256sum | cut -d' ' -f1)"; }
forward() {
  P=$1
  timeout 25 iperf3 -s -1 -J -B 10.0.2.15 -p 5201 > /tmp/forward.json 2>/tmp/forward.err & L=$!
  R=0
  for i in $(seq 10); do
    if kill -0 $L 2>/dev/null && awk '$2 == "0F02000A:1451" && $4 == "0A" { found=1 } END { exit !found }' /proc/net/tcp; then
      R=1; echo "NET-FORWARD-READY $P pid=$L"; break
    fi
    sleep 1
  done
  if [ $R = 0 ]; then kill $L 2>/dev/null; fi
  wait $L; E=$?
  echo "NET-FORWARD-EXIT $P $E"
  sed "s/^/NET-FORWARD-JSON $P /" /tmp/forward.json
  sed "s/^/NET-FORWARD-ERROR $P /" /tmp/forward.err
}
reset_forward() {
  D=$(ls /sys/bus/virtio/drivers/virtio_net | grep '^virtio')
  echo $D > /sys/bus/virtio/drivers/virtio_net/unbind && echo "NET-UNBOUND $D"
  ls /sys/class/net | grep -q eth0 || echo NET-ETH0-GONE
  echo $D > /sys/bus/virtio/drivers/virtio_net/bind && echo "NET-REBOUND $D"
  dhcp
}
loopback() {
  curl -f -sS --connect-timeout 3 --max-time 8 http://10.0.2.2:8000/blob -o /tmp/loopback.blob
  E=$?; echo "NET-LOOPBACK-EXIT $E"
  if [ $E = 0 ]; then echo "NET-LOOPBACK-FETCH $(sha256sum /tmp/loopback.blob | cut -d' ' -f1)"; fi
}
dhcp
case $C in
forward)
  forward before-tcp; reset_forward; forward after-tcp;;
loopback-off)
  forward loopback-tcp; loopback;;
loopback-on)
  forward before-tcp; forward before-udp
  reset_forward
  forward after-tcp; forward after-udp
  loopback;;
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
  fetch first; echo NET-END; reboot -f;;
stress)
  iperf3 -c $H -t 60 -P 8 --bidir -f m 2>&1 | grep -E 'SUM.*(TX|RX)-C.* (sender|receiver)|error' | sed 's/^/NET-STRESS bidir /'
  iperf3 -c $H -t 5 -P 100 -f m 2>&1 | grep -E 'SUM.* (sender|receiver)|error' | sed 's/^/NET-STRESS p100 /'
  for i in $(seq 500); do printf 'url = "http://%s:8000/small"\noutput = "/dev/null"\n' $H; done > /tmp/urls
  echo "NET-STRESS conns $(curl -s --parallel --parallel-max 64 -K /tmp/urls -w '%{http_code}\n' | grep -c '^200$')"
  iperf3 -c $H -t 5 -u -b 2G -R -f m 2>&1 | grep -E 'receiver|error' | sed 's/^/NET-STRESS udp-rx /'
  iperf3 -c $H -t 5 -u -b 2G -f m 2>&1 | grep -E 'receiver|error' | sed 's/^/NET-STRESS udp-tx /'
  fetch after; ping -c 3 -W 2 10.0.2.2 >/dev/null && echo NET-PING-GATEWAY-OK;;
cancel)
  RX0=$(cat /sys/class/net/eth0/statistics/rx_bytes)
  iperf3 -c $H -t 60 -R >/tmp/cancel.log 2>&1 & L=$!
  echo "NET-CANCEL-START pid=$L rx_before=$RX0"
  R=0
  for i in $(seq 30); do
    RX=$(cat /sys/class/net/eth0/statistics/rx_bytes); DELTA=$((RX - RX0))
    if [ $DELTA -ge 1048576 ] && kill -0 $L 2>/dev/null; then
      echo "NET-CANCEL-PROGRESS pid=$L rx_delta=$DELTA alive=1"
      echo NET-CANCEL-READY; R=1; break
    fi
    sleep 1
  done
  if [ $R = 0 ]; then echo NET-CANCEL-NO-PROGRESS; kill $L 2>/dev/null; fi
  wait $L;;
esac
echo NET-END
poweroff -f
'''
# --transport pci: the guest prints the PCI probe before NET-END and drains the console (stty TCSADRAIN, as the other runners do)
# before poweroff -f / reboot -f, which would drop queued output.
INIT_PCI = patch(INIT, [
    ('C=$(arg net_case); H=$(arg net_host); S=$(arg net_sha)\n',
     'C=$(arg net_case); H=$(arg net_host); S=$(arg net_sha)\n' + PCI_PROBE.replace('@P@', 'NET')
     + 'drain_console() { m=$(stty -F /dev/console -g) && stty -F /dev/console "$m"; }\n'),
    ('  fetch first; echo NET-END; reboot -f;;', '  fetch first; pci_probe; echo NET-END; drain_console; reboot -f;;'),
    ('esac\necho NET-END\npoweroff -f\n', 'esac\npci_probe\necho NET-END\ndrain_console\npoweroff -f\n')])
APPLETS_PCI = APPLETS + ('dmesg', 'stty')


def prepare(out, transport='mmio'):
    pci = transport == 'pci'
    out.mkdir(parents=True)
    tree = out / 'initramfs'
    shutil.copytree(TOOLS_ROOTFS, tree, symlinks=True)     # Debian layout: lib -> usr/lib
    (tree / 'bin').mkdir()
    shutil.copy2(BLOCK_INPUTS / 'bootstrap/bin/busybox', tree / 'bin/busybox')
    for name in APPLETS_PCI if pci else APPLETS: (tree / 'bin' / name).symlink_to('busybox')
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
    for path, text in (('etc/dhcp.sh', DHCP_SCRIPT), ('init', INIT_PCI if pci else INIT)):
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


def forward_protocols(case):
    if case == 'loopback-on': return ('tcp', 'udp')
    return ('tcp',) if case in ('forward', 'loopback-off', 'cancel') else ()


def forward_phases(case):
    if case == 'loopback-on': return ('before-tcp', 'before-udp', 'after-tcp', 'after-udp')
    if case == 'forward': return ('before-tcp', 'after-tcp')
    return ('loopback-tcp',) if case == 'loopback-off' else ()


def network_summary(case):
    policy = ('enabled: 10.0.2.2:PORT -> host 127.0.0.1:PORT' if case == 'loopback-on' else 'disabled')
    return ['libslirp IPv4; DHCP expected 10.0.2.15; gateway 10.0.2.2; DNS 10.0.2.3', f'host loopback {policy}'] + [
        f'{protocol} 127.0.0.1:{FORWARD_PORT} -> 10.0.2.15:5201' for protocol in forward_protocols(case)]


def cancel_ready(text):
    starts = re.findall(r'^NET-CANCEL-START pid=([1-9]\d*) rx_before=(\d+)$', text, re.M)
    progress = re.findall(r'^NET-CANCEL-PROGRESS pid=([1-9]\d*) rx_delta=(\d+) alive=1$', text, re.M)
    return (len(re.findall(r'^NET-CANCEL-START(?: |$)', text, re.M)) == len(starts) == 1
            and len(re.findall(r'^NET-CANCEL-PROGRESS(?: |$)', text, re.M)) == len(progress) == 1
            and starts[0][0] == progress[0][0] and int(progress[0][1]) >= 1 << 20
            and re.findall(r'^NET-CANCEL-READY$', text, re.M) == ['NET-CANCEL-READY']
            and text.index('NET-CANCEL-START ') < text.index('NET-CANCEL-PROGRESS ') < text.index('NET-CANCEL-READY'))


def receiver_ok(output, udp, server=True):
    """Require the actual receiver's JSON, not the sender/control connection's byte count."""
    try:
        data = json.loads(output)
        if 'error' in data: return False
        received = data['end']['sum_received']
        # iperf's TCP client labels sum_received with the local sender role; the guest server must be a receiver.
        return (received['bytes'] > 0 and (not server or received.get('sender') is False) and
                (not udp or received['packets'] - received.get('lost_packets', 0) > 0))
    except (ValueError, KeyError, TypeError):
        return False


def judge(case, text, stderr, code, elapsed, blob_sha, internet, pci=None, host_results=None):
    """PCI: the vCPU count when the run used --transport pci (adds the PCI probe checks), else None."""
    reasons = []
    def require(ok, reason):
        if not ok: reasons.append(reason)
    def lines(tag): return re.findall(rf'^{tag} ?(.*)$', text, re.M)
    require(len(lines('NET-BEGIN')) == 1 and lines('NET-BEGIN')[0].startswith(f'case={case} '), 'missing/wrong NET-BEGIN')
    dhcp = lines('NET-DHCP')
    require('natemu: network:' not in stderr, 'libslirp reported a guest error')
    require(re.findall(r'^natemu-net: (.*)$', stderr, re.M) == network_summary(case), 'missing/wrong network summary')
    host_results = host_results or {}
    protocols = forward_protocols(case)
    if protocols:
        require(host_results.get('release') == {protocol: True for protocol in protocols}, 'forward listeners not released')
    stops = re.findall(r'^natemu: (?!(?:cpu|fp|speculation|perf|template|compile) )(\S+)', stderr, re.M)   # the stops, not the startup lines `natemu: cpu ...`, `fp`, `speculation barriers`, `perf`, `template pool`, `compile cache`
    if case == 'cancel':
        require(dhcp and all(d == 'ip=10.0.2.15 mask=24 router=10.0.2.2 dns=10.0.2.3' for d in dhcp), f'DHCP lease {dhcp}')
        require(code == 2 and stops and all(s == 'Cancelled' for s in stops), f'cancel: exit {code}, stops {stops}')
        require(elapsed < 10, f'cancel took {elapsed:.1f} s')
        require(cancel_ready(text), 'cancel: missing live receive progress of at least 1 MiB before readiness')
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
    phases = forward_phases(case)
    if phases:
        ready = lines('NET-FORWARD-READY')
        require(len(ready) == len(phases) and all(re.fullmatch(re.escape(phase) + r' pid=[1-9]\d*', row)
                                                for phase, row in zip(phases, ready)), 'forward readiness phases')
        require(lines('NET-FORWARD-EXIT') == [f'{phase} 0' for phase in phases], 'forward guest server exits')
        require(not lines('NET-FORWARD-ERROR'), 'forward guest server errors')
        clients = host_results.get('clients', [])
        require([client.get('phase') for client in clients] == list(phases), 'forward host client phases')
        for phase in phases:
            outputs = lines('NET-FORWARD-JSON ' + phase)
            require(receiver_ok('\n'.join(outputs), phase.endswith('udp')), f'{phase}: no guest receiver bytes/datagrams')
        for client in clients:
            require(client.get('exit') == 0 and client.get('timeout') is False, f"{client.get('phase')}: host client failed")
            require(receiver_ok(client.get('stdout', ''), client.get('phase', '').endswith('udp'), server=False),
                    f"{client.get('phase')}: no host-reported receiver bytes/datagrams")
        require(len(dhcp) == (2 if case in ('forward', 'loopback-on') else 1), 'forward DHCP lease count')
    if case in ('forward', 'loopback-on'):
        require(len(lines('NET-UNBOUND')) == 1 and lines('NET-UNBOUND') == lines('NET-REBOUND') and lines('NET-ETH0-GONE') == [''],
                'forward driver reset')
        # The post-reset sessions must follow the fresh DHCP lease.
        before = 'before-udp' if case == 'loopback-on' else 'before-tcp'
        require(text.find(f'NET-FORWARD-EXIT {before} ') < text.find('NET-UNBOUND ') < text.find('NET-ETH0-GONE') < text.find('NET-REBOUND ')
                < text.rfind('NET-DHCP ') < text.find('NET-FORWARD-READY after-tcp'), 'forward reset/lease phase ordering')
    if case in ('loopback-off', 'loopback-on'):
        controls = host_results.get('http', [])
        require([control.get('phase') for control in controls] == ['before', 'after'] and all(
            control.get('exit') == 0 and control.get('sha256') == blob_sha and control.get('bytes') == BLOB
            for control in controls), 'host loopback HTTP positive controls')
        if case == 'loopback-off':
            require(len(lines('NET-LOOPBACK-EXIT')) == 1 and lines('NET-LOOPBACK-EXIT')[0] in ('7', '28', '52', '56')
                    and not lines('NET-LOOPBACK-FETCH'), 'default host loopback access was not denied')
        else:
            require(lines('NET-LOOPBACK-EXIT') == ['0'] and lines('NET-LOOPBACK-FETCH') == [blob_sha],
                    'enabled host loopback HTTP not byte-exact')
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


def listener_release(protocols, port=FORWARD_PORT, address='127.0.0.1'):
    """TCP listen distinguishes a live listener from TIME_WAIT; UDP must not permit reuse."""
    released = {}
    for protocol in protocols:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM if protocol == 'tcp' else socket.SOCK_DGRAM) as listener:
                if protocol == 'tcp': listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((address, port))
                if protocol == 'tcp': listener.listen(1)
            released[protocol] = True
        except OSError:
            released[protocol] = False
    return released


def http_control(work, phase):
    """An exact, bounded fetch through localhost; bypass proxy environment settings."""
    output = work / f'http-{phase}.blob'
    command = ['curl', '--noproxy', '*', '-f', '-sS', '--connect-timeout', '2', '--max-time', '8',
               'http://127.0.0.1:8000/blob', '-o', str(output)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        stop_group(process); stdout, stderr = process.communicate()
    except BaseException:
        stop_group(process); raise
    result = {'phase': phase, 'command': command, 'exit': process.returncode,
              'sha256': digest(output) if output.exists() else None, 'bytes': output.stat().st_size if output.exists() else 0}
    (work / f'http-{phase}.stdout').write_bytes(stdout); (work / f'http-{phase}.stderr').write_bytes(stderr)
    if output.exists(): output.unlink()  # the retained fixture already contains the expected bytes
    write_json(work / f'http-{phase}.json', result)
    return result


def launch(args, case, vcpus, host, blob_sha, work):
    cmdline = (f'console=ttyS0 mitigations=off nokaslr net_case={case} net_host={host} net_sha={blob_sha} '
               f'net_internet={0 if args.offline else 1}')
    command = [str(args.natemu), '--runtime', str(args.runtime), '--fexc', str(args.fexc), '--kernel', str(args.prepared / 'linux'),
               '--initrd', str(args.prepared / 'initramfs.cpio'), '--net', '--vcpus', str(vcpus), '--ram', '1G',
               '--compile-cache', 'off', '--serial', str(work / 'serial.log'), '--cmdline', cmdline]
    if args.transport == 'pci': command += ['--virtio-transport', 'pci']
    if case == 'stress': command[1:1] = ['--perf-log', str(work / 'perf.log')]
    for protocol in forward_protocols(case): command += ['--net-forward', f'{protocol}:{FORWARD_PORT}:5201']
    if case == 'loopback-on': command += ['--net-host-loopback']
    limit = 400 if case == 'stress' else 180
    write_json(work / 'command.json', {'command': command})
    env = dict(os.environ); env.pop('NATEMU_PERF_LOG', None)
    started, timeout, signalled = time.monotonic(), False, None
    clients, active = [], None
    phases = forward_phases(case)
    def finish_client(timed_out=False):
        nonlocal active
        child, phase, began, stdout, stderr, argv = active
        if child.poll() is None: stop_group(child)
        stdout.close(); stderr.close()
        clients.append({'phase': phase, 'command': argv, 'exit': child.returncode, 'timeout': timed_out,
                        'seconds': round(time.monotonic() - began, 2),
                        'stdout': (work / f'client-{phase}.stdout').read_text(errors='replace'),
                        'stderr': (work / f'client-{phase}.stderr').read_text(errors='replace')})
        write_json(work / 'host-clients.json', clients)
        active = None
    with (work / 'stdout.log').open('wb') as out, (work / 'stderr.log').open('wb') as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, start_new_session=True)
        try:
            while process.poll() is None:
                now = time.monotonic()
                if now - started > limit or (signalled is not None and now - signalled >= 10):
                    timeout = True; stop_group(process); break
                serial = work / 'serial.log'
                text = serial.read_text(errors='replace') if serial.exists() else ''
                if active is not None and (active[0].poll() is not None or now - active[2] >= 20):
                    finish_client(active[0].poll() is None)
                if active is None and len(clients) < len(phases):
                    phase = phases[len(clients)]
                    if re.search(r'^NET-FORWARD-READY ' + re.escape(phase) + r' pid=[1-9]\d*$', text, re.M):
                        argv = ['iperf3', '-c', '127.0.0.1', '-p', str(FORWARD_PORT), '-t', '2', '-J', '--connect-timeout', '5000']
                        if phase.endswith('udp'): argv += ['-u', '-b', '4M', '-l', '1200']
                        stdout = (work / f'client-{phase}.stdout').open('wb'); stderr = (work / f'client-{phase}.stderr').open('wb')
                        try:
                            child = subprocess.Popen(argv, stdout=stdout, stderr=stderr, start_new_session=True)
                        except BaseException:
                            stdout.close(); stderr.close(); raise
                        active = child, phase, now, stdout, stderr, argv
                if case == 'cancel' and signalled is None and cancel_ready(text):
                    process.send_signal(signal.SIGINT); signalled = time.monotonic()
                time.sleep(0.1)
        except BaseException: stop_group(process); raise
        finally:
            if active is not None:
                # A guest can shut down just before its final client finishes reading the result.
                try: active[0].wait(timeout=max(0, min(2, 20 - (time.monotonic() - active[2]))))
                except subprocess.TimeoutExpired: pass
                finish_client(active[0].poll() is None)
    return process.returncode, timeout, time.monotonic() - (signalled or started), {'clients': clients}


def run(args):
    args.out.mkdir(parents=True)
    if args.prepared is None:
        args.prepared = args.out / 'prepared'; prepare(args.prepared, args.transport)
    manifest = json.loads((args.prepared / 'manifest.json').read_text())
    for name, sha in manifest['outputs'].items(): assert digest(args.prepared / name) == sha, name
    blob_sha = digest(args.prepared / 'serve/blob')
    needs_lan = any(case not in LOCAL_CASES for case in args.cases)
    host = (args.host or lan_address()) if needs_lan else '127.0.0.1'
    # Serialize the fixed HTTP, iperf and forwarded ports across both transports.
    ports = open(RAW_ROOT / '.net-ports.lock', 'a'); fcntl.flock(ports, fcntl.LOCK_EX)
    serve = ('import http.server as h, sys; h.ThreadingHTTPServer.request_queue_size = 512; '
             'h.ThreadingHTTPServer((sys.argv[1], 8000), h.SimpleHTTPRequestHandler).serve_forever()')
    http_hosts = set()
    if any(case in ('basic', 'rebind', 'reboot', 'stress') for case in args.cases): http_hosts.add(host)
    if any(case in ('loopback-off', 'loopback-on') for case in args.cases): http_hosts.add('127.0.0.1')
    fixtures = [(f'http-{address}', ['tcp'], 8000, address, [sys.executable, '-c', serve, address])
                for address in sorted(http_hosts)]
    if any(case in ('basic', 'cancel', 'stress') for case in args.cases):
        fixtures.append(('iperf', ['tcp', 'udp'], 5201, host, ['iperf3', '-s', '-B', host]))
    servers, results = [], []
    try:
        for name, protocols, port, address, command in fixtures:
            assert all(listener_release(protocols, port, address).values()), f'fixture port occupied: {address}:{port} {protocols}'
            with (args.out / f'{name}.log').open('wb') as output:
                child = subprocess.Popen(command, cwd=args.prepared / 'serve', stdout=output, stderr=output, start_new_session=True)
            servers.append(child)
            # Listen readiness without connecting to iperf (which would create a bogus session).
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and child.poll() is None:
                if not listener_release(('tcp',), port, address)['tcp']: break
                time.sleep(0.05)
            else:
                raise RuntimeError(f'fixture did not listen: {name}; see {name}.log')
        with open(args.lock, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            for case in args.cases:
                for vcpus in args.vcpus:
                    work = args.out / f'{case}-n{vcpus}'; work.mkdir()
                    protocols = forward_protocols(case)
                    assert all(listener_release(protocols).values()), f'fixture port occupied: 127.0.0.1:{FORWARD_PORT}'
                    assert all(server.poll() is None for server in servers), 'a host fixture exited'
                    controls = []
                    if case.startswith('loopback-'):
                        controls.append(http_control(work, 'before'))
                        assert controls[0]['exit'] == 0 and controls[0]['sha256'] == blob_sha, 'host HTTP fixture not byte-exact'
                    code, timeout, elapsed, evidence = launch(args, case, vcpus, host, blob_sha, work)
                    if protocols: evidence['release'] = listener_release(protocols)
                    if case.startswith('loopback-'):
                        controls.append(http_control(work, 'after')); evidence['http'] = controls
                    write_json(work / 'host-results.json', evidence)
                    serial = work / 'serial.log'
                    text = serial.read_text(errors='replace') if serial.exists() else ''
                    stderr = (work / 'stderr.log').read_text(errors='replace')
                    reasons = judge(case, text, stderr, code, elapsed, blob_sha, not args.offline,
                                    vcpus if args.transport == 'pci' else None, evidence)
                    if timeout: reasons.insert(0, 'timeout')
                    if any(server.poll() is not None for server in servers): reasons.append('a host fixture exited')
                    speeds = re.findall(r'^NET-(?:SPEED|STRESS|UDP) (.*)$', text, re.M)
                    perf = work / 'perf.log'
                    drops = re.findall(r'^net (\{.*\})$', perf.read_text(errors='replace'), re.M) if perf.exists() else []
                    results.append({'case': case, 'vcpus': vcpus, 'exit': code, 'seconds': round(elapsed, 2),
                                    'pass': not reasons, 'reasons': reasons, 'speeds': speeds,
                                    'drops': json.loads(drops[-1]) if drops else None})
                    print(f"{case} n={vcpus}: {'PASS' if not reasons else 'FAIL ' + '; '.join(reasons)} ({elapsed:.1f} s)"
                          + (f" drops {results[-1]['drops']}" if results[-1]['drops'] else ''), flush=True)
    finally:
        for server in servers: stop_group(server)
        ports.close()
        write_json(args.out / 'results.json', {'host': host, 'offline': args.offline, **({'transport': 'pci'} if args.transport == 'pci' else {}),
                                               'prepared': str(args.prepared), 'natemu': {str(args.natemu): digest(args.natemu)}, 'results': results})
    return 0 if all(r['pass'] for r in results) else 1


def runner_self_check():
    """Host-only checks: real occupied sockets, TIME_WAIT, and a synthetic process (no VM)."""
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0)); listener.listen(1)
        port = listener.getsockname()[1]
        assert listener_release(('tcp',), port) == {'tcp': False}, 'live TCP listener was hidden by reuse'
        with socket.create_connection(('127.0.0.1', port), timeout=1) as client:
            accepted, _ = listener.accept(); accepted.close()
            assert client.recv(1) == b''
    assert listener_release(('tcp',), port) == {'tcp': True}, 'TIME_WAIT falsely counted as a TCP listener'
    for reuse in (False, True):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            if reuse: listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
            assert listener_release(('udp',), port) == {'udp': False}, 'live UDP listener was hidden by reuse'
        assert listener_release(('udp',), port) == {'udp': True}
    with tempfile.TemporaryDirectory(prefix='natemu-net-self-check-') as temporary:
        work = Path(temporary)
        # Execute each real init prelude against a stand-in sysctl, before any phase records.
        printk = work / 'printk'
        for init in (INIT, INIT_PCI):
            printk.write_text('7\n')
            prelude = init.split('arg() {', 1)[0].replace(
                'mount -t proc proc /proc; mount -t sysfs sys /sys; mount -t devtmpfs dev /dev', ':')
            subprocess.run(['/bin/sh', '-eu'], input=prelude.replace('/proc/sys/kernel/printk', str(printk)),
                           text=True, capture_output=True, timeout=2, check=True)
            assert printk.read_text() == '5\n', 'console must retain warnings/errors while suppressing notice/info phase interleaving'
        fake = work / 'fake-natemu'
        fake.write_text('#!' + sys.executable + '\n' + '''import pathlib, signal, sys, time
args = sys.argv[1:]
assert '--net-host-loopback' not in args
assert 'tcp:15201:5201' in args and 'udp:15201:5201' not in args
signal.signal(signal.SIGINT, lambda *_: sys.exit(2))
proof = args[args.index('--fexc') + 1]
text = 'NET-CANCEL-START pid=42 rx_before=8192\\n'
if proof != 'missing': text += 'NET-CANCEL-PROGRESS pid=42 rx_delta=' + ('1048576' if proof == 'valid' else '0') + ' alive=1\\n'
text += 'NET-CANCEL-READY\\n'
pathlib.Path(args[args.index('--serial') + 1]).write_text(text)
time.sleep(1)
sys.exit(77)
''')
        fake.chmod(0o755)
        for proof in ('missing', 'zero', 'valid'):
            case_dir = work / proof; case_dir.mkdir()
            args = argparse.Namespace(natemu=fake, runtime=work, fexc=Path(proof), prepared=work, offline=True, transport='mmio')
            code, timeout, elapsed, _ = launch(args, 'cancel', 2, '127.0.0.1', 'ab' * 32, case_dir)
            assert not timeout and code == (2 if proof == 'valid' else 77), (proof, code, timeout)
            if proof == 'valid': assert elapsed < 1, 'cancel used a fixed delay after progress proof'


def self_check():
    assert {case: forward_protocols(case) for case in ('forward', 'loopback-off', 'loopback-on', 'cancel')} == {
        'forward': ('tcp',), 'loopback-off': ('tcp',), 'loopback-on': ('tcp', 'udp'), 'cancel': ('tcp',)}, 'UDP requires loopback opt-in'
    assert {case: forward_phases(case) for case in ('forward', 'loopback-off', 'loopback-on')} == {
        'forward': ('before-tcp', 'after-tcp'), 'loopback-off': ('loopback-tcp',),
        'loopback-on': ('before-tcp', 'before-udp', 'after-tcp', 'after-udp')}, 'forward/reset case coverage'
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
        'cancel': (f'NET-BEGIN case=cancel cpus=2 kernel={KVER}\n{lease}'
                   'NET-CANCEL-START pid=42 rx_before=8192\nNET-CANCEL-PROGRESS pid=42 rx_delta=1048576 alive=1\nNET-CANCEL-READY\n',
                   'natemu: Cancelled\nnatemu: Cancelled\n', 2, 0.5),
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
    host = {'cancel': {'release': {'tcp': True}}}
    received = json.dumps({'end': {'sum_received': {'bytes': 2097152, 'packets': 2048, 'lost_packets': 0, 'sender': False}}})
    for case in ('forward', 'loopback-off', 'loopback-on'):
        text = f'NET-BEGIN case={case} cpus=2 kernel={KVER}\n{lease}'
        for phase in forward_phases(case):
            if phase == 'after-tcp': text += 'NET-UNBOUND virtio0\nNET-ETH0-GONE\nNET-REBOUND virtio0\n' + lease
            text += f'NET-FORWARD-READY {phase} pid=42\nNET-FORWARD-EXIT {phase} 0\nNET-FORWARD-JSON {phase} {received}\n'
        if case == 'loopback-off': text += 'NET-LOOPBACK-EXIT 7\n'
        if case == 'loopback-on': text += f'NET-LOOPBACK-EXIT 0\nNET-LOOPBACK-FETCH {sha}\n'
        good[case] = (text + 'NET-END\n', 'natemu: Power(1)\n', 0, 12.0)
        host[case] = {'release': {protocol: True for protocol in forward_protocols(case)},
                      'clients': [{'phase': phase, 'exit': 0, 'timeout': False,
                                   'stdout': received if phase.endswith('udp') else received.replace('false', 'true'), 'stderr': ''}
                                  for phase in forward_phases(case)]}
        if case.startswith('loopback-'):
            host[case]['http'] = [{'phase': phase, 'exit': 0, 'sha256': sha, 'bytes': BLOB} for phase in ('before', 'after')]
    good = {case: (text, ''.join(f'natemu-net: {line}\n' for line in network_summary(case)) + err, code, t)
            for case, (text, err, code, t) in good.items()}
    def check(case, text=None, err=None, evidence=None):
        original, stderr, code, elapsed = good[case]
        return judge(case, original if text is None else text, stderr if err is None else err, code, elapsed, sha, True,
                     host_results=host.get(case, {}) if evidence is None else evidence)
    for case in good: assert check(case) == [], (case, check(case))
    assert all(case in CASES for case in ('forward', 'loopback-off', 'loopback-on'))
    for case in good:
        text, err, code, elapsed = good[case]
        for line in re.findall(r'^natemu-net: .*\n', err, re.M):
            assert check(case, err=err.replace(line, '', 1)), (case, line)
            assert check(case, err=err + line), (case, line)
        assert check(case, err=err + 'natemu: network: bad\n')
        assert check(case, err=err + 'natemu: Halted\n')
        assert check(case, err=err.replace('host loopback disabled', 'host loopback enabled', 1)) if case != 'loopback-on' else True
    for case in ('forward', 'loopback-off', 'loopback-on'):
        text = good[case][0]
        for line in text.splitlines(keepends=True):
            assert check(case, text=text.replace(line, '', 1)), (case, 'removed', line)
            assert check(case, text=text.replace(line, line + line, 1)), (case, 'duplicated', line)
        for field in host[case]:
            mutated = copy.deepcopy(host[case]); del mutated[field]
            assert check(case, evidence=mutated), (case, field)
        for i, client in enumerate(host[case]['clients']):
            for key, value in (('exit', 1), ('timeout', True), ('stdout', '{}'), ('stdout', received.replace('2097152', '0')),
                               ('stdout', received.replace('2048', '0'))):
                if key == 'stdout' and '"packets": 0' in value and not client['phase'].endswith('udp'): continue
                mutated = copy.deepcopy(host[case]); mutated['clients'][i][key] = value
                assert check(case, evidence=mutated), (case, i, key, value)
        assert check(case, text=text.replace('2097152', '0', 1))
        assert check(case, text=text.replace('"sender": false', '"sender": true', 1))
        for phase in forward_phases(case):
            prefix = f'NET-FORWARD-JSON {phase} '
            assert check(case, text=text.replace(prefix, prefix[:20] + '[ 9.14] ACPI: PM: Preparing S5\n' + prefix[20:], 1)), phase
            assert check(case, text=text.replace(prefix + received, prefix + received[:-1], 1)), phase
        if case == 'loopback-on':
            for phase in ('before-udp', 'after-udp'):
                for broken in (received.replace('2048', '0'), received.replace('"lost_packets": 0', '"lost_packets": 2048')):
                    assert check(case, text=text.replace(f'NET-FORWARD-JSON {phase} ' + received,
                                                        f'NET-FORWARD-JSON {phase} ' + broken)), phase
        if case.startswith('loopback-'):
            for i in range(2):
                for field, value in (('exit', 1), ('sha256', 'cd' * 32), ('bytes', 0)):
                    mutated = copy.deepcopy(host[case]); mutated['http'][i][field] = value
                    assert check(case, evidence=mutated), (case, i, field)
    assert check('loopback-off', text=good['loopback-off'][0].replace('NET-LOOPBACK-EXIT 7', 'NET-LOOPBACK-EXIT 0'))
    assert check('loopback-on', err=good['loopback-on'][1].replace(
        'host loopback enabled: 10.0.2.2:PORT -> host 127.0.0.1:PORT', 'host loopback disabled'))
    assert check('loopback-on', text=good['loopback-on'][0].replace('NET-LOOPBACK-FETCH ' + sha, 'NET-LOOPBACK-FETCH ' + 'cd' * 32))
    for old, new in (('rx_delta=1048576', 'rx_delta=0'), ('rx_delta=1048576', 'rx_delta=1048575'), ('alive=1', 'alive=0'),
                     ('NET-CANCEL-START pid=42', 'NET-CANCEL-START pid=43'), ('NET-CANCEL-READY\n', '')):
        assert check('cancel', text=good['cancel'][0].replace(old, new)), old
    for line in good['cancel'][0].splitlines(keepends=True):
        assert check('cancel', text=good['cancel'][0].replace(line, '', 1)), line
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
        assert check(case, text=text.replace(old, new, 1)), (case, old)
    text, err, code, t = good['basic']
    assert judge('basic', text, err + 'natemu: network: bad\n', code, t, sha, True)
    assert judge('basic', text, 'natemu: Halted\n', 2, t, sha, True)
    assert judge('basic', text.replace('NET-HTTPS-OK\n', ''), err, code, t, sha, False) == []
    text, err, code, t = good['reboot']
    assert judge('reboot', text, 'natemu: Power(1)\n', 0, t, sha, True)
    text, err, code, t = good['nodriver']
    assert judge('nodriver', text, 'natemu: Halted\n', 2, t, sha, True)
    assert judge('nodriver', text, 'natemu: cpu x86-64-v3 +adx\nnatemu: fp fast, degraded: host lacks FEAT_AFP (x)\nnatemu: speculation barriers off\nnatemu: perf all host fast paths\nnatemu: template pool on\nnatemu: compile cache off\n' + err, code, t, sha, True) == []   # the startup line is no stop
    text, err, code, t = good['cancel']
    assert judge('cancel', text.replace('NET-CANCEL-READY\n', ''), err, code, t, sha, True), 'cancel accepted without traffic proof'
    assert judge('cancel', text, err, code, 12.0, sha, True)
    assert judge('cancel', text, 'natemu: Power(1)\n', 0, t, sha, True)
    text, err, code, t = good['basic']
    probe = ('NET-PCI-BEGIN\nNET-PCI-DEV 0000:00:00.0 0x8086 0x1237 0x060000\nNET-PCI-DEV 0000:00:02.0 0x1af4 0x1041 0x020000\n'
             'NET-PCI-IRQ  24:          0          0  PCI-MSIX-0000:00:02.0   0-edge      virtio0-config\n'
             'NET-PCI-IRQ  25:          2          0  PCI-MSIX-0000:00:02.0   1-edge      virtio0-input.0\n'
             'NET-PCI-DMESG [    1.9] virtio-pci 0000:00:02.0: enabling device (0000 -> 0002)\nNET-PCI-END\n')
    assert judge('basic', text.replace('NET-END\n', probe + 'NET-END\n'), err, code, t, sha, True, 2) == []
    assert judge('basic', text, err, code, t, sha, True, 2)                                    # no probe
    pci_self_test()
    for old, new in (('0x1041', '0x1042'), ('  2          0  PCI', '  0          0  PCI'), ('IRQ  25:          2', 'IRQ  25:          x'),
                     ('virtio-pci', "can't claim; no compatible bridge window: virtio-pci")):
        assert judge('basic', text.replace('NET-END\n', probe.replace(old, new) + 'NET-END\n'), err, code, t, sha, True, 2), old
    runner_self_check()
    print('self-check: ok (judge mutations, socket lifecycle, synthetic cancellation; no VM)')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    pp = sub.add_parser('prepare'); pp.add_argument('--out', type=Path, required=True)
    pp.add_argument('--transport', choices=('mmio', 'pci'), default='mmio', help='pci: the init with the PCI probe (what run --transport pci needs)')
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
    if args.command == 'prepare': print(json.dumps(prepare(args.out, args.transport)['outputs'], indent=2)); return 0
    if args.command == 'run': return run(args)
    return self_check()


if __name__ == '__main__':
    sys.exit(main())
