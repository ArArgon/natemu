"""Build/MIG-generate/disassemble only; never executes the resulting binary.

Pipeline:
  1. Retain the selected SDK mach_exc.defs and source/build inputs.
  2. Run MIG with every MACH_EXC_SERVER_* extension off and
     KERNEL_USER/KERNEL_SERVER/KOBJECT_SERVER false, arm64 target.
  3. Grep the generated dispatcher for the literal routine-id declaration to
     verify mach_exception_raise_state is routine 2406.
  4. Freeze the exact request/reply wire layout by (a) a small Python model
     of MIG's `#pragma pack(push, 4)` field placement and (b) a `-fsyntax-
     only` C translation unit that `_Static_assert`s every offset/size the
     Python model computed against the *actual* generated headers -- if the
     two disagree, the build fails loudly rather than shipping a guessed
     layout. Neither step compiles or runs a linked binary.
  5. Compile src/state_server.c with the generated MIG sources into a real
     binary and disassemble the fault-site load/store and the retry path.
     The binary is written to disk and never executed here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import shutil
import plistlib
import uuid

from run import HERE, REPO, raw_root, snapshot_sources, file_manifest, sha, write_new, command


MIG_DEFINES = [
    "-DKERNEL_USER=0", "-DKERNEL_SERVER=0", "-DKOBJECT_SERVER=0",
    "-DMACH_EXC_SERVER_SECTOKEN=0", "-DMACH_EXC_SERVER_AUDITTOKEN=0",
    "-DMACH_EXC_SERVER_TASKIDTOKEN=0", "-DMACH_EXC_SERVER_BACKTRACE=0",
    "-DMACH_EXC_SERVER_TASKIDTOKEN_STATE=0", "-DSANDBOX_COMPILER=0",
    "-DTypeCheck=1",
]

# ---- frozen wire layout, computed then cross-checked against the actual
# generated headers via -fsyntax-only _Static_assert (never a linked/run
# binary). (size, alignment) for every field type that appears; MIG's
# `#pragma pack(push, 4)` caps every field's alignment at 4 bytes.
FIELD_TYPES = {
    "mach_msg_header_t": (24, 4),
    "NDR_record_t": (8, 1),
    "exception_type_t": (4, 4),
    "mach_msg_type_number_t": (4, 4),
    "int64_t[2]": (16, 8),
    "int": (4, 4),
    "natural_t[1296]": (5184, 4),
}
MIG_PACK = 4


def layout_of(fields):
    offset = 0
    laid_out = []
    for name, typename in fields:
        size, align = FIELD_TYPES[typename]
        align = min(align, MIG_PACK)
        offset = (offset + align - 1) // align * align
        laid_out.append(dict(field=name, type=typename, offset=offset, size=size))
        offset += size
    return laid_out, offset


REQUEST_FIELDS = [
    ("Head", "mach_msg_header_t"), ("NDR", "NDR_record_t"),
    ("exception", "exception_type_t"), ("codeCnt", "mach_msg_type_number_t"),
    ("code", "int64_t[2]"), ("flavor", "int"),
    ("old_stateCnt", "mach_msg_type_number_t"), ("old_state", "natural_t[1296]"),
]
REPLY_FIELDS = [
    ("Head", "mach_msg_header_t"), ("NDR", "NDR_record_t"),
    ("RetCode", "int"), ("flavor", "int"),
    ("new_stateCnt", "mach_msg_type_number_t"), ("new_state", "natural_t[1296]"),
]


def wire_layout():
    req_fields, req_size = layout_of(REQUEST_FIELDS)
    rep_fields, rep_size = layout_of(REPLY_FIELDS)
    return dict(
        routine_id=2406, routine_name="mach_exception_raise_state", subsystem=2405,
        request=dict(sizeof=req_size, fields=req_fields),
        reply=dict(sizeof=rep_size, fields=rep_fields),
        ndr_record_sizeof=8, mach_msg_header_sizeof=24,
        arm_thread_state64_count=68, arm_thread_state64_flavor=6,
        exc_bad_access=1, exception_state_behavior=2, mach_exception_codes_flag="0x80000000",
        mig_pack=MIG_PACK,
        source_of_truth="generated mach_excServer.h/.c from the selected SDK's mach_exc.defs; "
                        "cross-checked at build time by _Static_assert/offsetof against those "
                        "exact generated headers via -fsyntax-only (never a linked/run binary)",
    )


def static_assert_source(layout):
    lines = ['#include "mach_excServer.h"', "#include <stddef.h>", ""]
    for name, fields, typename in (
            ("Request", layout["request"]["fields"], "__Request__mach_exception_raise_state_t"),
            ("Reply", layout["reply"]["fields"], "__Reply__mach_exception_raise_state_t")):
        lines.append(f'_Static_assert(sizeof({typename}) == {layout[name.lower()]["sizeof"]}, "{name.lower()} size");')
        for f in fields:
            lines.append(f'_Static_assert(offsetof({typename}, {f["field"]}) == {f["offset"]}, "{name.lower()}.{f["field"]}");')
        lines.append("")
    lines.append(f'_Static_assert(sizeof(NDR_record_t) == {layout["ndr_record_sizeof"]}, "ndr size");')
    lines.append(f'_Static_assert(sizeof(mach_msg_header_t) == {layout["mach_msg_header_sizeof"]}, "header size");')
    lines.append('#include <mach/arm/thread_status.h>')
    lines.append(f'_Static_assert(ARM_THREAD_STATE64_COUNT == {layout["arm_thread_state64_count"]}, "state count");')
    lines.append(f'_Static_assert(ARM_THREAD_STATE64 == {layout["arm_thread_state64_flavor"]}, "state flavor");')
    lines.append('#include <mach/exception_types.h>')
    lines.append(f'_Static_assert(EXC_BAD_ACCESS == {layout["exc_bad_access"]}, "exc bad access");')
    lines.append(f'_Static_assert(EXCEPTION_STATE == {layout["exception_state_behavior"]}, "exception state behavior");')
    lines.append(f'_Static_assert(MACH_EXCEPTION_CODES == {layout["mach_exception_codes_flag"]}, "mach exception codes flag");')
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sdk", type=Path, help="SDK directory; default xcrun macosx SDK")
    args = parser.parse_args()
    SDK = (args.sdk or Path(command(["xcrun", "--sdk", "macosx", "--show-sdk-path"]))).resolve(strict=True)
    DEFS = SDK / "usr/include/mach/mach_exc.defs"
    root = raw_root()
    output = args.output
    if not output.is_absolute() or output.parent.resolve(strict=True) != output.parent:
        raise SystemExit("build output must be canonical and absolute")
    if output == REPO or REPO in output.parents or not root in output.parents:
        raise SystemExit("build output must be a new path in external NATEMU_RAW_ROOT")
    output.mkdir(parents=True, exist_ok=False)

    commands = []
    try:
        sources = snapshot_sources(output)
        defs = output / "mach_exc.defs"
        with defs.open("xb") as stream:
            stream.write(DEFS.read_bytes())
        # 1. MIG generation.
        mig_argv = (["xcrun", "mig", "-arch", "arm64", "-isysroot", str(SDK)] + MIG_DEFINES +
                    ["-header", str(output / "mach_exc.h"),
                     "-user", str(output / "mach_excUser.c"),
                     "-server", str(output / "mach_excServer.c"),
                     "-sheader", str(output / "mach_excServer.h"),
                     str(defs)])
        with (output / "build.log").open("xb") as log:
            subprocess.run(mig_argv, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
        commands.append(mig_argv)

        # 2. Verify routine id 2406 in the generated dispatcher text.
        dispatcher = (output / "mach_excServer.c").read_text()
        if '__DeclareRcvRpc(2406, "mach_exception_raise_state")' not in dispatcher:
            raise SystemExit("generated dispatcher does not declare routine 2406 for mach_exception_raise_state")

        # 3. Freeze and cross-check the wire layout.
        layout = wire_layout()
        write_new(output / "wire-layout.json", layout)
        assert_src = output / "verify_layout.c"
        assert_src.write_text(static_assert_source(layout))
        verify_argv = ["xcrun", "clang", "-arch", "arm64", "-isysroot", str(SDK), "-fsyntax-only",
                       "-I", str(output), str(assert_src)]
        with (output / "verify_layout.log").open("xb") as log:
            subprocess.run(verify_argv, stdout=log, stderr=subprocess.STDOUT, check=True, cwd=output, timeout=120)
        commands.append(verify_argv)

        # 4. Compile the server binary (not executed).
        binary = output / "state_server"
        cc = (["xcrun", "clang", "-arch", "arm64", "-isysroot", str(SDK), "-O1", "-g", "-std=c11",
               "-Wall", "-Wextra", "-Werror", "-pthread",
               "-I", str(output), str(sources / "src/state_server.c"),
               str(output / "mach_excServer.c"), str(output / "mach_excUser.c"),
               "-o", str(binary)])
        with (output / "compile.log").open("xb") as log:
            subprocess.run(cc, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
        commands.append(cc)

        # Actual local capability cells: plain ad-hoc, and hardened+allow-jit.
        signing = {}
        for name, executable, entitlements in (
                ("adhoc-plain", binary, {}),
                ("adhoc-hardened-allow-jit", output / "state_server-hardened",
                 {"com.apple.security.cs.allow-jit": True})):
            if executable != binary:
                shutil.copy2(binary, executable)
            argv = ["codesign", "--force", "--sign", "-"]
            if entitlements:
                plist = output / "hardened-entitlements.plist"
                plist.write_bytes(plistlib.dumps(entitlements))
                argv += ["--options", "runtime", "--entitlements", str(plist)]
            argv.append(str(executable))
            subprocess.run(argv, check=True, capture_output=True, timeout=30)
            commands.append(argv)
            subprocess.run(["codesign", "--verify", "--strict", str(executable)], check=True, capture_output=True, timeout=30)
            details = subprocess.run(["codesign", "-d", "--verbose=4", "--entitlements", ":-", str(executable)], check=True, capture_output=True, timeout=30)
            raw = details.stdout + details.stderr
            (output / (name + "-signature.txt")).write_bytes(raw)
            start,end = raw.find(b"<?xml"),raw.find(b"</plist>")
            actual = plistlib.loads(raw[start:end+8]) if start>=0 and end>=start else {}
            hardened = b"(runtime)" in raw or b",runtime)" in raw
            if actual != entitlements or hardened != bool(entitlements):
                raise ValueError("actual local signature mismatch")
            signing[name] = dict(binary=executable.name, sha256=sha(executable),
                                 entitlements=actual, hardened_runtime=hardened)

        # 5. Disassemble the fault-site load/store and the retry path.
        disasm = subprocess.run(["xcrun", "llvm-objdump", "-d", "--no-show-raw-insn",
                                  "--disassemble-symbols=_m1_access,_m1_load,_m1_store,_catch_mach_exception_raise_state",
                                  str(binary)], capture_output=True, text=True, check=True, timeout=120)
        (output / "disassembly.txt").write_text(disasm.stdout)
        commands.append(["xcrun", "llvm-objdump", "-d", str(binary)])
        retry_path_pc_unchanged = (
            "memcpy(new_state, old_state" in (sources / "src/state_server.c").read_text() and
            "set_pc" not in (sources / "src/state_server.c").read_text().lower()
        )

        files = file_manifest(output)
        write_new(output / "build-manifest.json", dict(
            schema="INV-01-06-m1-build-v3", signing=signing, build_id=uuid.uuid4().hex, commands=[list(map(str, c)) for c in commands], files=files,
            runtime_executed=False, sdk=str(SDK), mach_exc_defs_sha256=sha(defs), sdk_version=command(["xcrun", "--sdk", str(SDK), "--show-sdk-version"]),
            mig_version=command(["xcrun", "mig", "-version"]),
            compiler=command(["xcrun", "clang", "--version"]),
            routine_id_verified=2406,
            retry_path_pc_unchanged=retry_path_pc_unchanged,
        ))
    except BaseException as error:
        try:
            files, hashing_complete = file_manifest(output), True
        except (OSError, ValueError):
            files, hashing_complete = {}, False
        write_new(output / "build-failure.json", dict(schema="INV-01-06-m1-build-failure-v2",
                  error=dict(type=type(error).__name__, message=str(error)), commands=commands,
                  files=files, hashing_complete=hashing_complete, runtime_executed=False))
        raise
    print(output)


if __name__ == "__main__":
    main()
