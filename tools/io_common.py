"""Helpers shared by the run_io_*, run_fio_*, run_virtio_* and run_serial_stress runners."""
import contextlib
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess

from raw_root import RAW_ROOT

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The startup lines natemu prints (`natemu: <topic>: ...`); crates/host/src/probe.rs `TOPICS` is this list (a unit test checks it).
# Stops, errors and warnings (`natemu: disk lock: ...`, `natemu: pci: ...`) are not startup lines.
STARTUP_TOPICS = ('cpu', 'fp', 'acceleration', 'meltdown', 'code cache')
STARTUP_LINE = re.compile(r'^natemu: (?:%s): ' % '|'.join(STARTUP_TOPICS))
VM_LOCK = RAW_ROOT / '.vm.lock'   # default --lock: one VM-running agent at a time (flock on this file)


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''): h.update(block)
    return h.hexdigest()


def write_json(path, value):
    pathlib.Path(path).write_text(json.dumps(value, indent=2) + '\n')


def stop_group(process):
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: return
    try: process.wait(timeout=2)
    except subprocess.TimeoutExpired: pass
    # A compiler descendant can survive the main process; terminate the whole session.
    try: os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    process.wait()


# --- PCI transport (--transport pci) ---------------------------------------------------------------------------------
# Guest probe (shell; @P@ is the runner's line prefix) and its judge. Slots are fixed by the machine description.
PCI_FUNCTIONS = {'block': ('0000:00:01.0', '0x1af4', '0x1042', '0x010000'), 'net': ('0000:00:02.0', '0x1af4', '0x1041', '0x020000'),
                 'rng': ('0000:00:03.0', '0x1af4', '0x1044', '0x00ff00')}
# Real 6.12 kernel strings: reassignment is `pci 0000:00:01.0: BAR 0 [mem 0x...-0x...]: assigned` (Linux should keep the BARs we
# programmed, so it is a failure), a claim failure `...: can't claim; no compatible bridge window`. The MMCONFIG complaint
# ("fail to add MMCONFIG ... can't access extended configuration space": no ECAM by design) must not match.
PCI_BAD_DMESG = (r'No config space access function found', r'Cannot find an available gap', r'BAR \d+ \[[^\]]*\]: assigned',
                 r"can't claim", r"can't assign", r'failed to assign')
PCI_PROBE = r'''pci_probe() {
  echo "@P@-PCI-BEGIN"
  for d in /sys/bus/pci/devices/*; do echo "@P@-PCI-DEV ${d##*/} $(cat $d/vendor) $(cat $d/device) $(cat $d/class)"; done
  grep virtio /proc/interrupts | sed 's/^/@P@-PCI-IRQ /'
  dmesg | grep -iE 'pci|virtio|msi|gap|claim|assign|config space' | sed 's/^/@P@-PCI-DMESG /'
  echo "@P@-PCI-END"
}
'''


def patch(text, edits):
    """TEXT with each (old, new) applied once; the PCI variants of the guest scripts are made this way so the default text stays as it was."""
    for old, new in edits:
        if text.count(old) != 1: raise ValueError(f'patch: {text.count(old)} matches of {old!r}')   # not an assert: -O must not skip it
        text = text.replace(old, new)
    return text


