#!/usr/bin/env python3
"""Synthetic reporting tests only: no host probe and no research evidence."""
import contextlib
import copy
from fractions import Fraction
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import check
import histogram as h


def valid():
    return json.loads((ROOT / "tests/valid.json").read_text())


def arm(data, test="MP", variant="plain-ldr-str", qos="background"):
    return next(r for r in data["litmus"] if (r["test"], r["variant"], r["qos"])
                == (test, variant, qos))


def counts(row, n, both, exposed=0):
    row["iterations"] = n
    row["relaxed_observed"] = exposed
    row["histogram"] = {key: 0 for key in h.CELLS}
    row["histogram"]["r0!=0,r1!=0"] = both
    row["histogram"]["r0=0,r1=0" if row["test"] == "SB" else "r0!=0,r1=0"] = exposed
    row["histogram"]["r0=0,r1!=0"] = n - both - exposed


def comparison(runs, key=("MP", "ldar-stlr", "background")):
    return next(r for r in h.comparisons(runs) if r["key"] == key)


class Reporting(unittest.TestCase):
    def cli(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = check.cli(["--historical"] + args)
        return rc, output.getvalue()

    def test_exact_boundaries_and_symmetry(self):
        cases = [(Fraction(1), "COMPARABLE"), (Fraction(333, 100), "COMPARABLE"),
                 (Fraction(10, 3) - Fraction(1, 10**20), "COMPARABLE"),
                 (Fraction(10, 3), "INDETERMINATE"),
                 (Fraction(10, 3) + Fraction(1, 10**20), "INDETERMINATE"),
                 (Fraction(30), "INDETERMINATE"),
                 (Fraction(30) + Fraction(1, 10**20), "INCONCLUSIVE")]
        for ratio, expected in cases:
            for fp, fs in ((Fraction(1), 1 / ratio), (1 / ratio, Fraction(1))):
                with self.subTest(ratio=ratio, fp=fp):
                    self.assertEqual(h.classify(fp, fs), (expected, ratio))
        self.assertEqual(h.classify(Fraction(0), Fraction(0)),
                         ("INCONCLUSIVE", "undefined (both zero)"))
        for fp, fs in ((0, 1), (1, 0)):
            self.assertEqual(h.classify(Fraction(fp), Fraction(fs)),
                             ("INCONCLUSIVE", "unbounded"))

    def test_count_weighted_pooling_not_mean_fractions(self):
        a, b = valid(), valid()
        counts(arm(a), 1_000_000, 100_000, 1)
        counts(arm(b), 3_000_000, 2_700_000, 2)
        counts(arm(a, variant="ldar-stlr"), 2_000_000, 200_000)
        counts(arm(b, variant="ldar-stlr"), 1_000_000, 900_000)
        row = comparison([a, b])
        self.assertEqual(row["plain"], (2_800_000, 4_000_000))
        self.assertEqual(row["strengthened"], (1_100_000, 3_000_000))
        self.assertEqual(row["ratio"], Fraction(21, 11))
        self.assertNotEqual(row["ratio"], 1)  # means of both arm fractions are .5
        self.assertEqual(row["plain_exposure"], [1, 2])
        self.assertTrue(row["eligible"])

    def test_eligibility_requires_matching_plain_and_zero_strengthened(self):
        d = valid()
        for qos in ("background", "user-interactive"):
            counts(arm(d, qos=qos), 1_000_000, 100_000, int(qos == "background"))
            counts(arm(d, variant="ldar-stlr", qos=qos), 1_000_000, 100_000)
        self.assertTrue(comparison([d])["eligible"])
        row = comparison([d], ("MP", "ldar-stlr", "user-interactive"))
        self.assertEqual(row["verdict"], "COMPARABLE")
        self.assertFalse(row["eligible"])
        counts(arm(d, variant="ldar-stlr"), 1_000_000, 100_000, 1)
        self.assertFalse(comparison([d])["eligible"])
        counts(arm(d, variant="ldar-stlr"), 1_000_000, 3000)
        self.assertFalse(comparison([d])["eligible"])

    def test_histogram_input_rejections(self):
        mutations = [lambda r: r["histogram"].pop("r0=0,r1=0"),
                     lambda r: r["histogram"].update(extra=0),
                     lambda r: r["histogram"].update({"r0=0,r1=0": -1}),
                     lambda r: r["histogram"].update({"r0=0,r1=0": True}),
                     lambda r: r.update(iterations=r["iterations"] + 1),
                     lambda r: r.update(relaxed_observed=True),
                     lambda r: r.update(relaxed_observed=r["relaxed_observed"] + 1)]
        for mutate in mutations:
            d = valid()
            mutate(d["litmus"][0])
            with self.assertRaises(ValueError):
                h.validate_histograms(d)

    def test_report_zero_boundary_and_matching_run_set(self):
        a, b = valid(), valid()
        counts(arm(a), 1_000_000, 3, 0)
        counts(arm(a, variant="ldar-stlr"), 1_000_000, 10)
        row = comparison([a])
        self.assertEqual(row["ratio"], Fraction(10, 3))
        self.assertEqual(row["verdict"], "INDETERMINATE")
        self.assertFalse(row["eligible"])
        counts(arm(a), 1_000_000, 0, 1)
        counts(arm(a, variant="ldar-stlr"), 1_000_000, 0)
        self.assertEqual(comparison([a])["ratio"], "undefined (both zero)")
        self.assertFalse(comparison([a])["eligible"])
        counts(arm(a), 1_000_000, 100, 0)
        counts(arm(a, variant="ldar-stlr"), 1_000_000, 100)
        counts(arm(b), 1_000_000, 100, 2)
        counts(arm(b, variant="ldar-stlr"), 1_000_000, 100)
        self.assertFalse(comparison([a])["eligible"])
        self.assertTrue(comparison([a, b])["eligible"])
        self.assertEqual(comparison([a, b])["plain_exposure"], [0, 2])

    def test_legacy_fixtures_and_suppression(self):
        self.assertEqual(self.cli([str(ROOT / "tests/valid.json")])[0], 0)
        for fixture in ("negative", "forbidden", "negative-coverage"):
            rc, output = self.cli([str(ROOT / f"tests/{fixture}.json")])
            self.assertEqual(rc, 1)
            self.assertNotIn("corroboration=", output)
            self.assertNotIn("  MEET  T", output)
            self.assertNotIn("  MISS  T", output)
        d = valid()
        d["litmus"][0]["histogram"]["r0=0,r1!=0"] += 1
        with tempfile.TemporaryDirectory(prefix="inv08-h-fixture-") as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(d))
            rc, output = self.cli([str(path)])
        self.assertEqual(rc, 1)
        self.assertIn("H-input", output)
        self.assertNotIn("corroboration=", output)
        self.assertNotIn("  MEET  T", output)

    def test_i2_failure_suppresses_otherwise_eligible_mp(self):
        d = valid()
        for qos in ("background", "user-interactive"):
            counts(arm(d, test="SB", qos=qos), 1_000_000, 100, 0)
        counts(arm(d), 1_000_000, 100, 1)
        counts(arm(d, variant="ldar-stlr"), 1_000_000, 100)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = check.main("synthetic", data=d, historical=True)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  I2", output.getvalue())
        self.assertNotIn("corroboration=", output.getvalue())
        self.assertNotIn("  MEET  T", output.getvalue())

    def test_late_malformed_field_cannot_leak_prior_claims(self):
        d = valid()
        d["fallback_global_lock"]["ns_per_op"] = "malformed"
        with patch.object(check, "json_read", return_value=d):
            rc, output = self.cli([str(ROOT / "tests/valid.json")])
        self.assertEqual(rc, 1)
        self.assertIn("TypeError", output)
        self.assertNotIn("corroboration=", output)
        self.assertNotIn("  H ", output)
        self.assertNotIn("  MEET  ", output)
        self.assertNotIn("  MISS  ", output)

    def test_late_single_rendering_failure_cannot_leak_claims(self):
        original = check.report

        def broken(runs):
            original(runs)
            raise RuntimeError("partial report: corroboration=ELIGIBLE  MEET T1")

        with patch.object(check, "report", side_effect=broken):
            rc, output = self.cli([str(ROOT / "tests/valid.json")])
        self.assertEqual(rc, 1)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("corroboration=", output)
        self.assertNotIn("MEET", output)
        self.assertNotIn("MISS", output)


