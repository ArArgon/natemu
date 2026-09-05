"""Exact INV-01-08 reporting; no probe execution or automatic run discovery."""

from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re

CELLS = {"r0=0,r1=0", "r0=0,r1!=0", "r0!=0,r1=0", "r0!=0,r1!=0"}
CONTEXT_FIELDS = {
    "host_id", "host_model", "os_build", "sdk", "features", "signing",
    "topology", "stimulus_layout", "method_revision", "code_revision",
}


def validate_histograms(data):
    """Reporting input gate, separate from the unchanged historical I rules."""
    if data.get("experiment") != "INV-01-08":
        raise ValueError("H-input: wrong experiment")
    arms = {}
    for row in data["litmus"]:
        key = (row["test"], row["variant"], row["qos"])
        n, h, relaxed = row["iterations"], row.get("histogram"), row["relaxed_observed"]
        if (type(n) is not int or n <= 0 or not isinstance(h, dict)
                or set(h) != CELLS or any(type(v) is not int or v < 0 for v in h.values())
                or sum(h.values()) != n or type(relaxed) is not int
                or relaxed != h["r0=0,r1=0" if key[0] == "SB" else "r0!=0,r1=0"]):
            raise ValueError(f"H-input: invalid histogram/counts for {key}")
        if key in arms:
            raise ValueError(f"H-input: duplicate arm {key}")
        arms[key] = row
    return arms


def classify(fp, fs):
    if fp == 0 or fs == 0:
        return "INCONCLUSIVE", "undefined (both zero)" if fp == fs else "unbounded"
    ratio = max(fp, fs) / min(fp, fs)
    verdict = ("COMPARABLE" if ratio < Fraction(10, 3) else
               "INDETERMINATE" if ratio <= 30 else "INCONCLUSIVE")
    return verdict, ratio


def comparisons(runs):
    """Caller must first approve cohort compatibility and every run's integrity."""
    indexed = [validate_histograms(run) for run in runs]
    if not indexed or any(set(indexed[0]) != set(arms) for arms in indexed):
        raise ValueError("H-input: empty or mismatched run coverage")
    for test in ("SB", "MP"):
        for qos in ("background", "user-interactive"):
            for variant in ("ldar-stlr", "dmb-ish"):
                p = [arms[(test, "plain-ldr-str", qos)] for arms in indexed]
                s = [arms[(test, variant, qos)] for arms in indexed]
                pc, pn = sum(a["histogram"]["r0!=0,r1!=0"] for a in p), sum(a["iterations"] for a in p)
                sc, sn = sum(a["histogram"]["r0!=0,r1!=0"] for a in s), sum(a["iterations"] for a in s)
                verdict, ratio = classify(Fraction(pc, pn), Fraction(sc, sn))
                pe, se = [a["relaxed_observed"] for a in p], [a["relaxed_observed"] for a in s]
                yield {
                    "key": (test, variant, qos), "plain": (pc, pn), "strengthened": (sc, sn),
                    "ratio": ratio, "verdict": verdict, "plain_exposure": pe,
                    "strengthened_exposure": se,
                    "eligible": verdict == "COMPARABLE" and sum(pe) > 0 and sum(se) == 0,
                }


def report(runs):
    for row in comparisons(runs):
        ratio = row["ratio"]
        if isinstance(ratio, Fraction):
            ratio = f"{ratio.numerator}/{ratio.denominator} ({float(ratio):.12g}x)"
        print(f"  {row['verdict']}  H {'/'.join(row['key'])}: "
              f"plain={row['plain'][0]}/{row['plain'][1]} "
              f"strengthened={row['strengthened'][0]}/{row['strengthened'][1]} ratio={ratio}; "
              f"plain-exposure-per-run={row['plain_exposure']} "
              f"strengthened-exposure-per-run={row['strengthened_exposure']}; "
              f"corroboration={'ELIGIBLE (bounded only)' if row['eligible'] else 'NOT ELIGIBLE'}")
    print("  H scope: histogram comparability alone is not corroboration; "
          "no finite zero establishes prohibition or a probability bound")


def cohort_digest(cohort):
    return hashlib.sha256(json.dumps(cohort, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def verified_bytes(base, item):
    if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
        raise ValueError("cohort: missing artifact path")
    path = (base / item["path"]).resolve()
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != item.get("sha256"):
        raise ValueError(f"cohort: digest mismatch for {path}")
    return path, data


def load_cohort(path):
    """A review binds exact declarations; it is not inferred from host labels.

    Review authenticity/independence and declarations' factual support remain
    research-review obligations. Here we check the explicit accepted record,
    its digest and exact cohort binding, run identities, and equal strata.
    """
    path = Path(path).resolve()
    envelope = json.loads(path.read_text())
    if envelope.get("schema") != "INV-01-08-cohort-v1":
        raise ValueError("cohort: unsupported schema")
    cohort = envelope["cohort"]
    if not isinstance(cohort.get("id"), str) or not cohort["id"].strip():
        raise ValueError("cohort: missing id")
    if not isinstance(cohort.get("compatibility_justification"), str) or not cohort["compatibility_justification"].strip():
        raise ValueError("cohort: missing compatibility justification")
    members = cohort.get("runs")
    if not isinstance(members, list) or len(members) < 2:
        raise ValueError("cohort: at least two explicitly reviewed runs required")
    review_path, review_bytes = verified_bytes(path.parent, envelope["review"])
    review = review_bytes.decode("utf-8")
    digest = cohort_digest(cohort)
    required = {
        "Review ID": r"`REV-\d{3}`",
        "Independence": "`independent`",
        "Verdict": "`accept`",
        "Cohort SHA-256": f"`{digest}`",
    }
    if not all(len(values := re.findall(rf"^- {key}: (.*)$", review, re.MULTILINE)) == 1
               and re.fullmatch(pattern, values[0].strip())
               for key, pattern in required.items()):
        raise ValueError("cohort: independently accepted review does not bind this exact cohort")
    runs, seen_paths, seen_hashes, seen_manifests = [], set(), set(), set()
    expected_context = None
    for member in members:
        context = member.get("context")
        if (not isinstance(context, dict) or set(context) != CONTEXT_FIELDS
                or any(not isinstance(v, str) or not v.strip() for v in context.values())):
            raise ValueError("cohort: incomplete compatibility context")
        if expected_context is not None and context != expected_context:
            raise ValueError("cohort: incompatible contexts (reviewed exceptions are not implemented)")
        expected_context = context
        manifest = member.get("manifest")
        if not isinstance(manifest, str) or not re.fullmatch(r"MAN-01-08-\d{2}", manifest):
            raise ValueError("cohort: missing INV-01-08 manifest identity")
        result_path, raw = verified_bytes(path.parent, member)
        sha = member["sha256"]
        if result_path in seen_paths or sha in seen_hashes or manifest in seen_manifests:
            raise ValueError("cohort: duplicate path, artifact digest, or manifest")
        seen_paths.add(result_path)
        seen_hashes.add(sha)
        seen_manifests.add(manifest)
        data = json.loads(raw)
        if data.get("mode") != "normal" or data.get("experiment") != "INV-01-08":
            raise ValueError("cohort: only normal INV-01-08 runs can pool")
        runs.append((str(result_path), data))
    return cohort["id"], digest, str(review_path), runs
