"""A disk cache for `ref.assemble` (tools/reference/ref.py): the same text assembles to the same bytes under the same
clang, but the fault runners assemble thousands of one-line programs and every shard and every `-1` step did it again
(each call spawns clang: about 15 ms, 54 s of a 63 s `run_slice1_faults.expectations`).

    import asmcache; asmcache.install(ref)

Wraps `ref.assemble` in place (tools/reference is not touched: its sources key the reference-result cache). The key is a
hash of the text, `labels`, `clang --version` (one spawn per process) and the source of `ref.assemble` itself, so a
different assembler or a change to how the object is read misses. Stored in NATEMU_RAW_ROOT/asm-cache.sqlite (WAL; no
persistence without NATEMU_RAW_ROOT); misses are written in batches and at exit, so parallel steps rarely meet a
locked database. Failures (an assembler error) are never cached."""
import atexit, hashlib, inspect, json, os, pathlib, sqlite3, subprocess

_PENDING_MAX = 500


def install(ref):
    if getattr(ref.assemble, 'flush', None): return ref.assemble.flush
    real, memo, pending, state = ref.assemble, {}, [], {}

    def db():
        if 'db' not in state:
            root, state['db'] = os.environ.get('NATEMU_RAW_ROOT'), None
            if root and os.environ.get('NATEMU_ASM_CACHE') != 'off':
                try:
                    state['db'] = sqlite3.connect(pathlib.Path(root) / 'asm-cache.sqlite', timeout=300)
                    state['db'].execute('pragma journal_mode=wal')
                    state['db'].execute('create table if not exists a (k text primary key, v text)')
                except sqlite3.Error: state['db'] = None
        return state['db']

    def flush():
        if pending and db():
            try:
                db().executemany('insert or ignore into a values (?, ?)', pending); db().commit()
            except sqlite3.Error: pass      # a cache that cannot be written only costs time
            pending.clear()

    def salt():
        if 'salt' not in state:
            clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout
            state['salt'] = hashlib.sha256((clang + inspect.getsource(real)).encode()).hexdigest()
        return state['salt']

    def assemble(text, labels=False):
        key = hashlib.sha256(json.dumps([salt(), text, labels]).encode()).hexdigest()
        if key in memo: return memo[key]
        row = db().execute('select v from a where k = ?', (key,)).fetchone() if db() else None
        if row:
            v = json.loads(row[0])
            out = (bytes.fromhex(v[0]), v[1]) if labels else bytes.fromhex(v)
        else:
            out = real(text, labels)
            pending.append((key, json.dumps([out[0].hex(), out[1]] if labels else out.hex())))
            if len(pending) >= _PENDING_MAX: flush()
        memo[key] = out
        return out

    assemble.flush = flush
    atexit.register(flush)
    ref.assemble = assemble
    return flush
