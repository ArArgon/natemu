#!/usr/bin/env python3
"""Admit genuine Debian fio on the installed ext4 root; no timed benchmarks.

verify-inputs is read-only. prepare builds a separate 512 MiB image without mounting.
run performs two N=2 boots, each bounded to 240 seconds: finite verification write,
then read-only restart verification. --restart-from repeats only the latter from
a previously verified image. No builds, downloads or automatic retries.
"""
import argparse
import hashlib
import io
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import time
import fcntl

import run_virtio_block as block

ROOT = block.ROOT
DISK_BYTES = 512 << 20
VERIFY_BYTES = 64 << 20
INDEX_SHA = '7778d3e3f303b7ddb8ce0fe7c8d57473a076c6bf2e8f241f75421d2396352498'
FIO_SHA = '92a0d1b5336c17e34d07e911a814fd6d0921a574a27bb210600f770ce575208c'
PROFILES = ('fsync-4k-q1', 'mixed-4k-q1', 'mixed-4k-q16', 'precondition', 'rand-read-4k-q1',
            'rand-read-4k-q16', 'seq-read-128k-q16', 'seq-write-128k-q16', 'verify-read', 'verify-write', 'warm-read')
digest, write_json, checked = block.digest, block.write_json, block.checked

INIT = r'''#!/bin/sh
export PATH=/usr/bin:/bin
export HOME=/tmp
exec </dev/console >/dev/console 2>&1
export_logs() {
  for f in /tmp/fio/*; do
    test -f "$f" || continue
    name=$(basename "$f")
    echo "FIO-ADMISSION-LOG-BEGIN $name"
    cat "$f"
    echo
    echo "FIO-ADMISSION-LOG-END $name"
  done
}
fail() { echo "FIO-ADMISSION-FAIL $*"; export_logs; dmesg; poweroff -f; while :; do sleep 1; done; }
mount -t tmpfs tmpfs /tmp || fail tmpfs
mkdir /tmp/fio || fail log-directory
phase=
for item in $(cat /proc/cmdline); do case "$item" in io_phase=*) phase=${item#*=};; esac; done
case "$phase" in write) mode=rw;; verify) mode=ro;; *) fail phase;; esac
echo "FIO-ADMISSION-BEGIN phase=$phase cpus=$(nproc) kernel=$(uname -r)"
rootinfo=$(awk '$2 == "/" {print $1, $3, $4}' /proc/mounts)
case "$rootinfo" in "/dev/vda ext4 $mode,"*) ;; *) fail "root=$rootinfo";; esac
echo "FIO-ADMISSION-ROOT $rootinfo"
test "$(cat /sys/block/vda/size)" = 1048576 || fail capacity
test "$(cat /sys/block/vda/queue/logical_block_size)" = 512 || fail sector
test "$(basename "$(readlink -f /sys/block/vda/device/driver)")" = virtio_blk || fail driver
for f in /sys/block/vda/queue/max_sectors_kb /sys/block/vda/queue/max_segments /sys/block/vda/queue/max_segment_size /sys/block/vda/queue/write_cache; do
  echo "$f: $(cat "$f")"
done
sha256sum -c /fio/inputs.sha256 || fail input-hash
/usr/bin/fio --version >/tmp/fio/version.txt 2>/tmp/fio/version.stderr
rc=$?; echo "FIO-ADMISSION-EXIT version $rc"; test "$rc" = 0 || fail version
for p in /fio/profiles/*.fio; do
  name=$(basename "$p" .fio)
  /usr/bin/fio --parse-only "$p" >"/tmp/fio/parse-$name.stdout" 2>"/tmp/fio/parse-$name.stderr"
  rc=$?; echo "FIO-ADMISSION-PARSE $name $rc"; test "$rc" = 0 || fail "parse-$name"
done
if test "$phase" = write; then
  test ! -e /bench/verify.bin || fail reused-image
  job=verify-write
else
  job=verify-read
fi
/usr/bin/fio "/fio/profiles/$job.fio" --output-format=json --output=/tmp/fio/result.json >/tmp/fio/fio.stdout 2>/tmp/fio/fio.stderr
rc=$?; echo "FIO-ADMISSION-EXIT $job $rc"; test "$rc" = 0 || fail fio
size=$(wc -c < /bench/verify.bin)
test "$size" = 67108864 || fail length
hash=$(sha256sum /bench/verify.bin); hash=${hash%% *}
echo "FIO-ADMISSION-DATA bytes=$size sha256=$hash"
sync || fail sync
mount -o remount,ro / || fail remount-ro
rootinfo=$(awk '$2 == "/" {print $1, $3, $4}' /proc/mounts)
case "$rootinfo" in "/dev/vda ext4 ro,"*) ;; *) fail final-root;; esac
echo FIO-ADMISSION-ROOT-READONLY
export_logs
cat /proc/interrupts
dmesg
echo "FIO-ADMISSION-PASS phase=$phase"
poweroff -f
fail poweroff-returned
'''


