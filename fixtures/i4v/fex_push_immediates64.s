# Adapted from FEX; see cases.json and upstream/FEX/LICENSE.
.intel_syntax noprefix
.text
.code64
push -0x41424345
.att_syntax prefix
pushw $0x5152
pushw $0x6162
.intel_syntax noprefix
mov rax, [rdx - 8]
mov ebx, [rdx - 12]
