"""Retained artifact integrity and run identity; no review-document interpretation."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import socket

EXPERIMENT = Path(__file__).resolve().parents[1]
REPO = Path(os.environ.get("NATEMU_F08_REPO", EXPERIMENT.parents[1])).resolve()
BUNDLE_FILES = ("Makefile", "run.sh", "src/litmus.c", "src/fallback.h", "src/launch_guard.h",
                "src/check.py", "src/fallback.py", "src/histogram.py", "src/evidence.py", "src/supervisor.py")

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def bundle():
    return {name: file_sha(EXPERIMENT / name) for name in BUNDLE_FILES}


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def require(ok, message):
    if not ok:
        raise ValueError(message)


def json_read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def artifact(base, ref):
    require(isinstance(ref, dict) and set(ref) == {"path", "sha256"}, "artifact reference schema")
    require(isinstance(ref["path"], str) and sha(ref["sha256"]), "artifact reference values")
    path = (base / ref["path"]).resolve(strict=True)
    require(path.is_file() and file_sha(path) == ref["sha256"], "artifact digest mismatch")
    return path


def external(path, root, must_exist=False):
    root = Path(root).resolve(strict=True)
    require(root.is_dir() and root != Path("/") and root != Path.home(), "explicit bounded external raw root required")
    require(not root.is_relative_to(REPO) and not REPO.is_relative_to(root), "raw root overlaps repository")
    target = Path(path).absolute()
    require(re.fullmatch(r"[A-Za-z0-9_./-]+", str(target)) is not None and
            re.fullmatch(r"[A-Za-z0-9_./-]+", str(root)) is not None,
            "external paths must use shell-safe ASCII path characters")
    require(target == target.resolve(strict=must_exist), "symlink/noncanonical output path")
    require(target.is_relative_to(root) and target != root, "output outside explicit raw root")
    require(target.parent.is_dir(), "output parent absent")
    return target


def live_context(numer, denom):
    def command(args):
        return subprocess.check_output(args, text=True, timeout=5).strip()
    return {"host_id": socket.gethostname(), "host_model": command(["sysctl", "-n", "hw.model"]),
            "os_build": command(["sw_vers", "-buildVersion"]),
            "architecture": command(["uname", "-m"]),
            "timebase": {"numer": numer, "denom": denom}}


def verify_files(directory, files):
    require(isinstance(files, dict) and files, "artifact list missing")
    for name, expected in files.items():
        target=(directory/name).resolve(strict=True)
        require(target.is_relative_to(directory) and sha(expected) and file_sha(target)==expected,
                "artifact path/hash mismatch")


def validate_result_binding(path, data):
    path=Path(path).resolve(strict=True)
    require(data.get("schema")=="INV-01-08-result-v3", "historical result needs its original reader")
    receipt=json_read(path.parent/"result.supervisor.json")
    require(receipt.get("schema")=="INV-01-08-supervisor-v2" and receipt.get("mode")=="normal" and
            type(receipt.get("returncode")) is int and receipt["returncode"]==0 and receipt.get("timed_out") is False,
            "successful supervised normal child required")
    require(receipt.get("result_sha256")==file_sha(path), "raw digest mismatch")
    invocation=json_read(path.parent/"invocation.json")
    build_path=artifact(path.parent, invocation["build"])
    build=json_read(build_path)
    require(build.get("schema")=="INV-01-08-build-v2" and build.get("outcome")=="built", "complete snapshot build required")
    verify_files(build_path.parent, build["artifacts"])
    require(data.get("run_id")==receipt.get("run_id")==invocation.get("run_id")==build.get("run_id"), "mixed run")
    require(data.get("compiled_bundle_sha256")==build["source_sha256"], "mixed producing build")
    require(invocation.get("binary_sha256")==build["artifacts"]["build/litmus"], "mixed binary")
    validate_campaign(path, build)
    timing=receipt.get("timing",{})
    if timing.get("status")=="context-matched":
        import sys
        sys.path.insert(0,str(build_path.parent/"snapshot/src"))
        from clock_evidence import validate
        return validate(timing.get("path"),receipt["context"])
    return {"status":"unqualified","reason":"clock evidence absent or mismatched"}


def validate_campaign(normal_path, build, *, supervision_required=True):
    from supervisor import control_oracle, sequential_oracle
    parent=Path(normal_path).resolve().parent.parent
    packet=json_read(parent/"campaign.json")
    require(packet.get("schema")=="INV-01-08-campaign-v3" and packet.get("run_id")==build["run_id"], "complete same-run v3 campaign required")
    if supervision_required:
        outer=json_read(parent/"campaign.supervisor.json")
        require(outer.get("schema")=="INV-01-08-campaign-supervisor-v3" and
                outer.get("run_id")==build["run_id"] and outer.get("status")=="completed" and
                type(outer.get("returncode")) is int and outer["returncode"]==0 and
                outer.get("timed_out") is False and outer.get("output_limited") is False,
                "successful aggregate campaign supervision required")
        require(outer.get("watchdog_seconds")==2700 and outer.get("output_limit_bytes")==128*1024*1024 and
                type(outer.get("supervisor_elapsed_ns")) is int and 0 <= outer["supervisor_elapsed_ns"] <= 2700*10**9 and
                type(outer.get("output_bytes")) is int and 0 <= outer["output_bytes"] < 128*1024*1024,
                "aggregate budget mismatch")
        require({"campaign.json","schedule.json"} <= outer.get("artifacts",{}).keys(),"aggregate artifact coverage")
        verify_files(parent,outer["artifacts"])
        require(outer.get("modes")=={mode:"completed" for mode in
                ("normal","negative","fallback-drop-write","fallback-lock-error","unexpected-fault")},
                "aggregate incomplete schedule")
    modes={"normal","negative","fallback-drop-write","fallback-lock-error","unexpected-fault"}
    require(set(packet.get("members",{}))==modes,"campaign exact control coverage")
    normal_context=json_read(Path(normal_path).parent/"result.supervisor.json")["context"]
    for mode,ref in packet["members"].items():
        manifest_path=artifact(parent,ref)
        require(manifest_path==parent/mode/"manifest.json", "canonical campaign member")
        m=json_read(manifest_path)
        require(m.get("schema")=="INV-01-08-execution-manifest-v2" and m.get("mode")==mode and
                type(m.get("classification_returncode")) is int and m["classification_returncode"]==0, "member failed")
        directory=manifest_path.parent
        verify_files(directory,m["artifacts"])
        require({"invocation.json","result.supervisor.json","stdout.txt","stderr.txt","check.txt",
                 "environment-before.json","environment-after.json"}<=m["artifacts"].keys(), "member artifact coverage")
        r=json_read(directory/"result.supervisor.json")
        inv=json_read(directory/"invocation.json")
        require(r.get("schema")=="INV-01-08-supervisor-v2" and inv.get("schema")=="INV-01-08-invocation-v2", "member schemas")
        require(r.get("mode")==mode and r.get("timed_out") is False and r.get("context")==normal_context, "member status/context")
        require(m.get("run_id")==r.get("run_id")==inv.get("run_id")==build["run_id"], "mixed member run")
        member_build=artifact(directory,inv["build"])
        require(json_read(member_build)==build, "mixed member build")
        require(inv.get("binary_sha256")==build["artifacts"]["build/litmus"] and
                inv.get("command")==[str(member_build.parent/"build/litmus"),"--mode",mode,"--out",str(directory/"result.json")], "member invocation mismatch")
        raw=directory/"result.json"
        d=json_read(raw) if raw.exists() else None
        require(r.get("result_sha256")== (file_sha(raw) if d is not None else None), "member raw digest")
        if d is not None:
            require(d.get("schema")=="INV-01-08-result-v3" and d.get("run_id")==build["run_id"] and
                    d.get("compiled_bundle_sha256")==build["source_sha256"] and d.get("timebase")==normal_context["timebase"], "member producer mismatch")
        if mode=="normal":
            require(raw==Path(normal_path) and type(r.get("returncode")) is int and r["returncode"]==0,"normal status")
        elif mode=="negative":
            require(sequential_oracle(d,r), "sequential control")
        else:
            require(control_oracle(mode,r,d,(directory/"stdout.txt").read_text()),"deterministic control")
