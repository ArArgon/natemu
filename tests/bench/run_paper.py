#!/usr/bin/env python3
"""guest-paper: a Paper (Minecraft server) start under natemu, from inputs the framework fetches (docs/benchmarks.md).

    tests/bench/run_paper.py run --inputs IO5_INPUTS --paper-inputs PAPER_INPUTS --out DIR --fexc FEXC --natemu NATEMU --runtime RUNTIME
                             [--vcpus 4] [--ram 2] [--starts 2] [--boots 3] [--speculation-barriers]

The guest is the stock Debian 13 cloud image IO5 boots (tests/io/run_io5.py: its prepared disk.raw, kernel and initramfs,
input io5-inputs), cloned per run and grown to 16 GiB, booted 1 + BOOTS times through the serial console:

  setup  apt's timers masked and time sync off for good; then air-gapped (checked): no default route, so past natemu's
         own user-mode network (its gateway and DNS) the guest reaches only this run's HTTP server, which serves the input paper-inputs
         (testkit/inputs.py:paper_inputs, every file a pinned download): the 12 Debian packages it installs from local
         files (OpenJDK 25 at JDK_VERSION and what it, growpart and fdisk need beyond the image), sha256-checked; the
         root partition grown; the Paper jar and Mojang's server jar it patches (placed where Paper's launcher looks, so
         it downloads nothing); the EULA run, a fixed world seed (SEED) and the first full start, which patches the jar
         and generates the world. Not timed.
         That start ends with `stop` and must exit with status 0 (the one clean-shutdown check); the server folder is
         then kept as /srv/paper.base.
  bench  (BOOTS times) every network link but lo goes down (checked: a link still up fails the boot), so nothing outside
         the guest can change a start (the boot itself has natemu's network until then, to fetch this script); then
         STARTS timed starts, each after an untimed restore of /srv/paper from /srv/paper.base and a drop of the page
         cache (so every start loads the same bytes, read the same way, the JDK's included): `java -Xms1G -Xmx1G -jar
         paper.jar --nogui` (HEAP: a fixed 1 GiB heap, whatever the guest's RAM) to Paper's "Done (...)!" line, then
         SIGKILL (what a start leaves behind goes with the next restore).

Each start's time is the host's: the arrival of the guest's "PAPER start" and "PAPER done" console lines (CLOCK_UPTIME_RAW,
as IO5 stamps its markers); Paper's own "Done (Xs)" and the time to the JVM's exit are recorded beside it. natemu runs with no compile
cache (--compile-cache off, as the other guest benchmarks): every boot compiles from nothing, so a boot's first start
makes its translations and later starts reuse those of unchanged code (the JDK's own machine code; JIT output is new
each time) from natemu's in-memory pool. The result is per start position: the median over the boots (by_position).
The guest reports what it ran with (installed packages' digest, JDK version, seed, links up) under each phase's facts. The case passes
--speculation-barriers, as the README advises: without it Linux turns on PTI, whose page-table switches natemu has no
fast path for yet (a start takes about 5x as long; docs/benchmarks.md, mitigations). Results: OUT/result.json and
OUT/bench.jsonl (one line). Exit 0 when every boot ended cleanly and every start reached "Done"; 3 when another VM ran
during a timed boot (contended: not comparable); 1 otherwise."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import json
import os
import re
import shutil
import signal
import statistics
from pathlib import Path

from io_common import clone, digest, stop_group, write_json
from run_io5 import CMDLINE, DISK_BYTES, launch, others, serve_files

JDK_VERSION = '25.0.5.1+2-1~deb13u1'          # openjdk-25-jre-headless, among the paper-deb-* packages (snapshot 20261120T000000Z)
JAR = 'paper-26.3-257.jar'
JAR_SHA = '64a5bd44910cb982b2648e9a35036cc95a6de4d561e15812aecf5411f84f7b01'   # = tests/resources.toml paper-jar
MC_VERSION = '26.3'
MOJANG_SHA = 'd052f14d7a173734fba553711e5b570162e2f2a313267ee31a21b975a679be64'  # = minecraft-server-26.3, and the Paper jar's own pin
SEED = 'natemu-bench'
HEAP = '-Xms1G -Xmx1G'                     # fixed, so the guest's RAM does not size it (Java's default: 1/4 of RAM); Mojang's own
                                              # server instructions use 1 GiB
LIMITS = {'setup': 5400, 'bench': 1800}       # seconds per boot

GUEST = r'''#!/bin/bash
# paper.sh PHASE HOST: markers "PAPER ..." on the console; each step's output in /root/paper/STEP.log.
P=$1 H=$2 L=/root/paper; mkdir -p $L
export DEBIAN_FRONTEND=noninteractive H     # H: run's bash -c needs it
say() { echo "PAPER $*"; }
span() { local n=$1 r; shift; if "$@" > $L/$n.log 2>&1; then r=ok; else r=fail:$?; fi; say step $n $r; [ $r = ok ]; }
run() { bash -o pipefail -c "$1"; }
# One server start: the JVM reads its console from a FIFO; "Done (" ends the timed part. END = stop: `stop` on its console
# and exit status 0 required; kill: SIGKILL. Fails when the JVM exits before "Done" or takes longer than the limit.
start() { local i=$1 end=$2 log=$L/start-$1.log
  rm -f /tmp/paper.in; mkfifo /tmp/paper.in; exec 3<>/tmp/paper.in; : > $log
  say start $i
  ( cd /srv/paper && exec java @HEAP@ -jar paper.jar --nogui < /tmp/paper.in >> $log 2>&1 ) & local pid=$!
  if timeout 900 grep -q -m1 'Done (' <(tail -n +1 -F --pid=$pid $log 2>/dev/null); then
    say done $i $(grep -o -m1 'Done ([0-9.]*s)' $log | tr -dc '0-9.')
  else say failed $i; kill $pid 2>/dev/null; exec 3>&-; return 1; fi
  if [ $end = stop ]; then echo stop >&3; else kill -9 $pid; fi
  wait $pid; local r=$?; exec 3>&-
  say exit $i $r; [ $end = kill ] || [ $r = 0 ]
}
say begin $P cpus=$(nproc) mem_mib=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo) kernel=$(uname -r)
say system $(systemctl is-system-running --wait)
if [ $P = setup ]; then
  # Nothing scheduled may run in a later boot: apt's timers masked, time sync off (it is the boot's one outside contact).
  span quiet run 'systemctl mask --now apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service &&
      { ! systemctl cat systemd-timesyncd.service >/dev/null 2>&1 || systemctl disable --now systemd-timesyncd.service; }' &&
  # No default route: past natemu's own network (its gateway and DNS), only the run's HTTP server is reachable.
  span airgap run 'gw=$(ip route show default | awk "{print \$3; exit}") && [ -n "$gw" ] && ip route add ${H%:*}/32 via $gw &&
      ip route del default && [ -z "$(ip route show default)" ]' &&
  say net routes=$(ip route | wc -l) default=$(ip route show default | wc -l) &&
  span fetch-debs run 'mkdir -p /root/debs && cd /root/debs && curl -fsS http://$H/debs.sha256 -o debs.sha256 &&
      while read -r s f; do curl -fsS "http://$H/debs/$f" -o "$f" || exit 1; done < debs.sha256 && sha256sum -c --quiet debs.sha256' &&
  span apt-install run 'apt-get install -y --no-install-recommends /root/debs/*.deb' &&
  span jdk-version run '[ "$(dpkg-query -W -f "\${Version}" openjdk-25-jre-headless)" = "@JDKV@" ]' &&
  say packages $(dpkg-query -W | sha256sum | cut -c1-16) java=$(java -version 2>&1 | head -1 | tr -d '"' | tr ' ' _) &&
  span grow run 'growpart /dev/vda 1 && resize2fs /dev/vda1' &&
  span fetch-paper run 'mkdir -p /srv/paper/cache && curl -fsS http://$H/paper.jar -o /srv/paper/paper.jar &&
      curl -fsS http://$H/mojang.jar -o /srv/paper/cache/mojang_@MCVER@.jar && cd /srv/paper &&
      printf "%s  paper.jar\n%s  cache/mojang_@MCVER@.jar\n" @JARSHA@ @MOJSHA@ | sha256sum -c' &&
  span java-version java -version &&
  span eula run 'cd /srv/paper && java @HEAP@ -jar paper.jar --nogui; grep -q eula=false eula.txt && sed -i s/eula=false/eula=true/ eula.txt' &&
  span seed run 'cd /srv/paper && touch server.properties && sed -i /^level-seed=/d server.properties && echo level-seed=@SEED@ >> server.properties' &&
  say seed $(grep '^level-seed=' /srv/paper/server.properties | cut -d= -f2) &&
  start 0 stop && say seed-used $(grep '^level-seed=' /srv/paper/server.properties | cut -d= -f2) &&
  span snapshot cp -a /srv/paper /srv/paper.base && span sync sync && say setup-ok
else
  # Timed starts depend on nothing outside the guest: every link but lo goes down (the script is already here).
  for d in $(ls /sys/class/net); do [ $d = lo ] || ip link set dev $d down; done
  up=$(ip -br link | awk '$1 != "lo" && $2 != "DOWN"' | wc -l); say net $up-up
  ok=1; [ $up = 0 ] || ok=0
  [ $ok = 1 ] && for i in $(seq 1 @STARTS@); do
    # The same files, read the same way: the snapshot restored, the page cache dropped (the JDK's too).
    span restore-$i run 'rm -rf /srv/paper && cp -a /srv/paper.base /srv/paper && sync && echo 3 > /proc/sys/vm/drop_caches' &&
      start $i kill || { ok=0; break; }
  done
  [ $ok = 1 ] && say bench-ok
fi
say df $(df -B1M --output=used / | tail -1) uptime=$(cut -d' ' -f1 /proc/uptime)
'''


def guest_script(n_starts):
    return (GUEST.replace('@JDKV@', JDK_VERSION).replace('@JARSHA@', JAR_SHA).replace('@MOJSHA@', MOJANG_SHA)
            .replace('@MCVER@', MC_VERSION).replace('@SEED@', SEED).replace('@HEAP@', HEAP).replace('@STARTS@', str(n_starts)))


def natemu_command(args, disk):
    return [args.natemu, '--runtime', args.runtime, '--fexc', args.fexc, '--kernel', args.inputs / 'vmlinuz',
            '--initrd', args.inputs / 'initrd.img', '--cmdline', CMDLINE, '--ram', f'{args.ram}G', '--vcpus', str(args.vcpus),
            '--serial-stdin', '--disk', disk, '--net', '--rng', '--compile-cache', 'off'] + \
           (['--speculation-barriers'] if args.speculation_barriers else [])


def markers(work, phase):
    """[(host seconds, marker words)] from launch's markers-PHASE.tsv."""
    f = work / f'markers-{phase}.tsv'
    if not f.exists(): return []
    rows = [l.split('\t', 1) for l in f.read_text().splitlines() if '\t' in l]
    return [(int(ns) / 1e9, text.split()[1:]) for ns, text in rows]


