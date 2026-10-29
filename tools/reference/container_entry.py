"""Runs inside the pinned container: QEMU Linux-user on every /work/jobs/*/prog.elf (no network, read-only root).
A job with a `flow` marker file has control flow: QEMU also logs each translation block it executes (`-d exec,nochain`),
which gives the executed instruction sequence. A job with a `faults` marker runs `-singlestep` (one instruction per
translation block): QEMU delivers a SIGSEGV; the registers in the signal context are exact, only lazily computed flags can be stale there."""
import pathlib, subprocess
for d in sorted(pathlib.Path('/work/jobs').iterdir()):
    debug = 'in_asm,exec,nochain' if (d / 'flow').exists() else 'in_asm'
    step = ['-singlestep'] if (d / 'faults').exists() else []
    cpu = (d / 'cpu').read_text().strip() if (d / 'cpu').exists() else 'qemu64'    # e.g. qemu64,+bmi1 for the I1 forms
    with open(d / 'qemu.stdout', 'wb') as out:
        r = subprocess.run(['/qemu', '-cpu', cpu, *step, '-d', debug, '-D', 'qemu.log', 'prog.elf'], cwd=d, stdout=out,
                           stderr=open(d / 'qemu.stderr', 'wb'), timeout=60)
    (d / 'rc').write_text(str(r.returncode))
