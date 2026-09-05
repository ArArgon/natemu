#!/usr/bin/env python3
"""M07-C1 build-only pipeline and reviewed fixed-campaign supervisor.

This code is NOT independently accepted merely because it exists. Build runs
synthetic tests/compiler/signing/disassembly, never the producer. Campaign
requires an exact reviewed gate receipt; structural output is never evidence
approval. File hashes/receipt syntax prevent accidental mismatches, not fraud
by a malicious parent/operator. No arbitrary external benchmark detector is
claimed: the explicit operator assertion and project lock are both required.
"""
import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys

import artifact
import cells
import check
import core

REPOSITORY = Path(__file__).resolve().parents[3]
SOURCE_PREFIX = "experiments/INV-01-07/c1/"
METHOD_PATH = "docs/research/investigations/INV-01-07.md"
# Fixed, known-number reviews. REV-196 is the independent critique that opened
# R2; it is permanently `revise` in this repository (REV-196.md is frozen).
# The independent RETURN that closes R2 gets a new number each cycle and is
# therefore supplied by the gate JSON, not hardcoded here (see
# `independent_return`).
REVIEWS = {name: "docs/research/reviews/REV-" + name + ".md"
           for name in ("193", "194", "195", "196")}
EXPECTED_VERDICT = {"193": "accept", "194": "accept", "195": "accept", "196": "revise"}
INDEPENDENT_REVIEWS = ("194", "196")
CRITIQUE_REVIEW = "196"
MODES = ("backward", "zero-nsec", "omit-clock", "omit-contention",
         "pthread-error", "global-inversion")
SCHEDULE = tuple((seed, "normal") for seed in (70701, 70702, 70703)) + tuple(
    (70701, mode) for mode in MODES)
CHILD_SECONDS = 300
EXPECTED_ARRAY_BYTES = 226962048
BUILD_SCHEMA = "INV-01-07-clock-c1-build-v1"
GATE_SCHEMA = "INV-01-07-clock-c1-gate-v2"
FAILURE_SCHEMA = "INV-01-07-clock-c1-failure-v1"
SDK_HEADERS = ("mach/mach_time.h", "time.h", "pthread.h", "pthread/qos.h", "sys/qos.h",
               "CommonCrypto/CommonDigest.h")
HEX = re.compile(r"[0-9a-f]{64}\Z")
PUBLIC_CODES = frozenset("""
dirty-reviewed-scope uncommitted-review-or-method review-hash review-id
review-not-accepted review-independence gate-location gate-fields gate-status
gate-reviews revision-changed reviewed-source-mismatch method-changed
independent-return-fields independent-return-review-id independent-return-file
independent-return-not-against-critique independent-return-type independent-return-subject
build-manifest-hash build-fields build-status-or-source build-artifact-inventory
build-artifact-path build-artifact-hash build-header-inventory sdk-header-changed
producer-not-executable unresolved-revision unresolved-repository git-command-failed
source-path source-symlink incomplete-source-inventory missing-NATEMU_RAW_ROOT
repository-internal-root destination-type destination-absolute
destination-parent-canonical destination-outside-root destination-exists
native-exclusivity-not-asserted preflight-payload-cap insufficient-disk-space
gate-changed disk-space-lost environment-identity-changed gate-changed-during-child
child-destination-exists child-directory-changed child-not-completed
producer-command-mismatch unexpected-control-rejection control-wrong-process-status
failure-fields failure-identity control-wrong-failure failure-domain
control-wrong-pthread-injection control-wrong-zero-nsec-status failure-with-completed-record
control-wrong-omission-size control-wrong-omission-target execution-fields
execution-identity execution-order resource-preflight resource-complete resource-payload
build-source-not-committed-clean build-source-changed build-source-became-dirty
lock-identity launch-parent launch-mode-seed launch-output launch-message-size
launch-pipe-short-write command-output-cap output-symlink output-not-regular
output-file-cap campaign-payload-cap injection-not-detected
""".split())


def public_error(error):
    """Fixed public codes only; exception prose belongs in diagnostic artifacts."""
    if isinstance(error, (core.Invalid, core.Unresolved)):
        value = str(error)
        code = value if value in PUBLIC_CODES else check.trusted_code(error)
        return {"category": "unresolved" if isinstance(error, core.Unresolved) else "invalid", "code": code}
    labels = {OSError: "os-error", ValueError: "value-error", TypeError: "type-error",
              KeyError: "key-error", OverflowError: "overflow-error", RecursionError: "recursion-error",
              KeyboardInterrupt: "interrupted", subprocess.TimeoutExpired: "command-timeout"}
    return {"category": "failure", "code": labels.get(type(error), "unexpected-error")}


