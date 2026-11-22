"""The judge of the INIT and SIPI cases (tests/system/run_initsipi.py): what natemu's run of a scenario of
tools/models/initsipi_model.py may show, from the model's `outcomes` (the architectural reference's).

Both sides are projected onto what a run shows (`project`, `observe`): how the run ended (the host's end, or a typed stop
at one CPU), and per CPU how it ended (`halted`, `wait-for-sipi`, `mailbox`, `done`) and the starts it made (each SIPI
with its vector, a BSP's reset), in order. Two regimes:

- **entry** (`NATEMU_INTERPRETER=off`: the boot-time interpreter stops at its entry, the stub): a started CPU stops at
  its start's first boundary, and the runner's `--entry-ends` makes that stop the CPU's own end. The reference is the
  model's outcomes of `entry_scenario`: every interpreted program (a CPU's `start` and `reset`) is one instruction after
  which the CPU is done, and the awaits are dropped (no AP signals). An entry stop with an NMI pending is the model's
  `nmi` stop there (the interpreter's first boundary takes it before anything); one with an INIT pending is open (the
  interpreter's first boundary would take that INIT, which the stub cannot show): any outcome of that CPU matches.
- **interpreter** (the interpreter runs the start programs): the model's own outcomes; a start counts when it reached
  the program's `signal` (the stub's arrival on the shared counter), the only part of a start a run shows.

A CPU the run cancelled (another CPU's stop ended it) matches any outcome of that CPU."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
from models import initsipi_model as m

ANY = 'any'                                            # a CPU whose end the run does not show (cancelled, or open)


def entry_scenario(sc):
    """`sc` as natemu runs it in the entry regime (module doc)."""
    cpus = []
    for c in sc['cpus']:
        c = dict(c, start=[('op',)], reset=[('op',)])
        c['boot'] = [i for i in c.get('boot', []) if i[0] != 'await']
        cpus.append(c)
    return dict(cpus=cpus)


def reference(name, regime):
    """The reference's outcomes of a scenario in a regime."""
    sc = m.SCENARIOS[name]
    return m.explore(entry_scenario(sc) if regime == 'entry' else sc, spec=True)['outcomes']


def _starts(sc, i, runs, regime):
    """The starts a run shows of CPU i's runs (the model's: (label, instructions), runs of none already dropped)."""
    out = []
    for label, k in runs:
        if label == 'boot':
            continue
        if regime == 'interpreter' and label != 'reset':
            prog = [x[0] for x in sc['cpus'][i].get('start', [])]
            if 'signal' not in prog or k <= prog.index('signal'):
                continue                               # stopped by an INIT before it signalled: not seen
        out.append(label)
    return tuple(out)


def project(name, outcome, regime):
    """A model outcome as a run shows it: ('end', ((label, starts), ...)), ('stop', cpu, reason) or ('stuck',)."""
    sc = m.SCENARIOS[name]
    if outcome[0] == 'end':
        return 'end', tuple((label, _starts(sc, i, runs, regime)) for i, (label, runs, _, _) in enumerate(outcome[1]))
    if outcome[0] == 'stop':
        return 'stop', outcome[1], outcome[2]
    return ('stuck',)


def matches(observed, projected):
    """An observation (as `project` gives, with ANY for a CPU it does not show) is this projected outcome."""
    if observed[0] != projected[0]:
        return False
    if observed[0] == 'end':
        return len(observed[1]) == len(projected[1]) and all(o == ANY or o == p for o, p in zip(observed[1], projected[1]))
    return observed == projected


def judge(name, observed, regime):
    """[] when the observation is one the reference permits for the scenario, else the differences."""
    allowed = {project(name, o, regime) for o in reference(name, regime)}
    if any(matches(observed, p) for p in allowed):
        return []
    return [f'{name}: observed {observed!r}, the reference permits only {sorted(allowed, key=repr)!r}']
