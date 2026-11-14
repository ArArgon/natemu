"""A disk cache for QEMU system-mode reference runs (`smp.run_image`, tools/bench/smp/smp.py) in the correctness runners.

    import qemucache; qemucache.install(smp)

A reference run is QEMU's deterministic answer for one image, so it is stored under a hash of the image, the QEMU
arguments (with the CPU model smp itself chooses, `smp.default_cpu`), the QEMU binary and the firmware files it loads from
its share directory (SeaBIOS, the multiboot and linuxboot option ROMs): a program run again (the next suite, or K1's shards
and the PIC modes, which all run the same programs) skips the QEMU boot, 0.2-0.8 s each (and 10-30 s for a guest that hangs).

What is stored: finished runs (exit status 1, the debug-exit device). A timeout is never stored from one observation: a caller
whose expected answer IS a timeout (a guest QEMU never finishes) passes `hang_ok=True`; the first fresh timeout leaves only a
strike (never served), a second fresh timeout for the same key with no finished run between confirms it, and a confirmed hang
is served only to a caller whose limit is at most the one observed. Other exit statuses are never stored. An unreadable row or
database is a miss (reported once on stderr), never an error.

Where it applies: every runner that imports tools/run_slice3_vm.py or tools/run_i5b.py (K1 and the PIC modes, slice 3's
corpora, kicks, events and system, x1, x3, m2, l3, l4, r1, s4, capacity, vector_split_smp ...), including their stress steps,
whose QEMU references are deterministic final states. Not wrapped: runs with extra QEMU arguments (a serial file the caller
reads back), and the benchmarks' and smp.py's own calls (timing, determinism samples), which never import this module.
Stored in NATEMU_RAW_ROOT/qemu-cache.sqlite (WAL; no persistence without NATEMU_RAW_ROOT; NATEMU_QEMU_CACHE=off disables it,
e.g. for a final gate)."""
import hashlib, json, os, pathlib, sqlite3, sys, zlib

FIRMWARE = ('bios-256k.bin', 'multiboot.bin', 'multiboot_dma.bin', 'linuxboot.bin', 'linuxboot_dma.bin', 'kvmvapic.bin', 'pvh.bin')


def key_of(smp, prog, img, mode, cpu, binary):
    """The cache key: QEMU binary and firmware (`binary`), its arguments with `cpu` (None: smp's own choice), the image."""
    args = smp.qemu_args(prog['ncpu'], mode, 'DBG', 'IMG', cpu or smp.default_cpu(prog))[1:]
    return hashlib.sha256(json.dumps([binary, args, hashlib.sha256(img).hexdigest()]).encode()).hexdigest()


def binary_hash(qemu):
    """sha256 of the QEMU executable and of the firmware files it loads from `<prefix>/share/qemu` (those present)."""
    exe = pathlib.Path(qemu).resolve()
    h = hashlib.sha256(exe.read_bytes())
    share = exe.parent.parent / 'share' / 'qemu'
    for name in FIRMWARE:
        if (share / name).is_file(): h.update(name.encode() + (share / name).read_bytes())
    return h.hexdigest()


def install(smp):
    if getattr(smp.run_image, 'cached', False): return
    real, state = smp.run_image, {}

    def warn(why):
        if not state.get('warned'):
            state['warned'] = True
            print(f'qemucache: {why}; running QEMU (a miss)', file=sys.stderr, flush=True)

    def db():
        if 'db' not in state:
            root, state['db'] = os.environ.get('NATEMU_RAW_ROOT'), None
            if root and os.environ.get('NATEMU_QEMU_CACHE') != 'off':
                try:
                    state['db'] = sqlite3.connect(pathlib.Path(root) / 'qemu-cache.sqlite', timeout=300)
                    state['db'].execute('pragma journal_mode=wal')
                    state['db'].execute('create table if not exists q (k text primary key, v blob)')
                except sqlite3.Error as e: warn(f'database unusable ({e})')
        return state['db']

    def lookup(key):
        """(rc, err, dump bytes, limit) stored under `key`, or None (no row, or one that cannot be read)."""
        try:
            row = db().execute('select v from q where k = ?', (key,)).fetchone()
            if not row: return None
            rc, err, dump, limit = json.loads(zlib.decompress(row[0]))
            return rc, err, bytes.fromhex(dump), limit
        except (sqlite3.Error, zlib.error, ValueError, TypeError) as e:
            warn(f'unreadable row ({type(e).__name__}: {e})')
            return None

    def store(key, rc, err, dump, limit):
        try:
            db().execute('insert or replace into q values (?, ?)', (key, zlib.compress(json.dumps([rc, err, dump.hex(), limit]).encode())))
            db().commit()
        except sqlite3.Error as e: warn(f'cannot write ({e})')      # a cache that cannot be written only costs time

    def run_image(prog, img, work, mode='multi', timeout=120, extra=(), hang_ok=False, cpu=None):
        if extra or not db(): return real(prog, img, work, mode, timeout, extra, cpu=cpu)
        if 'binary' not in state: state['binary'] = binary_hash(smp.QEMU)
        key = key_of(smp, prog, img, mode, cpu, state['binary'])
        old = lookup(key)
        if old:
            if old[0] == 1: return 1, old[1], old[2], 0.0
            if hang_ok and old[0] == 'timeout' and old[3] >= timeout: return 'timeout', old[1], b'', 0.0
        rc, err, dump, took = real(prog, img, work, mode, timeout, extra, cpu=cpu)
        if rc == 1: store(key, rc, err, dump, timeout)
        elif rc == 'timeout' and hang_ok:       # a first timeout is a strike (never served); a second one confirms the hang
            confirmed = bool(old) and old[0] in ('strike', 'timeout')
            store(key, 'timeout' if confirmed else 'strike', err, b'', max(timeout, old[3] if confirmed else 0))
        return rc, err, dump, took

    run_image.cached = True
    smp.run_image = run_image