def guest(root, name):
    """Resolve a guest symlink without ever following an absolute link on the host."""
    pending, parts, links = list(PurePosixPath(name).parts), [], 0
    while pending:
        part = pending.pop(0)
        if part in ('/', '.', ''): continue
        if part == '..':
            if parts: parts.pop()
            continue
        p = root.joinpath(*parts, part)
        if p.is_symlink():
            links += 1
            if links > 64: raise ValueError(f'guest symlink loop: {name}')
            target = os.readlink(p)
            if target.startswith('/'): parts = []
            pending = list(PurePosixPath(target).parts) + pending
        else: parts.append(part)
    return root.joinpath(*parts)


def tree_manifest(root):
    result = {}
    for p in sorted(root.rglob('*')):
        mode = p.lstat().st_mode
        result[str(p.relative_to(root))] = {'mode': oct(mode), **(
            {'link': os.readlink(p)} if p.is_symlink() else
            {'sha256': digest(p)} if stat.S_ISREG(mode) else {})}
    return result


def verify_inputs(args):
    prep, old = args.fio_prep, args.block_inputs
    pin = json.loads((old / 'inputs.json').read_text())
    for name, sha in pin['outputs'].items():
        if digest(old / name) != sha: raise ValueError(f'block input changed: {name}')
    if digest(old / 'linux') != block.KERNEL_SHA: raise ValueError('kernel pin')
    actual = tree_manifest(old / 'rootfs')
    if set(actual) != set(pin['rootfs']): raise ValueError('block root tree changed')
    for name, entry in actual.items():
        for k, value in entry.items():
            if pin['rootfs'][name][k] != value: raise ValueError(f'block root changed: {name}')
    for line in (prep / 'preparation-files.sha256').read_text().splitlines():
        sha, name = line.split('  ', 1)
        rel = PurePosixPath(name)
        if rel.is_absolute() or '..' in rel.parts or digest(prep / name) != sha: raise ValueError(f'preparation hash: {name}')
    index = prep / 'metadata/Packages.trixie.xz'
    if digest(index) != INDEX_SHA: raise ValueError('signed index pin')
    packages = json.loads((prep / 'packages.json').read_text())
    index_by_file = {}
    for record in lzma.decompress(index.read_bytes()).decode().split('\n\n'):
        fields = dict(line.split(': ', 1) for line in record.splitlines() if line and not line[0].isspace() and ': ' in line)
        if 'Filename' in fields: index_by_file[fields['Filename']] = fields
    expected, links = {}, []
    for package in packages:
        fields = index_by_file[package['filename']]
        for k, field in [('sha256', 'SHA256'), ('package', 'Package'), ('version', 'Version')]:
            if package[k] != fields[field]: raise ValueError(f'package differs from signed index: {package["package"]}')
        deb = prep / 'packages' / Path(package['filename']).name
        if digest(deb) != package['sha256'] or deb.stat().st_size != int(fields['Size']): raise ValueError(f'package changed: {deb}')
        if package['package'] == 'fio' and (package['version'] != '3.39-1' or package['sha256'] != FIO_SHA): raise ValueError('fio pin')
        names = subprocess.check_output(['ar', 't', str(deb)], text=True, timeout=30).splitlines()
        data = subprocess.check_output(['ar', 'p', str(deb), next(x for x in names if x.startswith('data.tar'))], timeout=30)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive:
                rel = PurePosixPath(member.name)
                if rel.is_absolute() or '..' in rel.parts: raise ValueError('package path')
                name = str(rel)
                if member.isfile(): expected[name] = {'sha256': hashlib.sha256(archive.extractfile(member).read()).hexdigest()}
                elif member.issym() or member.islnk(): links.append((name, member.linkname, member.islnk()))
                elif not member.isdir(): raise ValueError('package special file')
    for name, target, hard in links:
        expected[name] = expected[str(PurePosixPath(target))].copy() if hard else {'link': target}
    expected.update({'lib': {'link': 'usr/lib'}, 'lib64': {'link': 'usr/lib64'}})
    staged = tree_manifest(prep / 'rootfs')
    if {n for n, e in staged.items() if 'link' in e or 'sha256' in e} != set(expected): raise ValueError('extra/missing package files')
    for name, e in expected.items():
        if any(staged[name].get(k) != v for k, v in e.items()): raise ValueError(f'package extraction changed: {name}')
    elf = json.loads((prep / 'elf-closure.json').read_text())
    if elf['missing'] or elf['entry'] != '/usr/bin/fio': raise ValueError('incomplete ELF closure')
    root = prep / 'rootfs'
    for name, obj in elf['objects'].items():
        p = guest(root, name)
        if digest(p) != obj['sha256']: raise ValueError(f'ELF object changed: {name}')
        for target in list(obj['needed'].values()) + ([obj['interpreter']] if obj['interpreter'] else []):
            key = '/' + str(guest(root, target).relative_to(root))
            if key not in elf['objects']: raise ValueError(f'ELF target missing: {target}')
    profiles = sorted(p.stem for p in (prep / 'profiles').glob('*.fio'))
    if profiles != list(PROFILES): raise ValueError('profile set changed')
    result = {'packages': len(packages), 'package_files': len(expected), 'elf_objects': len(elf['objects']),
              'profiles': profiles, 'block_inputs_sha256': digest(old / 'inputs.json'), 'package_manifest_sha256': digest(prep / 'packages.json')}
    print(json.dumps(result), flush=True)
    return pin, result


