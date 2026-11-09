#!/usr/bin/env python3
"""Stock Linux N4 serial-only reproducer: identical unpaced/paced deterministic bytes.

prepare uses retained unmodified Debian/BusyBox inputs; run launches one normal CLI
VM, no build/retry/download. 240s,16MiB serial/log bound. This is a diagnostic
counterfactual, not a UART fix or a production performance benchmark.
"""
import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import run_virtio_rng as rng

ROOT=Path(__file__).resolve().parent.parent
BUSYBOX_SHA='7ae95c260622412ceba6eb9f45d68f616cdc091a1d71d6c0a08b5875bb2c31dd'
PAYLOAD_BYTES=4_656_640
CHUNK=65536
LOG_BOUND=16<<20
# Finite four-CPU fixture acceptance criterion, not a universal OS scheduling guarantee.
MAX_HEARTBEAT_GAP=2.0
STRESS_SHA='8de2f3cf5a0c0856b59e54fed9f035fac6ec752820ad6d5df8bd2dc5440814e0'
STRESS_SOURCE_SHA='ffe8907154cc3b65cc66cde8663106355f88c948b58169cee1badbde9e0f52bd'
LOAD_NAMES=('cpu0','cpu1','cpu2')
LOAD_RECORDS=('load-version','load-status','load-live',*LOAD_NAMES)
LOAD_LOG_BOUND=128<<10
ERROR=r'SERIAL-STRESS-FAIL|BUG:|Oops:|Kernel panic|Call Trace:|soft lockup|hung task|rcu:.*(?:stall|starved)|Out of memory:|oom-kill:|Killed process'

