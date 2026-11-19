"""Load an exclusive claim cannot see (spec R4: an exclusive window means nothing else runs): processes outside the
scheduler, such as other tools' builds, containers, QEMU reference runs or a person's own work. While an exclusive
unit runs, the controller samples the machine every few seconds; busy processes outside the run
(not the controller or its descendants) are recorded in timings.json and named on a failure of that unit, so a failure under foreign load is not mistaken for
a natemu bug."""
import os
import subprocess

BUSY = 25.0          # %CPU (ps's decaying average) from which a foreign process counts


def sample(root: int | None = None) -> list[tuple[int, float, str]]:
    """(pid, %cpu, command) of busy processes that are not `root` (default: this process) or its descendants."""
    r = subprocess.run(['ps', '-A', '-o', 'pid=,ppid=,%cpu=,comm='], capture_output=True, text=True)
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4: continue
        try: rows.append((int(parts[0]), int(parts[1]), float(parts[2]), os.path.basename(parts[3].strip())))
        except ValueError: continue
    kids = {}
    for pid, ppid, _, _ in rows: kids.setdefault(ppid, []).append(pid)
    own, todo = set(), [root or os.getpid()]
    while todo:
        q = todo.pop()
        if q in own: continue
        own.add(q); todo += kids.get(q, [])
    return [(pid, cpu, name) for pid, _, cpu, name in rows if cpu >= BUSY and pid not in own]


def summary(seen: dict[str, float]) -> str:
    """'name (peak %cpu), ...', busiest first."""
    return ', '.join(f'{n} ({c:.0f}%)' for n, c in sorted(seen.items(), key=lambda x: -x[1])[:6])
