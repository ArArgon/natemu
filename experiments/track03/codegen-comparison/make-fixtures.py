#!/usr/bin/env python3
"""Create minimal dependency-free ELF inputs with precisely retained code bytes."""
import json
from pathlib import Path
import struct
import sys

fixtures=json.loads(Path(sys.argv[1]).read_text())['Instructions']
fixtures=dict(fixtures, nop={'ExpectedBytes':'90','x86Insts':['nop']})
out=Path(sys.argv[2]); out.mkdir()
for name, fixture in fixtures.items():
    code=bytes.fromhex(fixture['ExpectedBytes'])
    # ELF64 ET_EXEC, one RX load segment, no sections/interpreter/dynamic entries.
    ident=b'\x7fELF\x02\x01\x01'+bytes(9)
    header=struct.pack('<16sHHIQQQIHHHHHH',ident,2,62,1,0x401000,64,0,0,64,56,1,0,0,0)
    segment=struct.pack('<IIQQQQQQ',1,5,0,0x400000,0x400000,4096+len(code),4096+len(code),4096)
    image=(header+segment).ljust(4096,b'\0')+code
    (out/(name+'.elf')).write_bytes(image)
    (out/(name+'.elf')).chmod(0o755)
    (out/(name+'.bin')).write_bytes(code)
    (out/(name+'.count')).write_text(str(len(fixture['x86Insts'])))
    assert image[4096:] == code and struct.unpack_from('<Q',image,24)[0] == 0x401000
