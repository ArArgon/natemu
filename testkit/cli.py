"""Subcommand implementations; each module they need is imported on use."""


def dispatch(command: str, args: list[str]) -> int:
    handler = globals().get('cmd_' + command.replace('-', '_'))
    if handler is None:
        print(f'natemu-test {command}: not implemented yet')
        return 2
    return handler(args)


def cmd_run(args):
    from testkit import runs
    rid = None
    if args[:1] == ['--run-id']: rid, args = args[1], args[2:]
    return runs.run(with_default_tier(args), rid)


def with_default_tier(args):
    """`run` without --tier, --gate or a path selects the affected tier (spec R6.1)."""
    import os
    if any(a.startswith('--tier') or a == '--gate' for a in args): return args
    if any(not a.startswith('-') and os.path.exists(a.split('::')[0]) for a in args): return args
    return ['--tier', 'affected', *args]


def cmd_submit(args):
    from testkit import runs
    print(runs.submit(with_default_tier(args)))
    return 0


def cmd_status(args):
    from testkit import runs
    print(runs.status(args[0] if args else None))
    return 0


def cmd_wait(args):
    from testkit import runs
    if not args: print('natemu-test wait RUN'); return 2
    return runs.wait(args[0])


def cmd_gc(args):
    from testkit import cache, runs
    import pathlib
    from testkit import paths
    import json
    roots = set()
    for f in (paths.state_dir() / 'used').glob('*'):
        rec = json.loads(f.read_text())
        if pathlib.Path(rec['worktree']).exists(): roots |= {pathlib.Path(p) for p in rec['paths']}
    deleted = cache.gc(cache.cap_bytes(), roots)
    print(f'deleted {len(deleted)} entries')
    return 0


def cmd_lock(args):
    """Build each deterministic suite's input, seed its answers, write tests.lock (spec R2.3, R2.8)."""
    import pathlib
    from testkit import family, lock
    if not args: print('natemu-test lock FAMILY_DIR [SUITE ...]'); return 2
    fam = family.load(pathlib.Path(args[0]).resolve() / 'family.toml')
    want = set(args[1:])
    data = lock.read(fam)
    for suite in fam.suites:
        if suite.kind != 'deterministic' or (want and suite.name not in want): continue
        h = family.harness_for(fam, suite)
        print(f'seeding {fam.name}/{suite.name} ...', flush=True)
        data.setdefault('suite', {})[suite.name] = h.seed(fam, suite)
        data.setdefault('input', {})[suite.input] = {'fingerprint': lock.fingerprint_input(fam, suite.input),
                                                    'output': data['suite'][suite.name]['input']}
        s = data['suite'][suite.name]
        print(f'  {len(s["cases"])} cases, {sum(1 for a in s["cases"].values() if not a)} without an answer, '
              f'{sum(len(v) for v in s.get("excluded", {}).values())} exclusions')
    lock.write(fam, data)
    return 0


def cmd_check_lock(args):
    import pathlib
    from testkit import family, lock
    fams = [family.load(pathlib.Path(a).resolve() / 'family.toml') for a in args] if args else family.families()
    probs = []
    for fam in fams:
        for s in fam.suites:
            h = family.harness_for(fam, s)
            if s.kind == 'deterministic' and hasattr(h, 'lock_status'):
                from testkit.harness import programs
                probs += lock.check(fam, lambda suite: (programs.sources(), programs.qemu_pin()))
                break
    for p in probs: print(p)
    return 1 if any('stale' in p or 'missing' in p or 'collapsed' in p or 'seeded by' in p for p in probs) else 0
