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
    found = units.python_test_files()
    files = {f.removeprefix('tools/').removesuffix('.py').replace('/', '.') for f in found if f.startswith('tools/')}
    assert files == mods
    beside = {f for f in found if not f.startswith('tools/')}     # next to the scripts they test (tests/README.md)
    assert beside == {p.relative_to(paths.repo_root()).as_posix() for p in (paths.repo_root() / 'tests').glob('*/test_*.py')}
    assert beside


def test_nextest_rest_of_is_the_complement_of_the_named_filters():
    import pytest
    from types import SimpleNamespace as NS
    a = NS(name='a', params={'tool': 'nextest', 'filter': 'package(x)'})
    b = NS(name='b', params={'tool': 'nextest', 'filter': 'test(/y/)'})
    rest = NS(name='rest', params={'tool': 'nextest', 'rest_of': ['a', 'b']})
    fam = NS(name='f', suites=[a, b, rest])
    assert units.Units.nextest_filter(fam, a) == ['-E', '(package(x))']
    assert units.Units.nextest_filter(fam, rest) == ['-E', 'not (package(x)) & not (test(/y/))']
    assert units.Units.nextest_filter(fam, NS(name='p', params={'tool': 'nextest'})) == []
    with pytest.raises(ValueError, match='rest_of names'):
        units.Units.nextest_filter(fam, NS(name='r', params={'rest_of': ['nope']}))
    with pytest.raises(ValueError, match='widen'):
        units.Units.nextest_filter(fam, NS(name='r', params={'filter': 'x', 'args': ['-E', 'y']}))


def test_the_unit_familys_rust_suites_are_consistent():
    from testkit import family
    f = family.load(paths.repo_root() / 'tests' / 'unit' / 'family.toml')
    for s in f.suites:
        if s.params.get('tool') == 'nextest': units.Units().cases(f, s)