class Cohorts(unittest.TestCase):
    cli = Reporting.cli

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="inv08-cohort-fixture-")
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        context = {field: "synthetic-not-evidence" for field in h.CONTEXT_FIELDS}
        self.cohort = {"id": "synthetic-only", "compatibility_justification": "fixture, not evidence",
                       "runs": []}
        for i in range(2):
            data = valid()
            data["mode"] = "normal"  # synthetic schema exercise, never an evidence run
            counts(arm(data), (1 + 2 * i) * 1_000_000, (100_000, 2_700_000)[i], i + 1)
            counts(arm(data, variant="ldar-stlr"), (2 - i) * 1_000_000,
                   (200_000, 900_000)[i])
            member = {"path": f"run{i}.json", "manifest": f"MAN-01-08-{i + 1:02d}",
                      "context": copy.deepcopy(context)}
            self.cohort["runs"].append(member)
            self.write_member(i, data)

    def write_member(self, i, data):
        member = self.cohort["runs"][i]
        raw = json.dumps(data).encode()
        (self.base / member["path"]).write_bytes(raw)
        member["sha256"] = hashlib.sha256(raw).hexdigest()

    def prepare(self, review_transform=lambda s: s):
        review = ("Synthetic parser fixture; not an actual review or research evidence.\n"
                  "- Review ID: `REV-000`\n- Independence: `independent`\n- Verdict: `accept`\n"
                  f"- Cohort SHA-256: `{h.cohort_digest(self.cohort)}`\n")
        raw = review_transform(review).encode()
        (self.base / "review.md").write_bytes(raw)
        self.envelope = {"schema": "INV-01-08-cohort-v1", "cohort": self.cohort,
                         "review": {"path": "review.md", "sha256": hashlib.sha256(raw).hexdigest()}}
        return self.write_envelope()

    def write_envelope(self):
        path = self.base / "cohort.json"
        path.write_text(json.dumps(self.envelope))
        return path

    def rejected(self, path):
        rc, output = self.cli(["--cohort", str(path)])
        self.assertEqual(rc, 1, output)
        self.assertNotIn("corroboration=", output)
        self.assertNotIn("  MEET  T", output)
        self.assertNotIn("  MISS  T", output)

    def test_success_exact_weighting_no_pooled_timing(self):
        rc, output = self.cli(["--cohort", str(self.prepare())])
        self.assertEqual(rc, 0, output)
        self.assertIn("ratio=21/11", output)
        self.assertIn("plain=2800000/4000000 strengthened=1100000/3000000", output)
        self.assertEqual(output.count("corroboration="), 8)
        self.assertNotIn("  MEET  T", output)
        self.assertNotIn("  MISS  T", output)

    def test_late_pooled_rendering_failure_cannot_leak_claims(self):
        path = self.prepare()
        original = check.report

        def broken(runs):
            original(runs)
            raise OverflowError("partial report: corroboration=ELIGIBLE  MEET T1")

        with patch.object(check, "report", side_effect=broken):
            rc, output = self.cli(["--cohort", str(path)])
        self.assertEqual(rc, 1)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn("OverflowError", output)
        self.assertNotIn("corroboration=", output)
        self.assertNotIn("MEET", output)
        self.assertNotIn("MISS", output)
        self.assertNotIn("member=", output)

    def test_each_context_dimension_rejects_mixing(self):
        context = self.cohort["runs"][1]["context"]
        for field in h.CONTEXT_FIELDS:
            context[field] = "different"
            self.rejected(self.prepare())
            context[field] = "synthetic-not-evidence"
        context.pop("sdk")
        self.rejected(self.prepare())

    def test_review_binding_and_uniqueness(self):
        for transform in (lambda s: s.replace("`accept`", "`revise`"),
                          lambda s: s.replace("`independent`", "`self`"),
                          lambda s: s + "- Verdict: `revise`\n",
                          lambda s: s.replace("Cohort SHA-256:", "Unbound SHA-256:")):
            self.rejected(self.prepare(transform))
        path = self.prepare()
        self.envelope["review"]["sha256"] = "0" * 64
        self.rejected(self.write_envelope())
        path = self.prepare()
        self.cohort["compatibility_justification"] += " tampered"
        self.rejected(self.write_envelope())

    def test_member_hash_mode_duplicates_and_failed_integrity(self):
        original = copy.deepcopy(self.cohort)
        for field in ("path", "sha256", "manifest"):
            self.cohort = copy.deepcopy(original)
            self.cohort["runs"][1][field] = self.cohort["runs"][0][field]
            self.rejected(self.prepare())
        self.cohort = copy.deepcopy(original)
        self.cohort["runs"][1]["sha256"] = "0" * 64
        self.rejected(self.prepare())
        for mode in ("negative", "fixture-valid"):
            d = valid()
            d["mode"] = mode
            self.write_member(1, d)
            self.rejected(self.prepare())
        d = valid()
        d["mode"] = "normal"
        d["lse"][0]["lost_updates"] = 1
        self.write_member(1, d)
        self.rejected(self.prepare())
        d = valid()
        d["mode"] = "normal"
        d["litmus"][0]["histogram"]["r0=0,r1!=0"] += 1
        self.write_member(1, d)
        self.rejected(self.prepare())

    def test_duplicate_artifact_identity_even_with_matching_bytes(self):
        first, second = self.cohort["runs"]
        raw = (self.base / first["path"]).read_bytes()
        (self.base / second["path"]).write_bytes(raw)
        second["sha256"] = first["sha256"]
        self.rejected(self.prepare())
        second["path"] = first["path"]
        self.rejected(self.prepare())

    def test_empty_single_and_missing_justification_rejected(self):
        original = copy.deepcopy(self.cohort)
        for n in (0, 1):
            self.cohort = copy.deepcopy(original)
            self.cohort["runs"] = self.cohort["runs"][:n]
            self.rejected(self.prepare())
        self.cohort = copy.deepcopy(original)
        self.cohort["compatibility_justification"] = ""
        self.rejected(self.prepare())


if __name__ == "__main__":
    unittest.main(verbosity=2)
