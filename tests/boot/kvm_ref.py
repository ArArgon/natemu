"""One session on the x86 reference machine for the boot family's KVM references (run_kut.py, run_ovmf.py): the files
go there by tar over ssh, a shell script runs in the builds' pinned container image (testkit/artifacts.py
BENCH_BUILDER) with /dev/kvm and Debian trixie's QEMU at the campaign images' snapshot (tools/correctness/recipes/
common.sh SNAP) installed for the session (with any other packages the script needs), and what the script writes to
/b/out comes back. The container is named and killed when the ssh session ends, the temporary folder there removed;
nothing stays on the machine. The session takes one of the machine's build slots (its RAM), as the other KVM
references do (tests/interp/interp_rig.py run_kvm, testkit/harness/campaign.py native_vm)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import io, re, shlex, subprocess, tarfile, uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMAGE = 'docker.io/library/debian@sha256:8f6a88feef3ed01a300dafb87f208977f39dccda1fd120e878129463f7fa3b8f'   # = testkit/artifacts.py BENCH_BUILDER
CPU = 'host,-pku'                       # = testkit/harness/campaign.py REFERENCE_CPU: the machine's CPU less protection keys
APT_TIMEOUT = 900
SLOTS = ('got=; while [ -z "$got" ]; do for i in $(seq 0 $(( $(nproc) / 4 > 1 ? $(nproc) / 4 - 1 : 0 ))); do '
         'exec 9>/tmp/natemu-build-slot-$i; if flock -n 9; then got=$i; break; fi; done; [ -n "$got" ] || sleep 5; done; ')
REF = re.compile(r'^ref-(\w+): (.*)$', re.M)


def snapshot() -> str:
    return re.search(r'^SNAP=(\S+)$', (ROOT / 'tools/correctness/recipes/common.sh').read_text(), re.M)[1]


def pin() -> str:
    """The KVM reference's identity in answer keys: the QEMU it runs (never the machine's address)."""
    return f'kvm: qemu-system-x86 of Debian trixie at snapshot {snapshot()}, -cpu {CPU}, image {IMAGE}'


def session(host: str, files: dict, script: str, work: pathlib.Path, timeout: int, packages=('qemu-system-x86',)) -> dict:
    """Run `script` (sh, in /b with the `files` {name: bytes}) on `host` under KVM; its /b/out comes back into
    `work`/out. Returns the machine's description (CPU, kernel, virtualization, QEMU) from the session's ref- lines;
    the session's stderr is kept in `work`/kvm.log."""
    work.mkdir(parents=True, exist_ok=True)
    pack = io.BytesIO()
    with tarfile.open(fileobj=pack, mode='w') as t:
        for n, data in files.items():
            info = tarfile.TarInfo(n); info.size = len(data); info.mode = 0o644
            t.addfile(info, io.BytesIO(data))
    name = f'natemu-ref-boot-{uuid.uuid4().hex[:12]}'
    apt = f'http://snapshot.debian.org/archive/debian/{snapshot()}/'
    guest = (f"rm -f /etc/apt/sources.list.d/*; echo 'deb [check-valid-until=no] {apt} trixie main' > /etc/apt/sources.list; "
             f'timeout {APT_TIMEOUT} apt-get -o Acquire::Retries=5 update -qq >&2 && '
             f'timeout {APT_TIMEOUT} apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends {" ".join(packages)} >&2 && '
             'echo "ref-qemu: $(qemu-system-x86_64 --version | head -1)" >&2 && mkdir -p /b/out && cd /b && '
             f'{{ {script} ; }}; chmod -R a+rwX /b/out')
    remote = (f'set -e; d=$(mktemp -d); '
              f"trap 'docker kill {name} >/dev/null 2>&1 || true; docker run --rm -v \"$d:/b\" {IMAGE} rm -rf /b/out "
              f">/dev/null 2>&1 || true; rm -rf \"$d\"' EXIT HUP TERM INT; tar xf - -C \"$d\"; "
              "echo \"ref-cpu: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')\" >&2; "
              'echo "ref-kernel: $(uname -r)" >&2; echo "ref-virt: $(systemd-detect-virt 2>/dev/null || echo unknown)" >&2; '
              f'{SLOTS}docker run --rm -i --name {name} --device /dev/kvm -v "$d:/b" {IMAGE} sh -c {shlex.quote(guest)} '
              '< /dev/null >&2 || true; tar cf - -C "$d" out')
    try:
        r = subprocess.run(['ssh', '-o', 'BatchMode=yes', host, remote], input=pack.getvalue(), capture_output=True,
                           timeout=2 * APT_TIMEOUT + timeout + 300)
    except subprocess.TimeoutExpired:
        subprocess.run(['ssh', '-o', 'BatchMode=yes', host, f'docker kill {name}'], capture_output=True, timeout=60)
        raise SystemExit(f'the KVM session on {host} took too long')
    err = r.stderr.decode(errors='replace')
    (work / 'kvm.log').write_text(err)
    if r.returncode != 0 or not r.stdout:
        raise SystemExit(f'the KVM session on {host} failed (exit {r.returncode}): {err[-800:]}')
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        t.extractall(work, filter='data')
    return {k: v.strip() for k, v in REF.findall(err)}
