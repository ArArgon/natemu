"""A disk cache for QEMU system-mode reference runs (`smp.run_image`, tools/bench/smp/smp.py) in the correctness runners.

    import qemucache; qemucache.install(smp)

A reference run is QEMU's deterministic answer for one image, so it is stored under a hash of the image, the QEMU
arguments and the QEMU binary: a program run again (the next suite, or K1's shards and the PIC modes, which all run the same
programs) skips the QEMU boot, 0.2-0.8 s each (and 10-30 s for a guest that hangs). Only finished runs (exit status 1, the
debug-exit device) are stored, so a flaky timeout is never remembered; a caller whose timeout IS the expected answer (a
guest QEMU never finishes) passes `hang_ok=True` and has a timeout of at least its own limit remembered too. Runs with
extra QEMU arguments (a serial file the caller reads back), the benchmarks' own calls (timing) and the litmus and stress
steps (samples of QEMU's behaviour) are not wrapped. Stored in NATEMU_RAW_ROOT/qemu-cache.sqlite (WAL; no persistence without
NATEMU_RAW_ROOT; NATEMU_QEMU_CACHE=off disables it, e.g. for a final gate)."""
import hashlib, json, os, pathlib, sqlite3, subprocess, time, zlib


def install(smp):
    if getattr(smp.run_image, 'cached', False): return
    real, state = smp.run_image, {}

    def db():
        if 'db' not in state:
            root, state['db'] = os.environ.get('NATEMU_RAW_ROOT'), None
            if root and os.environ.get('NATEMU_QEMU_CACHE') != 'off':
                try:
                    state['db'] = sqlite3.connect(pathlib.Path(root) / 'qemu-cache.sqlite', timeout=300)
                    state['db'].execute('pragma journal_mode=wal')
                    state['db'].execute('create table if not exists q (k text primary key, v blob)')
                except sqlite3.Error: state['db'] = None
        return state['db']

    def binary():
        if 'binary' not in state:
            state['binary'] = hashlib.sha256(pathlib.Path(smp.QEMU).resolve().read_bytes()).hexdigest()
        return state['binary']

    def boot(prog, img, work, mode, timeout, cpu):      # `smp.run_image` with a chosen QEMU CPU model (tools/run_i5b.py)
        work.mkdir(parents=True, exist_ok=True); p, dbg = work / 'image.bin', work / 'dbg.out'
        p.write_bytes(img); dbg.unlink(missing_ok=True); t0 = time.perf_counter()
        try:
            r = subprocess.run(smp.qemu_args(prog['ncpu'], mode, dbg, p, cpu), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
            rc, err = r.returncode, r.stderr.decode(errors='replace')[-300:]
        except subprocess.TimeoutExpired:
            rc, err = 'timeout', ''
        return rc, err, (dbg.read_bytes() if dbg.exists() else b''), time.perf_counter() - t0

    def run_image(prog, img, work, mode='multi', timeout=120, extra=(), hang_ok=False, cpu=None):
        if extra or not db(): return real(prog, img, work, mode, timeout, extra) if cpu is None else boot(prog, img, work, mode, timeout, cpu)
        key_cpu = cpu or (smp.SYSTEM_CPU if prog.get('system') else 'max')
        key = hashlib.sha256(json.dumps([binary(), smp.qemu_args(prog['ncpu'], mode, 'DBG', 'IMG', key_cpu)[1:], hashlib.sha256(img).hexdigest()]).encode()).hexdigest()
        row = db().execute('select v from q where k = ?', (key,)).fetchone()
        if row:
            rc, err, dump, limit = json.loads(zlib.decompress(row[0]))
            if rc == 1: return rc, err, bytes.fromhex(dump), 0.0
            if hang_ok and rc == 'timeout' and limit >= timeout: return rc, err, b'', 0.0
        rc, err, dump, took = real(prog, img, work, mode, timeout, extra) if cpu is None else boot(prog, img, work, mode, timeout, cpu)
        if rc == 1 or (hang_ok and rc == 'timeout'):
            try:
                db().execute('insert or replace into q values (?, ?)', (key, zlib.compress(json.dumps([rc, err, dump.hex(), timeout]).encode())))
                db().commit()
            except sqlite3.Error: pass      # a cache that cannot be written only costs time
        return rc, err, dump, took

    run_image.cached = True
    smp.run_image = run_image
