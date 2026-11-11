#!/usr/bin/env python3
"""PIC study stage 2: the page-base slot (natemu_abi frame::PAGE_BASE) has exactly the design's two writers: the resident's
entry into a position-independent block (`enter`, one store) and the block's own full entry (`pic::STORE_BASE`, which only
`pic::rewrite` puts in code and `pic::proved` requires as the one store it adds)."""
import pathlib, re, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class SlotWriters(unittest.TestCase):
    def test_resident_writes_the_slot_once_in_enter(self):
        writes = []
        for f in sorted((ROOT / 'crates/runtime/src').rglob('*.rs')):
            text = f.read_text()
            for m in re.finditer(r'PAGE_BASE as usize\)\s*\.(\w+)\(', text):
                if m.group(1).startswith('write'):
                    fn = re.findall(r'\nunsafe fn (\w+)|\nfn (\w+)', text[:m.start()])[-1]
                    writes.append((f.name, ''.join(fn)))
        self.assertEqual(writes, [('execution.rs', 'enter')])

    def test_store_base_only_from_the_rewrite(self):
        code = lambda f: '\n'.join(l for l in f.read_text().splitlines() if not l.strip().startswith('//'))
        users = {f.relative_to(ROOT).as_posix() for f in (ROOT / 'crates').rglob('*.rs') if 'STORE_BASE' in code(f)}
        self.assertEqual(users, {'crates/abi/src/pic.rs'})
        pic = code(ROOT / 'crates/abi/src/pic.rs')
        body = pic[:pic.index('#[cfg(test)]')]
        uses = [m.start() for m in re.finditer(r'STORE_BASE', body)]
        fns = {re.findall(r'\npub (?:const )?fn (\w+)|\npub const (\w+)', body[:u + 10])[-1] for u in uses}
        self.assertEqual({''.join(f) for f in fns}, {'STORE_BASE', 'rewrite', 'proved'})


if __name__ == '__main__':
    unittest.main()