def prepare(args):
    pin, verified = verify_inputs(args)
    args.out.mkdir(parents=True, exist_ok=False)
    root = args.out / 'rootfs'
    shutil.copytree(args.block_inputs / 'rootfs', root, symlinks=True)
    shutil.copytree(args.fio_prep / 'rootfs', root, symlinks=True, dirs_exist_ok=True)
    if digest(root / 'bin/busybox') != pin['rootfs']['bin/busybox']['sha256']: raise ValueError('BusyBox overwritten')
    (root / 'sbin/init').write_text(INIT); (root / 'sbin/init').chmod(0o755)
    (root / 'bench').mkdir(); (root / 'fio').mkdir()
    shutil.copytree(args.fio_prep / 'profiles', root / 'fio/profiles')
    elf = json.loads((args.fio_prep / 'elf-closure.json').read_text())
    hashes = {name: obj['sha256'] for name, obj in elf['objects'].items()}
    hashes.update({'/fio/profiles/' + p.name: digest(p) for p in (root / 'fio/profiles').glob('*.fio')})
    (root / 'fio/inputs.sha256').write_text(''.join(f'{sha}  {name}\n' for name, sha in sorted(hashes.items())))
    for name in ('linux', 'bootstrap.cpio', 'mke2fs.conf'): shutil.copyfile(args.block_inputs / name, args.out / name)
    tools = pin['tools']
    for info in tools.values():
        if digest(info['path']) != info['sha256']: raise ValueError('native tool changed')
    image = args.out / 'root.raw'
    with image.open('xb') as f: f.truncate(DISK_BYTES)
    env = dict(os.environ, MKE2FS_CONFIG=str((args.out / 'mke2fs.conf').resolve()))
    checked([tools['mke2fs']['path'], '-F', '-t', 'ext4', '-b', '4096', '-L', 'FIOADMISSION',
             '-U', '673af719-2222-4444-8888-12453af21737', '-E', 'root_owner=0:0', '-d', root, image], args.out / 'mke2fs.log', env)
    edits = ''.join(f'set_inode_field /{p.relative_to(root)} {field} 0\n' for p in sorted(root.rglob('*')) for field in ('uid', 'gid'))
    (args.out / 'ownership.debugfs').write_text(edits)
    checked([tools['debugfs']['path'], '-w', '-f', args.out / 'ownership.debugfs', image], args.out / 'ownership.log')
    checked([tools['e2fsck']['path'], '-fn', image], args.out / 'initial-fsck.log')
    checked([tools['debugfs']['path'], '-R', 'stats', image], args.out / 'filesystem-stats.log')
    stats = (args.out / 'filesystem-stats.log').read_text()
    free = int(re.search(r'^Free blocks:\s+(\d+)', stats, re.M)[1]) * 4096
    if free < VERIFY_BYTES + (32 << 20): raise ValueError('less than verification file plus32MiB reserve')
    manifest = {'purpose': 'finite fio installed-root admission, not performance', 'verified_inputs': verified,
                'block_inputs': str(args.block_inputs.resolve()), 'fio_preparation': str(args.fio_prep.resolve()),
                'disk_bytes': DISK_BYTES, 'verify_bytes': VERIFY_BYTES, 'free_bytes': free, 'tools': tools,
                'producer': {str(p.resolve()): digest(p) for p in (Path(__file__), Path(block.__file__), ROOT / 'tools/run_virtio_rng.py')},
                'rootfs': tree_manifest(root), 'outputs': {name: digest(args.out / name) for name in ('linux', 'bootstrap.cpio', 'root.raw', 'mke2fs.conf', 'rootfs/sbin/init')}}
    write_json(args.out / 'inputs.json', manifest)
    print(json.dumps({'outputs': manifest['outputs'], 'free_bytes': free}), flush=True)


