#!/usr/bin/env python3
"""
bisect.py -- find the first differing instruction between two emulator runs,
given the checkpoint files written by plugin_state.so.

The whole divergence-bisection idea in ~50 lines:

  1. Both sides emit (icount, rolling_hash) every 2^k instructions.  A billion
     instructions at k=16 is 16 k lines, ~500 kB -- nothing.
  2. Compare the two checkpoint lists.  The last icount where the hashes agree
     and the first where they differ bound a 2^k-instruction window.
  3. Re-run both sides with wlo/whi set to that window.  The plugin then dumps
     full per-instruction state for those 65 536 instructions only.
  4. Diff the two window dumps line by line -> the exact first differing
     instruction, with both architectural states printed.

Step 3 is the only step that needs the run to be *reproducible*.  That is the
entire reason docs/research/25 puts deterministic replay at the top of the
debuggability ranking: without it, this loop does not close.

Usage:
  bisect.py ckpt A.ckpt B.ckpt              # stage 1+2: locate the window
  bisect.py win  A.win  B.win               # stage 4: first differing insn
"""
import sys


def load_ckpt(path):
    out = []
    with open(path) as f:
        for line in f:
            a, b = line.split()
            out.append((int(a), b))
    return out


def stage_window(pa, pb):
    A, B = load_ckpt(pa), load_ckpt(pb)
    da = dict(A)
    common = [ic for ic, _ in B if ic in da]
    if not common:
        print("no common checkpoint icounts -- runs share no instruction grid")
        return 2
    db = dict(B)
    agree, first_bad = 0, None
    for ic in sorted(common):
        if da[ic] == db[ic]:
            agree = ic
        else:
            first_bad = ic
            break
    print("A: %d checkpoints, %d insns total" % (len(A), A[-1][0]))
    print("B: %d checkpoints, %d insns total" % (len(B), B[-1][0]))
    if first_bad is None:
        if A[-1] == B[-1]:
            print("IDENTICAL: same final icount and same final hash")
            return 0
        print("checkpoints agree up to icount %d; runs differ only in length "
              "(A=%d B=%d)" % (agree, A[-1][0], B[-1][0]))
        return 1
    print("last agreeing checkpoint : %d" % agree)
    print("first differing checkpoint: %d" % first_bad)
    print("window                    : [%d, %d)  (%d instructions)"
          % (agree, first_bad, first_bad - agree))
    print("\nre-run both sides with:")
    print("  ...,wlo=%d,whi=%d,wout=/tmp/X.win" % (agree, first_bad))
    print("then: bisect.py win A.win B.win")
    return 1


def stage_first_insn(pa, pb):
    with open(pa) as fa, open(pb) as fb:
        for la, lb in zip(fa, fb):
            if la != lb:
                fa_, fb_ = la.split(), lb.split()
                print("FIRST DIVERGING INSTRUCTION at icount %s" % fa_[0])
                print("  A pc=%s" % fa_[1])
                print("  B pc=%s" % fb_[1])
                ka = dict(x.split("=", 1) for x in fa_[2:])
                kb = dict(x.split("=", 1) for x in fb_[2:])
                for k in ka:
                    if ka[k] != kb.get(k):
                        print("  %-7s A=%-18s B=%-18s" % (k, ka[k], kb.get(k)))
                return 1
    print("window dumps identical")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("ckpt", "win"):
        print(__doc__)
        sys.exit(2)
    sys.exit(stage_window(sys.argv[2], sys.argv[3]) if sys.argv[1] == "ckpt"
             else stage_first_insn(sys.argv[2], sys.argv[3]))