def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write_json(p,v):Path(p).write_text(json.dumps(v,indent=2)+'\n')
def payload(factor=1):
    if factor not in (1,3):raise ValueError("payload factor must be1or3")
    # 80 ASCII bytes per line, unique sequence plus deterministic content; no marker collisions.
    lines=[]
    for i in range(PAYLOAD_BYTES*factor//80):
        line=f'{i:08d} '+hashlib.sha256(f'serial-stress-v1:{i}'.encode()).hexdigest()+' DATA\n'
        assert len(line)==79
        lines.append(line[:-1]+'!\n')
    result=''.join(lines).encode();assert len(result)==PAYLOAD_BYTES*factor;return result

HEARTBEAT=r'''#!/bin/sh
v=$1
sample=0
while test ! -e /tmp/stop; do
  read up rest < /proc/uptime
  /bin/busybox awk -v v="$v" -v t="$up" -v s="$sample" '{printf "cpu=%s sample=%s uptime=%s pid=%s start=%s current=%s ticks=%.0f\n",v,s,t,$1,$22,$39,$14+$15}' /proc/$$/stat >> /tmp/live-$v || exit 1
  sample=$((sample+1))
  sleep 1
done
'''
INIT=r'''#!/bin/sh
export PATH=/bin HOME=/tmp
pids=
fail() { echo "SERIAL-STRESS-FAIL $*"; dmesg; stty -F /dev/console opost onlcr >/dev/null 2>&1 || :; poweroff -f; while :; do sleep 1; done; }
mount -t proc proc /proc || fail proc
mount -t sysfs sysfs /sys || fail sysfs
mount -t devtmpfs devtmpfs /dev || fail devtmpfs
mount -t tmpfs tmpfs /tmp || fail tmpfs
exec </dev/console >/dev/console 2>&1
mode=
for item in $(cat /proc/cmdline); do case "$item" in serial_mode=*) mode=${item#*=};; esac; done
case "$mode" in unpaced|paced) ;; *) fail mode;; esac
test "$(uname -r)" = '@KVER@' || fail kernel
test "$(nproc)" = 4 || fail cpus
test "$(cat /sys/devices/system/cpu/online)" = 0-3 || fail online
sha256sum -c /inputs.sha256 || fail input-hash
# Preserve normal terminal output processing; bind its exact expected LF -> CRLF transformation.
stty -F /dev/console opost onlcr || fail termios
stty -F /dev/console -a > /tmp/termios || fail termios-read
for v in 0 1 2 3; do
  taskset -c "$v" /bin/sh /heartbeat "$v" & pids="$pids $!"
done
attempt=0
while test ! -s /tmp/live-0 || test ! -s /tmp/live-1 || test ! -s /tmp/live-2 || test ! -s /tmp/live-3; do
  attempt=$((attempt+1)); test "$attempt" -le 10 || fail heartbeat-start; sleep 1
done
sleep 2
cat /proc/interrupts > /tmp/irq-before || fail irq-before
cat /proc/softirqs > /tmp/softirq-before || fail softirq-before
read before rest < /proc/uptime
echo "SERIAL-STRESS-BEGIN mode=$mode cpus=$(nproc) online=$(cat /sys/devices/system/cpu/online) kernel=$(uname -r)"
echo "SERIAL-STRESS-PAYLOAD-BEGIN bytes=@BYTES@ sha256=@SHA@ uptime=$before"
if test "$mode" = unpaced; then
  cat /payload || fail payload-cat
else
  i=0
  while test "$i" -lt @CHUNKS@; do
    dd if=/payload bs=@CHUNK@ skip="$i" count=1 2>/tmp/dd.log || fail payload-dd
    i=$((i+1)); sleep 0.05
  done
fi
echo SERIAL-STRESS-PAYLOAD-END
read after rest < /proc/uptime
echo "SERIAL-STRESS-OUTPUT-DONE uptime=$after"
sleep 2
cat /proc/interrupts > /tmp/irq-after || fail irq-after
cat /proc/softirqs > /tmp/softirq-after || fail softirq-after
: > /tmp/stop
for p in $pids; do wait "$p" || fail heartbeat-exit; done
pids=
for name in irq-before irq-after softirq-before softirq-after termios live-0 live-1 live-2 live-3; do
  echo "SERIAL-STRESS-RECORD-BEGIN $name"
  cat "/tmp/$name" || fail record
  echo "SERIAL-STRESS-RECORD-END $name"
done
echo SERIAL-STRESS-DMESG-BEGIN
dmesg
echo SERIAL-STRESS-DMESG-END
echo SERIAL-STRESS-PASS
# BusyBox 1.37.0 stty applies explicit settings with TCSADRAIN even when unchanged.
stty -F /dev/console opost onlcr || fail tty-drain
poweroff -f
fail poweroff-returned
'''

# Same setsid process-group and /proc actual-worker observation method as run_io_soak.
LOAD_WORKER=r'''#!/bin/sh
name=$1
seed=$2
read begin rest < /proc/uptime
/bin/busybox setsid /bin/stress-ng --cpu 1 --cpu-method int64 --seed "$seed" --timeout 180s --verify --metrics-brief >"/tmp/$name" 2>&1 &
group=$!
echo "$group" >"/tmp/group-$name"
wait "$group"
rc=$?
read end rest < /proc/uptime
echo "name=$name group=$group begin=$begin end=$end exit=$rc" >"/tmp/status-$name"
exit "$rc"
'''
LOAD_OBSERVER=r'''#!/bin/sh
sample=0
while test ! -e /tmp/load-stop; do
  read up rest < /proc/uptime
  for name in cpu0 cpu1 cpu2; do
    test -s "/tmp/group-$name" || continue
    read group <"/tmp/group-$name"
    /bin/busybox awk -v g="$group" -v n="$name" -v t="$up" -v s="$sample" 'BEGIN {for(i=1;i<ARGC;i++) {if((getline line < ARGV[i])>0) {$0=line; if($5==g && $3!="Z" && $3!="X") {comm=substr($2,2,length($2)-2); if(comm=="stress-ng-cpu") {printf "name=%s sample=%s uptime=%s group=%s pid=%s start=%s comm=%s ticks=%.0f\n",n,s,t,g,$1,$22,comm,$14+$15; if($14+$15>0) print "ready" > ("/tmp/ready-" n)}}} close(ARGV[i])} exit}' /proc/[0-9]*/stat 2>/dev/null
  done
  sample=$((sample+1))
  sleep 1
done
'''
LOAD_START=r'''
/bin/stress-ng --version >/tmp/load-version 2>&1 || fail load-version
/bin/sh /load-worker cpu0 20261108 & load0=$!
/bin/sh /load-worker cpu1 20261109 & load1=$!
/bin/sh /load-worker cpu2 20261110 & load2=$!
/bin/sh /load-observer >/tmp/load-live & observer=$!
attempt=0
while test ! -s /tmp/ready-cpu0 || test ! -s /tmp/ready-cpu1 || test ! -s /tmp/ready-cpu2; do
  attempt=$((attempt+1)); test "$attempt" -le 15 || fail load-readiness; sleep 1
done
# Two actual-worker observations with increasing CPU ticks must precede the payload.
sleep 2
'''
LOAD_FINISH=r'''
: > /tmp/load-stop
wait "$observer" || fail load-observer
# Wrappers record exit status off-wire. The host checks statuses after bounded export.
for p in "$load0" "$load1" "$load2"; do wait "$p" || :; done
cat /tmp/status-cpu0 /tmp/status-cpu1 /tmp/status-cpu2 >/tmp/load-status || fail load-status
for name in load-version load-status load-live cpu0 cpu1 cpu2; do
  test "$(/bin/busybox wc -c < "/tmp/$name")" -le 131072 || fail load-log-size
  echo "SERIAL-STRESS-RECORD-BEGIN $name"
  cat "/tmp/$name" || fail load-record
  echo "SERIAL-STRESS-RECORD-END $name"
done
'''

def init(data,cpu_load=False):
    text=INIT
    if cpu_load:
        text=text.replace("cat /proc/interrupts > /tmp/irq-before",LOAD_START+"cat /proc/interrupts > /tmp/irq-before")
        text=text.replace("echo SERIAL-STRESS-DMESG-BEGIN",LOAD_FINISH+"echo SERIAL-STRESS-DMESG-BEGIN")
        # Drain the final queued payload tail before the measured overlap endpoint; no per-burst pacing.
        text=text.replace("echo SERIAL-STRESS-PAYLOAD-END","stty -F /dev/console opost onlcr || fail load-payload-drain\necho SERIAL-STRESS-PAYLOAD-END")
    for key,value in {'KVER':rng.KVER,'BYTES':len(data),'SHA':hashlib.sha256(data).hexdigest(),'CHUNKS':(len(data)+CHUNK-1)//CHUNK,'CHUNK':CHUNK}.items():text=text.replace('@'+key+'@',str(value))
    return text

def prepare(a):
    origin=a.matrix_inputs.resolve(strict=True);pin=json.loads((origin/'inputs.json').read_text())
    kernel=origin/'linux';busybox=origin/'rootfs/bin/busybox'
    if digest(kernel)!=rng.KERNEL_SHA or pin['outputs']['linux']!=rng.KERNEL_SHA:raise ValueError('pinned kernel mismatch')
    if digest(busybox)!=BUSYBOX_SHA or pin['rootfs']['bin/busybox']['sha256']!=BUSYBOX_SHA:raise ValueError('pinned BusyBox mismatch')
    if a.cpu_load:
        if a.factor!=3 or a.stress is None or a.stress_source is None:raise ValueError('CPU load requires factor3 and pinned stress binary/source')
        if digest(a.stress)!=STRESS_SHA or digest(a.stress_source)!=STRESS_SOURCE_SHA:raise ValueError('pinned stress binary/source mismatch')
    a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False);root=a.out/'rootfs'
    for name in ('bin','dev','proc','sys','tmp'):(root/name).mkdir(parents=True)
    shutil.copyfile(kernel,a.out/'linux');shutil.copyfile(busybox,root/'bin/busybox');(root/'bin/busybox').chmod(0o755)
    for name in ('sh','mount','cat','nproc','uname','sha256sum','sleep','dd','stty','taskset','dmesg','poweroff'):(root/'bin'/name).symlink_to('busybox')
    data=payload(a.factor);(root/'payload').write_bytes(data);(root/'heartbeat').write_text(HEARTBEAT);(root/'init').write_text(init(data,a.cpu_load))
    for name in ('heartbeat','init'):(root/name).chmod(0o755)
    names=['payload','heartbeat','init','bin/busybox']
    if a.cpu_load:
        shutil.copyfile(a.stress,root/'bin/stress-ng');(root/'bin/stress-ng').chmod(0o755)
        (root/'load-worker').write_text(LOAD_WORKER);(root/'load-observer').write_text(LOAD_OBSERVER)
        names+=['bin/stress-ng','load-worker','load-observer']
    (root/'inputs.sha256').write_text(''.join(digest(root/name)+'  /'+name+'\n' for name in names))
    for text in (HEARTBEAT,init(data,a.cpu_load),*([LOAD_WORKER,LOAD_OBSERVER] if a.cpu_load else [])):subprocess.run(['sh','-n'],input=text,text=True,check=True,timeout=10)
    helper=Path(__file__).with_name('mkcpio.py')
    if not helper.is_file():helper=ROOT/'tools/play/mkcpio.py'
    spec=importlib.util.spec_from_file_location('serial_mkcpio',helper);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.main(str(root),str(a.out/'serial.cpio'))
    sources=(Path(__file__),Path(rng.__file__),helper)
    oracle=a.out/'oracle';oracle.mkdir()
    for p in sources:shutil.copyfile(p,oracle/p.name)
    shutil.copyfile(origin/'inputs.json',a.out/'prior-inputs.json')
    wire=data.replace(b'\n',b'\r\n')
    write_json(a.out/'inputs.json',{'purpose':__doc__,'kernel':rng.KVER,'cpus':4,'cpu_load':a.cpu_load,'stress':({'binary':str(a.stress.resolve()),'binary_sha256':STRESS_SHA,'source':str(a.stress_source.resolve()),'source_sha256':STRESS_SOURCE_SHA,'workers':3,'seconds':180,'method':'int64','seeds':[20261108,20261109,20261110]} if a.cpu_load else None),'payload_factor':a.factor,'payload_bytes':len(data),'payload_sha256':hashlib.sha256(data).hexdigest(),'wire_bytes':len(wire),'wire_sha256':hashlib.sha256(wire).hexdigest(),
        'paced':{'chunk_bytes':CHUNK,'sleep_seconds':0.05},'origin':{'path':str(origin),'manifest_sha256':digest(origin/'inputs.json')},
        'sources':{str(p):digest(p) for p in sources},'outputs':{name:digest(a.out/name) for name in ('linux','serial.cpio','rootfs/payload','rootfs/init','rootfs/heartbeat','rootfs/bin/busybox','rootfs/inputs.sha256','prior-inputs.json',*(['rootfs/bin/stress-ng','rootfs/load-worker','rootfs/load-observer'] if a.cpu_load else []))}})


def counter_rows(text,numeric=False):
    result={}
    for line in text.splitlines():
        match=re.match(r'^\s*(\S+):\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)(?:\s+(.*))?$',line)
        if match:
            name=match[1];description=match[6] or ''
            if numeric and name.isdecimal() and 'ttyS0' in description:name='ttyS0'
            if name in result:raise ValueError('duplicate counter row '+name)
            result[name]=[int(match[i]) for i in range(2,6)]
    return result

def judge(raw,mode,expected,cpu_load=False):
    errors=[];metrics={}
    def need(ok,why):
        if not ok:errors.append(why)
    text=raw.decode('utf-8',errors='replace').replace('\r','')
    need(not re.search(ERROR,text),'guest/kernel error')
    begin=f'SERIAL-STRESS-PAYLOAD-BEGIN bytes={len(expected)} sha256={hashlib.sha256(expected).hexdigest()} uptime='
    starts=[m for m in re.finditer(re.escape(begin.encode())+rb'([0-9]+\.[0-9]+)\r\n',raw)]
    ends=[m for m in re.finditer(rb'SERIAL-STRESS-PAYLOAD-END\r\n',raw)]
    need(len(starts)==len(ends)==1,'exact payload framing')
    if len(starts)==len(ends)==1:
        actual=raw[starts[0].end():ends[0].start()];wire=expected.replace(b'\n',b'\r\n')
        need(actual==wire,'exact raw payload bytes (including CRLF)')
        metrics.update(payload_bytes=len(actual),payload_sha256=hashlib.sha256(actual).hexdigest(),expected_wire_sha256=hashlib.sha256(wire).hexdigest())
    need(re.findall(r'^SERIAL-STRESS-BEGIN mode=(\S+) cpus=(\d+) online=(\S+) kernel=(\S+)$',text,re.M)==[(mode,'4','0-3',rng.KVER)],'mode/kernel/all4-online binding')
    need(text.splitlines().count('SERIAL-STRESS-PASS')==1,'guest pass')
    done=re.findall(r'^SERIAL-STRESS-OUTPUT-DONE uptime=([0-9]+\.[0-9]+)$',text,re.M)
    records={}
    names=['irq-before','irq-after','softirq-before','softirq-after','termios','live-0','live-1','live-2','live-3']+list(LOAD_RECORDS if cpu_load else ())
    for name in names:
        opening='SERIAL-STRESS-RECORD-BEGIN '+name+'\n';closing='SERIAL-STRESS-RECORD-END '+name+'\n'
        need(text.count(opening)==text.count(closing)==1,'record framing '+name)
        if text.count(opening)==text.count(closing)==1:records[name]=text.split(opening,1)[1].split(closing,1)[0]
    need(len(done)==1 and len(starts)==1,'timing markers')
    if len(done)==1 and len(starts)==1:
        start=float(starts[0][1]);end=float(done[0]);need(end>=start,'ordered guest output timestamps');metrics['guest_output_seconds']=end-start
        live={}
        for v in range(4):
            rows=re.findall(r'^cpu=(\d+) sample=(\d+) uptime=([0-9]+\.[0-9]+) pid=(\d+) start=(\d+) current=(\d+) ticks=(\d+)$',records.get('live-'+str(v),''),re.M)
            need(len(rows)>=3 and len(rows)==len(records.get('live-'+str(v),'').splitlines()),'CPU'+str(v)+' complete heartbeat records')
            if not rows:continue
            parsed=[dict(cpu=int(x[0]),sample=int(x[1]),uptime=float(x[2]),pid=int(x[3]),start=int(x[4]),current=int(x[5]),ticks=int(x[6])) for x in rows]
            need(all(x['cpu']==x['current']==v for x in parsed),'CPU'+str(v)+' actual affinity')
            need(len({(x['pid'],x['start']) for x in parsed})==1,'CPU'+str(v)+' process identity')
            need([x['sample'] for x in parsed]==list(range(len(parsed))),'CPU'+str(v)+' monotonic progress')
            need(all(b['uptime']>a['uptime'] and b['ticks']>=a['ticks'] for a,b in zip(parsed,parsed[1:])),'CPU'+str(v)+' time/accounting monotonic')
            need(parsed[0]['uptime']<=start and parsed[-1]['uptime']>end,'CPU'+str(v)+' before/after progress')
            if end-start>=2:need(any(start<t['uptime']<end for t in parsed),'CPU'+str(v)+' progress during payload')
            need(max((b['uptime']-a['uptime'] for a,b in zip(parsed,parsed[1:])),default=0)<=MAX_HEARTBEAT_GAP,'CPU'+str(v)+' heartbeat gap exceeds 2s')
            live[str(v)]={'samples':len(parsed),'ticks':parsed[-1]['ticks']-parsed[0]['ticks'],'max_gap_seconds':max((b['uptime']-a['uptime'] for a,b in zip(parsed,parsed[1:])),default=0),'first':parsed[0],'last':parsed[-1]}
        metrics['heartbeat']=live
        if cpu_load:
            load_errors,load_metrics=load_result(records,start,end)
            errors+=load_errors;metrics['cpu_load']=load_metrics
    for kind,names_needed in [('irq',['ttyS0']),('softirq',['TIMER','RCU'])]:
        try:
            before=counter_rows(records.get(kind+'-before',''),kind=='irq');after=counter_rows(records.get(kind+'-after',''),kind=='irq');deltas={}
            for name in names_needed:
                need(name in before and name in after,kind+' '+name+' counters')
                if name in before and name in after:
                    delta=[b-a for a,b in zip(before[name],after[name])];need(all(x>=0 for x in delta) and sum(delta)>0,kind+' '+name+' progress');deltas[name]=delta
            metrics[kind+'_delta']=deltas
        except ValueError as e:errors.append(str(e))
    return errors,metrics,records


def load_result(records,start,end):
    errors=[];metrics={}
    def need(ok,why):
        if not ok:errors.append('CPU load: '+why)
    need(all(n in records and len(records[n].encode())<=LOAD_LOG_BOUND for n in LOAD_RECORDS),'complete bounded logs')
    need(re.findall(r'stress-ng, version (\S+)',records.get('load-version',''))==['0.19.02'],'version')
    statuses=re.findall(r'^name=(cpu[012]) group=(\d+) begin=([0-9.]+) end=([0-9.]+) exit=(\d+)$',records.get('load-status',''),re.M)
    need(len(statuses)==3 and [r[0] for r in statuses]==list(LOAD_NAMES) and len(records.get('load-status','').splitlines())==3,'exact worker statuses')
    need(len({r[1] for r in statuses})==3,'three distinct worker process groups')
    rows=re.findall(r'^name=(cpu[012]) sample=(\d+) uptime=([0-9.]+) group=(\d+) pid=(\d+) start=(\d+) comm=stress-ng-cpu ticks=(\d+)$',records.get('load-live',''),re.M)
    need(len(rows)==len(records.get('load-live','').splitlines()),'complete actual-worker observations')
    need(len({(r[4],r[5]) for r in rows})==3,'three distinct actual worker PID/start identities')
    for name in LOAD_NAMES:
        text=records.get(name,'')
        need(re.findall(r'passed: (\d+): (\w+) \((\d+)\)',text)==[('1','cpu','1')],name+' one passed worker')
        for label in ('failed','skipped'):
            need(re.findall(r'\b'+label+r': (\d+)',text)==['0'],name+' '+label)
        need([v.strip() for v in re.findall(r'\bmetrics[ -]untrustworthy:[ \t]*([^\r\n]*)',text)]==['0'],name+' exact trustworthy metrics')
        work=re.findall(r'stress-ng:\s+metrc:\s+\[\s*\d+\]\s+cpu\s+(\d+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)',text)
        need(len(work)==1 and int(work[0][0])>0 and float(work[0][1])>=175 and float(work[0][2])+float(work[0][3])>0,name+' verified work/runtime')
        need('successful run completed' in text and not re.search(r'stress-ng:\s+(?:fail|error):',text),name+' verification')
        status=[r for r in statuses if r[0]==name]
        observations=[r for r in rows if r[0]==name and int(r[6])>0]
        valid=False
        if len(status)==1 and observations:
            _,group,begin,finish,rc=status[0]
            need(rc=='0' and float(begin)<=start<end<=float(finish),name+' launch/wait envelope and exit')
            identities={(r[3],r[4],r[5]) for r in observations}
            pre=[r for r in observations if float(r[2])<=start]
            post=[r for r in observations if float(r[2])>end]
            valid=(len(identities)==1 and next(iter(identities))[0]==group and len(pre)>=2 and bool(post)
                and int(pre[-1][6])>int(pre[0][6])>0
                and all(int(b[1])==int(a[1])+1 and 0<float(b[2])-float(a[2])<=3 and int(b[6])>int(a[6]) for a,b in zip(observations,observations[1:])))
            metrics[name]={'samples':len(observations),'identity':sorted(identities),'observed_first':float(observations[0][2]),'observed_last':float(observations[-1][2]),'cpu_ticks_first':int(observations[0][6]),'cpu_ticks_last':int(observations[-1][6]),'verified_work':work,'wrapper_status':status[0]}
        need(valid,name+' same actual PID/start with CPU progress before/through/after payload')
    return errors,metrics


def sample_load():
    records={'load-version':'stress-ng, version 0.19.02\n','load-status':'','load-live':''}
    for i,name in enumerate(LOAD_NAMES):
        records[name]='stress-ng: metrc: [123] cpu 100 180.00 90.00 0.10\nstress-ng: info: [123] passed: 1: cpu (1)\nstress-ng: info: [123] failed: 0\nstress-ng: info: [123] skipped: 0\nstress-ng: info: [123] metrics untrustworthy: 0\nstress-ng: info: [123] successful run completed\n'
        records['load-status']+=f'name={name} group={10+i} begin=0.00 end=180.00 exit=0\n'
        records['load-live']+=''.join(f'name={name} sample={k} uptime={k+1:.2f} group={10+i} pid={20+i} start=1 comm=stress-ng-cpu ticks={k*10}\n' for k in range(8))
    return records


def self_check():
    data=b'0123456789abcdef\n'*4;wire=data.replace(b'\n',b'\r\n')
    records={'irq-before':'4: 0 0 1 0 IO-APIC 4-edge ttyS0\n','irq-after':'4: 0 0 500 0 IO-APIC 4-edge ttyS0\n',
        'softirq-before':'TIMER: 1 1 1 1\nRCU: 2 2 2 2\n','softirq-after':'TIMER: 5 5 5 5\nRCU: 6 6 6 6\n','termios':'opost onlcr\n'}
    for v in range(4):records['live-'+str(v)]=''.join(f'cpu={v} sample={i} uptime={i+1:.2f} pid={20+v} start=100 current={v} ticks={i}\n' for i in range(6))
    before=f'SERIAL-STRESS-BEGIN mode=unpaced cpus=4 online=0-3 kernel={rng.KVER}\nSERIAL-STRESS-PAYLOAD-BEGIN bytes={len(data)} sha256={hashlib.sha256(data).hexdigest()} uptime=2.00\n'.encode().replace(b'\n',b'\r\n')
    after='SERIAL-STRESS-PAYLOAD-END\nSERIAL-STRESS-OUTPUT-DONE uptime=4.00\n'+''.join(f'SERIAL-STRESS-RECORD-BEGIN {n}\n{t}SERIAL-STRESS-RECORD-END {n}\n' for n,t in records.items())+'SERIAL-STRESS-PASS\n'
    good=before+wire+after.encode().replace(b'\n',b'\r\n');assert not judge(good,'unpaced',data)[0],judge(good,'unpaced',data)[0]
    corruptions=[good.replace(wire,wire[:-1],1),good.replace(wire,wire+b'x',1),good.replace(wire,wire.replace(b'0',b'1',1),1),good.replace(wire,wire+b'rcu: INFO: rcu_preempt detected stalls on CPUs/tasks:\r\n',1),good.replace(b'cpu=2 sample=3',b'cpu=2 sample=9'),good.replace(b'current=2',b'current=1'),good.replace(b'RCU: 6 6 6 6',b'RCU: 2 2 2 2'),good.replace(b'SERIAL-STRESS-PASS',b'NO-PASS'),good+before,good.replace(b'cpus=4',b'cpus=2')]
    for index,bad in enumerate(corruptions):assert judge(bad,'unpaced',data)[0],index
    # Only CPU2's otherwise valid monotonic timeline changes: exact2s accepted,2.01s rejected.
    for gap,accepted in ((2.0,True),(2.01,False)):
        altered=good
        for i in (3,4,5):
            old=f'cpu=2 sample={i} uptime={i+1:.2f}'.encode()
            new=f'cpu=2 sample={i} uptime={i+gap:.2f}'.encode()
            altered=altered.replace(old,new)
        errors=judge(altered,'unpaced',data)[0]
        assert (not errors)==accepted,(gap,errors)
        if not accepted:assert errors==['CPU2 heartbeat gap exceeds 2s'],errors
    plain,loaded_init=init(data),init(data,True)
    assert 'load-payload-drain' not in plain
    assert loaded_init.count('load-payload-drain')==1
    assert loaded_init.index('cat /payload') < loaded_init.index('load-payload-drain') < loaded_init.index('echo SERIAL-STRESS-PAYLOAD-END') < loaded_init.index('SERIAL-STRESS-OUTPUT-DONE')
    # Exercise the exact observer filter against a vanished earlier glob path. Explicit getline
    # is nonfatal for that path; normal awk positional input would abort before the worker.
    with tempfile.TemporaryDirectory() as tmp:
        tmp=Path(tmp)
        program=re.search(r"awk -v g=\"\$group\" -v n=\"\$name\" -v t=\"\$up\" -v s=\"\$sample\" '(.+?)' /proc",LOAD_OBSERVER)[1]
        program=program.replace('/tmp/ready-',str(tmp/'ready-'))
        fields=['0']*24;fields[0]='20';fields[1]='(stress-ng-cpu)';fields[2]='R';fields[4]='10';fields[13]='40';fields[21]='1'
        stable=tmp/'worker-stat';stable.write_text(' '.join(fields)+'\n')
        argv=['awk','-v','g=10','-v','n=cpu0','-v','t=5.00','-v','s=4',program,str(tmp/'vanished-unrelated-stat'),str(stable)]
        observed=subprocess.check_output(argv,text=True)
        assert observed=='name=cpu0 sample=4 uptime=5.00 group=10 pid=20 start=1 comm=stress-ng-cpu ticks=40\n',observed
        assert (tmp/'ready-cpu0').read_text()=='ready\n'
        stable.unlink();missing=subprocess.check_output(argv,text=True);assert missing==''
        disappeared=sample_load()
        # Removing the actual worker's sample still violates the unchanged consecutive-index oracle.
        disappeared['load-live']=''.join(missing if line.startswith('name=cpu0 sample=4 ') else line for line in disappeared['load-live'].splitlines(True))
        assert load_result(disappeared,3.,6.)[0]
    print('Observer synthetic transient-path check passes; missing actual worker remains rejected')
    load=sample_load();assert not load_result(load,3.,6.)[0],load_result(load,3.,6.)[0]
    # Exercise opt-in framing through the unchanged byte/heartbeat oracle as well.
    # Synthetic serial payload interval2..4 needs two positive pre-observations.
    load['load-live']=load['load-live'].replace('ticks=0','ticks=1')
    frames=''.join(f'SERIAL-STRESS-RECORD-BEGIN {n}\n{v}SERIAL-STRESS-RECORD-END {n}\n' for n,v in load.items()).encode().replace(b'\n',b'\r\n')
    loaded=good.replace(b'SERIAL-STRESS-PASS',frames+b'SERIAL-STRESS-PASS')
    assert not judge(loaded,'unpaced',data,True)[0],judge(loaded,'unpaced',data,True)[0]
    assert judge(good,'unpaced',data,True)[0]
    load_negatives=0
    for name in LOAD_NAMES:
        for before,after in [('metrics untrustworthy: 0','metrics untrustworthy: 1'),
                ('metrics untrustworthy: 0','metric missing'),
                ('metrics untrustworthy: 0','metrics untrustworthy: 0\nmetrics-untrustworthy: 0'),
                ('passed: 1: cpu (1)','passed: 0'),('failed: 0','failed: 1'),('skipped: 0','skipped: 1'),
                ('cpu 100 180.00','cpu 0 180.00'),('cpu 100 180.00','cpu 100 30.00')]:
            bad=copy.deepcopy(load);bad[name]=bad[name].replace(before,after);assert load_result(bad,3.,6.)[0];load_negatives+=1
        for bad_kind in ('missing','supervisor','pid-reuse','stalled','no-pre','no-post','gap','exit'):
            bad=copy.deepcopy(load);lines=bad['load-live'].splitlines(True)
            if bad_kind=='missing':bad.pop(name)
            elif bad_kind=='exit':bad['load-status']=re.sub(r'('+name+r' group=.*?exit=)0',r'\g<1>1',bad['load-status'])
            else:
                changed=[]
                for line in lines:
                    if line.startswith(f'name={name} '):
                        k=int(re.search(r'sample=(\d+)',line)[1])
                        if bad_kind=='supervisor':line=line.replace('stress-ng-cpu','stress-ng')
                        if bad_kind=='pid-reuse' and k>=4:line=line.replace('start=1','start=2')
                        if bad_kind=='stalled':line=re.sub(r'ticks=\d+','ticks=10',line)
                        if bad_kind=='no-pre' and k<3:continue
                        if bad_kind=='no-post' and k>=6:continue
                        if bad_kind=='gap' and k==4:continue
                    changed.append(line)
                bad['load-live']=''.join(changed)
            assert load_result(bad,3.,6.)[0],(name,bad_kind);load_negatives+=1
    bad=copy.deepcopy(load);bad['load-live']=bad['load-live'].replace('pid=21','pid=20')
    assert load_result(bad,3.,6.)[0];load_negatives+=1
    bad=copy.deepcopy(load);bad['load-status']=bad['load-status'].replace('group=11','group=10')
    assert load_result(bad,3.,6.)[0];load_negatives+=1
    # A payload continuing past the actual workers has no post-boundary live observation.
    assert load_result(load,3.,181.)[0]
    for script in (HEARTBEAT,init(payload()),init(data,True),LOAD_WORKER,LOAD_OBSERVER):subprocess.run(['sh','-n'],input=script,text=True,check=True,timeout=10)
    print(f'CPU-load oracle: valid actual-worker case and {load_negatives+2} missing/lifetime/identity/progress/status/work negatives')
    assert len(payload())==PAYLOAD_BYTES
    assert len(payload(3))==3*PAYLOAD_BYTES
    print('Payload, shell syntax, byte-corruption/interleaving/CPU/IRQ/RCU/liveness negatives pass; no VM')


def run(a):
    pin=json.loads((a.inputs/'inputs.json').read_text())
    for name,sha in pin['outputs'].items():
        if digest(a.inputs/name)!=sha:raise ValueError('input changed: '+name)
    expected=(a.inputs/'rootfs/payload').read_bytes()
    if expected!=payload(pin.get('payload_factor',1)):raise ValueError('payload recipe differs')
    if pin.get('cpu_load') and (a.mode!='unpaced' or pin.get('payload_factor')!=3):raise ValueError('loaded case requires triple unpaced payload')
    a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False)
    binaries={}
    for name in ('natemu','runtime','fexc'):
        path=getattr(a,name).resolve(strict=True);setattr(a,name,path);binaries[name]={'path':str(path),'sha256':digest(path)}
    write_json(a.out/'manifest.json',{'mode':a.mode,'cpus':4,'cpu_load':pin.get('cpu_load',False),'inputs':{'path':str(a.inputs.resolve()),'sha256':digest(a.inputs/'inputs.json')},'binaries':binaries,'oracle':{str(Path(__file__).resolve()):digest(__file__)},'bounds':{'vm_launches':1,'seconds':240,'log_bytes':LOG_BOUND}})
    command=['/usr/bin/time','-lp',str(a.natemu),'--runtime',str(a.runtime),'--fexc',str(a.fexc),'--kernel',str((a.inputs/'linux').resolve()),'--initrd',str((a.inputs/'serial.cpio').resolve()),'--vcpus','4','--ram','2G','--compile-cache','off','--serial',str(a.out/'serial.log'),'--cmdline','console=ttyS0 earlyprintk=serial mitigations=off nokaslr serial_mode='+a.mode]
    write_json(a.out/'command.json',command);began=time.monotonic();timeout=limited=False;markers=[];offset=0;pending=b''
    env=dict(os.environ);env.pop('NATEMU_PERF_LOG',None)
    with a.lock.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with (a.out/'stdout.log').open('wb') as stdout,(a.out/'stderr.log').open('wb') as stderr:
            process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,env=env,start_new_session=True)
            try:
                while process.poll() is None:
                    serial=a.out/'serial.log'
                    if serial.exists():
                        with serial.open('rb') as f:f.seek(offset);chunk=f.read();offset=f.tell()
                        pending+=chunk;lines=pending.split(b'\n');pending=lines.pop()
                        for line in lines:
                            if line.startswith(b'SERIAL-STRESS-'):
                                markers.append({'host_seconds':time.monotonic()-began,'line':line.decode(errors='replace').rstrip('\r')})
                    limited=sum(f.stat().st_size for f in a.out.glob('*.log'))>LOG_BOUND;timeout=time.monotonic()-began>=240
                    if timeout or limited:rng.stop_group(process);break
                    try:process.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:pass
            except BaseException:rng.stop_group(process);raise
    raw=(a.out/'serial.log').read_bytes() if (a.out/'serial.log').exists() else b''
    limited |= sum(f.stat().st_size for f in a.out.glob('*.log'))>LOG_BOUND
    try:errors,metrics,records=judge(raw,a.mode,expected,pin.get('cpu_load',False))
    except (ValueError,KeyError,OverflowError,TypeError) as error:errors,metrics,records=['invalid guest output: '+str(error)],{},{}
    if process.returncode or timeout or limited:errors.append('host/process deadline/log-limit failure')
    stderr=(a.out/'stderr.log').read_text(errors='replace')
    if 'natemu: Power(1)' not in stderr.splitlines():errors.append('normal S5 absent')
    timing={k:float(v) for k,v in re.findall(r'^(real|user|sys)\s+([0-9.]+)$',stderr,re.M)}
    if set(timing)!={'real','user','sys'}:errors.append('host CPU/time metrics absent')
    exported=a.out/'guest';exported.mkdir()
    for name,text in records.items():(exported/name).write_text(text)
    result={'pass':not errors,'errors':errors,'mode':a.mode,'cpu_load':pin.get('cpu_load',False),'returncode':process.returncode,'timeout':timeout,'log_limit':limited,'seconds':time.monotonic()-began,'host_time':timing,'serial_sha256':hashlib.sha256(raw).hexdigest(),'metrics':metrics,'markers':markers}
    write_json(a.out/'result.json',result);print(json.dumps(result),flush=True);return int(bool(errors))


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('self-check')
    prep=sub.add_parser('prepare');prep.add_argument('--matrix-inputs',type=Path,required=True);prep.add_argument('--out',type=Path,required=True);prep.add_argument('--factor',type=int,choices=(1,3),default=1);prep.add_argument('--cpu-load',action='store_true');prep.add_argument('--stress',type=Path);prep.add_argument('--stress-source',type=Path)
    runp=sub.add_parser('run');runp.add_argument('--mode',choices=('unpaced','paced'),required=True)
    for name in ('inputs','natemu','runtime','fexc','out'):runp.add_argument('--'+name,type=Path,required=True)
    runp.add_argument('--lock',type=Path,default=Path.home()/'orca/natemu-raw/.vm.lock')
    a=p.parse_args()
    if a.command=='self-check':self_check();return 0
    if a.command=='prepare':prepare(a);return 0
    return run(a)
if __name__=='__main__':raise SystemExit(main())