def judge(text, phase):
    errors = []
    def require(ok, why):
        if not ok: errors.append(why)
    require(re.findall(r'^FIO-ADMISSION-BEGIN phase=(\S+) cpus=(\d+) kernel=(\S+)$', text, re.M) == [(phase, '2', block.KVER)], 'phase/CPU/kernel')
    mode = 'rw' if phase == 'write' else 'ro'
    require(len(re.findall(r'^FIO-ADMISSION-ROOT /dev/vda ext4 ' + mode + r',.*$', text, re.M)) == 1, 'installed root/mode')
    require(re.findall(r'^FIO-ADMISSION-PARSE (\S+) (\d+)$', text, re.M) == [(p, '0') for p in PROFILES], 'all11 profiles must parse once')
    job = 'verify-write' if phase == 'write' else 'verify-read'
    require(re.findall(r'^FIO-ADMISSION-EXIT (\S+) (\d+)$', text, re.M) == [('version', '0'), (job, '0')], 'fio exit status')
    expected_logs = {'version.txt', 'version.stderr', 'fio.stdout', 'fio.stderr', 'result.json'}
    expected_logs.update(f'parse-{p}.{stream}' for p in PROFILES for stream in ('stdout', 'stderr'))
    require(len(re.findall(r'^FIO-ADMISSION-LOG-BEGIN(?: |$)', text, re.M)) == len(expected_logs), 'missing/extra log begin frame')
    require(len(re.findall(r'^FIO-ADMISSION-LOG-END(?: |$)', text, re.M)) == len(expected_logs), 'missing/extra log end frame')
    logs = {}
    for name, content, end in re.findall(r'^FIO-ADMISSION-LOG-BEGIN ([\w.-]+)\n(.*?)^FIO-ADMISSION-LOG-END ([\w.-]+)$', text, re.M | re.S):
        require(name == end and name not in logs, 'duplicate/mismatched log frame')
        logs[name] = content
    require(logs.get('version.txt', '').strip() == 'fio-3.39', 'unmodified fio version')
    require(set(logs) == expected_logs, 'missing/unexpected version/parse/fio output logs')
    result = None
    try:
        result = json.loads(logs['result.json'])
        require(result['fio version'] == 'fio-3.39', 'JSON fio version')
        jobs = result['jobs']; require(len(jobs) == 1, 'job count')
        j = jobs[0]; opts = j['job options']
        require(j['jobname'] == job and j['error'] == 0, 'fio job error/name')
        for key, value in {'ioengine': 'io_uring', 'direct': '1', 'iodepth': '16', 'verify': 'crc32c', 'bs': '4k', 'size': '64m'}.items():
            require(str(opts.get(key)) == value, f'job option {key}')
        require(j['read']['io_bytes'] == VERIFY_BYTES, 'full verification read')
        require(j['write']['io_bytes'] == (VERIFY_BYTES if phase == 'write' else 0), 'finite write size')
        require(bool(j.get('iodepth_level')), 'achieved depth distribution missing')
    except (KeyError, ValueError, TypeError, IndexError, AttributeError) as e: errors.append(f'invalid fio JSON: {e}')
    data = re.findall(r'^FIO-ADMISSION-DATA bytes=(\d+) sha256=([a-f0-9]{64})$', text, re.M)
    require(len(data) == 1 and data[0][0] == str(VERIFY_BYTES), 'file length/hash')
    require(text.count('\nFIO-ADMISSION-ROOT-READONLY\n') == 1, 'root not remounted read-only')
    require(re.findall(r'^FIO-ADMISSION-PASS phase=(\S+)$', text, re.M) == [phase], 'guest completion')
    require(not re.search(r'FIO-ADMISSION-FAIL|VIRTIO-BLOCK-FAIL|BUG:|Oops:|Kernel panic|Call Trace:|EXT4-fs error|Buffer I/O error', text), 'guest/kernel/filesystem error')
    return errors, logs, result, data[0][1] if len(data) == 1 else None


