# Adapted from FEX; see cases.json and upstream/FEX/LICENSE.
.intel_syntax noprefix
.text
.code32
mov edx, 0x100000
mov eax, 0x42
mov [edx], eax
mov eax, -1
.byte 0xa1
.long 0x100000
mov edx, eax
mov eax, -1
.byte 0x66, 0xa1
.long 0x100000
mov ecx, eax
