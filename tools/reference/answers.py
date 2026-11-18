#!/usr/bin/env python3
"""Locked QEMU answers: the comparison points of the runners (tools/run_*.py) in three modes, env NATEMU_ANSWERS.

    live (default, also when unset): today's behavior: QEMU runs (through its caches), comparisons are field by field.
    seed:   as live, and every answer also appends one JSON line to NATEMU_ANSWERS_LOG:
            {key, sha256, value?, repro, aux}: the sha256 of the comparison's *projection* (exactly the data it compares,
            after the masks it applies) of QEMU's answer; `value`, that canonical projection itself when it is at most
            VALUE_MAX bytes; `repro`, how to reproduce it (supplier, its pin, the key inputs: program or image sha256,
            QEMU's arguments); `aux`, small derived values the projection (or the runner) needs that only the reference knows.
    locked: QEMU never runs. With an inline `value` (checked against its sha256) the QEMU-running functions return the
            reference result rebuilt from it, so the runners' field-by-field comparisons run unchanged; with a hash only
            they return a placeholder carrying the key (`Ref`, `Dump`) and the comparison projects natemu's state the
            same way (with the aux) and compares hashes ("differs from the locked answer (hash only: ...)"). A key not in
            NATEMU_ANSWERS_LOCK raises `Missing`: the line `ANSWER-MISSING <key>` is printed and the process exits non-zero.

    python3 tools/reference/answers.py lock LOG... -o tests/<family>/tests.lock    # a lock from seed logs

The lock (TOML): a table [answers."<key>"] per answer with sha256, value (optional, a string of the canonical JSON),
aux (a string of JSON) and repro (an inline table). Keys are machine-independent: the program or image, the QEMU pin
*name* (env NATEMU_QEMU_PIN, e.g. "qemu-user 7.2 deb12u18" or "qemu-system 11.1.2 src"; several separated by ';'), the
QEMU arguments; never the backend, a binary hash or a code hash. A derived key adds a suffix: `/label` for a view (a
transform the runner applies to the answer before comparing, e.g. a mask), `#label` for a derived value kept in aux.
Stdlib only."""
import atexit, hashlib, json, os, sys, threading

MODES = ('live', 'seed', 'locked')
USER_PIN = 'qemu-user 7.2 deb12u18'       # ref.py checks the binary against ref.PINS: its identity is certain
HANG = 'hang'
VALUE_MAX = 1024                          # a projection whose canonical JSON is at most this many bytes is kept inline
HASH_ONLY = 'differs from the locked answer (hash only: run with --diff to see)'
_lock = threading.Lock()
_cache = {}
MISSING = set()


def mode():
    m = os.environ.get('NATEMU_ANSWERS') or 'live'
    if m not in MODES: raise ValueError(f'NATEMU_ANSWERS={m!r}: one of {MODES}')
    return m


def active(): return mode() != 'live'


def pin(kind):
    """The QEMU pin name of `kind` ('user' or 'system') from NATEMU_QEMU_PIN (the entry starting with 'qemu-<kind>')."""
    for p in (os.environ.get('NATEMU_QEMU_PIN') or '').split(';'):
        if p.strip().startswith(f'qemu-{kind}'): return p.strip()
    if kind == 'user': return USER_PIN
    raise RuntimeError(f'NATEMU_QEMU_PIN names no qemu-{kind} pin (e.g. "qemu-system 11.1.2 src"); it is part of every answer key')


def digest(data):
    if isinstance(data, str): data = data.encode()
    return hashlib.sha256(data).hexdigest()


