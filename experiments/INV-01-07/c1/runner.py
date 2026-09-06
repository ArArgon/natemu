#!/usr/bin/env python3
"""M07-C1 build-only pipeline and fixed-campaign supervisor.

Review is a human workflow prerequisite. Runtime validates retained build
integrity, child identity and controls; it does not interpret approval prose.
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
import uuid
import platform
import socket

import artifact
import cells
import check
import core

REPOSITORY = artifact.repository()
SOURCE_PREFIX = "experiments/INV-01-07/c1/"
MODES = ("backward", "zero-nsec", "omit-clock", "omit-contention",
         "pthread-error", "global-inversion")
SCHEDULE = tuple((seed, "normal") for seed in (70701, 70702, 70703)) + tuple(
    (70701, mode) for mode in MODES)
CHILD_SECONDS = 300
EXPECTED_ARRAY_BYTES = 226962048
BUILD_SCHEMA = "INV-01-07-clock-c1-build-v2"
CAMPAIGN_SCHEMA = "INV-01-07-clock-c1-campaign-v2"
FAILURE_SCHEMA = "INV-01-07-clock-c1-failure-v1"
SDK_HEADERS = ("mach/mach_time.h", "time.h", "pthread.h", "pthread/qos.h", "sys/qos.h",
               "CommonCrypto/CommonDigest.h")
HEX = re.compile(r"[0-9a-f]{64}\Z")
PUBLIC_CODES = frozenset("""
build-fields build-status-or-source build-artifact-inventory build-identity
build-artifact-path build-artifact-hash build-changed build-changed-during-child timebase-changed
producer-not-executable unresolved-revision unresolved-repository git-command-failed
source-path source-symlink incomplete-source-inventory missing-NATEMU_RAW_ROOT
repository-internal-root destination-type destination-absolute
destination-parent-canonical destination-outside-root destination-exists
native-exclusivity-not-asserted preflight-payload-cap insufficient-disk-space
disk-space-lost environment-identity-changed
child-destination-exists child-directory-changed child-not-completed
producer-command-mismatch unexpected-control-rejection control-wrong-process-status
failure-fields failure-identity control-wrong-failure failure-domain
control-wrong-pthread-injection control-wrong-zero-nsec-status failure-with-completed-record
control-wrong-omission-size control-wrong-omission-target execution-fields
execution-identity execution-order resource-preflight resource-complete resource-payload
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


def snapshot_sources(output):
    source = REPOSITORY / SOURCE_PREFIX
    destination = output / "sources"
    destination.mkdir()
    for path in sorted(source.iterdir()):
        if path.suffix in (".py", ".c"):
            require(path.resolve(strict=True) == path, "source-symlink")
            write_new(destination / path.name, artifact.read_regular(path, artifact.PAYLOAD_LIMIT))
    require((destination / "producer.c").is_file() and (destination / "runner.py").is_file(),
            "incomplete-source-inventory")
    write_json(destination / "source-context.json", {"repository": str(REPOSITORY)})
    return destination