def diagnostic(directory, stem, error):
    # Explicitly non-finding raw diagnostics; never copied to CLI/summary.
    write_new(directory / (stem + ".diagnostic.txt"),
              (type(error).__name__ + ": " + str(error) + "\n").encode("utf-8", errors="backslashreplace"))


def require(condition, code):
    if not condition:
        raise core.Invalid(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode()


def file_digest(path, limit=artifact.PAYLOAD_LIMIT):
    return digest(artifact.read_regular(path, limit))


def write_new(path, data):
    """Exclusive durable output; never truncate any existing artifact."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def write_json(path, value):
    write_new(path, canonical_json(value) + b"\n")


def external_root(value):
    require(type(value) is str and bool(value), "missing-NATEMU_RAW_ROOT")
    root = artifact.canonical_directory(value, "raw-root")
    require(root != REPOSITORY and not root.is_relative_to(REPOSITORY),
            "repository-internal-root")
    require(REPOSITORY.resolve(strict=True) == REPOSITORY
            and (REPOSITORY / ".git").exists(), "unresolved-repository")
    return root


def new_destination(value, root):
    require(type(value) is str, "destination-type")
    path = Path(value)
    require(path.is_absolute() and str(path) == value, "destination-absolute")
    require(path.parent.resolve(strict=True) == path.parent, "destination-parent-canonical")
    require(path != root and path.is_relative_to(root)
            and not path.is_relative_to(REPOSITORY), "destination-outside-root")
    require(not path.exists() and not path.is_symlink(), "destination-exists")
    return path


def command(args, *, timeout=60, cwd=None):
    completed = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=timeout, check=False)
    require(len(completed.stdout) + len(completed.stderr) <= 4 * 1024 * 1024,
            "command-output-cap")
    return completed


def git(*args):
    result = command(["git", "-C", str(REPOSITORY), *args])
    require(result.returncode == 0, "git-command-failed")
    return result.stdout.decode("utf-8", errors="strict")


def revision():
    value = git("rev-parse", "HEAD").strip()
    require(re.fullmatch(r"[0-9a-f]{40}", value) is not None, "unresolved-revision")
    return value


def source_files():
    names = git("ls-files", "-z", "--", SOURCE_PREFIX).split("\0")
    result = {}
    for name in filter(None, names):
        require(name.startswith(SOURCE_PREFIX) and ".." not in Path(name).parts,
                "source-path")
        path = REPOSITORY / name
        require(path.resolve(strict=True) == path, "source-symlink")
        result[name] = file_digest(path)
    require(SOURCE_PREFIX + "producer.c" in result
            and SOURCE_PREFIX + "runner.py" in result, "incomplete-source-inventory")
    return result


def clean_scope(extra_paths=()):
    """`extra_paths` covers gate-supplied files not known at import time (the
    independent return review, whose number/path are assigned per cycle) so
    a campaign cannot collect while its own authorising review is an
    untracked or otherwise dirty working-tree file."""
    paths = [SOURCE_PREFIX, METHOD_PATH, *REVIEWS.values(), *extra_paths]
    require(git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths) == "",
            "dirty-reviewed-scope")
    for path in paths[1:]:
        require(git("ls-files", "--error-unmatch", "--", path).strip() == path,
                "uncommitted-review-or-method")


def review_record(path, number, expected_hash, *, independent, verdict="accept"):
    """`path` is passed explicitly: fixed reviews resolve it from REVIEWS,
    but the independent return review's number/path are gate-supplied."""
    data = artifact.read_regular(path, artifact.METADATA_LIMIT)
    require(type(expected_hash) is str and HEX.fullmatch(expected_hash)
            and digest(data) == expected_hash, "review-hash")
    text = data.decode("utf-8", errors="strict")
    require(len(re.findall(r"^- Review ID(?: \([^\n]*\))?:\s*`REV-" + number + r"`", text, re.M)) == 1,
            "review-id")
    other_verdicts = "|".join(sorted({"accept", "revise", "reject"} - {verdict}))
    require(len(re.findall(r"^- Verdict:\s*`" + verdict + r"`(?:[.\s]|$)", text, re.M)) == 1
            and not re.search(r"^- Verdict:\s*`(?:" + other_verdicts + r")`", text, re.M),
            "review-not-accepted")
    label = "independent" if independent else "self"
    require(len(re.findall(r"^- Independence:\s*`" + label + r"`", text, re.M)) == 1,
            "review-independence")
    return text


def receipt(text, label, value):
    matches = re.findall(r"^- " + re.escape(label) + r":\s*`([0-9a-f]{64})`\s*$", text, re.M)
    require(matches == [value], "review-receipt:" + label)


def independent_return_shape(entry):
    """Structural checks only (no disk read): derives (number, file) so
    `load_gate` can fold the gate-supplied review path into `clean_scope()`
    before trusting its content."""
    cells.fields(entry, ("review_id", "file", "sha256", "expected_verdict", "independence"),
                 "independent-return-fields")
    number = entry["review_id"]
    require(type(number) is str and re.fullmatch(r"[1-9][0-9]{1,4}", number)
            and number not in REVIEWS, "independent-return-review-id")
    require(entry["file"] == "docs/research/reviews/REV-" + number + ".md",
            "independent-return-file")
    require(entry["expected_verdict"] == "accept", "independent-return-fields")
    require(entry["independence"] == "independent", "independent-return-fields")
    return number, entry["file"]


def independent_return_record(entry):
    """The independent RETURN review (against the fixed critique196) gets a
    new number each cycle, so its id/file/verdict/independence are supplied
    by the gate JSON itself and independently re-verified here, not assumed.
    It must also be a `code` review of this exact subject: nothing else pins
    its type/subject, since (unlike reviews193-196) its number is not fixed."""
    number, file = independent_return_shape(entry)
    text = review_record(REPOSITORY / file, number, entry["sha256"],
                         independent=True, verdict="accept")
    require(len(re.findall(r"^- Review type:\s*`code`(?:[.\s]|$)", text, re.M)) == 1,
            "independent-return-type")
    subject = re.search(r"^- Subject ID\(s\)[^\n]*:(.*?)(?=\n- |\n\n|\Z)", text, re.M | re.S)
    require(subject is not None and "INV-01-07" in subject.group(1)
            and "M07-C1" in subject.group(1), "independent-return-subject")
    require("REV-" + CRITIQUE_REVIEW in text, "independent-return-not-against-critique")
    return text


def load_gate(path, root):
    gate_path = Path(path)
    require(gate_path.is_absolute() and gate_path.resolve(strict=True) == gate_path
            and gate_path.is_relative_to(root), "gate-location")
    gate = artifact.read_json(gate_path)
    cells.fields(gate, ("schema", "revision", "source_files", "method_sha256", "reviews",
                       "independent_return", "build_directory", "build_sha256",
                       "required_changes_closed"),
                 "gate-fields")
    require(gate["schema"] == GATE_SCHEMA and gate["required_changes_closed"] is True,
            "gate-status")
    _, return_file = independent_return_shape(gate["independent_return"])
    clean_scope(extra_paths=(return_file,))
    require(gate["revision"] == revision(), "revision-changed")
    current_sources = source_files()
    require(gate["source_files"] == current_sources, "reviewed-source-mismatch")
    method_hash = file_digest(REPOSITORY / METHOD_PATH)
    require(gate["method_sha256"] == method_hash, "method-changed")
    cells.fields(gate["reviews"], ("193", "194", "195", "196"), "gate-reviews")
    review_text = {}
    for number in REVIEWS:
        review_text[number] = review_record(REPOSITORY / REVIEWS[number], number, gate["reviews"][number],
                                            independent=number in INDEPENDENT_REVIEWS,
                                            verdict=EXPECTED_VERDICT[number])
    return_text = independent_return_record(gate["independent_return"])
    build_dir = artifact.canonical_directory(gate["build_directory"], "build-directory")
    require(build_dir.is_relative_to(root) and build_dir != root, "build-outside-root")
    require(file_digest(build_dir / "build.json", artifact.METADATA_LIMIT)
            == gate["build_sha256"], "build-manifest-hash")
    build = artifact.read_json(build_dir / "build.json")
    cells.fields(build, ("schema", "source_files", "source_revision", "files", "commands",
                        "sdk", "compiler", "headers", "validation_exit", "native_executed"),
                 "build-fields")
    require(build["schema"] == BUILD_SCHEMA and build["source_files"] == current_sources
            and type(build["validation_exit"]) is int and build["validation_exit"] == 0
            and build["native_executed"] is False, "build-status-or-source")
    require(type(build["files"]) is dict and {"clock_c1", "disassembly.stdout", "validation.stdout",
                                             "validation.stderr"}.issubset(build["files"]),
            "build-artifact-inventory")
    for name, expected in build["files"].items():
        require(type(name) is str and Path(name).name == name and name not in (".", ".."),
                "build-artifact-path")
        require(type(expected) is str and HEX.fullmatch(expected)
                and file_digest(build_dir / name) == expected, "build-artifact-hash")
    sdk = artifact.canonical_directory(build["sdk"], "build-sdk")
    expected_headers = {str(sdk / "usr/include" / name) for name in SDK_HEADERS}
    clockid = sdk / "usr/include/sys/_types/_clockid_t.h"
    if clockid.exists():
        expected_headers.add(str(clockid))
    require(type(build["headers"]) is dict and set(build["headers"]) == expected_headers,
            "build-header-inventory")
    for path, expected in build["headers"].items():
        require(type(path) is str and Path(path).is_absolute()
                and type(expected) is str and HEX.fullmatch(expected)
                and file_digest(Path(path)) == expected,
                "sdk-header-changed")
    # Receipts bind the independent RETURN (not the frozen revise196 critique)
    # to these exact bytes: 196 will never carry a post-completion receipt.
    receipt(return_text, "C1 source SHA-256", digest(canonical_json(current_sources)))
    receipt(return_text, "C1 build SHA-256", gate["build_sha256"])
    receipt(return_text, "C1 method SHA-256", method_hash)
    require(os.access(build_dir / "clock_c1", os.X_OK), "producer-not-executable")
    return gate, build_dir / "clock_c1"


class CampaignLock:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path("/private/tmp") / (
            "natemu-c1-" + digest(str(REPOSITORY).encode())[:24] + ".lock")
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "lock-identity")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *unused):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.fd = None


