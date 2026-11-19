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
    'measured': ('M', 'MEASURED'),
    'exclusion-changed': ('C', 'EXCLUSION CHANGED'),
    'known-bug': ('k', 'KNOWN BUG'),                    # testkit/known_bugs.py: reported, never a gate failure
    'known-bug-passed': ('K', 'KNOWN BUG PASSED'),
    # campaigns (testkit/harness/campaign.py): a test's own verdict, adjusted by the suite's expected and flaky lists
    'expected-fail': ('x', 'EXPECTED FAIL'),            # listed in `expected` (with its reason) and failed: not a failure
    'flaky': ('~', 'FLAKY'),                            # listed in `flaky`, failed twice (the part was rerun once)
    'unverified': ('?', 'UNVERIFIED'),                  # ran to the end, but nothing decides it is right: never a pass
    'missing': ('m', 'MISSING'),                        # in the image's list, not in the output (crash, hang): fails
    'unexpected-pass': ('X', 'UNEXPECTED PASS'),        # listed in `expected` but passed: fails until the entry goes
    'stale-entry': ('!', 'STALE ENTRY'),                # an `expected`/`flaky` entry naming no test of the image: fails
}
GATE_FAILS = ('failed', 'error', 'stale-lock', 'build-failure', 'unpreparable', 'not-runnable', 'exclusion-changed',
              'missing', 'unexpected-pass', 'stale-entry')


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
