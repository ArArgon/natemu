"""Self-test of tools/fexc/replay.py's only_moves (32-bit code's one guest-RIP move form) on synthetic replies, with a
non-zero shift: OLD's two-word move against NEW's three-word twin, a cbz over the move. The real streams' self-check
(fexc-replay-order) compares a compiler with itself, where every shift is 0."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import importlib.util, struct, unittest

REPLAY = pathlib.Path(__file__).resolve().parents[2] / 'tools' / 'fexc' / 'replay.py'


def load(source):
    spec = importlib.util.spec_from_loader('replay_under_test', loader=None)
    m = importlib.util.module_from_spec(spec)
    exec(compile(source, str(REPLAY), 'exec'), m.__dict__)
    return m


r = load(REPLAY.read_text())
NONE32, VALUE = 0xffffffff, 0xf7c4_5346
MOVZ, MOVK = 0x52800000 | (VALUE & 0xffff) << 5 | 5, 0x72a00000 | (VALUE >> 16) << 5 | 5   # w5
ADD = 0x91000421                                                                            # add x1, x1, #1


def cbz(at, to):
    return 0x34000001 | ((to - at) // 4 & 0x7ffff) << 5                                     # cbz w1, to


def reply(words, move_at, tail_at):
    """A --serve reply whose code is `words` (word 0 the offset of FEX's tail, set here), the tail at `tail_at` with one
    RIP entry (host +4, guest +1), one guest-RIP move of w5 at `move_at`, one PC entry at 4."""
    code = [tail_at] + words[1:]
    code += [0] * (tail_at // 4 - len(code))
    body = bytearray(b''.join(struct.pack('<I', w) for w in code))
    tail = struct.pack('<QQQIIIB', 0, VALUE - 6, 6, 1, 40, 0, 0).ljust(40, b'\0') + b'\x00'
    body += tail.ljust(48, b'\0')
    struct.pack_into('<Q', body, tail_at, len(body))
    pcs = struct.pack('<IIB3x', 4, 0, 0)
    relocs = struct.pack('<IBBBBQ', move_at, 3, 5, 0, 0, VALUE)
    h = bytearray(128)
    h[:8] = r.MAGIC
    end = max(i for i, w in enumerate(code) if w) * 4 + 4
    for o, v in ((40, 128), (44, len(body)), (48, 4), (52, NONE32), (56, end), (64, 128 + len(body)), (68, 0),
                 (72, 128 + len(body)), (76, 1), (80, 128 + len(body) + 12), (84, 1)):
        struct.pack_into('<I', h, o, v)
    struct.pack_into('<I', h, 16, 128 + len(body) + len(pcs) + len(relocs))
    obj = bytes(h) + bytes(body) + pcs + relocs
    json = b'{"status":"ok"}'
    return struct.pack('<iIIII', 0, len(json), len(obj), 6, 0) + json + obj


# OLD: cbz over the two-word move to the add at 16; NEW: the move three words, the add at 20.
OLD = reply([0, cbz(4, 16), MOVZ, MOVK, ADD], 8, 24)
NEW = reply([0, cbz(4, 20), MOVZ, MOVK, r.NOP, ADD], 8, 24)
STALE = reply([0, cbz(4, 16), MOVZ, MOVK, r.NOP, ADD], 8, 24)   # NEW with OLD's cbz word: its target is the nop


class OnlyMoves(unittest.TestCase):
    def test_the_twin_differs_only_by_its_move(self):
        self.assertIsNone(r.only_moves(OLD, NEW))
        self.assertIsNone(r.only_moves(NEW, NEW))

    def test_a_branch_over_a_move_must_move_its_target(self):
        self.assertIn('the code differs at +0x4', r.only_moves(OLD, STALE))
        # The rule it replaced (a PC-relative word checked only when its words differ) passes it.
        old_rule = REPLAY.read_text().replace('        if wa != wb or pa_ is not None:\n', '        if wa != wb:\n')
        self.assertNotEqual(old_rule, REPLAY.read_text())
        self.assertIsNone(load(old_rule).only_moves(OLD, STALE))

    def test_other_changes_are_reported(self):
        unpadded = reply([0, cbz(4, 20), MOVZ, MOVK, 0xaa0003e0, ADD], 8, 24)    # mov x0, x0 for the nop
        other = reply([0, cbz(4, 20), MOVZ, MOVK ^ 1 << 5, r.NOP, ADD], 8, 24)  # another value
        word = reply([0, cbz(4, 20), MOVZ, MOVK, r.NOP, ADD ^ 1 << 10], 8, 24)  # add #2
        for bad in (unpadded, other, word):
            self.assertIsNotNone(r.only_moves(OLD, bad))
        for k, m in enumerate(r.mutations(NEW)):
            self.assertIsNotNone(r.only_moves(OLD, m), k)


if __name__ == '__main__':
    unittest.main()