def canon(value):
    """Canonical bytes of a JSON-able value (tuples as lists, bytes as hex)."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=_plain).encode()


def _plain(v):
    if isinstance(v, (bytes, bytearray)): return bytes(v).hex()
    if isinstance(v, (set, frozenset)): return sorted(v)
    raise TypeError(f'not JSON-able: {type(v).__name__}')


def make_key(*parts): return digest(canon(list(parts)))


class Missing(Exception):
    """A key the lock does not have: the case is not seeded (never a pass)."""
    def __init__(self, key):
        super().__init__(f'ANSWER-MISSING {key}')
        self.key = key
        with _lock:
            new = key not in MISSING
            MISSING.add(key)
        if new: print(f'ANSWER-MISSING {key}', flush=True)


class Unavailable(Exception):
    """Locked mode: a runner read a field of QEMU's answer that the lock does not hold (a hash-only answer, or a field
    outside the projection); convert the read to a comparison (`equal`), a view or a derived value."""


@atexit.register
def _exit():
    if MISSING:
        print(f'answers: {len(MISSING)} answers not in the lock (not seeded)', file=sys.stderr, flush=True)
        try: sys.stdout.flush()
        finally: os._exit(3)


class Ref(dict):
    """A QEMU answer that carries its key and repro. seed: the answer itself; locked: rebuilt from the lock's value
    (`partial`: only the projection's fields; another field read raises Unavailable), or a placeholder
    {'answer': key, 'locked': True} (`locked`) for a hash-only answer, which refuses every other field."""
    def __init__(self, data, key, repro=None, locked=False, partial=False):
        super().__init__({'answer': key, 'locked': True} if locked else data)
        self.key, self.repro, self.locked, self.partial = key, repro or {}, locked, partial

    @classmethod
    def placeholder(cls, key, repro=None): return cls(None, key, repro, locked=True)

    def _no(self, k):
        if self.locked and k not in ('answer', 'locked') or self.partial and not super().__contains__(k):
            raise Unavailable(f'answer {self.key}: field {k!r} of the QEMU answer is not in the lock (locked mode)')

    def __getitem__(self, k): self._no(k); return super().__getitem__(k)
    def get(self, k, default=None): self._no(k); return super().get(k, default)
    def __contains__(self, k): self._no(k); return super().__contains__(k)
    def __bool__(self): return True if self.locked else bool(len(self))
    def __reduce__(self): return (Ref, (None if self.locked else dict(self), self.key, self.repro, self.locked, self.partial))


class Dump(bytes):
    """QEMU system mode's debug-console dump with its key and repro (empty in locked mode)."""
    def __new__(cls, data, key, repro=None, locked=False):
        d = super().__new__(cls, b'' if locked else data)
        d.key, d.repro, d.locked = key, repro or {}, locked
        return d


def key_of(obj): return getattr(obj, 'key', None)


def is_ref(obj): return isinstance(obj, (Ref, Dump))


def _derived(repro, kind, label):
    out = dict(repro)
    out[kind] = [*out.get(kind, []), label]
    return out


# --- the log and the lock ---

def record(key, projection, repro, aux=None):
    """Seed mode: one answer's projection (canonical bytes) under `key`, inline when small."""
    entry = {'key': key, 'sha256': digest(projection), 'repro': repro, 'aux': aux or {}}
    if len(projection) <= VALUE_MAX: entry['value'] = projection.decode()
    line = json.dumps(entry, sort_keys=True, separators=(',', ':'))
    path = os.environ.get('NATEMU_ANSWERS_LOG')
    if not path: raise RuntimeError('NATEMU_ANSWERS=seed needs NATEMU_ANSWERS_LOG (the JSON-lines file the answers go to)')
    with _lock:
        seen = _cache.setdefault(('seen', path), {})
        old = seen.get(key)
        if old is not None and old != entry['sha256']:
            print(f'ANSWER-CONFLICT {key}: seeded twice with different answers in one run', file=sys.stderr, flush=True)
        seen[key] = entry['sha256']
        with open(path, 'a') as f: f.write(line + '\n')


class Entry:
    def __init__(self, sha256, value=None, aux=None, repro=None):
        self.sha256, self.value, self.aux, self.repro = sha256, value, aux or {}, repro or {}


def read_lock(path=None):
    import tomllib
    path = path or os.environ.get('NATEMU_ANSWERS_LOCK')
    if not path: raise RuntimeError('NATEMU_ANSWERS=locked needs NATEMU_ANSWERS_LOCK (the lock with [answers])')
    ck = ('lock', path, os.stat(path).st_mtime_ns)
    with _lock:
        if ck not in _cache:
            with open(path, 'rb') as f: table = tomllib.load(f).get('answers', {})
            _cache[ck] = {k: Entry(v['sha256'], v.get('value'), json.loads(v.get('aux') or '{}'), v.get('repro'))
                          for k, v in table.items()}
        return _cache[ck]


def find(key):
    """Locked mode: the lock's Entry of `key` or None; an inline value is checked against its sha256."""
    e = read_lock().get(key)
    if e is not None and e.value is not None and digest(e.value) != e.sha256:
        raise ValueError(f'lock entry {key}: its value does not hash to its sha256 (a corrupt lock)')
    return e


def lookup(key):
    e = find(key)
    if e is None: raise Missing(key)
    return e


def build_lock(logs):
    """{key: Entry} from seed logs; a key seeded with two answers is a determinism problem (ValueError)."""
    out, bad = {}, []
    for path in logs:
        with open(path) as f:
            for n, line in enumerate(f, 1):
                if not line.strip(): continue
                r = json.loads(line)
                if r['key'] in out and out[r['key']].sha256 != r['sha256']: bad.append(f"{r['key']} ({path}:{n})")
                out.setdefault(r['key'], Entry(r['sha256'], r.get('value'), r.get('aux'), r.get('repro')))
    if bad: raise ValueError(f'{len(bad)} keys seeded with different answers: ' + ', '.join(bad[:20]))
    return out


def _toml(v):
    """A TOML value: JSON strings and arrays of them are valid TOML; tables become inline tables."""
    if isinstance(v, dict): return '{ ' + ', '.join(f'{json.dumps(str(k))} = {_toml(x)}' for k, x in sorted(v.items())) + ' }'
    if isinstance(v, (list, tuple)): return '[' + ', '.join(_toml(x) for x in v) + ']'
    if isinstance(v, bool): return 'true' if v else 'false'
    return json.dumps(v if isinstance(v, (str, int)) else str(v))


def write_lock(entries, path, header=''):
    lines = [header.rstrip(), ''] if header else []
    for k in sorted(entries):
        e = entries[k]
        lines.append(f'[answers.{json.dumps(k)}]')
        lines.append(f'sha256 = "{e.sha256}"')
        if e.value is not None: lines.append(f'value = {json.dumps(e.value)}')
        if e.aux: lines.append(f'aux = {json.dumps(json.dumps(e.aux, sort_keys=True, separators=(",", ":")))}')
        lines.append(f'repro = {_toml(e.repro)}')
        lines.append('')
    with open(path, 'w') as f: f.write('\n'.join(lines))


# --- generic comparison points ---

def equal(obj, label, got, want=None):
    """Whether natemu's `got` equals QEMU's `want` (both JSON-able projections) for the answer `obj` carries, under the
    derived key `<key>/<label>`. `obj` without a key (live mode, or an answer not from QEMU): plain equality."""
    key = key_of(obj)
    if key is None or not active(): return got == want
    k = f'{key}/{label}'
    if mode() == 'seed':
        record(k, canon(want), _derived(obj.repro, 'view', label))
        return canon(got) == canon(want)
    return lookup(k).sha256 == digest(canon(got))


def compared(want, label, project_want, project_got, live):
    """A runner's own comparison point (its own projection, e.g. logs with TSC fields cleared): `live()` gives today's
    differences; `project_want(want)` -> (projection, aux) of QEMU's answer; `project_got(aux)` -> natemu's projection.
    seed: records it under `<key>/<label>` and compares live; locked: a rebuilt answer compares live, a placeholder
    by hash ([] or one difference)."""
    if not isinstance(want, Ref) or not active(): return live()
    k = f'{want.key}/{label}'
    if mode() == 'seed':
        proj, aux = project_want(want)
        record(k, canon(proj), _derived(want.repro, 'view', label), aux)
        return live()
    if not want.locked: return live()
    e = lookup(k)
    return [] if digest(canon(project_got(e.aux))) == e.sha256 else [f'{HASH_ONLY}: {k}']


def view(obj, label, fn):
    """The answer transformed by the runner before a comparison (a mask, a subset, a move): `fn(obj)` with the key
    `<key>/<label>`. `label` must name the transform deterministically (it is part of the key). Locked: from a
    rebuilt answer the view is computed; from a placeholder it is the lock's value of the view, or a placeholder."""
    if not isinstance(obj, Ref): return fn(obj)
    k, repro = f'{obj.key}/{label}', _derived(obj.repro, 'view', label)
    if not obj.locked: return Ref(fn(obj), k, repro, partial=obj.partial)
    return rebuilt(k, repro)


def derive(obj, label, fn):
    """A small value derived from the answer that the runner needs before (or instead of) a comparison: `fn(obj)`, kept in
    the lock's aux under `<key>#<label>` (seed returns it through JSON as locked mode does: tuples become lists)."""
    if not is_ref(obj) or not active(): return fn(obj)
    k = f'{obj.key}#{label}'
    if mode() == 'seed':
        v = json.loads(canon(fn(obj)))
        record(k, canon(v), _derived(obj.repro, 'derived', label), {'value': v})
        return v
    return lookup(k).aux['value']


# --- rebuilding answers from inline values ---

def rebuilt(key, repro=None):
    """Locked: the answer of `key` rebuilt from the lock's inline value, else a placeholder (Missing comes at the
    comparison, so a runner that never compares does not need the key)."""
    e = find(key)
    if e is None or e.value is None: return Ref.placeholder(key, repro or (e.repro if e else None))
    supplier = e.repro.get('supplier')
    v = json.loads(e.value)
    if supplier == 'qemu-user':
        data = {'qemu': {'gpr': [hex(x) for x in v['gpr']], 'rflags': hex(v['rflags']), 'rip': hex(v['rip']),
                         'memory': [{'addr': hex(a), 'data': d} for a, d in v['memory']]},
                'rflags_compare_mask': hex(e.aux['mask'])}
    elif supplier == 'qemu-system':
        data = {'cpus': [{'gpr': c['gpr'], 'rflags': c['rflags'], 'fault': tuple(c['fault']) if c['fault'] is not None else None}
                         for c in v['cpus']],
                'memory': [{'addr': a, 'data': bytes.fromhex(d)} for a, d in v['memory']]}
    else:
        return Ref.placeholder(key, e.repro)
    return Ref(data, key, e.repro, partial=True)


# --- user mode (tools/reference/ref.py) ---

def user_key(p, args):
    """(key, repro) of a normalized ref.py program `p` (ref.norm) run with QEMU arguments `args`; the name is a label."""
    q = {k: v for k, v in p.items() if k != 'name'}
    q['code'] = q['code'].hex(); q['regions'] = [[a, d.hex()] for a, d in q['regions']]
    repro = {'supplier': 'qemu-user', 'pin': pin('user'), 'program_sha256': digest(canon(q)), 'args': list(args)}
    return make_key('qemu-user', repro['pin'], list(args), q), repro


def user_projection(gpr, rflags, rip, memory, mask):
    """What ref.compare compares: 16 GPRs, RFLAGS under the mask, RIP, the regions (address, data) in order
    (the same bytes as testkit/harness/normalize_programs.normalize)."""
    num = lambda v: int(v, 0) if isinstance(v, str) else int(v)
    return json.dumps({'gpr': [num(v) for v in gpr], 'rflags': num(rflags) & mask, 'rip': num(rip),
                       'memory': [[num(m['addr']), m['data']] for m in memory]}, separators=(',', ':')).encode()


def user_record(result):
    """Seed: the projection of a ref.run_programs result (or a view of one)."""
    q, mask = result['qemu'], int(result['rflags_compare_mask'], 0)
    record(result.key, user_projection(q['gpr'], q['rflags'], q['rip'], q['memory'], mask), result.repro, {'mask': mask})


def user_results(keyed, run):
    """ref.run_programs' hook: `keyed` [(key, repro)] of the programs; `run()` runs QEMU (seed only)."""
    if mode() == 'locked':
        return [rebuilt(k, r) for k, r in keyed], {'answers': 'locked', 'cache_hits': 0}
    results, prov = run()
    results = [Ref(x, k, r) for x, (k, r) in zip(results, keyed)]
    for x in results: user_record(x)
    return results, prov


def user_compare(result, gpr, rflags, rip, memory, live):
    """ref.compare's hook: `live()` computes today's field-by-field differences."""
    if not isinstance(result, Ref) or not active(): return live()
    if mode() == 'seed':
        user_record(result)
        return live()
    if not result.locked: return live()
    e = lookup(result.key)
    return [] if digest(user_projection(gpr, rflags, rip, memory, e.aux['mask'])) == e.sha256 else [f'{HASH_ONLY}: {result.key}']


# --- system mode (tools/bench/smp/smp.py) ---

def system_key(args, img):
    """(key, repro): `args` QEMU's arguments without the executable, paths replaced by placeholders; `img` the image."""
    repro = {'supplier': 'qemu-system', 'pin': pin('system'), 'image_sha256': digest(img), 'args': [str(a) for a in args]}
    return make_key('qemu-system', repro['pin'], repro['args'], repro['image_sha256']), repro


def system_run(keyed, run, hang_ok=False):
    """smp.run_image's hook: (rc, err, dump, seconds). seed: runs QEMU (`run()`), records its exit ('exit <rc>', or
    'hang' for a timeout when the expected answer is a hang); locked: the recorded exit and an empty dump."""
    key, repro = keyed
    if mode() == 'locked':
        e = lookup(key + '#exit')
        return e.aux['rc'], '', Dump(b'', key, repro, locked=True), 0.0
    rc, err, dump, took = run()
    if rc == 'timeout' and not hang_ok: return rc, err, Dump(dump, key, repro), took     # a timeout is an answer only for a hang
    record(key + '#exit', HANG.encode() if rc == 'timeout' else f'exit {rc}'.encode(), _derived(repro, 'derived', 'exit'), {'rc': rc})
    return rc, err, Dump(dump, key, repro), took


def system_projection(state, ncpu=None):
    """What smp.compare compares: per CPU (the first `ncpu`) the 16 GPRs, RFLAGS under smp.FLAG_MASK and the fault record;
    the captured regions (address, data) in order."""
    mask = 0x8d5
    cpus = state['cpus'] if ncpu is None else state['cpus'][:ncpu]
    return canon({'cpus': [{'gpr': list(c['gpr']), 'rflags': c['rflags'] & mask, 'fault': c.get('fault')} for c in cpus],
                  'memory': [[m['addr'], bytes(m['data']).hex()] for m in state['memory']]})


def system_record(want):
    record(want.key, system_projection(want), want.repro, {'ncpu': len(want['cpus'])})


def system_parsed(dump, parse):
    """smp.parse_dump's hook: the parsed answer as a Ref (seed: also recorded, so seeding does not depend on natemu)."""
    if mode() == 'locked': return rebuilt(dump.key, dump.repro)
    want = Ref(parse(), dump.key, dump.repro)
    system_record(want)
    return want


def system_compare(got, want, live):
    if not isinstance(want, Ref) or not active(): return live()
    if mode() == 'seed':
        system_record(want)
        return live()
    if not want.locked: return live()
    e = lookup(want.key)
    return [] if digest(system_projection(got, e.aux['ncpu'])) == e.sha256 else [f'{HASH_ONLY}: {want.key}']


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    lk = sub.add_parser('lock', help='write a lock ([answers."<key>"]) from seed logs')
    lk.add_argument('logs', nargs='+')
    lk.add_argument('-o', '--output', required=True)
    lk.add_argument('--merge', help='an existing lock whose answers are kept (the logs must agree with it)')
    a = ap.parse_args(argv)
    entries = build_lock(a.logs)
    if a.merge and os.path.exists(a.merge):
        old = read_lock(a.merge)
        bad = [k for k in entries if k in old and old[k].sha256 != entries[k].sha256]
        if bad: sys.exit(f'{len(bad)} keys differ from {a.merge}: {bad[:10]}')
        entries = {**old, **entries}
    write_lock(entries, a.output, "# QEMU answers: sha256 of each comparison's projection, inline when small (tools/reference/answers.py)")
    inline = sum(e.value is not None for e in entries.values())
    print(f'{len(entries)} answers ({inline} inline) -> {a.output}')


if __name__ == '__main__': main()
