import unittest
import sys

from testkit import paths
from testkit.harness import units

LIBTEST = '\n'.join([
    '{"type":"suite","event":"started","test_count":3}',
    '{"type":"test","event":"ok","name":"natemu-abi::natemu_abi$clock::tests::monotonic","exec_time":0.01}',
    '{"type":"test","event":"failed","name":"natemu-abi::natemu_abi$x::y","stdout":"boom"}',
    '{"type":"test","event":"ignored","name":"natemu-abi::natemu_abi$x::z"}',
    'not json'])


def test_nextest_libtest_json_parse():
    subs = units.parse_libtest_json(LIBTEST)
    assert [(s.name, s.outcome) for s in subs] == [('natemu-abi::natemu_abi::clock::tests::monotonic', 'passed'),
                                                  ('natemu-abi::natemu_abi::x::y', 'failed'),
                                                  ('natemu-abi::natemu_abi::x::z', 'skipped')]
    assert subs[1].longrepr == 'boom'


def test_python_cases_match_unittest_discover():
    tools = str(paths.repo_root() / 'tools')
    sys.path.insert(0, tools)
    try:
        suite = unittest.TestLoader().discover(tools, 'test_*.py')
    finally:
        sys.path.remove(tools)

    def walk(s):
        for t in s:
            yield from walk(t) if isinstance(t, unittest.TestSuite) else [t]
    mods = {type(t).__module__ for t in walk(suite)}
    files = {f.removeprefix('tools/').removesuffix('.py').replace('/', '.') for f in units.python_test_files()}
    assert files == mods