def starts(rows):
    """Per start: host seconds from "start i" to "done i" and from "done i" to "exit i", and Paper's own seconds."""
    at, out = {}, []
    for t, w in rows:
        if w[:1] in (['start'], ['done'], ['exit']) and len(w) > 1: at[(w[0], w[1])] = (t, w[2:])
    for (kind, i), (t, rest) in sorted(at.items(), key=lambda kv: kv[1][0]):
        if kind != 'done' or ('start', i) not in at: continue
        s = {'start': int(i), 'wall_to_done': round(t - at[('start', i)][0], 2),
             'paper_done': float(rest[0]) if rest and re.fullmatch(r'[0-9.]+', rest[0]) else None}
        if ('exit', i) in at: s.update(to_exit=round(at[('exit', i)][0] - t, 2), exit=int(at[('exit', i)][1][0]))
        out.append(s)
    return out


def run(args):
    args.out.mkdir(parents=True, exist_ok=True)
    recorded = json.loads((args.inputs / 'inputs.json').read_text())
    for name in ('disk.raw', 'vmlinuz', 'initrd.img'): assert digest(args.inputs / name) == recorded[name], name
    p = args.paper_inputs
    assert digest(p / 'paper.jar') == JAR_SHA, 'paper jar'
    assert digest(p / 'mojang.jar') == MOJANG_SHA, 'mojang jar'
    listed = dict(l.split()[::-1] for l in (p / 'debs.sha256').read_text().splitlines())
    assert listed and all(digest(p / 'debs' / f) == h for f, h in listed.items()), 'debs'
    disk = args.out / 'disk.raw'
    disk.unlink(missing_ok=True)
    clone(args.inputs / 'disk.raw', disk); os.truncate(disk, DISK_BYTES)
    serve = args.out / 'serve'; serve.mkdir(exist_ok=True)
    (serve / 'paper.sh').write_text(guest_script(args.starts))
    for name in ('paper.jar', 'mojang.jar', 'debs.sha256', 'debs'):
        if (serve / name).is_dir(): shutil.rmtree(serve / name)
        else: (serve / name).unlink(missing_ok=True)
        clone(p / name, serve / name)
    server, host = serve_files(serve)
    result = {'name': 'paper', 'emu': 'natemu', 'vcpus': args.vcpus, 'ram_gib': args.ram, 'jar': JAR, 'jdk': JDK_VERSION,
              'speculation_barriers': args.speculation_barriers, 'seed': SEED, 'heap': HEAP, 'debs': listed,
              'inputs': recorded, 'natemu': digest(args.natemu),
              'command': [str(c) for c in natemu_command(args, disk)], 'phases': {}}
    contended = []
    phases = ['setup'] + [f'bench-{b}' for b in range(1, args.boots + 1)]
    try:
        for phase in phases:
            print(f'{phase}: launching', flush=True)
            before = others()
            code, timed_out, seconds = launch(natemu_command(args, disk), args.out, phase, host,
                                              LIMITS[phase.split('-')[0]], script='paper')
            rows = markers(args.out, phase)
            words = [w for _, w in rows]
            r = {'exit': code, 'timed_out': timed_out, 'seconds': seconds, 'starts': starts(rows),
                 'steps': {w[1]: w[2] for w in words if w[:1] == ['step'] and len(w) > 2},
                 'facts': {w[0]: w[1:] for w in words if w[:1] in (['packages'], ['seed'], ['seed-used'], ['net'], ['begin'])},
                 'ok': [f'{phase.split("-")[0]}-ok'] in words and code == 0 and not timed_out}
            if phase != 'setup': r['contended'] = before + others(); contended += r['contended']
            result['phases'][phase] = r
            print(f'{phase}: exit {code} in {seconds}s, ok {r["ok"]}, starts {r["starts"]}', flush=True)
            if not r['ok']: break
    finally:
        stop_group(server)
        disk.unlink(missing_ok=True)
    boots = [result['phases'][p] for p in phases[1:] if p in result['phases']]
    result['pass'] = len(boots) == args.boots and all(b['ok'] and len(b['starts']) == args.starts for b in boots)
    # Per start position (1 = the boot's first, with natemu's translations still to make): the median over the boots.
    result['by_position'] = [{'position': k, 'wall_to_done': [b['starts'][k - 1]['wall_to_done'] for b in boots],
                              'median': statistics.median(b['starts'][k - 1]['wall_to_done'] for b in boots)}
                             for k in range(1, args.starts + 1)] if result['pass'] else []
    write_json(args.out / 'result.json', result)
    (args.out / 'bench.jsonl').write_text(json.dumps({k: result[k] for k in ('name', 'emu', 'vcpus', 'ram_gib', 'speculation_barriers',
                                                                           'by_position', 'pass')}) + '\n')
    print('PASS' if result['pass'] else 'FAIL', args.out, flush=True)
    if result['pass'] and contended: return 3
    return 0 if result['pass'] else 1


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = a.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--inputs', type=Path, required=True, help='the io5-inputs folder (disk.raw, vmlinuz, initrd.img)')
    r.add_argument('--paper-inputs', type=Path, required=True, help='the paper-inputs folder (testkit/inputs.py:paper_inputs)')
    r.add_argument('--out', type=Path, required=True); r.add_argument('--fexc', type=Path, required=True)
    r.add_argument('--natemu', type=Path, required=True); r.add_argument('--runtime', type=Path, required=True)
    r.add_argument('--vcpus', type=int, default=4); r.add_argument('--ram', type=int, default=2, help='GiB')
    r.add_argument('--starts', type=int, default=2, help='timed starts in each timed boot')
    r.add_argument('--boots', type=lambda v: max(1, int(v)), default=3, help='timed boots (the median per start position is reported)')
    r.add_argument('--speculation-barriers', action='store_true',
                   help="natemu's page-walk barriers: the CPU reports RDCL_NO, so Linux runs without PTI (README)")
    args = a.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    return run(args)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