def pci_errors(text, prefix, cpus, used):
    """Errors for a PCI-mode serial TEXT: the host bridge and every function of USED (keys of PCI_FUNCTIONS) present, each with
    a nonzero PCI-MSIX count on a virtio queue vector, and no enumeration/BAR trouble in the probed dmesg."""
    import re
    probes = re.findall(rf'^{prefix}-PCI-BEGIN$(.*?)^{prefix}-PCI-END$', text, re.M | re.S)
    if len(probes) != 1: return [f'{len(probes)} PCI probe blocks (want 1)']
    probe, errors = probes[0], []
    devs = re.findall(rf'^{prefix}-PCI-DEV (\S+) (\S+) (\S+) (\S+)$', probe, re.M)
    want = [('0000:00:00.0', '0x8086', '0x1237', '0x060000')] + [PCI_FUNCTIONS[u] for u in used]
    if sorted(devs) != sorted(want): errors.append(f'PCI functions {devs}, wanted {want}')
    irq = re.findall(rf'^{prefix}-PCI-IRQ\s+\d+:\s+((?:\d+\s+){{{cpus}}})\s*PCI-MSIX-(\S+)\s+\d+-edge\s+virtio\d+-(\S+)$', probe, re.M)
    for u in used:
        queues = [sum(map(int, c.split())) for c, slot, name in irq if slot == PCI_FUNCTIONS[u][0] and not name.startswith('config')]
        if not queues or max(queues) == 0: errors.append(f'no nonzero PCI-MSIX queue count for {u}: {queues}')
    for bad in PCI_BAD_DMESG:
        if re.search(rf'^{prefix}-PCI-DMESG .*{bad}', probe, re.M): errors.append('dmesg: ' + bad)
    return errors


def pci_self_test():
    """pci_errors against the kernel's real line formats: the benign lines pass, each complaint fails (no VM)."""
    probe = lambda *extra: ('X-PCI-BEGIN\nX-PCI-DEV 0000:00:00.0 0x8086 0x1237 0x060000\nX-PCI-DEV 0000:00:01.0 0x1af4 0x1042 0x010000\n'
        'X-PCI-IRQ  28:          0        161  PCI-MSIX-0000:00:01.0   1-edge      virtio0-req.0\n'
        'X-PCI-DMESG [    0.9] pci 0000:00:01.0: BAR 0 [mem 0xe0010000-0xe0017fff]\n'
        'X-PCI-DMESG [    0.9] PCI: MMCONFIG for domain 0000 [bus 00-ff] at [mem 0xb0000000-0xbfffffff] (base 0xb0000000)\n'
        "X-PCI-DMESG [    0.9] PCI: not using MMCONFIG\nX-PCI-DMESG [    0.9] PCI: fail to add MMCONFIG information, can't access extended configuration space under this bridge\n"
        + ''.join('X-PCI-DMESG [    1.0] ' + e + '\n' for e in extra) + 'X-PCI-END\n')
    assert pci_errors(probe(), 'X', 2, ('block',)) == [], pci_errors(probe(), 'X', 2, ('block',))
    for bad in ('pci 0000:00:01.0: BAR 0 [mem 0xe0010000-0xe0017fff]: assigned',
                "pci 0000:00:01.0: BAR 0 [mem 0xe0010000-0xe0017fff]: can't claim; no compatible bridge window",
                "pci 0000:00:01.0: BAR 0: can't assign [mem size 0x00008000]", 'pci 0000:00:01.0: BAR 0: failed to assign [mem size 0x00008000]',
                'PCI: Cannot find an available gap in the 32-bit address range', 'PCI: No config space access function found'):
        assert pci_errors(probe(bad), 'X', 2, ('block',)), bad
    assert pci_errors(probe().replace('161', '0'), 'X', 2, ('block',)) and pci_errors(probe().replace('0x1042', '0x1041'), 'X', 2, ('block',))
    assert pci_errors(probe().replace('X-PCI-END\n', ''), 'X', 2, ('block',))


# --- qcow2 images (--image-format qcow2) -----------------------------------------------------------------------------
# The runners' own checks (e2fsck, debugfs, digests) read raw files, so a qcow2 run converts the image back to a temporary
# raw file (qemu-img writes it sparse), checks that, and deletes it at once. The qcow2 input is prepared ONCE per inputs
# directory from the raw one with preallocation=metadata: a plain convert would leave free space unallocated, and the run
# would measure cluster allocation instead of the steady state. Nothing here touches raw runs.
# --image-format overlay: a new, empty qcow2 overlay per run over the raw input (`qemu-img create -b BASE -F raw`, in the
# run folder, deleted with the run's disk), launched with `--set disk.disk0.backing=BASE` (natemu refuses an overlay whose
# base the launch does not name); the base's hash must be unchanged afterwards.
IMAGE_FORMATS = ('raw', 'qcow2', 'overlay')


