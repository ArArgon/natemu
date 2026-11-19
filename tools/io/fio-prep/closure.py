#!/usr/bin/env python3
"""closure.py WORK RECIPE OUT: the fio preparation folder (run_io_matrix.py / run_fio_admission.py --fio-prep) from what
container.sh downloaded into WORK (debs/, Packages.trixie.xz).

The steps of natemu-raw/m2-fio-setup-20261114-01 (prepare_bundle.py, finish_bundle.py, check_dependencies.py), folded
into one script with the same results:
- each archive is checked against the signed index (SHA256, Size) and kept as packages/<pool file name>;
- every Depends/Pre-Depends clause of every package must be met by a package in the set (Debian version order):
  dependency-check.json;
- the packages' data.tar members are unpacked into rootfs/ (regular files with their mode, symbolic links, hard links
  as copies, folders created plain), plus the lib and lib64 links of a merged-/usr system;
- elf-closure.json: /usr/bin/fio's DT_NEEDED/interpreter closure, resolved in the guest root through each object's
  RPATH/RUNPATH ($ORIGIN) and then the standard library folders; nothing may be missing. The ELF headers are read here
  (the original used Homebrew's llvm-readelf -d -l);
- packages.json, profiles/ (this folder's, reviewed text), metadata/Packages.trixie.xz and preparation-files.sha256
  (every file but itself).
Only the standard library is used; it runs on the Mac with the framework's Python."""
import hashlib
import io
import json
import lzma
import os
import pathlib
import re
import shutil
import struct
import sys
import tarfile

SEARCH = ['/usr/lib/x86_64-linux-gnu', '/usr/lib64', '/usr/lib', '/lib/x86_64-linux-gnu', '/lib64', '/lib']
ENTRY = '/usr/bin/fio'


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def index_records(index: pathlib.Path) -> dict:
    """(Package, Version) -> the index record's fields."""
    out = {}
    for record in lzma.decompress(index.read_bytes()).decode().split('\n\n'):
        fields = dict(line.split(': ', 1) for line in record.splitlines() if line and not line[0].isspace() and ': ' in line)
        if 'Package' in fields: out[(fields['Package'], fields['Version'])] = fields
    return out


# ---------------------------------------------------------------- Debian version order (check_dependencies.py)
def _order(c):
    if c == '~': return -1
    if not c: return 0
    if c.isalpha(): return ord(c)
    return ord(c) + 256


def _revcmp(a, b):
    while a or b:
        while (a and not a[0].isdigit()) or (b and not b[0].isdigit()):
            ac = a[0] if a and not a[0].isdigit() else ''; bc = b[0] if b and not b[0].isdigit() else ''
            if _order(ac) != _order(bc): return (_order(ac) > _order(bc)) - (_order(ac) < _order(bc))
            if ac: a = a[1:]
            if bc: b = b[1:]
        a = a.lstrip('0'); b = b.lstrip('0')
        ad = re.match(r'\d*', a)[0]; bd = re.match(r'\d*', b)[0]
        if len(ad) != len(bd): return (len(ad) > len(bd)) - (len(ad) < len(bd))
        if ad != bd: return (ad > bd) - (ad < bd)
        a = a[len(ad):]; b = b[len(bd):]
    return 0


def _ver(v):
    epoch, tail = v.split(':', 1) if ':' in v else ('0', v)
    up, rev = tail.rsplit('-', 1) if '-' in tail else (tail, '0')
    return int(epoch), up, rev


def compare(a, b):
    ae, au, ar = _ver(a); be, bu, br = _ver(b)
    return (ae > be) - (ae < be) or _revcmp(au, bu) or _revcmp(ar, br)


def dependency_check(records: dict) -> dict:
    """records: package -> index fields. Each Depends/Pre-Depends clause must be met by a package of the set."""
    checks, bad = [], []
    for name, f in records.items():
        for field in ('Depends', 'Pre-Depends'):
            for clause in f.get(field, '').split(','):
                if not clause.strip(): continue
                match = None
                for alt in clause.split('|'):
                    m = re.fullmatch(r'\s*([a-z0-9+.-]+)(?::[a-z]+)?(?:\s*\((<<|<=|=|>=|>>)\s*([^\)]+)\))?\s*', alt)
                    if not m: raise ValueError(f'unparsed dependency of {name}: {clause}')
                    dep, op, want = m.groups()
                    if dep not in records: continue
                    actual = records[dep]['Version']; c = compare(actual, want) if op else 0
                    if not op or {'<<': c < 0, '<=': c <= 0, '=': c == 0, '>=': c >= 0, '>>': c > 0}[op]:
                        match = {'selected': dep, 'version': actual}; break
                row = {'package': name, 'field': field, 'clause': clause.strip(), 'satisfied_by': match}
                checks.append(row)
                if not match: bad.append(row)
    return {'checks': checks, 'unsatisfied': bad}


