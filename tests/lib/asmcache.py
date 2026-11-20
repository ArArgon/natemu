"""A disk cache for `ref.assemble` (tools/reference/ref.py): the same text assembles to the same bytes under the same
clang, but the fault runners assemble thousands of one-line programs and every shard and every `-1` step did it again
(each call spawns clang: about 15 ms, 54 s of a 63 s `run_slice1_faults.expectations`).

    import asmcache; asmcache.install(ref)

Wraps `ref.assemble` in place (tools/reference is not touched: its sources key the reference-result cache). The key is a
hash of the text, `labels`, `clang --version` (one spawn per process) and the source of `ref.assemble` itself, so a
different assembler or a change to how the object is read misses. Stored in NATEMU_STATE_ROOT/asm-cache.sqlite (else
NATEMU_RAW_ROOT; no persistence without either; WAL; NATEMU_ASM_CACHE=off disables it); misses are written in batches and at exit, so
parallel steps rarely meet a locked database. Failures (an assembler error) are never cached; an unreadable row or database
is a miss (reported once on stderr), so a `ValueError('assembler: ...')` always means clang rejected the text."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import atexit, hashlib, inspect, json, os, pathlib, sqlite3, subprocess, sys

_PENDING_MAX = 500


def install(ref):
    if getattr(ref.assemble, 'flush', None): return ref.assemble.flush
    real, memo, pending, state = ref.assemble, {}, [], {}

    def warn(why):
        if not state.get('warned'):
            state['warned'] = True
            print(f'asmcache: {why}; assembling (a miss)', file=sys.stderr, flush=True)

    def db():
        if 'db' not in state:
            root, state['db'] = os.environ.get('NATEMU_STATE_ROOT') or os.environ.get('NATEMU_RAW_ROOT'), None
            if root and os.environ.get('NATEMU_ASM_CACHE') != 'off':
                try:
                    state['db'] = sqlite3.connect(pathlib.Path(root) / 'asm-cache.sqlite', timeout=300)
                    state['db'].execute('pragma journal_mode=wal')
                    state['db'].execute('create table if not exists a (k text primary key, v text)')
                except sqlite3.Error as e:
                    state['db'] = None; warn(f'database unusable ({e})')
        return state['db']

    def lookup(key, labels):
        """The stored result for `key` in `assemble`'s return form, or None (no row, or one that cannot be read)."""
        try:
            row = db().execute('select v from a where k = ?', (key,)).fetchone() if db() else None
            if not row: return None
            v = json.loads(row[0])
            return (bytes.fromhex(v[0]), dict(v[1])) if labels else bytes.fromhex(v)
        except (sqlite3.Error, ValueError, TypeError, IndexError) as e:
            warn(f'unreadable row ({type(e).__name__}: {e})')
            return None

    def flush():
        if pending and db():
            try:
                db().executemany('insert or replace into a values (?, ?)', pending); db().commit()
            except sqlite3.Error as e: warn(f'cannot write ({e})')      # a cache that cannot be written only costs time
            pending.clear()

    def salt():
        if 'salt' not in state:
            clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout
            state['salt'] = hashlib.sha256((clang + inspect.getsource(real)).encode()).hexdigest()
        return state['salt']

    def assemble(text, labels=False):
        key = hashlib.sha256(json.dumps([salt(), text, labels]).encode()).hexdigest()
        if key in memo: return memo[key]
        out = lookup(key, labels)
        if out is None:
            out = real(text, labels)        # an assembler error propagates and is never cached
            pending.append((key, json.dumps([out[0].hex(), out[1]] if labels else out.hex())))
            if len(pending) >= _PENDING_MAX: flush()
        memo[key] = out
        return out

    assemble.flush = flush
    atexit.register(flush)
    ref.assemble = assemble
    return flush
