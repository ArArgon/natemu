"""INV-01-06 M06-M1 supervisor. No native execution on import or on `plan`.

`run` requires BOTH an accepted self code review (REV-228) and an accepted
INDEPENDENT code review (REV-229, reserved -- not yet written) bound to the
exact source-packet digest, per INV-01-06.md:806-814 ("no runtime preflight/
evidence before independent code acceptance") and this author's own
boundary. Until REV-229 exists and accepts, `run` refuses before it ever
builds a job list, spawns a child, or touches a Mach API -- confirmed by
running `python3 run.py run ...` in this authoring session, which is safe
because the refusal is pure Python control flow ahead of any subprocess.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

from coverage import manifest, digest, canonical

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            result.update(chunk)
    return result.hexdigest()


def command(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def raw_root():
    text = os.environ.get("NATEMU_RAW_ROOT")
    if not text:
        raise SystemExit("refusing to run: NATEMU_RAW_ROOT is not set")
    path = Path(text).expanduser().resolve()
    if not path.is_dir():
        raise SystemExit("refusing to run: NATEMU_RAW_ROOT is not an existing directory")
    if path == REPO or REPO in path.parents:
        raise SystemExit("refusing to run: NATEMU_RAW_ROOT is inside the repository")
    return path


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def packet():
    """Filename -> SHA-256 for every tracked file in this experiment
    directory, plus the packet digest over that mapping. This is the exact
    digest REV-228/REV-229 must bind to."""
    files = {str(path.relative_to(HERE)): sha(path) for path in sorted(HERE.rglob("*"))
             if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}
    return dict(files=files, sha256=digest(files))


def file_manifest(directory):
    return {str(path.relative_to(directory)): sha(path) for path in sorted(Path(directory).rglob("*"))
            if path.is_file() and path.name != "manifest.json"}


def review_gate(path, expected_digest, independence):
    text = Path(path).read_text()
    if (not re.search(r"^- Verdict: `accept`\.?\s*$", text, re.M) or
        not re.search(rf"^- Independence: `{independence}`\s*$", text, re.M) or
        not re.search(r"^- Review type: `code`\s*$", text, re.M) or
            expected_digest not in text):
        raise SystemExit(f"{independence} code review {path} does not accept exact packet digest {expected_digest}")
    return dict(path=str(Path(path).resolve()), sha256=sha(path), accepted_packet=expected_digest,
                independence=independence)


def clean_tree():
    changed = command(["git", "status", "--porcelain", "--", str(HERE)])
    if changed:
        raise SystemExit("refusing to run: commit the full m1 packet before evidence/runtime preflight\n" + changed)


def environment():
    result = dict(platform=platform.platform(), machine=platform.machine(),
                  captured_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    for key, args in dict(
            os_build=["sw_vers", "-buildVersion"], os_version=["sw_vers", "-productVersion"],
            model=["sysctl", "-n", "hw.model"], cpus=["sysctl", "-n", "hw.logicalcpu"],
            page_size=["sysctl", "-n", "hw.pagesize"],
            sdk=["xcrun", "--sdk", "macosx", "--show-sdk-version"],
            mig_version=["xcrun", "mig", "-version"],
            compiler=["xcrun", "clang", "--version"]).items():
        try:
            result[key] = command(args)
        except subprocess.CalledProcessError as exc:
            result[key] = dict(unavailable=True, output=exc.output)
    result["distribution_scope"] = "local signing capability, not Developer ID/notarization"
    return result


def jobs(plan):
    """One job per fresh-exec child. Positive cells: 3 children (seeds
    60601-60603) per eligible cell. Auxiliary arms: 3 children per arm."""
    rows = []
    for cell in plan["cells"]:
        if cell["status"] != "planned":
            rows.append(dict(cell_id=cell["cell_id"], cell=cell, kind="not-run",
                              reason=cell["invalid_reason"]))
            continue
        for seed in plan["positive_sample_plan"]["seeds"]:
            rows.append(dict(cell_id=cell["cell_id"], cell=cell, kind="positive", seed=seed,
                              warmup=plan["positive_sample_plan"]["warmup_events_per_worker"],
                              checked=plan["positive_sample_plan"]["checked_events_per_worker"]))
    for arm in plan["auxiliary_arms"]:
        for seed in plan["auxiliary_sample_plan"]["seeds"]:
            rows.append(dict(arm_id=arm["arm_id"], arm=arm, kind=arm["kind"], seed=seed))
    return rows


def native_args(binary, job):
    if job["kind"] == "not-run":
        return None
    result = [str(binary)]
    if job["kind"] == "positive":
        c = job["cell"]
        result += ["--n", str(c["n"]), "--jit", str(c["jit"]), "--access", c["access"],
                   "--topology", c["topology"], "--path", c["path"],
                   "--warmup", str(job["warmup"]), "--checked", str(job["checked"]),
                   "--seed", str(job["seed"])]
    elif job["kind"] == "negative":
        result += ["--n", "2", "--jit", "0", "--access", "load", "--topology", "perworker",
                   "--path", "direct", "--warmup", "0", "--checked", "1",
                   "--seed", str(job["seed"]), "--negative", job["arm"]["arm"]]
    else:  # control / terminal
        result += ["--n", "1", "--jit", "0", "--access", "load", "--topology", "serial",
                   "--path", "direct", "--warmup", "0", "--checked", "1",
                   "--seed", str(job["seed"]), "--arm", job["arm"]["arm"]]
    return result


def run_child(binary, job, directory):
    directory.mkdir(parents=True)
    write_new(directory / "job.json", job)
    argv = native_args(binary, job)
    if argv is None:
        write_new(directory / "check.json", dict(status="not-run", reason=job["reason"]))
        return dict(status="not-run", reason=job["reason"])
    write_new(directory / "invocation.json", dict(argv=argv, binary_sha256=sha(binary)))
    env = dict(os.environ, NATEMU_RAW_ROOT=str(directory))
    start = time.monotonic()
    with (directory / "trace.jsonl").open("xb") as out, (directory / "stderr.txt").open("xb") as err:
        child = subprocess.Popen(argv, stdout=out, stderr=err, env=env, start_new_session=True)
        timed_out = False
        try:
            code = child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            timed_out = True
            child.kill()
            code = child.wait()
        out.flush(); err.flush()
        os.fsync(out.fileno()); os.fsync(err.fileno())
    status = dict(returncode=code, timed_out=timed_out, watchdog_elapsed_seconds=time.monotonic() - start,
                  watchdog_is_latency_bound=False, no_progress_budget_seconds=5, total_budget_seconds=30)
    write_new(directory / "exit.json", status)
    from check import two_pass_check, Invalid
    try:
        fails = two_pass_check((directory / "trace.jsonl").read_text())
        result = dict(status="INVALID" if fails else "checked", fails=fails)
    except Invalid as exc:
        result = dict(status="INVALID", invariant=str(exc))
    write_new(directory / "check.json", result)
    files = {p.name: sha(p) for p in directory.iterdir() if p.is_file()}
    write_new(directory / "manifest.json", dict(job_sha256=digest(job), files=files, result=result))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "run"))
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--self-review", type=Path, default=REPO / "docs/research/reviews/REV-228.md")
    parser.add_argument("--independent-review", type=Path, default=REPO / "docs/research/reviews/REV-229.md")
    args = parser.parse_args()
    plan = manifest()
    sources = packet()

    if args.action == "plan":
        print(canonical(dict(plan=plan, code_packet=sources)))
        return

    if not args.binary:
        parser.error("run needs --binary")
    if not args.self_review.is_file():
        raise SystemExit(f"missing self code review at {args.self_review}")
    self_approval = review_gate(args.self_review, sources["sha256"], "self")
    if not args.independent_review.is_file():
        raise SystemExit(
            "refusing to run: independent code review REV-229 does not exist yet. "
            "INV-01-06.md:810 requires independent code acceptance before any runtime "
            "preflight/evidence; self REV-228 alone never authorizes execution.")
    independent_approval = review_gate(args.independent_review, sources["sha256"], "independent")

    clean_tree()
    root = raw_root() / "INV-01-06" / "m1"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    campaign = root / stamp
    campaign.mkdir(parents=True)
    host = environment()
    schedule = jobs(plan)
    write_new(campaign / "coverage.json", plan)
    write_new(campaign / "code-packet.json", sources)
    write_new(campaign / "environment.json", host)
    write_new(campaign / "gates.json", dict(self_review=self_approval, independent_review=independent_approval,
                                            producing_revision=command(["git", "rev-parse", "HEAD"])))
    write_new(campaign / "schedule.json", dict(jobs=schedule, planned=len(schedule), child_ceiling_seconds=30))

    results = []
    for index, job in enumerate(schedule):
        result = run_child(args.binary.resolve(), job, campaign / f"{index:06d}")
        results.append(dict(index=index, **result))
    write_new(campaign / "results.json", dict(results=results, planned=len(schedule)))
    write_new(campaign / "manifest.json", dict(files=file_manifest(campaign)))
    print(campaign)


if __name__ == "__main__":
    main()