def launch_message(output, seed, mode, parent_pid):
    require(type(parent_pid) is int and parent_pid > 0, "launch-parent")
    require(type(seed) is int and seed in (70701, 70702, 70703)
            and mode in ("normal", *MODES), "launch-mode-seed")
    require(Path(output).is_absolute() and "\n" not in str(output), "launch-output")
    message = f"C1-LAUNCH-v1\n{parent_pid}\n{seed}\n{mode}\n{output}\n".encode()
    require(len(message) <= 4096, "launch-message-size")
    return message


def supervise(executable, output, seed, mode, campaign, root):
    """Only called by campaign after gate checks; tests use a mocked process."""
    require(not output.exists(), "child-destination-exists")
    stem = output.name
    message = launch_message(output, seed, mode, os.getpid())
    write_new(campaign / (stem + ".launch.txt"), message)
    stdout_fd = os.open(campaign / (stem + ".stdout"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    stderr_fd = os.open(campaign / (stem + ".stderr"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    read_fd, write_fd = os.pipe()
    process = None
    timed_out = False
    returncode = None
    supervisor_error = None
    try:
        require(os.write(write_fd, message) == len(message), "launch-pipe-short-write")
        os.close(write_fd)
        write_fd = None
        environment = dict(os.environ)
        environment["NATEMU_RAW_ROOT"] = str(root)
        environment["NATEMU_C1_LAUNCH_FD"] = str(read_fd)
        process = subprocess.Popen([str(executable), str(output), str(seed), mode],
                                   stdin=subprocess.DEVNULL, stdout=stdout_fd, stderr=stderr_fd,
                                   env=environment, pass_fds=(read_fd,), start_new_session=True)
        os.close(read_fd)
        read_fd = None
        try:
            returncode = process.wait(timeout=CHILD_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            returncode = process.wait()
    except BaseException as error:
        supervisor_error = error
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            returncode = process.wait()
        elif process is not None:
            returncode = process.returncode
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        for fd in (stdout_fd, stderr_fd):
            os.fsync(fd)
            os.close(fd)
    if not output.exists():
        output.mkdir(mode=0o700)  # Failure-only artifact directory; no fake producer record.
    require(output.is_dir() and output.resolve(strict=True) == output, "child-directory-changed")
    terminal = {"exit_status": returncode if returncode is not None and returncode >= 0 else None,
                "signal": -returncode if returncode is not None and returncode < 0 else None,
                "timeout": timed_out,
                "complete": returncode == 0 and (output / "record.json").is_file()
                            and not (output / "failure.json").exists()}
    write_json(output / "process.json", terminal)
    if supervisor_error is not None:
        diagnostic(output, "supervisor-error", supervisor_error)
        write_json(output / "supervisor-failure.json", public_error(supervisor_error))
        raise supervisor_error
    return terminal


def failure_control(directory, mode, seed, terminal):
    expected = {"zero-nsec": ("nsec-zero", "clock_gettime_nsec_np"),
                "pthread-error": ("pthread-lock", "pthread_mutex_lock")}
    require(terminal == {"exit_status": 2, "signal": None, "timeout": False, "complete": False},
            "control-wrong-process-status")
    record = artifact.read_json(directory / "failure.json")
    cells.fields(record, ("schema", "code", "api", "status", "mode", "seed", "cell", "worker"),
                 "failure-fields")
    require(record["schema"] == FAILURE_SCHEMA and record["mode"] == mode
            and type(record["seed"]) is int and record["seed"] == seed,
            "failure-identity")
    require((record["code"], record["api"]) == expected[mode], "control-wrong-failure")
    require(type(record["status"]) is int and type(record["cell"]) is int
            and record["cell"] >= 0 and type(record["worker"]) is int,
            "failure-domain")
    if mode == "pthread-error":
        require(record["status"] == errno.EINVAL and record["worker"] == 0 and record["cell"] == 13,
                "control-wrong-pthread-injection")
    else:
        require(record["status"] == errno.EINVAL and record["cell"] == 1 and record["worker"] == -1,
                "control-wrong-zero-nsec-status")
    require(not (directory / "record.json").exists(), "failure-with-completed-record")
    return {"status": "EXPECTED_CONTROL_FAILURE", "mode": mode,
            "observed": record["code"], "evidence_approval": False}


def omitted_shape(record, mode):
    """Verify exactly one designated array is short by one; no arbitrary shape failure."""
    cells.matrix(record["clock_cells"], core.QOS, core.CLOCKS, "clock-matrix")
    cells.matrix(record["contention_cells"], core.QOS, cells.WORKERS, "contention-matrix")
    short = []
    for cell in record["clock_cells"]:
        item = cell["readings"]
        expected = 200001
        if item["records"] != expected:
            require(type(item["records"]) is int and item["records"] == expected - 1,
                    "control-wrong-omission-size")
            short.append(("clock", cell["qos"], cell["clock"]))
    for cell in record["contention_cells"]:
        item = cell["trace"]
        expected = cell["workers"] * 200000
        if item["records"] != expected:
            require(type(item["records"]) is int and item["records"] == expected - 1,
                    "control-wrong-omission-size")
            short.append(("contention", cell["qos"], cell["workers"]))
    expected_target = ("clock", "user-interactive", "mach_absolute_time") if mode == "omit-clock" else (
        "contention", "user-interactive", 2)
    require(short == [expected_target],
            "control-wrong-omission-target")
    return short[0]


def expected_order(seed):
    require(type(seed) is int and seed in (70701, 70702, 70703), "order-seed")
    order = list(range(20))
    state = seed
    for index in range(19, 0, -1):
        state = (1664525 * state + 1013904223) % (2**32)
        other = state % (index + 1)
        order[index], order[other] = order[other], order[index]
    return order


def execution_order(directory, seed):
    record = artifact.read_json(directory / "execution.json")
    cells.fields(record, ("schema", "seed", "order"), "execution-fields")
    require(record["schema"] == "INV-01-07-clock-c1-order-v1"
            and type(record["seed"]) is int and record["seed"] == seed,
            "execution-identity")
    require(type(record["order"]) is list and len(record["order"]) == 20
            and all(type(item) is int for item in record["order"])
            and record["order"] == expected_order(seed), "execution-order")


def resource_records(directory, mode, *, complete):
    preflight = artifact.read_json(directory / "preflight.json")
    expected = {"schema": "INV-01-07-clock-c1-preflight-v1",
                "planned_payload_bytes": 226962048, "maximum_owned_buffer_bytes": 40759456,
                "buffer_limit_bytes": 268435456, "payload_limit_bytes": 536870912,
                "probe_stack_buffer_reserve_bytes": 1048576}
    require(type(preflight) is dict
            and set(preflight) == set(expected) | {"named_stack_buffer_upper_bound_bytes"}
            and all(preflight[name] == value for name, value in expected.items())
            and all(type(preflight[name]) is int for name in preflight if name != "schema")
            and 0 < preflight["named_stack_buffer_upper_bound_bytes"] <= 1048576,
            "resource-preflight")
    if not complete:
        require(not (directory / "resources.json").exists()
                and not (directory / "execution.json").exists(), "failure-with-completed-record")
        return
    resources = artifact.read_json(directory / "resources.json")
    omitted = 16 if mode == "omit-clock" else 24 if mode == "omit-contention" else 0
    expected_resources = {"schema": "INV-01-07-clock-c1-resources-v1",
                          "peak_heap_global_buffer_bytes": 39710880,
                          "peak_owned_buffer_upper_bound_bytes": 40759456,
                          "probe_stack_buffer_reserve_bytes": 1048576,
                          "array_payload_bytes": 226962048 - omitted,
                          "remaining_heap_global_buffer_bytes": 1310880}
    require(type(resources) is dict and resources == expected_resources
            and all(type(resources[name]) is int for name in expected_resources if name != "schema"),
            "resource-complete")
    binaries = list(directory.glob("*.bin"))
    require(len(binaries) == 80 and all(p.is_file() and not p.is_symlink() for p in binaries)
            and sum(p.stat().st_size for p in binaries) == 226962048 - omitted,
            "resource-payload")


def inspect_child(directory, root, seed, mode, terminal):
    if mode in ("zero-nsec", "pthread-error"):
        resource_records(directory, mode, complete=False)
        return failure_control(directory, mode, seed, terminal)
    require(terminal == {"exit_status": 0, "signal": None, "timeout": False, "complete": True},
            "child-not-completed")
    resource_records(directory, mode, complete=True)
    execution_order(directory, seed)
    producer = artifact.read_json(directory / "record.json")
    require(producer.get("mode") == mode and type(producer.get("seed")) is int
            and producer["seed"] == seed, "producer-command-mismatch")
    target = omitted_shape(producer, mode) if mode.startswith("omit-") else None
    expected_error = {"backward": "clock-backward", "global-inversion": "global-trace-backward",
                      "omit-clock": "array-shape", "omit-contention": "array-shape"}
    try:
        record, hashes = artifact.load_child(str(directory), str(root), str(REPOSITORY))
        findings = cells.validate_child(record)
    except core.Invalid as error:
        if mode == "normal":
            raise
        require(mode in expected_error and str(error) == expected_error[mode], "unexpected-control-rejection")
        return {"status": "EXPECTED_CONTROL_FAILURE", "mode": mode, "observed": str(error),
                "omission_target": target, "evidence_approval": False}
    require(mode == "normal", "injection-not-detected")
    return {"status": "STRUCTURAL_PASS", "findings": findings,
            "array_sha256": hashes, "evidence_approval": False}


def environment_snapshot():
    commands = {
        "os": ["/usr/bin/sw_vers"], "uname": ["/usr/bin/uname", "-a"],
        "hardware": ["/usr/sbin/sysctl", "hw.model", "hw.ncpu", "hw.physicalcpu", "hw.memsize", "hw.pagesize"],
        "power": ["/usr/bin/pmset", "-g", "batt"], "thermal": ["/usr/bin/pmset", "-g", "therm"],
        "load": ["/usr/bin/uptime"], "sdk": ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
    }
    result = {}
    for key, args in commands.items():
        value = command(args)
        require(value.returncode == 0, "environment-capture:" + key)
        result[key] = {"command": args, "status": value.returncode,
                       "stdout": value.stdout.decode("utf-8", errors="strict"),
                       "stderr": value.stderr.decode("utf-8", errors="strict")}
    return result


def environment_identity(record):
    return {key: record[key] for key in ("os", "uname", "hardware", "sdk")}


def hash_tree(directory):
    hashes = {}
    total = 0
    for path in sorted(directory.rglob("*")):
        require(not path.is_symlink(), "output-symlink")
        if path.is_dir():
            continue
        require(path.is_file(), "output-not-regular")
        size = path.stat().st_size
        require(size <= artifact.PAYLOAD_LIMIT, "output-file-cap")
        total += size
        require(total <= 10 * artifact.PAYLOAD_LIMIT, "campaign-payload-cap")
        hashes[str(path.relative_to(directory))] = file_digest(path)
    return hashes


def campaign(args):
    require(args.no_other_native_benchmarks is True, "native-exclusivity-not-asserted")
    root = external_root(os.environ.get("NATEMU_RAW_ROOT"))
    output = new_destination(args.output, root)
    require(EXPECTED_ARRAY_BYTES <= artifact.PAYLOAD_LIMIT, "preflight-payload-cap")
    require(shutil.disk_usage(root).free >= len(SCHEDULE) * (artifact.PAYLOAD_LIMIT + 4 * 1024 * 1024),
            "insufficient-disk-space")
    with CampaignLock():
        gate, executable = load_gate(args.gate, root)
        output.mkdir(mode=0o700)
        rows = [{"index": i, "seed": seed, "mode": mode, "status": "NOT_RUN"}
                for i, (seed, mode) in enumerate(SCHEDULE)]
        write_json(output / "plan.json", {"schema": "INV-01-07-clock-c1-campaign-v1",
                   "schedule": rows, "gate": gate, "gate_sha256": file_digest(Path(args.gate)),
                   "expected_array_bytes_per_complete_child": EXPECTED_ARRAY_BYTES,
                   "child_watchdog_seconds": CHILD_SECONDS, "native_overlap_operator_assertion": True,
                   "evidence_approval": False})
        error = None
        try:
            before = environment_snapshot()
            write_json(output / "environment-before.json", before)
            for row in rows:
                repeated_gate, repeated_exe = load_gate(args.gate, root)
                require(repeated_gate == gate and repeated_exe == executable, "gate-changed")
                require(shutil.disk_usage(root).free >= artifact.PAYLOAD_LIMIT + 4 * 1024 * 1024,
                        "disk-space-lost")
                current = environment_snapshot()
                require(environment_identity(current) == environment_identity(before), "environment-identity-changed")
                stem = f'{row["index"]:02d}-{row["mode"]}-{row["seed"]}'
                write_json(output / (stem + ".environment-before.json"), current)
                directory = output / stem
                row["status"] = "STARTED"
                terminal = supervise(executable, directory, row["seed"], row["mode"], output, root)
                row["process"] = terminal
                after = environment_snapshot()
                write_json(output / (stem + ".environment-after.json"), after)
                require(environment_identity(after) == environment_identity(before), "environment-identity-changed")
                post_gate, post_exe = load_gate(args.gate, root)
                require(post_gate == gate and post_exe == executable, "gate-changed-during-child")
                result = inspect_child(directory, root, row["seed"], row["mode"], terminal)
                serialized = json.dumps(result, default=check.encode, sort_keys=True, allow_nan=False).encode() + b"\n"
                write_new(directory / "check.json", serialized)
                row["status"] = result["status"]
                write_json(output / (stem + ".receipt.json"), row)
        except BaseException as caught:
            error = public_error(caught)
            for row in rows:
                if row["status"] == "STARTED":
                    row["status"] = "UNRESOLVED" if isinstance(caught, core.Unresolved) else "INVALID"
            diagnostic(output, "campaign-error", caught)
            write_json(output / "campaign-failure.json", error)
        # One final immutable inventory, including failures/partial output.
        complete = error is None and all(row["status"] == (
            "STRUCTURAL_PASS" if row["mode"] == "normal" else "EXPECTED_CONTROL_FAILURE") for row in rows)
        try:
            hashes = hash_tree(output)
            hashing_complete = True
        except (core.Invalid, OSError, ValueError) as caught:
            complete = False
            hashing_complete = False
            hashes = {}
            error = public_error(caught)
            diagnostic(output, "inventory-error", caught)
            write_json(output / "inventory-failure.json", error)
        public_rows = rows if complete else [{**row, "status": "CHECKED_NOT_PROMOTED"
                                               if row["status"] == "STRUCTURAL_PASS" else row["status"]} for row in rows]
        write_json(output / "campaign.json", {"status": "COMPLETE_STRUCTURAL_CAMPAIGN" if complete else "INCOMPLETE",
                   "children": public_rows, "failure": error, "sha256": hashes, "hashing_complete": hashing_complete,
                   "evidence_approval": False, "replication_or_result_review": False})
        return 0 if complete else 1


def build(args):
    """Compiler/signing/synthetic validation only; no execution of clock_c1."""
    root = external_root(os.environ.get("NATEMU_RAW_ROOT"))
    output = new_destination(args.output, root)
    # Method is accepted before authoring/building; code196 is deliberately
    # not required to create the binary/disassembly which it must inspect.
    review_record(REPOSITORY / REVIEWS["194"], "194", file_digest(REPOSITORY / REVIEWS["194"]),
                  independent=True)
    sources = source_files()
    require(git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", SOURCE_PREFIX) == "",
            "build-source-not-committed-clean")
    source_revision = revision()
    output.mkdir(mode=0o700)
    calls = []
    def run(label, argv, timeout=60):
        calls.append({"stage": label, "argv": argv})
        result = command(argv, timeout=timeout, cwd=REPOSITORY)
        write_new(output / (label + ".stdout"), result.stdout)
        write_new(output / (label + ".stderr"), result.stderr)
        require(result.returncode == 0, "build-stage:" + label)
        return result.stdout.decode("utf-8", errors="strict").strip()
    try:
        sdk = str(Path(run("sdk", ["xcrun", "--sdk", "macosx", "--show-sdk-path"])).resolve(strict=True))
        run("sdk-version", ["xcrun", "--sdk", "macosx", "--show-sdk-version"])
        compiler = run("compiler", ["xcrun", "--sdk", "macosx", "clang", "--version"])
        run("validation", [sys.executable, "-B", "-m", "unittest", "discover", "-s",
                           SOURCE_PREFIX.rstrip("/"), "-p", "test_*.py", "-v"], timeout=240)
        run("compile", ["xcrun", "--sdk", "macosx", "clang", "-std=c11", "-O2", "-g",
                        "-Wall", "-Wextra", "-Werror", "-Wno-deprecated-declarations", "-pthread",
                        "-isysroot", sdk,
                        '-DC1_REPO="' + str(REPOSITORY) + '"',
                        str(REPOSITORY / SOURCE_PREFIX / "producer.c"), "-o", str(output / "clock_c1")])
        run("sign", ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(output / "clock_c1")])
        run("signature", ["/usr/bin/codesign", "-d", "--verbose=4", str(output / "clock_c1")])
        run("entitlements", ["/usr/bin/codesign", "-d", "--entitlements", ":-", str(output / "clock_c1")])
        run("disassembly", ["xcrun", "otool", "-tvV", str(output / "clock_c1")])
        header_names = list(SDK_HEADERS)
        if (Path(sdk) / "usr/include/sys/_types/_clockid_t.h").exists():
            header_names.append("sys/_types/_clockid_t.h")
        headers = {str(Path(sdk) / "usr/include" / name): file_digest(Path(sdk) / "usr/include" / name)
                   for name in header_names}
        require(sources == source_files() and revision() == source_revision, "build-source-changed")
        require(git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", SOURCE_PREFIX) == "",
                "build-source-became-dirty")
        write_json(output / "build.json", {"schema": BUILD_SCHEMA, "source_files": sources,
                   "source_revision": source_revision, "files": hash_tree(output), "commands": calls,
                   "sdk": sdk, "compiler": compiler, "headers": headers,
                   "validation_exit": 0, "native_executed": False})
        return 0
    except BaseException as error:
        diagnostic(output, "build-error", error)
        write_json(output / "build-failure.json", {"error": public_error(error),
                   "commands": calls, "source_files": sources, "native_executed": False})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("build", "campaign"):
        item = sub.add_parser(name)
        item.add_argument("output", help="new canonical absolute destination below NATEMU_RAW_ROOT")
        if name == "campaign":
            item.add_argument("--gate", required=True, help="exact external reviewed gate JSON")
            item.add_argument("--no-other-native-benchmarks", action="store_true")
    args = parser.parse_args(argv)
    try:
        return build(args) if args.action == "build" else campaign(args)
    except Exception as error:
        print(json.dumps({"status": "REFUSED_OR_FAILED", "error": public_error(error),
                          "evidence_approval": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
