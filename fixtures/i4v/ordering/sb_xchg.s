.intel_syntax noprefix
.text
mov dword ptr [rbx],1
xchg dword ptr [rsi],eax
mov eax,dword ptr [rdx]
