.intel_syntax noprefix
.text
mov dword ptr [rbx],1
mfence
mov eax,dword ptr [rdx]
