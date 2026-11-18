"""Result categories beyond pass/fail/skip, and the gate verdict (spec R5.8)."""

# category -> (short letter, word shown with -v)
CATEGORIES = {
    'excluded': ('-', 'EXCLUDED'),
    'not-runnable': ('N', 'NOT RUNNABLE'),
    'stale-lock': ('L', 'STALE LOCK'),
    'build-failure': ('B', 'BUILD FAILURE'),
    'unpreparable': ('U', 'UNPREPARABLE'),
    'reused': ('R', 'REUSED'),
    'unsupported': ('u', 'UNSUPPORTED'),
    'wrapped': ('W', 'WRAPPED'),
    'exclusion-changed': ('C', 'EXCLUSION CHANGED'),
}
GATE_FAILS = ('failed', 'error', 'stale-lock', 'build-failure', 'unpreparable', 'not-runnable', 'exclusion-changed')


def category(report) -> str | None:
    return dict(report.user_properties).get('testkit')


def teststatus(report):
    c = category(report)
    if c is None: return None
    if c == 'wrapped' and report.failed: return None        # a failing wrapped step is a failure like any other
    if report.when == 'call' or (report.when == 'setup' and report.skipped):
        letter, word = CATEGORIES[c]
        return c, letter, word
    return None


def gate_verdict(stats: dict) -> tuple[bool, list[str]]:
    bad = [f'{len(stats[k])} {k}' for k in GATE_FAILS if stats.get(k)]
    return not bad, bad
