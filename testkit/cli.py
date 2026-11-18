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
