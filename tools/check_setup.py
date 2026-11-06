#!/usr/bin/env python3
"""Read-only local prerequisite check; run with python3 -B tools/check_setup.py.

Exit 0: checked local prerequisites present. Exit 1: missing/unsupported or
unverifiable prerequisite. Neither status establishes compilation, VM admission,
FEX integration or runtime conformance. No installs, builds or VM/container runs.
"""

import os
from pathlib import Path
import platform
import shutil
import subprocess
import time


REPO = Path(__file__).resolve().parents[1]
TARGETS = ("aarch64-apple-darwin", "aarch64-unknown-none")
COMMAND_TIMEOUT = 3.0
QUERY_BUDGET = 20.0


def query(argv, deadline):
    """Bound read-only queries and disable rustup's implicit toolchain install."""
    timeout = min(COMMAND_TIMEOUT, deadline - time.monotonic())
    if timeout <= 0:
        return None, "query budget exhausted"
    env = dict(os.environ, RUSTUP_AUTO_INSTALL="0")
    try:
        result = subprocess.run(
            argv, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "query timed out"
    except OSError as error:
        return None, str(error)
    if result.returncode:
        return None, "exit {}: {}".format(
            result.returncode, result.stderr.strip()[:300] or "no diagnostic")
    return result.stdout.strip(), None


def external_root(value, repo=REPO):
    """Resolve symlinks before enforcing the repository output boundary."""
    root = Path(value).expanduser().resolve()
    repo = repo.resolve()
    if root == repo or repo in root.parents:
        raise ValueError("raw root is inside the repository (after resolving symlinks)")
    return root


def installed_targets(output):
    # `--installed` returns bare triples; also accept rustup's annotated listing.
    return {
        line.split()[0] for line in output.splitlines()
        if line.strip() and (
            len(line.split()) == 1 or line.strip().endswith("(installed)"))
    }


def main():
    failed = []
    deadline = time.monotonic() + QUERY_BUDGET

    def report(ok, label, detail):
        print("{} {}: {}".format("OK" if ok else "MISSING", label, detail))
        if not ok:
            failed.append(label)

    print("Local prerequisites only; no build, VM, container or download is performed.")
    system, machine = platform.system(), platform.machine()
    version = platform.mac_ver()[0] if system == "Darwin" else platform.release()
    report(system == "Darwin" and machine == "arm64" and version.split(".")[0] == "26",
           "host", "{} {} {}; current scope is native arm64 macOS 26".format(
               system, version, machine))

    paths = {}
    for name in ("clang", "xcrun", "codesign", "rustc", "cargo", "rustup"):
        paths[name] = shutil.which(name)
        report(bool(paths[name]), name, paths[name] or "not on PATH")

    if paths["clang"]:
        output, error = query([paths["clang"], "--version"], deadline)
        report(bool(output) and error is None, "clang query",
               error or (output.splitlines()[0] if output else "empty version"))
    if paths["xcrun"]:
        for option, label in (("--show-sdk-path", "selected macOS SDK"),
                              ("--show-sdk-version", "SDK version")):
            output, error = query([paths["xcrun"], "--sdk", "macosx", option], deadline)
            valid = bool(output) and error is None
            if option == "--show-sdk-path" and valid:
                valid = Path(output).is_dir()
            report(valid, label, error or output or "no selected SDK")
    print("INFO SDK 26.4 is the recorded reference, not an absolute version gate; "
          "SDK changes need actual build/runtime admission.")

    if paths["rustup"]:
        active, error = query([paths["rustup"], "show", "active-toolchain"], deadline)
        report(bool(active) and error is None, "active Rust toolchain",
               error or active or "none")
        if active and error is None:
            toolchain = active.split()[0]
            output, error = query([paths["rustup"], "target", "list", "--installed",
                                   "--toolchain", toolchain], deadline)
            report(output is not None, "Rust target query", error or "completed")
            present = installed_targets(output or "")
            for target in TARGETS:
                report(target in present, target,
                       "installed" if target in present else "not confirmed installed")
    print("INFO rustc/cargo checks report PATH presence only; toolchain usability "
          "and compilation remain unverified.")

    value = os.environ.get("NATEMU_RAW_ROOT")
    if not value:
        report(False, "NATEMU_RAW_ROOT", "unset; suggested durable sibling: {} "
               "(not created or selected)".format(REPO.parent / "natemu-raw"))
    else:
        try:
            root = external_root(value)
            if not root.is_dir():
                report(False, "NATEMU_RAW_ROOT", "{} is not an existing directory".format(root))
            else:
                writable = os.access(str(root), os.W_OK | os.X_OK)
                report(writable, "NATEMU_RAW_ROOT",
                       "{}; writable/searchable={}".format(root, writable))
                free = shutil.disk_usage(str(root)).free
                print("INFO raw-root disk free: {:.2f} GiB (no capacity threshold; "
                      "permissions checked without a write probe)".format(free / 1024**3))
        except (OSError, RuntimeError, ValueError) as error:
            report(False, "NATEMU_RAW_ROOT", str(error))

    docker = shutil.which("docker")
    print("INFO Docker CLI: {}; historical FEX Linux exporter only; "
          "daemon, dependencies and inputs are unverified".format(docker or "not on PATH"))
    print("UNVERIFIED FEX outputs, main macOS adapter, signing/VM entitlement "
          "feasibility, compilation and runtime conformance.")
    print("RESULT {}".format(
        "local prerequisites incomplete: " + ", ".join(failed) if failed else
        "checked local prerequisites present; this is not full runtime readiness"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
