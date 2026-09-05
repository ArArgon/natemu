"""Independent check that named_stack conservatively bounds real local buffers.

REV-196 R2 flagged that maximum_owned_buffer_bytes omitted simultaneous local
C buffers (main's/preflight's own struct json, clock_cell's elapsed[200]).
producer.c's preflight() now sums named, sizeof-based local-buffer terms into
`named_stack` and asserts it fits STACK_BUFFER_RESERVE. This test does not
trust that formula: it independently re-derives, from a plain reading of
every large local declaration in producer.c, the true worst-case sum of
locals that could be live in one probe run, using real compiled sizeof()
values (not producer.c's own arithmetic) for the platform ABI, and requires
the declared named_stack expression to dominate it. No producer execution,
no NATEMU_RAW_ROOT use: this only compiles a throwaway sizeof/arithmetic
probe that never opens an external root or launches the reviewed producer.
"""
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

C1 = Path(__file__).resolve().parent
PRODUCER = C1 / "producer.c"


def compiler_available():
    return subprocess.run(["xcrun", "--sdk", "macosx", "clang", "--version"],
                          capture_output=True).returncode == 0


@unittest.skipUnless(compiler_available(), "no Apple clang toolchain on this host")
class ProducerStackAccounting(unittest.TestCase):
    def test_named_stack_formula_dominates_independently_enumerated_locals(self):
        source = PRODUCER.read_text(encoding="utf-8")
        match = re.search(r"uint64_t named_stack = (.*?);\n", source, re.S)
        self.assertIsNotNone(match, "producer.c's named_stack statement moved or was renamed")
        expression = match.group(1)

        # A second, independent enumeration of every named local >= 8 bytes
        # in producer.c, read function by function (not copied from the
        # producer's own comment/formula):
        #   die(): buffer[1024]
        #   clock_failure(): record[256]
        #   array(): digest[CC_SHA256_DIGEST_LENGTH=32] + hex[65] + name[96]
        #   integer128(): text[40]
        #   canonical()'s callers, open_output(): root/repo/parent/resolved/identity[PATH_MAX] (5x)
        #   authorize(): received/expected[PATH_MAX+256] (2x)
        #   clock_cell(): 1x struct quality, temporary[4] uint64, elapsed[BATCHES] uint64
        #   contention_cell(): 1x struct contention, global+workers[8] struct scan (9x),
        #     previous[8]+counts[8]+transitions uint64 (17x)
        #   main(): order[CELLS] unsigned, 2x struct json (main's + preflight's own)
        # Functions are summed as if simultaneous (producer.c's own conservatism,
        # verified separately below), so this need not track the call graph.
        probe_body = (
            'printf("{\\"named_stack\\":%llu,\\"reserve\\":%llu,\\"json\\":%zu,'
            '\\"quality\\":%zu,\\"scan\\":%zu,\\"contention\\":%zu,\\"path_max\\":%d,'
            '\\"batches\\":%d,\\"cells\\":%d}\\n",'
            '(unsigned long long)named_stack, (unsigned long long)STACK_BUFFER_RESERVE,'
            'sizeof(struct json), sizeof(struct quality), sizeof(struct scan),'
            'sizeof(struct contention), (int)PATH_MAX, (int)BATCHES, (int)CELLS);\n'
        )
        probe_source = (
            '#define main producer_main\n'
            f'#include "{PRODUCER}"\n'
            '#undef main\n'
            '#include <stdio.h>\n'
            'int main(void) {\n'
            f'    uint64_t named_stack = {expression};\n'
            f'    {probe_body}'
            '    return 0;\n'
            '}\n'
        )
        with tempfile.TemporaryDirectory(prefix="c1-accounting-probe-") as tmp:
            tmp_dir = Path(tmp)
            probe_path = tmp_dir / "probe.c"
            probe_path.write_text(probe_source, encoding="utf-8")
            binary_path = tmp_dir / "probe"
            sdk = subprocess.run(["xcrun", "--sdk", "macosx", "--show-sdk-path"],
                                capture_output=True, check=True, text=True).stdout.strip()
            compile_result = subprocess.run(
                ["xcrun", "--sdk", "macosx", "clang", "-std=c11", "-O2", "-pthread",
                 "-isysroot", sdk, f'-DC1_REPO="{C1.resolve().parents[3]}"',
                 str(probe_path), "-o", str(binary_path)],
                capture_output=True, text=True)
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            run_result = subprocess.run([str(binary_path)], capture_output=True, text=True,
                                        check=True, timeout=30)
        import json
        reported = json.loads(run_result.stdout)

        path_max, batches, cells = reported["path_max"], reported["batches"], reported["cells"]
        true_sum = (
            2 * reported["json"]                       # main's + preflight's own struct json
            + 2 * (path_max + 256)                      # authorize(): received, expected
            + 5 * path_max                              # open_output(): root/repo/parent/resolved/identity
            + batches * 8                                # clock_cell(): elapsed[BATCHES]
            + 4 * 8                                      # clock_cell(): temporary[4]
            + reported["quality"]                         # clock_cell(): struct quality quality
            + reported["contention"]                     # contention_cell(): struct contention shared
            + 9 * reported["scan"]                        # contention_cell(): global + workers[8]
            + 17 * 8                                      # contention_cell(): previous/counts[8]+transitions
            + 1024                                        # die(): buffer[1024]
            + 256                                         # clock_failure(): record[256]
            + 32 + 65 + 96                                # array(): digest+hex+name
            + 40                                          # integer128(): text[40]
            + cells * 4                                   # main(): order[CELLS] (sizeof(unsigned)==4)
        )
        self.assertGreaterEqual(
            reported["named_stack"], true_sum,
            "producer.c's named_stack under-counts the true worst-case simultaneous "
            f"local-buffer bytes ({reported['named_stack']} < {true_sum})")
        self.assertLessEqual(reported["named_stack"], reported["reserve"],
                             "named_stack exceeds STACK_BUFFER_RESERVE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
