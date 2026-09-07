#!/usr/bin/env python3
"""Retain, build, and run the disposable Track 01 experiment. Python stdlib only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import resource
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import uuid

BASE = 0x40000000
MAX_GUEST = 2 * 1024 * 1024


def allocate(root, repo, name):
    if not root:
        raise ValueError("NATEMU_RAW_ROOT is required")
    root = Path(root).expanduser().resolve()
    repo = Path(repo).resolve()
    if root == repo or repo in root.parents:
        raise ValueError("NATEMU_RAW_ROOT must resolve outside the repository")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", name):
        raise ValueError("unsafe run name")
    root.mkdir(parents=True, exist_ok=True)
    run = root / name
    run.mkdir()  # Atomic refusal of any existing run, including a symlink.
    return run


def save(run, record):
    temporary = run / "manifest.tmp"
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    temporary.replace(run / "manifest.json")


def limits(cpu_limit=True):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if cpu_limit:
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024 * 1024,) * 2)


def command(run, record, argv, timeout=30, cpu_limit=True):
    index = len(record["commands"])
    stem = f"command-{index:02d}"
    item = {"argv": list(map(str, argv)), "cwd": str(run),
            "timeout_seconds": timeout, "stdout": stem + ".stdout",
            "stderr": stem + ".stderr", "status": "running",
            "cpu_limit": [60, 60] if cpu_limit else list(resource.getrlimit(resource.RLIMIT_CPU))}
    record["commands"].append(item)
    save(run, record)
    started = time.monotonic()
    child = None
    try:
        with (run / item["stdout"]).open("xb") as out, (run / item["stderr"]).open("xb") as err:
            child = subprocess.Popen(item["argv"], cwd=run, stdout=out, stderr=err,
                                     start_new_session=True, preexec_fn=lambda: limits(cpu_limit))
            item["pid"] = child.pid
            try:
                item["returncode"] = child.wait(timeout=timeout)
                item["status"] = "passed" if child.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                item["status"] = "timeout"
                raise
    except BaseException as exc:
        item["error"] = f"{type(exc).__name__}: {exc}"
        if item["status"] == "running":
            item["status"] = "failed"
        raise
    finally:
        if child is not None:
            # Kill the group even if its leader exited, retaining no background child.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            item["returncode"] = child.returncode
        item["elapsed_seconds"] = time.monotonic() - started
        save(run, record)
    if item["returncode"] != 0:
        raise RuntimeError(f"{argv[0]} exited {item['returncode']}; see {stem}.stderr")
    return (run / item["stdout"]).read_text()


def checksums(run):
    hashes = {}
    for path in sorted(run.rglob("*")):
        if path.is_file() and path.name not in ("SHA256SUMS", "manifest.tmp"):
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[str(path.relative_to(run))] = digest.hexdigest()
    (run / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in hashes.items()))


def extract(macho, output):
    data = macho.read_bytes()
    if len(data) < 32:
        raise ValueError("truncated Mach-O header")
    magic, cpu, _, _, ncmds, sizeofcmds, _, _ = struct.unpack_from("<8I", data)
    if magic != 0xFEEDFACF or cpu != 0x0100000C or 32 + sizeofcmds > len(data):
        raise ValueError("expected bounded little-endian arm64 Mach-O")
    pos = 32
    segments = []
    for _ in range(ncmds):
        if pos + 8 > 32 + sizeofcmds:
            raise ValueError("truncated load command")
        cmd, size = struct.unpack_from("<II", data, pos)
        if size < 8 or pos + size > 32 + sizeofcmds:
            raise ValueError("invalid load command size")
        if cmd == 0x19:
            if size < 72:
                raise ValueError("truncated LC_SEGMENT_64")
            _, _, name, address, memory, offset, length, _, prot, _, _ = struct.unpack_from("<II16sQQQQiiII", data, pos)
            if prot and memory:
                if not (BASE <= address < address + memory <= BASE + MAX_GUEST):
                    raise ValueError("guest segment exceeds reserved 2 MiB")
                if length > memory or offset + length > len(data):
                    raise ValueError("invalid segment file range")
                segments.append((address, memory, offset, length, name.rstrip(b"\0")))
        pos += size
    segments.sort()
    if pos != 32 + sizeofcmds or not segments or segments[0][0] != BASE or segments[0][4] != b"__TEXT":
        raise ValueError("guest must begin with __TEXT at 0x40000000")
    end = BASE
    for address, memory, _, _, _ in segments:
        if address < end:
            raise ValueError("overlapping guest segments")
        end = address + memory
    image = bytearray(end - BASE)
    for address, _, offset, length, _ in segments:
        image[address - BASE:address - BASE + length] = data[offset:offset + length]
    output.write_bytes(image)
    return [{"address": a, "memory_size": m, "file_offset": o, "file_size": s,
             "name": n.decode("ascii")} for a, m, o, s, n in segments]


def build(run, record):
    source = run / "source"
    sdk = command(run, record, ["xcrun", "--show-sdk-path"]).strip()
    sdk_version = command(run, record, ["xcrun", "--show-sdk-version"]).strip()
    record["sdk"] = {"path": sdk, "version": sdk_version}
    for argv in (["sw_vers"], ["uname", "-a"], ["sysctl", "hw.model", "hw.memsize", "hw.ncpu", "machdep.cpu.brand_string"],
                 ["xcrun", "clang", "--version"], ["xcrun", "ld", "-v"]):
        command(run, record, argv)
    guest_flags = ["-target", "arm64-apple-macos26.0", "-isysroot", sdk,
                   "-ffreestanding", "-fno-stack-protector", "-fno-builtin", "-fno-pic",
                   "-mgeneral-regs-only", "-O2", "-Wall", "-Wextra", "-Werror"]
    guest_inputs = ("guest.S", "guest.c", "x86.c", "q30.c", "q30_cases.c", "q31.c", "q32.c", "q33.c", "q34.c")
    for name in guest_inputs:
        command(run, record, ["xcrun", "clang", *guest_flags, "-c", source / name, "-o", run / (name + ".o")])
    command(run, record, ["xcrun", "ld", "-arch", "arm64", "-platform_version", "macos", "26.0", sdk_version,
                          "-e", "_start", "-static", "-pagezero_size", "0", "-segaddr", "__TEXT", hex(BASE),
                          "-o", run / "guest.macho", *(run / (name + ".o") for name in guest_inputs)])
    record["guest_segments"] = extract(run / "guest.macho", run / "guest.bin")
    symbols = command(run, record, ["xcrun", "nm", "-n", run / "guest.macho"])
    entries = [int(line.split()[0], 16) for line in symbols.splitlines() if line.split()[-1:] == ["_start"]]
    if len(entries) != 1 or not BASE <= entries[0] < BASE + (run / "guest.bin").stat().st_size:
        raise ValueError("missing or out-of-range _start")
    entry = hex(entries[0])
    (run / "entry.txt").write_text(entry + "\n")
    command(run, record, ["xcrun", "clang", "-isysroot", sdk, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                          source / "host.c", "-framework", "Hypervisor", "-o", run / "host"])
    command(run, record, ["codesign", "--force", "--sign", "-", "--entitlements", source / "entitlements.plist", run / "host"])
    command(run, record, ["codesign", "--verify", "--verbose=2", run / "host"])
    return entry


def self_check():
    with tempfile.TemporaryDirectory(prefix="natemu-runner-check-") as temporary:
        root = Path(temporary)
        repo = root / "repo"
        repo.mkdir()
        for candidate, name in ((None, "x"), (repo, "x"), (repo / "child", "x"), (root, "../x")):
            try:
                allocate(candidate, repo, name)
            except ValueError:
                pass
            else:
                raise AssertionError("output guard accepted invalid path")
        (root / "alias").symlink_to(repo, target_is_directory=True)
        try:
            allocate(root / "alias", repo, "x")
        except ValueError:
            pass
        else:
            raise AssertionError("repository symlink accepted")
        run = allocate(root, repo, "retained")
        try:
            allocate(root, repo, "retained")
        except FileExistsError:
            pass
        else:
            raise AssertionError("overwrite accepted")
        record = {"commands": []}
        for script, timeout, status in (("print('partial', flush=True); raise SystemExit(7)", 5, "failed"),
                                        ("import time; print('partial', flush=True); time.sleep(20)", 0.2, "timeout")):
            try:
                command(run, record, [sys.executable, "-c", script], timeout)
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            else:
                raise AssertionError("failure did not propagate")
            assert record["commands"][-1]["status"] == status
            assert "partial" in (run / record["commands"][-1]["stdout"]).read_text()
            assert json.loads((run / "manifest.json").read_text())["commands"][-1]["status"] == status
        checksums(run)
        assert "manifest.json" in (run / "SHA256SUMS").read_text()
        for enabled in (True, False):
            result = command(run, record, [sys.executable, "-c",
                "import resource; print(resource.getrlimit(resource.RLIMIT_CPU)); "
                "print(resource.getrlimit(resource.RLIMIT_FSIZE))"], cpu_limit=enabled)
            expected = (60, 60) if enabled else resource.getrlimit(resource.RLIMIT_CPU)
            assert result.splitlines() == [str(expected), str((2 * 1024 * 1024 * 1024,) * 2)]
        header = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 72, 0, 0)
        segment = struct.pack("<II16sQQQQiiII", 0x19, 72, b"__TEXT", BASE, 4096, 0, 104, 7, 5, 0, 0)
        macho, binary = root / "synthetic.macho", root / "synthetic.bin"
        macho.write_bytes(header + segment)
        extract(macho, binary)
        assert binary.read_bytes() == header + segment + bytes(4096 - 104)
        macho.write_bytes(header + segment[:-1])
        try:
            extract(macho, binary)
        except ValueError:
            pass
        else:
            raise AssertionError("truncated Mach-O accepted")
    print("self-check PASS: output guards, failure/timeout retention, Mach-O extraction")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--entry-control", choices=("gic", "plain", "plain-no-tso", "plain-sctlr"), help="stop at first HVC; no foundation finding")
    parser.add_argument("--snapshot", type=Path, help="rebuild sources from a retained source directory")
    parser.add_argument("--name", default="track01-" + uuid.uuid4().hex)
    parser.add_argument("--vcpus", type=int, choices=range(2, 5), default=2)
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0
    if not args.build_only and resource.getrlimit(resource.RLIMIT_CPU) != (resource.RLIM_INFINITY, resource.RLIM_INFINITY):
        parser.error("VM host requires unlimited inherited CPU limits; wall watchdogs bound execution")
    repo = next((p for p in (Path.cwd(), *Path.cwd().parents)
                 if (p / "AGENTS.md").is_file() and (p / "docs/research/README.md").is_file()), None)
    if repo is None:
        parser.error("invoke from the repository (also when rebuilding a retained snapshot)")
    run = allocate(os.environ.get("NATEMU_RAW_ROOT"), repo.resolve(), args.name)
    record = {"format": "natemu-track01-run-v7", "stage": "entry-control" if args.entry_control else "q34startup",
              "run_id": args.name, "status": "preparing",
              "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "platform": platform.platform(), "python": sys.version, "machine": platform.machine(),
              "environment": {k: os.environ[k] for k in ("PATH", "SDKROOT", "DEVELOPER_DIR", "MACOSX_DEPLOYMENT_TARGET", "NATEMU_RAW_ROOT") if k in os.environ},
              "inputs": {"vcpus": args.vcpus, "build_only": args.build_only, "snapshot": str(args.snapshot), "entry_control": args.entry_control,
                         "host_inherit_cpu_limit": True},
              "limits": {"command_wall_seconds": 30, "guest_wall_seconds": 30,
                         "build_child_cpu_seconds": 60,
                         "host_cpu_limit": list(resource.getrlimit(resource.RLIMIT_CPU)),
                         "child_file_bytes": 2 * 1024 * 1024 * 1024}, "commands": []}
    save(run, record)
    print(run, flush=True)
    try:
        source = (args.snapshot or Path(__file__).resolve().parent).resolve()
        if not source.is_dir():
            raise ValueError("snapshot source directory is absent")
        if source == run or source in run.parents or run in source.parents:
            raise ValueError("snapshot source and run directory must not contain each other")
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"source symlink is not a retained local input: {path}")
        shutil.copytree(source, run / "source", ignore=shutil.ignore_patterns("__pycache__"))
        if (run / "source/run.py").read_bytes() != Path(__file__).read_bytes():
            raise ValueError("runner differs from snapshot; invoke the snapshot's run.py from the repository")
        record["source_origin"] = str(source)
        entry = build(run, record)
        record["status"] = "built"
        save(run, record)
        if not args.build_only:
            command(run, record, [run / "host", run / "guest.bin", entry, str(args.vcpus)] +
                    ([args.entry_control] if args.entry_control else []), cpu_limit=False)
            record["status"] = "executed"
    except BaseException as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        print(record["error"], file=sys.stderr)
        return 1
    finally:
        record["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save(run, record)
        checksums(run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
