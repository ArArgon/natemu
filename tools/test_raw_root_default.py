"""No tool computes its own `<checkout>/../natemu-raw` default: they use raw_root.py (tools/with-env for shell), which also
finds the main checkout's sibling from inside a worktree."""
import pathlib, re, unittest

TOOLS = pathlib.Path(__file__).resolve().parent
ALLOWED = {'raw_root.py', 'test_raw_root_default.py', 'test_check_setup.py', 'check_setup.py'}   # check_setup: a message only
OLD = re.compile(r"/ ['\"]natemu-raw['\"]|\.\./natemu-raw\}|\$\{?\w+\}?/\.\./natemu-raw\b(?!/)")


class RawRootDefault(unittest.TestCase):
    def test_no_hardcoded_default(self):
        bad = [f'{p.relative_to(TOOLS)}:{n}' for ext in ('*.py', '*.sh') for p in TOOLS.rglob(ext) if p.name not in ALLOWED
               for n, line in enumerate(p.read_text(errors='replace').splitlines(), 1) if OLD.search(line)]
        self.assertEqual(bad, [])


if __name__ == '__main__': unittest.main()
