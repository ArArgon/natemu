#!/usr/bin/env python3 -I
"""Split the Debian bzImage and unpack the initramfs. Usage: extract.py RAW_DIR  (RAW_DIR/dl/{linux,initrd.gz})"""
import gzip, os, struct, subprocess, sys

raw = sys.argv[1]
out = os.path.join(raw, 'x'); os.makedirs(out, exist_ok=True)
d = open(os.path.join(raw, 'dl/linux'), 'rb').read()
u32 = lambda o: struct.unpack_from('<I', d, o)[0]
ss = d[0x1f1] or 4
pm = (ss + 1) * 512
po, pl = u32(0x248), u32(0x24c)
print(f'setup_sects={ss} pm_start={pm:#x} payload_offset={po:#x} payload_length={pl:#x} syssize={u32(0x1f4):#x} init_size={u32(0x260):#x}')
pmd = d[pm:]
parts = {'setup.bin': d[:pm], 'head.bin': pmd[:po], 'tail.bin': pmd[po + pl:]}
payload = pmd[po:po + pl]
assert payload[:4] == bytes.fromhex('28b52ffd'), 'not zstd'  # CONFIG_KERNEL_ZSTD=y
size = struct.unpack('<I', payload[-4:])[0]  # kernel appends the uncompressed size
vm = subprocess.run(['zstd', '-d', '-c'], input=payload[:-4], capture_output=True, check=True).stdout
assert len(vm) == size, (len(vm), size)
parts['vmlinux.elf'] = vm
print('vmlinux', len(vm), vm[:4])
for n, b in parts.items():
    open(os.path.join(out, n), 'wb').write(b); print(n, len(b))

# initramfs: gzip (maybe several members) of newc cpio
cpio = gzip.open(os.path.join(raw, 'dl/initrd.gz')).read()
print('initrd cpio bytes', len(cpio))
p, inv = 0, []
os.makedirs(os.path.join(out, 'initrd'), exist_ok=True)
while True:
    assert cpio[p:p+6] == b'070701', p
    f = [int(cpio[p+6+8*i:p+14+8*i], 16) for i in range(13)]
    mode, fsize, nl = f[1], f[6], f[11]
    name = cpio[p+110:p+110+nl-1].decode(); p = (p + 110 + nl + 3) & ~3
    data = cpio[p:p+fsize]; p = (p + fsize + 3) & ~3
    if name == 'TRAILER!!!': break
    inv.append((name, mode, fsize, data[:4] == b'\x7fELF', data[:2] == b'#!'))
    if mode & 0o170000 == 0o100000 and (data[:4] == b'\x7fELF' or name == 'init'):
        fn = os.path.join(out, 'initrd', name.replace('/', '__'))
        open(fn, 'wb').write(data)
with open(os.path.join(raw, 'initrd-inventory.txt'), 'w') as fh:
    for n, m, s, e, sh in inv: fh.write(f'{n}\t{oct(m)}\t{s}\t{"ELF" if e else "script" if sh else ""}\n')
elfs = [i for i in inv if i[3]]
print('entries', len(inv), 'ELF', len(elfs), 'ELF bytes', sum(i[2] for i in elfs))
