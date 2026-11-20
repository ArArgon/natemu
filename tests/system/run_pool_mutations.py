#!/usr/bin/env python3
"""Deliberate mutations of the template pool (cross-vCPU reuse plan, chunks 2 and 3): each is applied to the sources, built and
run against the tests that must turn red, then reverted (the sources are restored in a `finally`, and checked clean at the end).

    python3 tools/vmlock.py shared python3 tests/system/run_pool_mutations.py --fexc FEXC --out DIR [--only NAME[,NAME]] [--list]

A mutation of kind `unit` runs `cargo test -p natemu-abi pool` (no VM); one of kind `k1` builds the release host and resident
and runs `tests/system/run_k1.py --cpus 1 2 --programs ...` (VM launches: hold the VM lock as above). The result of each is `red`
(the tests failed, as required), `GREEN` (they passed: the mutation survived, a defect of the tests) or `build failed`; the
exit status is 1 when any is not red. Run the unmutated programs first: a mutation is only meaningful against a green baseline
(`tests/system/run_k1.py --cpus 1 2 --programs pool-peer,pool-frame,pool-smc,pool-ctx,pool-cross,pool-wrap,pool-fence`).

What the tests cannot catch: removing a fence of the ordering protocol (module `natemu_abi::pool`, the argument in its doc
comment: no test shows a missing fence on this hardware), and the slot-position check inside K1 (only the unit tests build
the slot ahead of its data)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
POOL = 'crates/abi/src/pool.rs'
RES = 'crates/runtime/src/execution.rs'
HOST = 'crates/host/src/execution.rs'
ALL = 'pool-peer,pool-frame,pool-smc,pool-ctx,pool-cross,pool-wrap,pool-fence,pool-race'

BYTES = ("""    if !same_bytes(&ram, s.fetch[0] | page_off as u64, &e.prefix[..first])
        || (crosses && !same_bytes(&ram, s.fetch[1], &e.prefix[first..]))
    {""")

# name: (kind, [(file, old, new), ...], programs the K1 run covers / what must go red)
MUTATIONS = {
    'no-bytes-compare': ('k1', [(RES, BYTES, '    if false {')], 'pool-smc,pool-cross'),
    'no-second-frame-compare': ('k1', [(RES, "|| (crosses && !same_bytes(&ram, s.fetch[1], &e.prefix[first..]))", "|| false")], 'pool-cross'),
    'no-context-compare': ('k1', [
        (RES, "let key = Key::new(context, rip, &lead, len);", "let key = Key::new(0, rip, &lead, len);"),
        (RES, "if e.context != context || (e.rip ^ rip)", "if (e.rip ^ rip)"),
        (HOST, "                &crate::publisher::Template {\n                    rip,\n                    context,", "                &crate::publisher::Template {\n                    rip,\n                    context: 0,"),
    ], 'pool-ctx'),
    'no-fence-check': ('k1', [(RES, "    if s.cpu.dr[5] & DR7_ENABLES != 0 {\n        c.pool_refused[", "    if false && s.cpu.dr[5] & DR7_ENABLES != 0 {\n        c.pool_refused[")], 'pool-fence'),
    'no-kept-marked-check': ('k1', [(RES, "b.flags & cache::DEAD != 0 || b.source_gpa.iter().any(|&g| unmarked(g))", "b.flags & cache::DEAD != 0")], 'pool-race'),
    'no-pool-take-invalidations': ('k1', [(RES, "    take_invalidations(s);\n    if s.generation != generation {\n        return Err(why::STALE);", "    if s.generation != generation {\n        return Err(why::STALE);")], 'pool-race,pool-smc'),
    'no-tail-check': ('k1', [(POOL, "        if position < tail {\n            return Err(Refusal::Overwritten);\n        }\n        let entry = decode", "        let _ = tail;\n        let entry = decode")], 'pool-wrap'),
    'no-slot-position-check': ('unit', [(POOL, "        if entry.position != position {\n            return Err(Refusal::Position);\n        }\n        Ok(entry)", "        Ok(entry)")], ''),
    'no-tail-check-unit': ('unit', [(POOL, "        if position < tail {\n            return Err(Refusal::Overwritten);\n        }\n        let entry = decode", "        let _ = tail;\n        let entry = decode")], ''),
    'no-bounds-check': ('unit', [(POOL, "    if end.next_multiple_of(ALIGN) != bytes.len() {\n        return Err(bad(\"parts\"));\n    }", "    let _ = end;")], ''),
    'no-prefix-length-check': ('unit', [(POOL, "    if guest_bytes == 0 || prefix > MAX_PREFIX as u32 || prefix < guest_bytes {", "    if guest_bytes == 0 || prefix > MAX_PREFIX as u32 {")], ''),
    'release-store-before-data': ('unit', [(POOL, "        self.store(place, &scratch[..n]);\n        self.link(&parts.key(), position);", "        self.link(&parts.key(), position);\n        self.store(place, &scratch[..n]);")], ''),
}


# Mutations that must stay green: the removed code is an early-out of a check `kept` makes again after the placement (it takes
# the same posted invalidations and tests the generation and the marks), so no outcome can change.
EQUIVALENT = {'no-pool-take-invalidations': "`kept` takes the same invalidations after the placement"}


def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def run_one(name, a):
    kind, edits, programs = MUTATIONS[name]
    saved = {}
    try:
        for path, old, new in edits:
            text = saved.setdefault(path, (ROOT / path).read_text()) if path not in saved else (ROOT / path).read_text()
            if old not in text:
                return 'pattern not found (the source changed: update this table): ' + path
            (ROOT / path).write_text(text.replace(old, new, 1))
        if kind == 'unit':
            r = sh(['cargo', 'test', '-p', 'natemu-abi', 'pool'])
            return 'red' if r.returncode != 0 else 'GREEN'
        sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference'), str(ROOT / 'tools' / 'bench' / 'smp')]
        import run_slice1_vm as vm
        try:
            host, runtime = vm.build()
        except subprocess.CalledProcessError as e:
            return f'build failed: {e}'
        out = a.out / name
        r = sh([sys.executable, 'tests/system/run_k1.py', '--fexc', str(a.fexc), '--out', str(out), '--cpus', '1', '2',
                '--programs', programs, '--host', str(host), '--runtime', str(runtime)])
        if r.returncode != 0:
            return 'red'
        return 'equivalent (green as documented): ' + EQUIVALENT[name] if name in EQUIVALENT else 'GREEN'
    finally:
        for path, text in saved.items():
            (ROOT / path).write_text(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path)
    ap.add_argument('--out', type=pathlib.Path)
    ap.add_argument('--only', type=lambda s: s.split(','))
    ap.add_argument('--list', action='store_true')
    a = ap.parse_args()
    if a.list:
        for n, (kind, edits, programs) in MUTATIONS.items():
            print(f'{n}: {kind}' + (f' ({programs})' if programs else ''))
        return 0
    if not (a.fexc and a.out):
        ap.error('--fexc and --out are required')
    a.out.mkdir(parents=True, exist_ok=True)
    results = {}
    for name in a.only or MUTATIONS:
        results[name] = run_one(name, a)
        print(f'{name}: {results[name]}', flush=True)
    (a.out / 'mutations.json').write_text(json.dumps(results, indent=1))
    dirty = sh(['git', 'status', '--porcelain', '--', POOL, RES, HOST]).stdout
    if dirty.strip():
        print('sources not restored:\n' + dirty)
        return 1
    return 0 if all(v == 'red' or v.startswith('equivalent') for v in results.values()) else 1


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
