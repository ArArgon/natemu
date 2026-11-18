"""natemu-test: run, submit and inspect test runs, lock answers, prepare and clean the cache.

    tools/natemu-test run [--tier smoke|affected|gate|campaign|bench] [--gate] [pytest args]
    tools/natemu-test submit [same as run]     start a detached run, print its id
    tools/natemu-test status [RUN]             the machine's queue, or one run
    tools/natemu-test wait RUN                 wait for a run, exit with its status
    tools/natemu-test lock FAMILY_DIR          build inputs, seed answers, write tests.lock
    tools/natemu-test check-lock [FAMILY_DIR]  stale or suspicious lock entries
    tools/natemu-test prepare [selection]      build and fetch what a selection needs, run nothing
    tools/natemu-test gc                       clean the cache down to its cap
    tools/natemu-test -m pytest ...            plain pytest (the framework's own tests)
"""
import argparse
import sys

COMMANDS = {
    'run': 'run tests in the foreground',
    'submit': 'start a detached run and print its id',
    'status': "show the machine's queue, or one run",
    'wait': 'wait for a run and exit with its status',
    'lock': 'build inputs, seed answers and write tests.lock',
    'check-lock': 'report stale or suspicious lock entries',
    'prepare': 'build and fetch what a selection needs without running it',
    'gc': 'clean the cache down to its size cap',
}


def main(argv: list[str]) -> int:
    if argv[:2] == ['-m', 'pytest']:
        import pytest
        return pytest.main(argv[2:])
    if argv and argv[0] in COMMANDS:            # everything after the command goes to it (pytest options included)
        from testkit import cli
        return cli.dispatch(argv[0], argv[1:])
    ap = argparse.ArgumentParser(prog='natemu-test', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    for name, help_ in COMMANDS.items():
        p = sub.add_parser(name, help=help_)
        p.add_argument('args', nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    from testkit import cli
    return cli.dispatch(a.command, a.args)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