def load_build(path, root):
    directory = artifact.canonical_directory(path, "build-directory")
    require(directory != root and directory.is_relative_to(root), "build-outside-root")
    record = artifact.read_json(directory / "build.json")
    cells.fields(record, ("schema", "build_id", "files", "commands", "sdk", "compiler",
                          "headers", "validation_exit", "native_executed"), "build-fields")
    require(record["schema"] == BUILD_SCHEMA and record["validation_exit"] == 0
            and type(record["validation_exit"]) is int and record["native_executed"] is False,
            "build-status-or-source")
    require(type(record["build_id"]) is str and re.fullmatch(r"[0-9a-f]{32}", record["build_id"]),
            "build-identity")
    require(type(record["files"]) is dict and {"clock_c1", "sources/producer.c", "sources/runner.py", "sources/source-context.json",
            "sources/artifact.py", "sources/core.py", "sources/cells.py", "sources/check.py", "sources/clock_evidence.py",
            "disassembly.stdout", "validation.stdout", "validation.stderr"}.issubset(record["files"]),
            "build-artifact-inventory")
    for name, expected in record["files"].items():
        item = directory / name
        require(type(name) is str and not Path(name).is_absolute() and ".." not in Path(name).parts
                and item.resolve(strict=True) == item, "build-artifact-path")
        require(type(expected) is str and HEX.fullmatch(expected) and file_digest(item) == expected,
                "build-artifact-hash")
    require(os.access(directory / "clock_c1", os.X_OK), "producer-not-executable")
    return record, directory / "clock_c1"


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
    """Only called by campaign after retained build checks; tests use a mocked process."""
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
        build_record, executable = load_build(args.build, root)
        run_id = uuid.uuid4().hex
        build_id = build_record["build_id"]
        output.mkdir(mode=0o700)
        rows = [{"index": i, "seed": seed, "mode": mode, "status": "NOT_RUN", "run_id": run_id, "build_id": build_id}
                for i, (seed, mode) in enumerate(SCHEDULE)]
        write_json(output / "plan.json", {"schema": CAMPAIGN_SCHEMA, "run_id": run_id, "build_id": build_id,
                   "schedule": rows, "build_directory": args.build, "build": build_record,
                   "expected_array_bytes_per_complete_child": EXPECTED_ARRAY_BYTES,
                   "child_watchdog_seconds": CHILD_SECONDS, "native_overlap_operator_assertion": True,
                   "evidence_approval": False})
        error = None
        context = {}
        try:
            before = environment_snapshot()
            write_json(output / "environment-before.json", before)
            context = {"host_id": socket.gethostname(), "architecture": platform.machine(),
                       "host_model": command(["/usr/sbin/sysctl", "-n", "hw.model"]).stdout.decode().strip(),
                       "os_build": command(["/usr/bin/sw_vers", "-buildVersion"]).stdout.decode().strip()}
            for row in rows:
                repeated_build, repeated_exe = load_build(args.build, root)
                require(repeated_build == build_record and repeated_exe == executable, "build-changed")
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
                write_json(directory / "identity.json", {"run_id": run_id, "build_id": build_id,
                           "index": row["index"], "seed": row["seed"], "mode": row["mode"]})
                after = environment_snapshot()
                write_json(output / (stem + ".environment-after.json"), after)
                require(environment_identity(after) == environment_identity(before), "environment-identity-changed")
                post_build, post_exe = load_build(args.build, root)
                require(post_build == build_record and post_exe == executable, "build-changed-during-child")
                result = inspect_child(directory, root, row["seed"], row["mode"], terminal)
                serialized = json.dumps(result, default=check.encode, sort_keys=True, allow_nan=False).encode() + b"\n"
                write_new(directory / "check.json", serialized)
                row["status"] = result["status"]
                if row["mode"] == "normal":
                    timebase = artifact.read_json(directory / "record.json")["timebase"]
                    timebase = {key: timebase[key] for key in ("numer", "denom")}
                    require("timebase" not in context or context["timebase"] == timebase, "timebase-changed")
                    context["timebase"] = timebase
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
        write_json(output / "campaign.json", {"schema": CAMPAIGN_SCHEMA, "run_id": run_id,
                   "build_id": build_id, "context": context, "status": "COMPLETE_STRUCTURAL_CAMPAIGN" if complete else "INCOMPLETE",
                   "children": public_rows, "failure": error, "sha256": hashes, "hashing_complete": hashing_complete,
                   "evidence_approval": False, "replication_or_result_review": False})
        return 0 if complete else 1