# ---------------------------------------------------------------- unpacking (prepare_bundle.py)
def ar_member(deb: pathlib.Path, prefix: str) -> bytes:
    """The first member of a Debian archive (ar format) whose name starts with prefix."""
    data = deb.read_bytes()
    if data[:8] != b'!<arch>\n': raise ValueError(f'not an ar archive: {deb}')
    pos = 8
    while pos + 60 <= len(data):
        name, size = data[pos:pos + 16].decode().strip().rstrip('/'), int(data[pos + 48:pos + 58].decode().strip())
        if name.startswith(prefix): return data[pos + 60:pos + 60 + size]
        pos += 60 + size + (size & 1)
    raise ValueError(f'no {prefix}* in {deb}')


def unpack(debs: list, root: pathlib.Path) -> None:
    links = {}
    for deb in debs:
        with tarfile.open(fileobj=io.BytesIO(ar_member(deb, 'data.tar'))) as archive:
            for m in archive:
                rel = pathlib.PurePosixPath(m.name)
                if rel.is_absolute() or '..' in rel.parts: raise ValueError(f'package path {m.name} in {deb}')
                p = root / str(rel)
                if m.isdir(): p.mkdir(parents=True, exist_ok=True); p.chmod(m.mode & 0o7777)
                elif m.isfile():
                    p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(archive.extractfile(m).read()); p.chmod(m.mode)
                    links.pop(str(rel), None)
                elif m.issym() or m.islnk(): links[str(rel)] = (m.linkname, m.islnk())
                else: raise ValueError(f'special file {m.name} in {deb}')
    for name, (target, hard) in links.items():
        p = root / name; p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() or p.is_symlink(): p.unlink()
        if hard: shutil.copy2(root / str(pathlib.PurePosixPath(target)), p)
        else: p.symlink_to(target)
    for name, target in (('lib', 'usr/lib'), ('lib64', 'usr/lib64')):
        if not (root / name).exists(): (root / name).symlink_to(target)


# ---------------------------------------------------------------- the ELF closure (finish_bundle.py)
def guest(root: pathlib.Path, name: str) -> pathlib.Path:
    """name resolved inside the guest root, never following an absolute link on the host."""
    todo, parts, n = list(pathlib.PurePosixPath(name).parts), [], 0
    while todo:
        part = todo.pop(0)
        if part in ('/', '.', ''): continue
        if part == '..':
            if parts: parts.pop()
            continue
        p = root.joinpath(*parts, part)
        if p.is_symlink():
            n += 1
            if n > 64: raise ValueError(f'guest symlink loop: {name}')
            target = os.readlink(p)
            if target.startswith('/'): parts = []
            todo = list(pathlib.PurePosixPath(target).parts) + todo
        else: parts.append(part)
    return root.joinpath(*parts)


def elf_dynamic(data: bytes) -> tuple[list, list, str | None]:
    """A little-endian ELF64's DT_NEEDED names and RPATH/RUNPATH entries (in dynamic-section order) and its PT_INTERP."""
    if data[:6] != b'\x7fELF\x02\x01': raise ValueError('not a little-endian ELF64 object')
    phoff, = struct.unpack_from('<Q', data, 32); phentsize, phnum = struct.unpack_from('<HH', data, 54)
    loads, dynamic, interp = [], None, None
    for i in range(phnum):
        p_type, _, off, vaddr, _, filesz, _, _ = struct.unpack_from('<IIQQQQQQ', data, phoff + i * phentsize)
        if p_type == 1: loads.append((vaddr, off, filesz))
        elif p_type == 2: dynamic = (off, filesz)
        elif p_type == 3: interp = data[off:off + filesz].split(b'\0')[0].decode()
    if dynamic is None: return [], [], interp
    entries = []
    for pos in range(dynamic[0], dynamic[0] + dynamic[1], 16):
        tag, val = struct.unpack_from('<qQ', data, pos)
        if tag == 0: break
        entries.append((tag, val))
    strtab = next(v for t, v in entries if t == 5)
    base = next(off + strtab - vaddr for vaddr, off, filesz in loads if vaddr <= strtab < vaddr + filesz)
    text = lambda v: data[base + v:data.index(b'\0', base + v)].decode()
    needed = [text(v) for t, v in entries if t == 1]
    paths = [text(v) for t, v in entries if t in (15, 29)]          # DT_RPATH, DT_RUNPATH
    return needed, paths, interp


