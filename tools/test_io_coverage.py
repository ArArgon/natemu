"""The block coverage judges' self-checks (no VM): every case's synthetic pass, and the mutations each judge must reject."""
import contextlib
import io
import unittest

import run_io_coverage
import run_virtio_lifecycle


class JudgeControls(unittest.TestCase):
    def test_linux_coverage_judge(self):
        with contextlib.redirect_stdout(io.StringIO()): run_io_coverage.self_check()

    def test_lifecycle_judge(self):
        with contextlib.redirect_stdout(io.StringIO()): run_virtio_lifecycle.self_check()


if __name__ == '__main__': unittest.main()
