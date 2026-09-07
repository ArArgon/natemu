#!/usr/bin/env python3
"""Independent, read-only Q30 foundation oracle; no guest execution or imports."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import resource
import struct

BASE, SIZE, SHARED, DATA = 0x40000000, 0x1000000, 0x800000, 0x900000
SENTINEL = 0x13579BDF2468ACE0
TCR = 0x100803520
STRIDE, SHARED_SIZE = 944, 3824
UNLIMITED = [resource.RLIM_INFINITY, resource.RLIM_INFINITY]
FIELDS = ("ready done failure tso_start tso_end sctlr tcr mmfr0 sync_count "
          "timer_count sgi_count esr far elr alias_value trace_count ticks_start "
          "ticks_end timer_intid redistributor waker_before waker_after").split()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def u64(data, offset):
    return struct.unpack_from("<Q", data, offset)[0]


def symbols(macho):
    """Read retained Mach-O nlist_64 directly, independently of runner/nm logs."""
    require(len(macho) >= 32, "Mach-O header")
    magic, cpu, _, _, count, size, _, _ = struct.unpack_from("<8I", macho)
    require((magic, cpu) == (0xFEEDFACF, 0x100000C), "arm64 Mach-O identity")
    end, pos, table = 32 + size, 32, None
    require(end <= len(macho), "Mach-O commands bounds")
    for _ in range(count):
        require(pos + 8 <= end, "Mach-O command header")
        kind, length = struct.unpack_from("<II", macho, pos)
        require(length >= 8 and pos + length <= end, "Mach-O command bounds")
        if kind == 2:
            require(table is None and length == 24, "single LC_SYMTAB")
            table = struct.unpack_from("<4I", macho, pos + 8)
        pos += length
    require(pos == end and table is not None, "Mach-O symbol table absent")
    offset, count, strings, length = table
    require(offset + 16 * count <= len(macho) and strings + length <= len(macho),
            "Mach-O symbols bounds")
    result = {}
    for i in range(count):
        name, kind, section, _, address = struct.unpack_from("<IBBHQ", macho, offset + 16 * i)
        require(name < length, "Mach-O string offset")
        stop = macho.find(b"\0", strings + name, strings + length)
        require(stop >= 0, "Mach-O string terminator")
        if kind & 0xE0 == 0 and kind & 0x0E == 0x0E and section:
            label = macho[strings + name:stop].decode("ascii")
            require(label not in result, "duplicate symbol")
            result[label] = address
    for label in ("_start", "_fault_site", "_l1", "_l2", "_device_l2", "_pages", "_aliases"):
        require(label in result, "missing " + label)
    return result


def memory_oracle(ram, shared, sym, ncpus, records):
    require(len(ram) == SIZE and len(shared) == SHARED_SIZE, "dump sizes")
    require(ram[SHARED:SHARED + SHARED_SIZE] == shared, "shared/RAM disagreement")
    magic, version, count, ready, timer, hz = struct.unpack_from("<6Q", shared)
    require((magic, version, count, ready) == (0x4E4154454D553031, 1, ncpus, 1),
            "shared identity/readiness")
    require(16 <= timer < 32 and hz > 0, "timer identity/frequency")
    require(u64(ram, DATA) == SENTINEL, "physical sentinel")
    expected = {
        "_l1": {0: sym["_device_l2"] | 3, 1: sym["_l2"] | 3},
        "_l2": {**{i: (sym["_pages"] + 4096 * i) | 3 for i in range(8)},
                256: sym["_aliases"] | 3},
        "_aliases": {0: (BASE + DATA) | 0x703},
        "_device_l2": {128: 0x10000000 | 0x405 | (3 << 53),
                       **{136 + i: (0x11000000 + i * 0x200000) | 0x405 | (3 << 53)
                          for i in range(8)}}}
    for label, entries in expected.items():
        address = sym[label]
        require(address % 4096 == 0 and BASE <= address <= BASE + SIZE - 4096,
                "table address " + label)
        require(all(u64(ram, address - BASE + 8 * i) == entries.get(i, 0)
                    for i in range(512)), "descriptor " + label)
    require(sym["_pages"] % 4096 == 0 and BASE <= sym["_pages"] <= BASE + SIZE - 32768,
            "pages bounds")
    require(all(u64(ram, sym["_pages"] - BASE + 8 * i) == (BASE + i * 4096) | 0x703
                for i in range(4096)), "identity page descriptors")
    start = sym["_start"] - BASE
    require(0 <= start <= len(ram) - 128, "entry bounds")
    require(struct.unpack_from("<I", ram, start)[0] == 0xD4000642, "entry HVC bytes")
    completions = [BASE + i + 4 for i in range(start, start + 128, 4)
                   if struct.unpack_from("<I", ram, i)[0] == 0xD4000602]
    require(len(completions) == 1, "completion HVC instruction identity")
    completion = completions[0]
    redistributors, results = set(), []
    for cpu in range(ncpus):
        off = 48 + cpu * STRIDE
        c = dict(zip(FIELDS, struct.unpack_from("<22Q", shared, off)))
        require((c["ready"], c["done"], c["failure"]) == (1, 1, 0), f"cpu{cpu} completion")
        require(c["tso_start"] & 2 and c["tso_end"] & 2, f"cpu{cpu} TSO")
        require(c["sctlr"] & 0x1005 == 0x1005 and c["tcr"] == TCR and
                (c["mmfr0"] >> 28) & 15 != 15, f"cpu{cpu} MMU/granule")
        require(c["alias_value"] == SENTINEL and c["sync_count"] == c["sgi_count"] == 1
                and c["timer_count"] >= 3, f"cpu{cpu} effects")
        esr = c["esr"]
        require(esr >> 26 == 0x25 and esr & (1 << 25) and esr & 0x7FF == 7 and
                c["far"] == 0x60001000 and c["elr"] == sym["_fault_site"], f"cpu{cpu} fault")
        redist = c["redistributor"]
        require(c["timer_intid"] == timer and not c["waker_after"] & 6 and
                0x11000000 <= redist < 0x12000000 and redist % 65536 == 0 and
                redist not in redistributors, f"cpu{cpu} GIC")
        redistributors.add(redist)
        length = c["trace_count"]
        require(length == c["timer_count"] + 4 and length <= 32, f"cpu{cpu} trace count/overflow")
        events = [struct.unpack_from("<3Q", shared, off + 176 + 24 * i) for i in range(length)]
        require(events[0][:2] == (1, SENTINEL) and events[1][:2] == (2, esr) and
                events[-1][:2] == (4, c["timer_count"]), f"cpu{cpu} trace endpoints")
        irqs = [e[1] for e in events[2:-1] if e[0] == 3]
        require(len(irqs) == length - 3 and irqs.count(1) == 1 and
                irqs.count(timer) == c["timer_count"], f"cpu{cpu} IRQ trace")
        ticks = [e[2] for e in events]
        require(0 < c["ticks_start"] <= ticks[0] and ticks == sorted(ticks) and
                ticks[-2] <= c["ticks_end"] <= ticks[-1] and
                c["ticks_start"] < c["ticks_end"], f"cpu{cpu} trace order")
        logs = [r for r in records if r.get("cpu") == cpu]
        initial = [r for r in logs if "initial_actlr" in r]
        final = [r for r in logs if "final_actlr" in r]
        exits = [r for r in logs if "exit_reason" in r]
        require(len(initial) == len(final) == 1 and initial[0]["initial_actlr"] & 2 and
                initial[0]["initial_sctlr"] == 0x30D00800 and final[0]["final_actlr"] & 2 and
                final[0]["final_exec_ticks"] > 0 and final[0]["final_pc"] == completion,
                f"cpu{cpu} host state")
        require([(r["exit_reason"], r["syndrome"] >> 26, r["syndrome"] & 65535, r["pc"])
                 for r in exits] == [(1, 0x16, 0x32, sym["_start"] + 4),
                                    (1, 0x16, 0x30, completion)], f"cpu{cpu} host exit identity")
        require([r["enter_run"] for r in logs if "enter_run" in r] == [0, 1],
                f"cpu{cpu} host run sequence")
        summaries = [r for r in logs if "foundation_check" in r]
        require(len(summaries) == 1, f"cpu{cpu} unique summary")
        summary = summaries[0]
        require(all(summary[k] == c[k] for k in FIELDS
                    if k not in ("ready", "timer_intid", "redistributor")) and
                summary["host_exits"] == 2 and summary["virtual_timer_exits"] == 0 and
                summary["physical_timer_intid"] == timer, f"cpu{cpu} summary/raw contradiction")
        results.append({"cpu": cpu, "timer_count": c["timer_count"], "trace_count": length})
    require(shared[48 + ncpus * STRIDE:] == bytes((4 - ncpus) * STRIDE), "unused CPU slots")
    require(not any(any(k in r for k in ("canceled_pc", "error", "watchdog_timeout",
                       "unexpected_virtual_timer_exit", "atexit_partial_ram_dump")) for r in records),
            "host failure/partial diagnostic")
    require([r["stopped_ram_dump_complete"] for r in records if "stopped_ram_dump_complete" in r]
            == [True], "stopped RAM dump completion")
    return results


def check_run(run, source, ncpus):
    run, source = run.resolve(), source.resolve()
    def artifact(name):
        path = run / name
        require(path.is_file() and not path.is_symlink() and run in path.resolve().parents,
                "artifact path " + name)
        return path
    hashes = {}
    for line in artifact("SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        require(name not in hashes, "duplicate checksum")
        require(hashlib.sha256(artifact(name).read_bytes()).hexdigest() == digest,
                "checksum " + name)
        hashes[name] = digest
    actual = {str(p.relative_to(run)) for p in run.rglob("*") if p.is_file()}
    require(actual == set(hashes) | {"SHA256SUMS"}, "checksum coverage")
    expected_source = {str(p.relative_to(source)): p for p in source.rglob("*")
                       if p.is_file() and "__pycache__" not in p.parts}
    require({n[7:] for n in hashes if n.startswith("source/")} == set(expected_source),
            "source file inventory")
    for name, path in expected_source.items():
        require(artifact("source/" + name).read_bytes() == path.read_bytes(), "source freeze " + name)
    manifest = json.loads(artifact("manifest.json").read_text())
    require(manifest["format"] == "natemu-track01-run-v2" and manifest["run_id"] == run.name and
            manifest["status"] == "executed", "run identity/status/format")
    inputs, limits = manifest["inputs"], manifest["limits"]
    require(inputs["vcpus"] == ncpus and not inputs["build_only"] and
            inputs["entry_control"] is None and inputs["host_inherit_cpu_limit"] is True,
            "normal foundation inputs")
    require(limits["build_child_cpu_seconds"] == 60 and limits["host_cpu_limit"] == UNLIMITED and
            limits["command_wall_seconds"] == limits["guest_wall_seconds"] == 30 and
            limits["child_file_bytes"] == 64 * 1024 * 1024, "v2 resource limits")
    commands = manifest["commands"]
    hosts = [c for c in commands if c["argv"][0] == str(run / "host")]
    require(len(hosts) == 1 and hosts[0] is commands[-1], "one final host invocation")
    sym = symbols(artifact("guest.macho").read_bytes())
    text_end = min(sym[k] for k in ("_l1", "_l2", "_device_l2", "_pages", "_aliases")) - BASE
    ram, guest = artifact("ram.bin").read_bytes(), artifact("guest.bin").read_bytes()
    require(0 < text_end <= len(guest) and ram[:text_end] == guest[:text_end] ==
            artifact("guest.macho").read_bytes()[:text_end], "retained/live text identity")
    require(hosts[0]["argv"] == [str(run / "host"), str(run / "guest.bin"),
                                 hex(sym["_start"]), str(ncpus)], "host argv identity")
    for c in commands:
        require(c["returncode"] == 0 and c["status"] == "passed" and
                c["cwd"] == str(run) and c["timeout_seconds"] == 30 and
                c["cpu_limit"] == (UNLIMITED if c is hosts[0] else [60, 60]), "command outcome/limits")
    require(manifest["machine"] == "arm64" and manifest["sdk"]["version"] == "26.4",
            "prepared host/SDK scope")
    records = [json.loads(line) for line in artifact(hosts[0]["stdout"]).read_text().splitlines()]
    require(not artifact(hosts[0]["stderr"]).read_bytes(), "nonempty host stderr")
    require([r["stage"] for r in records if "stage" in r] == ["foundation"] and
            [r["gic_created"] for r in records if "gic_created" in r] == [True] and
            [r["selected_el2_enabled"] for r in records if "selected_el2_enabled" in r] == [False],
            "foundation/GIC/EL2 context")
    result = memory_oracle(ram, artifact("shared.bin").read_bytes(), sym, ncpus, records)
    return {"independent_foundation_oracle": "pass", "run_id": run.name, "cpus": result,
            "source": str(source), "manifest_sha256": hashes["manifest.json"],
            "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "scope": "finite native foundation only; no Q30 paging resolution or timing claim"}


def self_check():
    """Synthetic native-memory fixtures; no Hypervisor call, guest or subprocess."""
    sym = {"_start": BASE + 0x800, "_fault_site": BASE + 0x1800,
           "_l1": BASE + 0x4000, "_l2": BASE + 0x5000, "_device_l2": BASE + 0x6000,
           "_pages": BASE + 0x7000, "_aliases": BASE + 0xF000}
    def put(data, offset, value):
        struct.pack_into("<Q", data, offset, value)
    rejected = []
    for ncpus in (2, 4):
        ram, shared = bytearray(SIZE), bytearray(SHARED_SIZE)
        struct.pack_into("<II", ram, 0x800, 0xD4000642, 0xD4000602)
        put(ram, DATA, SENTINEL)
        put(ram, 0x4000, sym["_device_l2"] | 3)
        put(ram, 0x4008, sym["_l2"] | 3)
        for i in range(8):
            put(ram, 0x5000 + i * 8, (sym["_pages"] + i * 4096) | 3)
            put(ram, 0x6000 + (136 + i) * 8, (0x11000000 + i * 0x200000) | 0x60000000000405)
        put(ram, 0x6000 + 128 * 8, 0x60000010000405)
        put(ram, 0x5800, sym["_aliases"] | 3)
        put(ram, 0xF000, (BASE + DATA) | 0x703)
        for i in range(4096):
            put(ram, 0x7000 + i * 8, (BASE + i * 4096) | 0x703)
        struct.pack_into("<6Q", shared, 0, 0x4E4154454D553031, 1, ncpus, 1, 30, 24000000)
        records = [{"stopped_ram_dump_complete": True}, {"foundation_pass": False}]
        for cpu in range(ncpus):
            offset, esr = 48 + STRIDE * cpu, 0x96000007
            values = [1, 1, 0, 2, 2, 0x30D01805, TCR, 0, 1, 3, 1, esr,
                      0x60001000, sym["_fault_site"], SENTINEL, 7, 100, 170,
                      30, 0x11000000 + cpu * 0x20000, 6, 0]
            struct.pack_into("<22Q", shared, offset, *values)
            events = [(1, SENTINEL), (2, esr), (3, 1), (3, 30), (3, 30), (3, 30), (4, 3)]
            for i, (kind, value) in enumerate(events):
                struct.pack_into("<3Q", shared, offset + 176 + 24 * i, kind, value, 110 + i * 10)
            records.extend([{"cpu": cpu, "initial_actlr": 2, "initial_sctlr": 0x30D00800},
                            {"cpu": cpu, "enter_run": 0},
                            {"cpu": cpu, "exit_reason": 1, "syndrome": 0x58000032, "pc": BASE + 0x804},
                            {"cpu": cpu, "enter_run": 1},
                            {"cpu": cpu, "exit_reason": 1, "syndrome": 0x58000030, "pc": BASE + 0x808},
                            {"cpu": cpu, "final_actlr": 2, "final_exec_ticks": 1, "final_pc": BASE + 0x808}])
            records.append({"cpu": cpu, "foundation_check": False, "host_exits": 2,
                            "virtual_timer_exits": 0, "physical_timer_intid": 30,
                            **{k: v for k, v in zip(FIELDS, values)
                               if k not in ("ready", "timer_intid", "redistributor")}})
        ram[SHARED:SHARED + SHARED_SIZE] = shared
        memory_oracle(ram, shared, sym, ncpus, records)
        # A false producer PASS field intentionally does not override raw evidence.
        corruptions = [("shared mismatch", "ram", SHARED + 8, 2),
                       ("stale alias", "ram", 0xF000, 0x40901703),
                       ("mapped fault page", "ram", 0xF008, 0x40901703),
                       ("wrong identity page", "ram", 0x7000, 0x40001703),
                       ("wrong device attribute", "ram", 0x6000 + 128 * 8, 0x10000701),
                       ("physical sentinel", "ram", DATA, 0),
                       ("guest TSO off", "shared", 48 + 3 * 8, 0),
                       ("wrong FAR", "shared", 48 + 12 * 8, 0x60000000),
                       ("wrong fault PC", "shared", 48 + 13 * 8, sym["_fault_site"] + 4),
                       ("wrong fault WnR", "shared", 48 + 11 * 8, 0x96000047),
                       ("wrong IRQ", "shared", 48 + 176 + 2 * 24 + 8, 2),
                       ("trace overflow", "shared", 48 + 15 * 8, 33),
                       ("wrong completion PC", "log", 0, 0),
                       ("stale JSON counter", "log", 0, 0),
                       ("watchdog", "log", 1, 0)]
        for name, target, offset, value in corruptions:
            r, s, logs = bytearray(ram), bytearray(shared), copy.deepcopy(records)
            if target == "shared":
                put(s, offset, value)
                r[SHARED:SHARED + SHARED_SIZE] = s
            elif target == "ram":
                put(r, offset, value)
            elif name == "wrong completion PC":
                next(x for x in logs if x.get("syndrome") == 0x58000030)["pc"] += 4
            elif name == "stale JSON counter":
                next(x for x in logs if "foundation_check" in x)["timer_count"] += 1
            else:
                logs.append({"watchdog_timeout": True})
            try:
                memory_oracle(r, s, sym, ncpus, logs)
            except ValueError:
                rejected.append(f"{ncpus}cpu: {name}")
            else:
                raise AssertionError("oracle accepted " + name)
    print(json.dumps({"self_check": "pass", "synthetic_positive_vcpus": [2, 4],
                      "rejected_corruptions": rejected}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--vcpus", type=int, choices=(2, 4))
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
    else:
        if not args.run or not args.source or not args.vcpus:
            parser.error("run, --source and --vcpus are required")
        print(json.dumps(check_run(args.run, args.source, args.vcpus), sort_keys=True))


if __name__ == "__main__":
    main()
