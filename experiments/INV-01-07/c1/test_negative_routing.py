"""Author-side synthetic control routing; no native control execution."""
import unittest

import cells
import core
from test_cells_independent import child_record


class NegativeRouting(unittest.TestCase):
    def test_labeled_backward_control_reaches_actual_raw_oracle(self):
        record = child_record()
        record["mode"] = "backward"
        cell = record["clock_cells"][0]
        cell["readings"] = cell["readings"].changed(1, (1, 0))
        with self.assertRaisesRegex(core.Invalid, "^clock-backward$"):
            cells.validate_child(record)

    def test_labeled_global_control_reaches_actual_global_oracle(self):
        record = child_record()
        record["mode"] = "global-inversion"
        cell = record["contention_cells"][1]
        cell["trace"] = cell["trace"].changed(200000, (200000, 1, 199999))
        with self.assertRaisesRegex(core.Invalid, "^global-trace-backward$"):
            cells.validate_child(record)

    def test_undetected_injection_never_returns_positive_findings(self):
        record = child_record()
        record["mode"] = "backward"
        with self.assertRaisesRegex(core.Invalid, "^injection-not-detected$"):
            cells.validate_child(record)


if __name__ == "__main__":
    unittest.main(verbosity=2)