def qemu_img():
    """The qemu-img binary, or None when it is not installed."""
    import shutil
    return shutil.which('qemu-img') or ('/opt/homebrew/bin/qemu-img' if os.access('/opt/homebrew/bin/qemu-img', os.X_OK) else None)


def need_qemu_img(what='--image-format qcow2'):
    q = qemu_img()
    if q is None: raise SystemExit(f'{what} needs qemu-img (brew install qemu); it is not installed')
    return q


def check_image_format(fmt):
    if fmt != 'raw': need_qemu_img(f'--image-format {fmt}')
    return fmt


def overlay_create(base, overlay, size=None):
    """A new, empty qcow2 overlay OVERLAY over the raw BASE (its absolute path recorded), as `qemu-img create -b -F raw` makes
    it; SIZE bytes (default the base's; reads past a shorter base are zeros)."""
    subprocess.run([need_qemu_img(), 'create', '-q', '-f', 'qcow2', '-b', str(pathlib.Path(base).resolve()), '-F', 'raw', str(overlay)]
                   + ([str(size)] if size else []), check=True, capture_output=True)


def overlay_set(base):
    """The natemu arguments naming BASE as the --disk overlay's backing file."""
    return ['--set', f'disk.disk0.backing={pathlib.Path(base).resolve()}']


def qcow2_convert(src, dst, fmt, *options, size=None):
    """qemu-img convert SRC (raw or qcow2) into DST of format FMT ('raw': sparse; 'qcow2': with OPTIONS, e.g. preallocation=metadata)."""
    cmd = [need_qemu_img(), 'convert', '-O', fmt] + (['-o', ','.join(options)] if options else []) + [str(src), str(dst)]
    subprocess.run(cmd, check=True, capture_output=True)
    if size is not None: subprocess.run([need_qemu_img(), 'resize', '-f', fmt] + (['--preallocation=metadata'] if fmt == 'qcow2' else []) + [str(dst), str(size)], check=True, capture_output=True)


def qcow2_input(raw, qcow, size=None):
    """Prepare QCOW once from RAW (reused when present): preallocated metadata, grown to SIZE bytes. Returns the seconds spent (0 if reused)."""
    import time
    if qcow.exists(): return 0.0
    began, part = time.monotonic(), qcow.with_name(qcow.name + '.part')
    qcow2_convert(raw, part, 'qcow2', 'preallocation=metadata', size=size)
    part.rename(qcow)
    return time.monotonic() - began


def qcow2_problems(image):
    """`qemu-img check` findings on a stopped image (empty list = clean); also returns the parsed report."""
    p = subprocess.run([need_qemu_img(), 'check', '--output=json', str(image)], capture_output=True, text=True)
    try: report = json.loads(p.stdout)
    except ValueError: return [f'qemu-img check: exit {p.returncode}, no report: {p.stderr.strip()[:200]}'], {}
    bad = [f'{k}={report[k]}' for k in ('corruptions', 'leaks', 'check-errors', 'corruptions-fixed', 'leaks-fixed') if report.get(k)]
    return ([f'qemu-img check exit {p.returncode}'] if p.returncode else []) + (['qemu-img check: ' + ' '.join(bad)] if bad else []), report


@contextlib.contextmanager
def raw_of(image, qcow, scratch):
    """The raw bytes of IMAGE for the runners' checks: IMAGE itself when raw, else a temporary raw copy at SCRATCH (deleted on exit)."""
    if not qcow: yield image; return
    qcow2_convert(image, scratch, 'raw')
    try: yield scratch
    finally: scratch.unlink(missing_ok=True)
