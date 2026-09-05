"""Review-bound execution prerequisite. Declarations alone never grant approval.

Authenticity and reviewer independence remain governance obligations; this
module verifies exact repository review bytes, accepted independent metadata,
the explicit reviewed bundle/applicability digest, and live applicability.
It intentionally ships with no authorized envelope or accepted code record.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

REPO = Path(__file__).resolve().parents[3]
EXPERIMENT = REPO / "experiments/INV-01-08"
METHOD_SHA = "e4ef2d552e8910f3fee3e920f937cea69f9245330f3925dfbbad39f7067af6ff"
BUNDLE_FILES = (
    "Makefile", "run.sh", "validate.sh", "src/litmus.c", "src/fallback.h",
    "src/launch_guard.h", "src/check.py", "src/fallback.py", "src/histogram.py",
    "src/authorization.py", "src/supervisor.py", "tests/histogram-fixtures.py",
    "tests/fallback-fixtures.py", "tests/valid.json", "tests/negative.json",
    "tests/forbidden.json", "tests/negative-coverage.json",
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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


def review(base, ref, kind, binding=None, expected_id=None, independence="independent"):
    path = artifact(base, ref)
    require(path.parent == REPO / "docs/research/reviews", "review must be canonical repository record")
    if expected_id:
        require(path.name == expected_id + ".md", "wrong review identity")
    content = path.read_text()
    fields = {"Review ID": f"`{path.stem}`", "Review type": f"`{kind}`",
              "Independence": f"`{independence}`", "Verdict": "`accept`"}
    if binding:
        fields.update(binding)
    for key, expected in fields.items():
        values = re.findall(rf"^- {re.escape(key)}: (.*)$", content, re.MULTILINE)
        require(len(values) == 1 and values[0].strip() == expected, "missing accepted independent review binding: " + key)
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
    host = command(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
    uuids = re.findall(r'"IOPlatformUUID" = "([^"]+)"', host)
    require(len(uuids) == 1, "host identity unavailable")
    return {"host_id": uuids[0], "host_model": command(["sysctl", "-n", "hw.model"]),
            "os_build": command(["sw_vers", "-buildVersion"]),
            "architecture": command(["uname", "-m"]),
            "timebase": {"numer": numer, "denom": denom}}


def load_authorization(path, mode, numer=None, denom=None, live=True):
    path = Path(path).resolve(strict=True)
    require(not path.is_relative_to(REPO), "authorization must be external, not a checked-in approval fixture")
    a = json_read(path)
    require(a.get("schema") == "INV-01-08-execution-authorization-v1", "authorization schema")
    require(a.get("kind") in ("evidence", "validation"), "authorization kind")
    require(mode in ("normal", "negative", "fallback-only", "fallback-drop-write",
                     "fallback-lock-error", "unexpected-fault"), "run mode")
    require(a.get("code_bundle_sha256") == digest(bundle()), "implementation bundle differs from authorization")
    require(a.get("method_review", {}).get("sha256") == METHOD_SHA, "accepted F08-2 method identity")
    review(path.parent, a["method_review"], "method", expected_id="REV-207")
    # REV-210 R5: an independent accept RETURN and the author's own self
    # review of this exact bundle are both required; neither substitutes for
    # the other (the "Independence" field enforced by `review()` differs),
    # and the required independent review is identified by its content
    # binding to this exact accepted bundle -- never a hard-coded filename --
    # so a later accepted return (e.g. REV-218, after REV-210's revise) can
    # satisfy this without a code change here. REV-210 itself is `revise`
    # and therefore cannot satisfy the accept-verdict requirement below.
    review(path.parent, a["code_review"], "code",
           {"Code bundle SHA-256": f"`{a['code_bundle_sha256']}`"})
    review(path.parent, a["code_self_review"], "code",
           {"Code bundle SHA-256": f"`{a['code_bundle_sha256']}`"}, independence="self")
    revision = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    require(a.get("code_revision") == revision, "authorization not current revision")
    # No performance/evidence collection from an uncommitted producing bundle.
    status = subprocess.check_output(["git", "-C", str(REPO), "status", "--porcelain", "--"] +
                                     [str(EXPERIMENT / p) for p in BUNDLE_FILES], text=True)
    require(not status, "producing code bundle must be committed and clean")
    if a["kind"] == "validation":
        require(mode not in ("normal", "negative"), "full evidence modes require prior clock evidence")
        return a
    clock = a.get("clock")
    require(isinstance(clock, dict), "prior clock prerequisite absent")
    packet = clock["packet"]
    packet_digest = digest(packet)
    review(path.parent, clock["applicability_review"], "result",
           {"Clock prerequisite SHA-256": f"`{packet_digest}`"})
    require(packet.get("schema") == "INV-01-07-clock-prerequisite-v1", "clock packet schema")
    require(packet.get("consumer") == "INV-01-08 F08-2", "clock applicability consumer")
    refs = packet.get("reviews")
    require(isinstance(refs, dict) and set(refs) == {"method", "code", "result", "replication", "adversarial"},
            "complete clock gate coverage required")
    reviewed = [review(path.parent, refs[k], k) for k in refs]
    require(len(set(reviewed)) == 5, "clock reviews must have distinct records")
    raw = packet.get("raw")
    require(isinstance(raw, list) and raw, "clock raw identities absent")
    raw_paths = [artifact(path.parent, ref) for ref in raw]
    require(len(set(raw_paths)) == len(raw_paths) and all(not p.is_relative_to(REPO) for p in raw_paths),
            "clock raw must be distinct external artifacts")
    require(isinstance(packet.get("scope_justification"), str) and packet["scope_justification"].strip(),
            "clock applicability rationale missing")
    context = packet.get("context")
    require(isinstance(context, dict) and set(context) == {"host_id", "host_model", "os_build", "architecture", "timebase"},
            "clock applicability context schema")
    tb = context["timebase"]
    require(isinstance(tb, dict) and set(tb) == {"numer", "denom"} and
            all(type(v) is int and 0 < v <= (1 << 32) - 1 for v in tb.values()), "clock timebase")
    if live:
        require(numer is not None and denom is not None, "native timebase required")
        require(context == live_context(numer, denom), "clock prerequisite not applicable to live host/OS/timebase")
    return a


def validate_result_binding(path, data):
    """A result requires the pre-execution authorization and successful child receipt."""
    path = Path(path).resolve(strict=True)
    receipt_path = path.parent / (path.stem + ".supervisor.json")
    receipt = json_read(receipt_path)
    require(receipt.get("schema") == "INV-01-08-supervisor-v1", "supervisor receipt schema")
    require(receipt.get("mode") == "normal" and receipt.get("returncode") == 0 and
            type(receipt.get("returncode")) is int and receipt.get("timed_out") is False,
            "successful supervised normal child required")
    require(receipt.get("result_sha256") == file_sha(path), "receipt does not bind raw result")
    auth_path = artifact(receipt_path.parent, receipt["authorization"])
    a = load_authorization(auth_path, "normal", live=False)
    require(a["kind"] == "evidence", "validation is not timing evidence")
    require(data.get("authorization_sha256") == file_sha(auth_path) and
            data.get("code_bundle_sha256") == a["code_bundle_sha256"] and
            data.get("code_revision") == a["code_revision"], "producer authorization mismatch")
    # REV-210 R3: the raw's stamped compiled-source digest (baked into the
    # binary at build time, checked pre-launch by native_authorize) must
    # still match the accepted bundle here too, independent of that gate.
    require(data.get("compiled_bundle_sha256") == a["code_bundle_sha256"],
            "compiled source-bundle digest mismatch")
    require(data.get("timebase") == a["clock"]["packet"]["context"]["timebase"], "producer timebase mismatch")
    require(receipt.get("context") == a["clock"]["packet"]["context"], "supervisor live context mismatch")
    require(receipt.get("authorization_validated_before_spawn") is True,
            "no prior authorization receipt")
    validate_campaign(path, file_sha(auth_path), a)
    return a


def validate_campaign(normal_path, authorization_sha, authorization):
    """Recompute all required control oracles; summary booleans are not proof.

    REV-210 R4: agreeing with the campaign summary is not enough. Every
    member's own raw producer identity and invocation are bound to the
    SAME accepted authorization explicitly here, so rehashing a raw/receipt/
    manifest chain after transplanting a different authorization/bundle/
    revision/timebase into one control cannot pass.
    """
    import contextlib
    import io
    import check
    from supervisor import control_oracle, sequential_oracle
    normal_path = Path(normal_path).resolve(strict=True)
    parent = normal_path.parent.parent
    packet = json_read(parent / "campaign.json")
    require(packet.get("schema") == "INV-01-08-campaign-v1", "complete campaign required before timing")
    modes = {"normal", "negative", "fallback-drop-write", "fallback-lock-error", "unexpected-fault"}
    members = packet.get("members")
    require(isinstance(members, dict) and set(members) == modes, "campaign exact control coverage")
    context = authorization["clock"]["packet"]["context"]
    timebase = context["timebase"]
    seen = set()
    for mode, ref in members.items():
        manifest_path = artifact(parent, ref)
        require(manifest_path == parent/mode/"manifest.json", "canonical campaign member location")
        require(manifest_path not in seen, "duplicate campaign member")
        seen.add(manifest_path)
        manifest = json_read(manifest_path)
        require(manifest.get("schema") == "INV-01-08-execution-manifest-v1" and manifest.get("mode") == mode and
                type(manifest.get("classification_returncode")) is int and manifest["classification_returncode"] == 0,
                "campaign member validation failure")
        require(manifest.get("code_bundle") == bundle(), "campaign member implementation mismatch")
        files = manifest["artifacts"]
        required = {"result.supervisor.json", "invocation.json", "stdout.txt", "stderr.txt", "check.txt",
                    "environment-before.json", "environment-after.json", "build.log", "build/litmus",
                    "build/litmus.disasm", "build/litmus.s", "build/litmus-verify", "build/litmus-verify.disasm"}
        if mode != "unexpected-fault":
            required.add("result.json")
        require(isinstance(files, dict) and required <= set(files), "campaign artifact coverage")
        for name, expected in files.items():
            candidate = (manifest_path.parent/name).resolve(strict=True)
            require(candidate.is_relative_to(manifest_path.parent) and sha(expected) and file_sha(candidate) == expected,
                    "campaign artifact digest/location mismatch")
        r = json_read(manifest_path.parent/"result.supervisor.json")
        require(r.get("schema") == "INV-01-08-supervisor-v1" and r.get("mode") == mode and r.get("timed_out") is False and
                r.get("authorization", {}).get("sha256") == authorization_sha and
                r.get("authorization_validated_before_spawn") is True and
                r.get("context") == context, "control prerequisite/receipt mismatch")
        # A placeholder that is never actually parsed cannot bind anything;
        # json_read raises on non-JSON content instead of only hashing bytes.
        invocation = json_read(manifest_path.parent/"invocation.json")
        require(invocation.get("schema") == "INV-01-08-invocation-v1", "campaign invocation schema")
        raw_path = manifest_path.parent/"result.json"
        expected_command = [str(manifest_path.parent/"build/litmus"), "--mode", mode, "--out", str(raw_path)]
        require(invocation.get("command") == expected_command, "campaign invocation mode/output-path mismatch")
        require(invocation.get("binary_sha256") == files.get("build/litmus"),
                "campaign invocation binary hash differs from its manifested build")
        require(invocation.get("authorization", {}).get("sha256") == authorization_sha and
                invocation.get("code_revision") == authorization["code_revision"] and
                invocation.get("code_bundle") == bundle(), "campaign invocation provenance mismatch")
        d = json_read(raw_path) if raw_path.exists() else None
        require(r.get("result_sha256") == (file_sha(raw_path) if d is not None else None), "control raw receipt mismatch")
        if d is not None:
            require(d.get("authorization_sha256") == authorization_sha and
                    d.get("code_bundle_sha256") == authorization["code_bundle_sha256"] and
                    d.get("code_revision") == authorization["code_revision"] and
                    d.get("timebase") == timebase, "control raw producer binding mismatch")
        if mode == "normal":
            require(raw_path == normal_path and type(r.get("returncode")) is int and r["returncode"] == 0,
                    "normal campaign identity/status")
        elif mode == "negative":
            require(sequential_oracle(d, r), "sequential negative oracle")
        else:
            require(control_oracle(mode, r, d, (manifest_path.parent/"stdout.txt").read_text()), "deterministic control oracle")
