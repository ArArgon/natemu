"""Subcommand implementations; each module they need is imported on use."""


def dispatch(command: str, args: list[str]) -> int:
    handler = globals().get('cmd_' + command.replace('-', '_'))
    if handler is None:
        print(f'natemu-test {command}: not implemented yet')
        return 2
    return handler(args)


def cmd_xref(args):
    from testkit import xref
    xref.main(args)
    return 0


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


def cmd_prepare(args):
    """Build and fetch everything a selection needs (same selection as `run`), run nothing (spec R14.4)."""
    from testkit import runs
    return runs.run(with_default_tier(args) + ['--testkit-prepare-only'])


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


def cmd_clean(args):
    from testkit import clean
    return clean.clean(dry='--dry-run' in args, all_worktrees='--all-worktrees' in args)


def cmd_purge(args):
    from testkit import clean
    return clean.clean(dry='--dry-run' in args, purge=True, all_worktrees='--all-worktrees' in args)


def cmd_times(args):
    from testkit import timings
    return timings.main(args)


def cmd_lock(args):
    """Build each deterministic suite's input, seed its answers, write tests.lock (spec R2.3, R2.8)."""
    import pathlib
    from testkit import family, lock
    if not args: print('natemu-test lock FAMILY_DIR [SUITE ...] | FAMILY_DIR --from-run RUN [RUN ...]'); return 2
    fam = family.load(pathlib.Path(args[0]).resolve() / 'family.toml')
    if args[1:2] == ['--from-run']: return lock_answers(fam, args[2:])
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


def lock_answers(fam, runs) -> int:
    """The family's QEMU answers ([answers] in tests.lock) from seeding runs (TESTKIT_SEED_ANSWERS=1): every case's log
    under <run>/answers. An answer that differs from the locked one is an error, not an update: delete the stale entry."""
    import json, pathlib, sys
    from testkit import lock, paths
    sys.path.insert(0, str(paths.repo_root() / 'tools' / 'reference'))
    import answers
    mine = {c for s in fam.suites for c in s.extra.get('cases', {})}
    logs = []
    for r in runs:
        d = pathlib.Path(r) if '/' in r else paths.runs_dir() / r
        got = sorted(p for p in (d / 'answers').glob('*.jsonl') if p.stem in mine)
        if not got: print(f'{d}: no answer logs for {fam.name} (was it run with TESTKIT_SEED_ANSWERS=1?)'); return 2
        logs += got
    dropped = []
    new = answers.build_lock([str(p) for p in logs], dropped)
    data = lock.read(fam)
    old, sets = data.get('answers', {}), data.get('repro', {})
    bad = [k for k, e in new.items() if k in old and old[k]['sha256'] != e.sha256]
    if bad:
        print(f'{len(bad)} answers differ from the locked ones (delete them from tests.lock to reseed): {bad[:5]}'); return 1
    for k, e in new.items():
        old[k] = {'sha256': e.sha256, **({'value': e.value} if e.value is not None else {}),
                  **({'aux': json.dumps(e.aux, sort_keys=True, separators=(',', ':'))} if e.aux else {}),
                  'repro': answers.share_repro(e.repro, sets)}
    data['answers'], data['repro'] = old, sets
    lock.write(fam, data)
    print(f'{fam.name}: {len(new)} answers from {len(logs)} logs ({sum(e.value is not None for e in new.values())} inline), '
          f'{len(dropped)} provisional ones left out (seeds differ); {len(old)} in the lock')
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