def elf_closure(root: pathlib.Path, entry: str = ENTRY) -> dict:
    pending, objects, missing = [entry], {}, []
    while pending:
        name = pending.pop(); p = guest(root, name); key = '/' + str(p.relative_to(root))
        if key in objects: continue
        needed, paths, interp = elf_dynamic(p.read_bytes())
        runpaths = [part.replace('$ORIGIN', str(pathlib.PurePosixPath(key).parent)) for group in paths for part in group.split(':')]
        resolved = {}
        for n in needed:
            target = next((c for c in (f'{d}/{n}' for d in runpaths + SEARCH) if guest(root, c).is_file()), None)
            if not target: missing.append((key, n)); continue
            target = '/' + str(guest(root, target).relative_to(root)); resolved[n] = target; pending.append(target)
        if interp: pending.append(interp)
        objects[key] = {'sha256': sha(p), 'needed': resolved, 'interpreter': interp}
    return {'entry': entry, 'objects': objects, 'missing': missing}


# ---------------------------------------------------------------- the folder
def main(work: pathlib.Path, recipe: pathlib.Path, out: pathlib.Path) -> None:
    os.umask(0o022)                     # folder modes must not depend on the caller's umask (not part of the cache key)
    index = index_records(work / 'Packages.trixie.xz')
    wanted = [tuple(line.split('=', 1)) for line in (recipe / 'packages.txt').read_text().split()]
    debs = {(n, v): work / 'debs' / f"{n}_{v.replace(':', '%3a')}_{index[(n, v)]['Architecture']}.deb" for n, v in wanted}
    (out / 'packages').mkdir(parents=True)
    manifest, records = [], {}
    for (n, v), deb in debs.items():
        f = index[(n, v)]
        if sha(deb) != f['SHA256'] or deb.stat().st_size != int(f['Size']): raise ValueError(f'{deb.name} differs from the signed index')
        shutil.copyfile(deb, out / 'packages' / pathlib.PurePosixPath(f['Filename']).name)
        manifest.append({'package': n, 'version': v, 'filename': f['Filename'], 'sha256': f['SHA256']}); records[n] = f
    deps = dependency_check(records)
    if deps['unsatisfied']: raise ValueError(f'dependency closure incomplete: {deps["unsatisfied"]}')
    root = out / 'rootfs'; root.mkdir()
    unpack([out / 'packages' / pathlib.PurePosixPath(m['filename']).name for m in manifest], root)
    closure = elf_closure(root)
    if closure['missing']: raise ValueError(f'ELF closure incomplete: {closure["missing"]}')
    (out / 'metadata').mkdir(); shutil.copyfile(work / 'Packages.trixie.xz', out / 'metadata/Packages.trixie.xz')
    shutil.copytree(recipe / 'profiles', out / 'profiles')
    for n in ('packages.txt', 'debs.sha256'): shutil.copyfile(recipe / n, out / n)
    (out / 'packages.json').write_text(json.dumps(manifest, indent=1) + '\n')
    (out / 'dependency-check.json').write_text(json.dumps(deps, indent=1) + '\n')
    (out / 'elf-closure.json').write_text(json.dumps(closure, indent=1) + '\n')
    files = sorted(p for p in out.rglob('*') if p.is_file() and not p.is_symlink())
    (out / 'preparation-files.sha256').write_text(''.join(f'{sha(p)}  {p.relative_to(out)}\n' for p in files))
    print('READY', len(manifest), 'packages', len(closure['objects']), 'ELFs', len(list((out / 'profiles').glob('*.fio'))), 'profiles')


if __name__ == '__main__':
    if len(sys.argv) != 4: raise SystemExit(__doc__)
    main(*map(pathlib.Path, sys.argv[1:]))