def build(args):
    """Compiler/signing/synthetic validation only; no execution of clock_c1."""
    root = external_root(os.environ.get("NATEMU_RAW_ROOT"))
    output = new_destination(args.output, root)
    output.mkdir(mode=0o700)
    sources = output / "sources"
    calls = []
    def run(label, argv, timeout=60):
        calls.append({"stage": label, "argv": argv})
        result = command(argv, timeout=timeout, cwd=sources)
        write_new(output / (label + ".stdout"), result.stdout)
        write_new(output / (label + ".stderr"), result.stderr)
        require(result.returncode == 0, "build-stage:" + label)
        return result.stdout.decode("utf-8", errors="strict").strip()
    try:
        snapshot_sources(output)
        sdk = str(Path(run("sdk", ["xcrun", "--sdk", "macosx", "--show-sdk-path"])).resolve(strict=True))
        run("sdk-version", ["xcrun", "--sdk", "macosx", "--show-sdk-version"])
        compiler = run("compiler", ["xcrun", "--sdk", "macosx", "clang", "--version"])
        run("validation", [sys.executable, "-B", "-m", "unittest", "discover", "-s",
                           str(sources), "-p", "test_*.py", "-v"], timeout=240)
        run("compile", ["xcrun", "--sdk", "macosx", "clang", "-std=c11", "-O2", "-g",
                        "-Wall", "-Wextra", "-Werror", "-Wno-deprecated-declarations", "-pthread",
                        "-isysroot", sdk,
                        '-DC1_REPO="' + str(REPOSITORY) + '"',
                        str(sources / "producer.c"), "-o", str(output / "clock_c1")])
        run("sign", ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(output / "clock_c1")])
        run("signature", ["/usr/bin/codesign", "-d", "--verbose=4", str(output / "clock_c1")])
        run("entitlements", ["/usr/bin/codesign", "-d", "--entitlements", ":-", str(output / "clock_c1")])
        run("disassembly", ["xcrun", "otool", "-tvV", str(output / "clock_c1")])
        header_names = list(SDK_HEADERS)
        if (Path(sdk) / "usr/include/sys/_types/_clockid_t.h").exists():
            header_names.append("sys/_types/_clockid_t.h")
        headers = {str(Path(sdk) / "usr/include" / name): file_digest((Path(sdk) / "usr/include" / name).resolve())
                   for name in header_names}
        # Retain the selected SDK declarations as recoverable build inputs.
        for name in header_names:
            target = output / "sdk-headers" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            write_new(target, artifact.read_regular((Path(sdk) / "usr/include" / name).resolve(), artifact.PAYLOAD_LIMIT))
        write_json(output / "build.json", {"schema": BUILD_SCHEMA, "build_id": uuid.uuid4().hex,
                   "files": hash_tree(output), "commands": calls,
                   "sdk": sdk, "compiler": compiler, "headers": headers,
                   "validation_exit": 0, "native_executed": False})
        return 0
    except BaseException as error:
        diagnostic(output, "build-error", error)
        try:
            files, hashing_complete = hash_tree(output), True
        except (OSError, ValueError, core.Invalid):
            files, hashing_complete = {}, False
        write_json(output / "build-failure.json", {"schema": "INV-01-07-clock-c1-build-failure-v2",
                   "error": public_error(error), "files": files, "hashing_complete": hashing_complete,
                   "commands": calls, "source_snapshot": str(sources), "native_executed": False})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("build", "campaign"):
        item = sub.add_parser(name)
        item.add_argument("output", help="new canonical absolute destination below NATEMU_RAW_ROOT")
        if name == "campaign":
            item.add_argument("--build", required=True, help="retained v2 build directory")
            item.add_argument("--no-other-native-benchmarks", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "campaign":
            _, executable = load_build(args.build, external_root(os.environ.get("NATEMU_RAW_ROOT")))
            retained_runner = executable.parent / "sources" / "runner.py"
            if Path(__file__).resolve() != retained_runner:
                os.execv(sys.executable, [sys.executable, "-B", str(retained_runner), "campaign",
                         args.output, "--build", args.build,
                         *(["--no-other-native-benchmarks"] if args.no_other_native_benchmarks else [])])
        return build(args) if args.action == "build" else campaign(args)
    except Exception as error:
        print(json.dumps({"status": "REFUSED_OR_FAILED", "error": public_error(error),
                          "evidence_approval": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
