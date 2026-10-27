"""Runs inside the pinned container: QEMU Linux-user on every /work/jobs/*/prog.elf (no network, read-only root)."""
import pathlib, subprocess
for d in sorted(pathlib.Path('/work/jobs').iterdir()):
    with open(d / 'qemu.stdout', 'wb') as out:
        r = subprocess.run(['/qemu', '-cpu', 'qemu64', '-d', 'in_asm', '-D', 'qemu.log', 'prog.elf'], cwd=d, stdout=out,
                           stderr=open(d / 'qemu.stderr', 'wb'), timeout=30)
    (d / 'rc').write_text(str(r.returncode))