def restart_seed(directory):
    """Reuse only an image whose prior read-only restart passed the complete oracle."""
    image = directory / 'root.raw'
    result = json.loads((directory / 'verify/result.json').read_text())
    sha = digest(image)
    if result.get('phase') != 'verify' or result.get('pass') is not True or result.get('errors') != [] \
            or result.get('returncode') != 0 or result.get('timeout') is not False or result.get('log_limit') is not False \
            or result.get('disk_before') != sha or result.get('disk_after') != sha \
            or not re.fullmatch(r'[0-9a-f]{64}', result.get('file_sha256', '')):
        raise ValueError('restart seed is not an unchanged, verified image')
    return image, sha, result['file_sha256']


def run(args):
    inputs = json.loads((args.inputs / 'inputs.json').read_text())
    for name, sha in inputs['outputs'].items():
        if digest(args.inputs / name) != sha: raise ValueError(f'prepared input changed: {name}')
    previous = json.loads(args.build_manifest.read_text())
    artifacts = {str(p.resolve()): digest(p) for p in (args.natemu, args.runtime, args.fexc)}
    for name, sha in artifacts.items():
        if previous['artifacts'].get(name) != sha: raise ValueError(f'binary differs from build evidence: {name}')
    fsck = Path(inputs['tools']['e2fsck']['path'])
    if digest(fsck) != inputs['tools']['e2fsck']['sha256']: raise ValueError('fsck changed')
    source, source_sha, persisted = restart_seed(args.restart_from) if args.restart_from else (args.inputs / 'root.raw', inputs['outputs']['root.raw'], None)
    phases = ('verify',) if args.restart_from else ('write', 'verify')
    args.out.mkdir(parents=True, exist_ok=False)
    write_json(args.out / 'manifest.json', {'purpose': 'functional tool admission only', 'inputs': inputs, 'seconds_per_vm': 240,
        'maximum_launches': len(phases), 'artifacts': artifacts, 'build_manifest': {'path': str(args.build_manifest.resolve()), 'sha256': digest(args.build_manifest)},
        'restart_seed': None if not args.restart_from else {'path': str(source.resolve()), 'sha256': source_sha, 'file_sha256': persisted,
            'result_sha256': digest(args.restart_from / 'verify/result.json')},
        'oracle_sources': {str(p.resolve()): digest(p) for p in (Path(__file__), Path(block.__file__), ROOT / 'tools/run_virtio_rng.py')},
        'note': 'oracle added separately; not claimed part of the frozen production build'})
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = args.out / 'root.raw'
        checked(['cp', '-c', source, image], args.out / 'clone.log')
        baseline, results = digest(image), []
        if baseline != source_sha: raise ValueError('clone differs')
        for phase in phases:
            case = args.out / phase; case.mkdir(); before = digest(image)
            command = [str(args.natemu.resolve()), '--runtime', str(args.runtime.resolve()), '--fexc', str(args.fexc.resolve()),
                       '--kernel', str((args.inputs / 'linux').resolve()), '--initrd', str((args.inputs / 'bootstrap.cpio').resolve()),
                       '--disk', str(image.resolve()), '--vcpus', '2', '--ram', '2G', '--compile-cache', 'off',
                       '--serial', str((case / 'serial.log').resolve()), '--cmdline',
                       f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr root=/dev/vda io_phase={phase} io_rng=0']
            write_json(case / 'command.json', command)
            started, timed_out, log_limit = time.monotonic(), False, False
            env = dict(os.environ); env.pop('NATEMU_PERF_LOG', None)
            with (case / 'stdout.log').open('wb') as out, (case / 'stderr.log').open('wb') as err:
                p = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, start_new_session=True)
                try:
                    while p.poll() is None:
                        if time.monotonic() - started >= 240:
                            timed_out = True; block.stop_group(p); break
                        if sum(f.stat().st_size for f in case.glob('*.log')) > 64 << 20:
                            log_limit = True; block.stop_group(p); break
                        try: p.wait(timeout=0.25)
                        except subprocess.TimeoutExpired: pass
                except BaseException: block.stop_group(p); raise
            serial = case / 'serial.log'
            text = serial.read_text(errors='replace').replace('\r', '') if serial.exists() else ''
            errors, logs, fio, content_hash = judge(text, phase)
            exported = case / 'fio'; exported.mkdir()
            for name, content in logs.items(): (exported / name).write_text(content)
            after = digest(image)
            if timed_out or p.returncode or log_limit: errors.append(f'host rc={p.returncode} timeout={timed_out} log_limit={log_limit}')
            if 'natemu: Power(1)' not in (case / 'stderr.log').read_text(errors='replace'): errors.append('S5 poweroff missing')
            if phase == 'write':
                persisted = content_hash
                if after == baseline: errors.append('write did not change image')
            else:
                if content_hash != persisted: errors.append('relaunch file hash differs')
                if after != before: errors.append('read-only boot changed whole image')
            try: checked([fsck, '-fn', image], case / 'fsck.log')
            except RuntimeError as e: errors.append(f'filesystem check: {e}')
            result = {'phase': phase, 'seconds': time.monotonic() - started, 'returncode': p.returncode, 'timeout': timed_out, 'log_limit': log_limit,
                      'disk_before': before, 'disk_after': after, 'file_sha256': content_hash, 'errors': errors, 'pass': not errors}
            write_json(case / 'result.json', result); results.append(result); write_json(args.out / 'results.json', results)
            print(json.dumps(result), flush=True)
            if errors: return 1
    return 0


