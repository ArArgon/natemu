# Adapted from FEX; see cases.json and upstream/FEX/LICENSE.
.intel_syntax noprefix
.text
.code32
mov eax, 0x42
.byte 0xa3
.long 0x100000
mov edx, 0x100000
mov edx, [edx]
mov eax, 0xffff0043
.byte 0x66, 0xa3
.long 0x100000
mov ecx, 0x100000
mov ecx, [ecx]