def self_check():
    subprocess.run(['sh', '-n'], input=INIT, text=True, check=True)
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); (root / 'usr/lib').mkdir(parents=True)
        (root / 'lib').symlink_to('/usr/lib'); (root / 'usr/lib/x').write_text('object')
        assert guest(root, '/lib/x') == root / 'usr/lib/x'
        (root / 'usr/lib/cycle').symlink_to('cycle')
        try: guest(root, '/lib/cycle')
        except ValueError: pass
        else: raise AssertionError('symlink loop accepted')
        (root / 'verify').mkdir(); (root / 'root.raw').write_bytes(b'restart seed')
        sha = digest(root / 'root.raw')
        good = {'phase': 'verify', 'pass': True, 'errors': [], 'returncode': 0, 'timeout': False, 'log_limit': False,
                'disk_before': sha, 'disk_after': sha, 'file_sha256': 'a' * 64}
        write_json(root / 'verify/result.json', good)
        assert restart_seed(root) == (root / 'root.raw', sha, 'a' * 64)
        for key, value in [('phase', 'write'), ('pass', False), ('errors', ['bad']), ('returncode', 1),
                           ('timeout', True), ('log_limit', True), ('disk_before', '0' * 64),
                           ('disk_after', '0' * 64), ('file_sha256', 'bad')]:
            write_json(root / 'verify/result.json', {**good, key: value})
            try: restart_seed(root)
            except ValueError: pass
            else: raise AssertionError(f'invalid restart seed accepted: {key}')
        write_json(root / 'verify/result.json', good); (root / 'root.raw').write_bytes(b'changed')
        try: restart_seed(root)
        except ValueError: pass
        else: raise AssertionError('changed restart image accepted')
    for phase in ('write', 'verify'):
        job = 'verify-write' if phase == 'write' else 'verify-read'
        opts = {'ioengine': 'io_uring', 'direct': '1', 'iodepth': '16', 'verify': 'crc32c', 'bs': '4k', 'size': '64m'}
        result = {'fio version': 'fio-3.39', 'jobs': [{'jobname': job, 'error': 0, 'job options': opts,
                  'read': {'io_bytes': VERIFY_BYTES}, 'write': {'io_bytes': VERIFY_BYTES if phase == 'write' else 0}, 'iodepth_level': {'16': 99}}]}
        logs = {'version.txt': 'fio-3.39', 'version.stderr': '', 'fio.stdout': '', 'fio.stderr': '', 'result.json': json.dumps(result)}
        for name in PROFILES:
            for stream in ('stdout', 'stderr'): logs[f'parse-{name}.{stream}'] = ''
        # 22 parse logs + version pair + fio pair + JSON =27.
        mode = 'rw' if phase == 'write' else 'ro'
        text = f'FIO-ADMISSION-BEGIN phase={phase} cpus=2 kernel={block.KVER}\nFIO-ADMISSION-ROOT /dev/vda ext4 {mode},relatime\n'
        text += 'FIO-ADMISSION-EXIT version 0\n' + ''.join(f'FIO-ADMISSION-PARSE {p} 0\n' for p in PROFILES)
        text += f'FIO-ADMISSION-EXIT {job} 0\n'
        text += ''.join(f'FIO-ADMISSION-LOG-BEGIN {name}\n{content}\nFIO-ADMISSION-LOG-END {name}\n' for name, content in logs.items())
        text += f'FIO-ADMISSION-DATA bytes={VERIFY_BYTES} sha256=' + 'a' * 64 + f'\nFIO-ADMISSION-ROOT-READONLY\nFIO-ADMISSION-PASS phase={phase}\n'
        assert not judge(text, phase)[0], judge(text, phase)[0]
        for old, new in [('cpus=2', 'cpus=1'), ('"io_uring"', '"psync"'), ('"error": 0', '"error": 5'),
                         ('"io_bytes": 67108864', '"io_bytes": 4096'), ('PARSE warm-read 0', 'PARSE warm-read 1'),
                         ('ROOT-READONLY', 'missing'), ('fio-3.39', 'fio-3.38')]:
            assert judge(text.replace(old, new), phase)[0], old
        assert judge(text + 'EXT4-fs error\n', phase)[0]
        assert judge(text.replace('parse-warm-read.stdout', 'unexpected.stdout'), phase)[0]
        missing = re.sub(r'^FIO-ADMISSION-LOG-BEGIN parse-warm-read.stdout\n.*?^FIO-ADMISSION-LOG-END parse-warm-read.stdout\n', '', text, flags=re.M | re.S)
        assert judge(missing, phase)[0]
        assert judge(text + 'FIO-ADMISSION-LOG-BEGIN result.json\n{', phase)[0]
        assert judge(text + 'FIO-ADMISSION-LOG-END result.json\n', phase)[0]
        assert judge(text + 'FIO-ADMISSION-LOG-BEGIN result.json\n{}\nFIO-ADMISSION-LOG-END result.json\n', phase)[0]
    print('Shell syntax, guest-symlink, restart-seed and negative result-parser controls pass; no VM/fio execution.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('verify-inputs', 'prepare'):
        p = sub.add_parser(name)
        p.add_argument('--fio-prep', type=Path, required=True); p.add_argument('--block-inputs', type=Path, required=True)
        if name == 'prepare': p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('run')
    for name in ('natemu', 'runtime', 'fexc', 'inputs', 'out', 'lock', 'build-manifest'): p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--restart-from', type=Path, help='clone root.raw from a prior passing write/restart run and repeat only the unchanged verification phase')
    sub.add_parser('self-check')
    args = parser.parse_args()
    if args.command == 'self-check': return self_check()
    if args.command == 'verify-inputs': verify_inputs(args); return 0
    if args.command == 'prepare': return prepare(args)
    return run(args)


if __name__ == '__main__': raise SystemExit(main())
