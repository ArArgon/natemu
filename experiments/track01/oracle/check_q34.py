#!/usr/bin/env python3
"""Independent finite startup oracle; preparation never executes a VM."""
import argparse
import copy
import json
from pathlib import Path
import struct
import check_q33 as q33
q30=q33.q30
R=q30.require
MASK=(1<<64)-1
BASE=0x300000; SIZE=0x20000; KBASE=0xffffffff80000000
TRAMP=0x301000; H=0x303000; LOCK=H+0x40; GDT=H+0x100
BOOT4=0x308000; BOOT5=0x309000; TRROOT=0x30a000; ROOT=0x30b000
LPDP=0x30c000; LPD=0x30d000; HPDP=0x30e000; HPD=0x30f000
CASE,MAIL,STEP,STATE,MEMORY,LOCK_EVENT,CONTROL,CONTINUE,END=range(1,10)
MOV,ADD,XOR,AND,OR,BT,BTS,CMP,TEST,JCC,JMP,PAUSE,LIDT,LGDT,FAR,SEG,RDCR,WRCR,RDMSR,WRMSR,PUSH,LRET,VERIFY,LOAD,STORE,READY,INC=range(1,28)
RAX,RCX,RDX,RBX,RSP,RBP,RSI,RDI=range(8)
RIP,FLAGS,CR0,CR3,CR4,EFER,CS,SS,DS,ES,FS,GS,GDTR,GDTL,IDTR,IDTL,MODE,TLB=range(16,34)
def K(x):return (KBASE+x)&MASK
def mask(w):return (1<<(8*w))-1

def initial_memory(n,serial):
 m=bytearray(SIZE)
 def put(p,w,v):m[p-BASE:p-BASE+w]=v.to_bytes(w,'little')
 rows={H:K(TRAMP+0x800),H+8:0x900,H+16:32,LOCK:0x89abcdef00000000,GDT+8:0x00cf9b000000ffff,GDT+16:0x00af9b000000ffff,GDT+24:0x00cf93000000ffff,
 BOOT4:LPDP|3,BOOT4+4088:HPDP|3,BOOT5:BOOT4|3,BOOT5+4088:BOOT4|3,TRROOT:LPDP|3,TRROOT+4088:HPDP|3,ROOT+4088:HPDP|3,
 LPDP:LPD|3,LPD+8:0x200083,HPDP+4080:HPD|3,HPD+8:0x200083,H+0x68:K(LOCK)}
 for p,v in rows.items():put(p,8,v)
 for p,w,v in ((H+0x130,2,31),(H+0x132,8,GDT),(H+0x140,4,TRAMP+0x100),(H+0x144,2,8),(H+0x64,4,1<<31),(H+0x70,4,n)):put(p,w,v)
 for cpu in range(n):
  for p,w,v in ((H+0x80+cpu*4,4,0x100+cpu),(H+0xa0+cpu*8,8,cpu*8),(H+0xc0+cpu*8,8,K(H+0xe0+cpu*8)),(H+0xe0+cpu*8,8,K(0x304ff0+cpu*4096)),(0x304ff0+cpu*4096,8,0x3405000000000000|(serial<<8)|cpu)):put(p,w,v)
 return m

def initial_arch(cpu,variant,vector):
 a=[0]*36
 a[:16]=[0xa500000000000000+cpu*256+i for i in range(16)]
 a[RIP]=vector;a[FLAGS]=2;a[CR0]=0x80050033;a[CR4]=32|(4096 if variant in (2,3) else 0)
 a[CR3]=BOOT5 if a[CR4]&4096 else BOOT4;a[EFER]=0x500|(0 if variant in (1,3) else 0x800)
 a[CS]=16;a[SS]=a[DS]=a[ES]=24;a[MODE]=1
 return a

class Startup:
 def __init__(self,cpu,variant,serial,n,vector,steps,reads,locks,snapshots,controls):
  self.cpu,self.variant,self.serial,self.n=cpu,variant,serial,n
  self.a=initial_arch(cpu,variant,vector);self.steps=steps;self.reads=copy.deepcopy(reads);self.locks=locks;self.snapshots=snapshots;self.controls=controls
  self.number=0;self.unknown=0;self.flag_unknown=0
 def memory(self,va,w,write=0,value=None):
  row=self.reads.setdefault(self.number+1,[])
  R(row,'startup source memory effect missing')
  d=row.pop(0);R((d[1],d[3],d[4])==(va,w,write),'startup source memory address/width/direction')
  if write:R(d[6]==value&mask(w),'startup source memory write value')
  return d[6]
 def emit(self,op,w,dst,operand,result,nextpc=None):
  a=self.a;pc=a[RIP];self.number+=1
  R(self.number<=128 and self.number in self.steps,'missing/beyond-bound source instruction')
  expected=(self.number,op,w,dst,operand&MASK,result&MASK,pc)
  R(self.steps[self.number]==expected,f'CPU{self.cpu} step{self.number} source recipe mismatch: {self.steps[self.number]} != {expected}')
  R(not self.reads.get(self.number),'extra startup memory operation not used by source')
  a[RIP]=(pc+4 if nextpc is None else nextpc)&MASK
  actual=self.snapshots[self.number]
  for i,(x,y) in enumerate(zip(a,actual)):
   if i<16 and self.unknown>>i&1:continue
   if i==FLAGS:R((x^y)&~self.flag_unknown==0,'startup defined flags mismatch')
   else:R(x==y,f'CPU{self.cpu} step{self.number} architectural word{i}: {x:x}!={y:x}')
  a[FLAGS]=actual[FLAGS] # AF is undefined by this finite logic/callee contract.
 def mov(self,r,v,w=8):
  self.a[r]=(self.a[r]&~65535)|(v&65535) if w==2 else v&mask(w);self.unknown&=~(1<<r)
  self.emit(MOV,w,r,v,self.a[r])
 def flags(self,l,r,out,w,op):
  m=mask(w);l&=m;r&=m;out&=m;sign=1<<(w*8-1)
  f=(4 if bin(out&255).count('1')%2==0 else 0)|(64 if out==0 else 0)|(128 if out&sign else 0)
  if op in (ADD,INC):f|=(out<l)|((l^r^out)&16)|(2048 if ~(l^r)&(l^out)&sign else 0)
  if op==CMP:f|=(l<r)|((l^r^out)&16)|(2048 if (l^r)&(l^out)&sign else 0)
  self.flag_unknown=16 if op in (XOR,AND,OR,TEST) else 0
  old=self.a[FLAGS];self.a[FLAGS]=(old&~0x8d5)|f
  if op==INC:self.a[FLAGS]=(self.a[FLAGS]&~1)|(old&1)
 def alu(self,op,r,v,w):
  R(not self.unknown>>r&1,'undefined callee output used before overwrite')
  l=self.a[r]&mask(w);out=({XOR:lambda:l^v,AND:lambda:l&v,OR:lambda:l|v,ADD:lambda:l+v,INC:lambda:l+v}[op]())&mask(w)
  self.flags(l,v,out,w,op);self.a[r]=out;self.emit(op,w,r,v,out)
 def compare(self,l,r,w,op=CMP):
  out=((l-r) if op==CMP else l&r)&mask(w);self.flags(l,r,out,w,op);self.emit(op,w,l,r,out)
 def branch(self,identity,condition):self.emit(JCC,0,identity,int(condition),int(condition));return condition
 def load(self,r,va,w=8):
  self.a[r]=self.memory(va,w);self.unknown&=~(1<<r);self.emit(LOAD,w,r,va,self.a[r])
 def rdcr(self,cr,r):
  self.a[r]=self.a[{0:CR0,3:CR3,4:CR4}[cr]];self.unknown&=~(1<<r);self.emit(RDCR,8,r,cr,self.a[r])
 def wrcr(self,cr,v):
  a=self.a
  if cr==0:
   R(v&1 and (not (a[CR0]^v)&(1<<31) or a[CS]!=16),'PG toggle in long64 CS')
   a[CR0]=v
   if v&(1<<31):R(a[CR4]&32 and a[EFER]&256,'paging activation missing PAE/LME');a[EFER]|=1024
   else:a[EFER]&=~1024
  elif cr==4:R(not (a[CR0]&(1<<31)) or not (a[CR4]^v)&4096,'LA57 changed while paging');a[CR4]=v
  else:R(cr==3 and v%4096==0,'invalid CR3');a[CR3]=v
  a[TLB]+=1;self.mode();self.emit(WRCR,8 if a[MODE]==1 else 4,cr,v,v)
 def mode(self):self.a[MODE]=3 if not self.a[CR0]&(1<<31) else 1 if self.a[CS]==16 else 2
 def far(self,selector,target,op=FAR):
  d=self.memory(self.a[GDTR]+(selector&~7),8)
  R(selector in (8,16) and d&(1<<47) and (d>>53&1)==(selector==16) and (d>>54&1)==(selector==8),'far descriptor semantics')
  self.a[CS]=selector;self.mode();self.emit(op,8 if op==LRET else 4,selector,target,target,target)
 def segments(self):
  for which in range(5):
   selector=self.a[RDX]&65535;d=self.memory(self.a[GDTR]+(selector&~7),8)
   R(selector==24 and d&0xffffffffff00ffff==0x00cf93000000ffff&0xffffffffff00ffff,'data segment descriptor')
   self.a[SS+which]=selector;self.emit(SEG,2,which,selector,selector)
   if which==0:self.alu(ADD,RSP,BASE,4)
 def lock(self):
  while True:
   d=self.locks[self.number+1];carry=d[2]&1
   self.a[FLAGS]=(self.a[FLAGS]&~1)|carry;self.emit(BTS,4,LOCK,0,d[2]|1)
   if self.branch(1,not carry):break
   self.emit(PAUSE,0,0,0,0);self.emit(JMP,0,0,1,1)
  self.mov(RSP,0x2ff0,4)
 def run(self):
  a=self.a;debian=self.variant in (2,3,4);matched=self.variant==4
  a[IDTL]=self.memory(H+0x120,2);a[IDTR]=self.memory(H+0x122,8);self.emit(LIDT,10,H+0x120,a[IDTR],a[IDTL])
  a[GDTL]=self.memory(H+0x130,2);a[GDTR]=self.memory(H+0x132,8);self.emit(LGDT,10,H+0x130,a[GDTR],a[GDTL])
  if debian:
   self.rdcr(4,RAX);self.alu(XOR,RAX,self.memory(H+16,4),4);self.compare(a[RAX],4096,4,TEST);self.branch(5,not bool(a[FLAGS]&64))
  if matched:
   self.lock();self.mov(RDX,24,2);self.segments();self.mov(RAX,TRROOT,4);self.wrcr(3,a[RAX])
   a[RSP]-=8;self.memory(a[RSP],8,1,16);self.emit(PUSH,8,RSP,16,a[RSP])
   target=self.memory(H,8);a[RSP]-=8;self.memory(a[RSP],8,1,target);self.emit(PUSH,8,RSP,target,a[RSP])
   pc=self.memory(a[RSP],8);cs=self.memory(a[RSP]+8,8);a[RSP]+=16;self.far(cs,pc,LRET)
  else:
   target=self.memory(H+0x140,4);cs=self.memory(H+0x144,2);self.far(cs,target);self.lock();self.mov(RDX,24,2)
   self.mov(RAX,0x50033,4);self.wrcr(0,a[RAX]);self.far(8,TRAMP+0x300);self.segments()
   f=self.memory(H+20,4);a[FLAGS]=(a[FLAGS]&~1)|(f&1);self.emit(BT,4,H+20,0,f&1);self.branch(2,not bool(a[FLAGS]&1))
   self.load(RAX,H+16,4);self.wrcr(4,a[RAX]);self.mov(RAX,TRROOT,4);self.wrcr(3,a[RAX]);self.mov(RCX,0xc0000080,4)
   a[RAX]=a[EFER]&0xffffffff;a[RDX]=a[EFER]>>32;self.emit(RDMSR,8,a[RCX],0,a[EFER])
   self.compare(a[RAX],self.memory(H+8,4),4);write=self.branch(3,not bool(a[FLAGS]&64))
   if not write:self.compare(a[RDX],self.memory(H+12,4),4);write=not self.branch(4,bool(a[FLAGS]&64))
   if write:
    self.load(RAX,H+8,4);self.load(RDX,H+12,4);a[EFER]=(a[RDX]<<32)|(a[RAX]&0xffffffff);self.emit(WRMSR,8,a[RCX],a[EFER],a[EFER])
   self.mov(RAX,0x80050033,4);self.wrcr(0,a[RAX]);self.far(16,TRAMP+0x700)
   target=self.memory(H,8);self.emit(JMP,8,H,target,target,target)
  ret=a[RIP]+4;a[RSP]-=8;self.memory(a[RSP],8,1,ret)
  old=a[RDI];a[RAX]=0;a[RDI]&=~65535;a[FLAGS]=(a[FLAGS]&~0x8c5)|0x44
  R(self.controls[13]==[(14,16,0,old,a[RDI],self.serial)],'verify_cpu finite return contract')
  self.unknown=14;self.flag_unknown=16;self.emit(VERIFY,8,0,ret,1)
  target=self.memory(a[RSP],8);a[RSP]+=8;self.emit(JMP,8,RSP,target,target,target)
  self.alu(XOR,15,a[15],4 if debian else 8)
  if debian:
   self.load(RAX,K(H+0x50));self.alu(ADD,RAX,ROOT,8);self.alu(ADD,RAX,self.memory(K(H+0x58),8),8);self.wrcr(3,a[RAX])
   self.mov(RDX,32|4096,4);self.alu(OR,RDX,64,4);self.rdcr(4,RCX);self.alu(AND,RCX,a[RDX],4)
   for bit in (4,7):
    a[FLAGS]=(a[FLAGS]&~1)|(a[RCX]>>bit&1);a[RCX]|=1<<bit;self.emit(BTS,4,RCX,bit,a[RCX]);self.wrcr(4,a[RCX])
  else:
   self.load(RAX,K(H+0x58));self.alu(ADD,RAX,ROOT,8);self.rdcr(4,RCX);self.alu(AND,RCX,64,4);self.alu(OR,RCX,32|16|128,4)
   self.compare(self.memory(K(H+0x60),1),1,1,TEST);self.branch(6,bool(a[FLAGS]&64));self.wrcr(4,a[RCX]);self.alu(ADD,RAX,self.memory(K(H+0x50),8),8);self.wrcr(3,a[RAX])
   self.rdcr(4,RCX);self.mov(RAX,a[RCX]);self.alu(XOR,RCX,128,8);self.wrcr(4,a[RCX]);self.wrcr(4,a[RAX]);self.mov(RAX,K(TRAMP+0x900));self.emit(JMP,8,RAX,a[RAX],a[RAX],a[RAX])
  self.load(RCX,K(H+0x64),4);self.compare(a[RCX],1<<31,4,TEST);self.branch(7,not bool(a[FLAGS]&64));self.mov(RCX,0x1b,4)
  a[RAX]=0xc00;a[RDX]=0;self.unknown&=~(1<<RDX);self.emit(RDMSR,8,0x1b,0,0xc00)
  self.compare(a[RAX],1<<10,4,TEST);self.branch(8,not bool(a[FLAGS]&64));self.mov(RCX,0x802,4);a[RAX]=0x100+self.cpu;a[RDX]=0;self.emit(RDMSR,8,0x802,0,a[RAX])
  self.alu(XOR,RCX,a[RCX],4 if debian else 8);self.mov(RBX,K(H+0x80))
  while True:
   self.compare(a[RAX],self.memory(a[RBX]+a[RCX]*4,4),4)
   if self.branch(9,bool(a[FLAGS]&64)):break
   self.alu(INC,RCX,1,4);self.compare(a[RCX],self.memory(K(H+0x70),4),4);self.branch(10,bool(a[FLAGS]&1))
  self.load(RDX,K(H+0xa0)+a[RCX]*8);self.load(RAX,K(H+0xc0)+a[RDX]);self.load(RSP,a[RAX]);R(a[RSP]==K(0x304ff0+self.cpu*4096),'private stack')
  self.load(RAX,K(H+0x68));self.compare(a[RAX],a[RAX],8,TEST);self.branch(11,bool(a[FLAGS]&64))
  self.emit(STORE,4,a[RAX],0,0);self.emit(READY,8,self.cpu,a[RSP],1)
  R(not self.unknown and a[CR3]==ROOT and a[MODE]==1 and not a[FLAGS]&512,'startup terminal long-mode context')
  ready=list(a)
  if self.number<len(self.steps):
   d=self.controls[12];R(len(d)==1,'explicit runtime descriptor installation missing')
   oldg,newg,oldi,newi,cs,ss=d[0];R((oldg,oldi,cs,ss)==(a[GDTR],a[IDTR],16,24),'descriptor import old state')
   a[GDTR],a[GDTL],a[IDTR],a[IDTL]=newg,31,newi,4095
   target=K(0x320000+self.cpu*0x20000+0x3500);old=a[RIP];self.emit(JMP,8,old,target,target,target)
  R(self.number==len(self.steps),'extra/skipped source instructions')
  return ready

def startup_trace(records,n):
 scopes={};current=None;arch=[None]*n;chunks=[None]*n;walks=[[] for _ in range(n)];owner=0
 def word(pa,w):
  R(BASE<=pa<=BASE+SIZE-w and w in (1,2,4,8),'startup memory bounds/width')
  return int.from_bytes(memory[pa-BASE:pa-BASE+w],'little')
 def put(pa,w,v):memory[pa-BASE:pa-BASE+w]=v.to_bytes(w,'little')
 def path(cpu,va,width,write):
  a=arch[cpu];chain=walks[cpu];walks[cpu]=[]
  if not a[CR0]&(1<<31):R(not chain and a[MODE]==3,'PG-off translation path');return va&0xffffffff
  levels=5 if a[CR4]&4096 else 4;bits=12+9*levels
  R(a[CR4]&32 and a[EFER]&1024 and va>>bits==((1<<(64-bits))-1 if va>>(bits-1)&1 else 0),'startup canonical/PAE/LMA')
  table=a[CR3]&~4095;R(len(chain)<=levels,'excess startup walk updates')
  for level in range(levels,0,-1):
   shift=12+9*(level-1);slot=table+((va>>shift)&511)*8
   R(chain,'startup A/D walk record omitted');d=chain.pop(0);entry=d[5]
   R((d[1],d[2],d[3],d[4])==(slot,slot,8,1) and entry&1 and (not write or entry&2),'startup walk GPA/nonleaf permission')
   leaf=level==1 or bool(entry&128)
   R(not entry&128 or level==2,'unsupported startup large leaf')
   expected=entry|32|(64 if leaf and write else 0)
   R(d[6]==expected,'startup A/D update wrong')
   if leaf:
    R(not chain and (va&((1<<shift)-1))+width<=1<<shift,'startup leaf/crossing')
    return (entry&0x000ffffffffff000&~((1<<shift)-1))|(va&((1<<shift)-1))
   table=entry&0x000ffffffffff000
  raise ValueError('startup missing leaf')
 for sequence,kind,cpu,epoch,data in records:
  if kind!=16:continue
  subtype=data[0];d=tuple(data[1:]);R(0<=cpu<n and subtype in range(1,10),'Q34 event schema')
  if subtype==CASE:
   variant,rep,start,serial,publisher,cpus,responders=d
   R(cpus==n and variant in range(8) and rep in range(16) and serial==rep*8+variant+1 and publisher==rep%n and responders==(0 if variant==6 else ((1<<n)-1)^(1<<publisher)),'Q34 finite case identity')
   if start:
    R(start==1 and current is None and (variant,rep) not in scopes,'nested/duplicate startup case')
    current=dict(start=sequence,stop=None,serial=serial,variant=variant,rep=rep,publisher=publisher,responders=responders,steps=[{} for _ in range(n)],reads=[{} for _ in range(n)],locks=[{} for _ in range(n)],snapshots=[{} for _ in range(n)],controls=[{} for _ in range(n)],mail=[],ends={},booted={},ready={},projection=None,raw_controls=[[] for _ in range(n)],continuations=[[] for _ in range(n)])
    scopes[variant,rep]=current;memory=bytearray(SIZE);arch=[None]*n;chunks=[None]*n;walks=[[] for _ in range(n)];owner=0;published=0;initialized=False;cached={}
   else:
    R(current and (variant,rep)==(current['variant'],current['rep']) and len(current['ends'])==n and not owner and word(LOCK,8)==0x89abcdef00000000,'startup incomplete case/lock')
    R(all(x is None for x in chunks) and not any(walks),'unfinished startup state chunks or translation walk')
    current['memory']=bytes(memory);current['stop']=sequence
    for c in range(n):
     expected_boot=variant not in (5,6) and bool(responders>>c&1)
     R(bool(current['ends'][c][3])==expected_boot and bool(current['ends'][c][4])==expected_boot,'startup responder readiness')
     if expected_boot:
      st=Startup(c,variant,serial,n,cached[c][1],current['steps'][c],current['reads'][c],current['locks'][c],current['snapshots'][c],current['controls'][c])
      R(current['snapshots'][c][0]==st.a,'initial source startup state')
      current['ready'][c]=st.run()
      R(set(current['snapshots'][c])==set(range(st.number+1)) and not any(st.reads.values()),'extra startup snapshot or unconsumed source memory')
      R(current['ends'][c][5]==st.number,'source instruction counter')
     else:R(not current['steps'][c] and not current['snapshots'][c],'nonresponder ran startup')
    R(len(current['booted'])==n-1 if variant not in (5,6) else not current['booted'],'mailbox responder coverage')
    expected_requests=1 if variant in (5,6) else n-1+int(variant==7)
    R(published==expected_requests,'mailbox finite publication count')
    if variant in (5,6,7):R(any(k==5 for _,_,k,_ in current['mail']) and word(BASE,2)==1,'diagnostic cold stop missing')
    else:R(word(BASE,2)==0,'mailbox remained unacknowledged')
    current=None
   continue
  R(current is not None,'Q34 event outside case')
  if subtype==MEMORY:
   step,va,pa,w,write,old,new=d
   R(write in (0,1) and word(pa,w)==old and (write or old==new),'startup raw memory old/new disagreement')
   if arch[cpu] is not None:
    table_page=pa&~4095
    if va==pa and w==8 and write and table_page in (BOOT4,BOOT5,TRROOT,ROOT,LPDP,LPD,HPDP,HPD):walks[cpu].append(d)
    else:
     R(path(cpu,va,w,write)==pa,'startup translated GPA disagreement')
     current['reads'][cpu].setdefault(step+1,[]).append(d)
   else:R(cpu==0 or pa in (BASE+4,BASE+8),'startup raw write before context')
   if write:put(pa,w,new)
  elif subtype==MAIL:
   action,request,target,vector,command,booted,serial=d
   R(serial==current['serial'] and action in range(1,6),'mailbox ABI/action')
   if action==1:
    if not initialized:
     expected=initial_memory(n,serial);expected[4:8]=target.to_bytes(4,'little');expected[8:16]=vector.to_bytes(8,'little')
     R(memory==expected,'startup initialized physical fixture differs');initialized=True
    R(cpu==current['publisher'] and word(BASE,2)==0 and request==published+1 and vector==TRAMP+(request&15)*16 and word(BASE+4,4)==target and word(BASE+8,8)==vector and command==1 and booted==0,'mailbox release publication')
    pub=current['publisher'];variant=current['variant'];expected_target=0xffffffff if variant==5 else 0x100+(pub+1)%n if variant==6 or request==n else 0x100+(pub+request)%n
    R(target==expected_target,'mailbox requested wrong finite target')
    published=request;put(BASE,2,1)
   else:
    R(request==published and target==word(BASE+4,4) and vector==word(BASE+8,8),'mailbox cached request/field widths')
    if action==2:R(cpu!=current['publisher'] and (target!=0x100+cpu or not current['responders']>>cpu&1 or cpu in cached) and command==1 and booted==int(cpu in cached),'wrong-ID/repeated-boot ignore')
    elif action==3:
     R(cpu!=current['publisher'] and target==0x100+cpu and current['responders']>>cpu&1 and cpu not in cached and word(BASE,2)==command==booted==1,'mailbox boot-once cache')
     cached[cpu]=(request,vector);current['booted'][cpu]=sequence
    elif action==4:R(cached.get(cpu)==(request,vector) and command==0 and booted==1 and word(BASE,2)==1,'ACK without prior cache');put(BASE,2,0)
    else:R(cpu==current['publisher'] and command==word(BASE,2)==1 and booted==0,'invalid external diagnostic termination')
   current['mail'].append((sequence,cpu,action,d))
  elif subtype==STATE:
   serial,step,chunk,*words=d
   R(serial==current['serial'] and chunk in range(9),'startup snapshot identity')
   if chunk==0:R(chunks[cpu] is None and step not in current['snapshots'][cpu],'duplicate/nested startup snapshot');chunks[cpu]=[]
   R(chunks[cpu] is not None and len(chunks[cpu])==chunk*4,'startup snapshot chunk order')
   chunks[cpu].extend(words)
   if chunk==8:arch[cpu]=chunks[cpu];current['snapshots'][cpu][step]=list(arch[cpu]);chunks[cpu]=None
  elif subtype==STEP:
   step=d[0];R(step==len(current['steps'][cpu])+1 and arch[cpu] is not None and d[6]==arch[cpu][RIP],'startup step/source PC order')
   current['steps'][cpu][step]=d
  elif subtype==LOCK_EVENT:
   step,action,old,new,carry,stated_owner,canary=d
   R(action in (1,2) and word(LOCK,4)==old and word(LOCK+4,4)==canary==0x89abcdef,'BTS32/release width/canary/old')
   R(path(cpu,LOCK if action==1 else arch[cpu][RAX],4,1)==LOCK,'lock physical alias')
   if action==1:
    R(new==old|1 and carry==old&1,'BTS old carry')
    if not carry:R(owner==0,'duplicate lock ownership');owner=cpu+1
    R(stated_owner==owner,'lock owner mismatch')
   else:
    R(owner==cpu+1==stated_owner and old==1 and new==carry==0 and arch[cpu][RSP]==K(0x304ff0+cpu*4096),'lock released before private stack')
    owner=0
   R(step not in current['locks'][cpu],'duplicate source lock effect');put(LOCK,4,new);current['locks'][cpu][step]=d
  elif subtype==CONTROL:
   action=d[0];R(action in range(1,14),'unknown startup control')
   current['controls'][cpu].setdefault(action,[]).append(d[1:]);current['raw_controls'][cpu].append((sequence,d))
   if action==4:R(cpu in cached and d[1:]==(cpu,cached[cpu][1],*(initial_arch(cpu,current['variant'],cached[cpu][1])[i] for i in (CR0,CR3,CR4,EFER))),'startup arm context')
   if action==8:
    R(cpu==0 and d[1:3]==(BASE,SIZE) and d[4:]==(ROOT,current['serial'],0),'projection metadata')
    R(current['projection'] is None and d[3]<=1024,'duplicate or oversized startup projection')
    current['projection']=(sequence,bytes(memory),d)
  elif subtype==CONTINUE:
   current['continuations'][cpu].append((sequence,d))
  elif subtype==END:
   R(cpu not in current['ends'] and d[:3]==(current['serial'],current['variant'],current['rep']),'startup END identity')
   current['ends'][cpu]=d
 R(current is None and len(scopes)==128,'incomplete eight-variant/sixteen-repetition startup coverage')
 return scopes

def model_replay(records,initial,final,n,scopes):
 memory=bytearray(initial);R(len(memory)==q30.RAM_SIZE,'synthetic initial RAM')
 contexts=[q30.DEFAULT_CONTEXT]*n;pending={};operations=[];projection={x['projection'][0]:x for x in scopes.values()}
 for seq,kind,cpu,epoch,d in records:
  if kind==q30.CONTEXT_EVENT:contexts[cpu]=d[:5]
  elif kind==q30.MUTATION:
   pa,old,new,width,cause=d[:5];R(width==8 and cause in (2,3) and int.from_bytes(memory[pa:pa+width],'little')==old,'synthetic paging mutation old/cause')
   if cause==2:R((new&~96)==(old&~96) and new|old==new,'synthetic A/D update clears/change unrelated bits')
   memory[pa:pa+width]=new.to_bytes(width,'little')
  elif kind==q30.BEGIN:
   R(cpu not in pending,'overlapping synthetic CPU operation');pending[cpu]=dict(sequence=seq,cpu=cpu,begin=d,context=contexts[cpu])
  elif kind==q30.END:
   op=pending.pop(cpu);R(d[0]==op['sequence'],'synthetic native operation identity')
   va,access,width,value=op['begin'][:4];ctx=dict(zip(q30.CONTEXT,op['context']));ctx.pop('pge')
   walked=q30.walk(memory,gva=va,access='execute' if access in (3,4) else 'write' if access in (1,2) else 'read',**ctx)
   if walked['status']=='ok':
    R(d[2:4]==(0,0) and d[4]==walked['gpa'],'synthetic access GPA/status contradicts independent walk')
    pa=d[4]
    if access in (0,4):R(d[1]==int.from_bytes(memory[pa:pa+width],'little'),'synthetic data/fetch value')
    elif access==1:memory[pa:pa+width]=(value&mask(width)).to_bytes(width,'little')
    else:R(False,'unsupported Q34 synthetic native access kind')
   else:R(walked['status'] in ('pf','gp') and d[2:4]==({'pf':14,'gp':13}[walked['status']],walked['error_code']),'synthetic x86 fault mismatch')
   op.update(end_sequence=seq,end=d,end_walk=walked);operations.append(op)
  if seq in projection:
   scope=projection[seq];R(memory[BASE:BASE+SIZE]==scope['projection'][1],'projected runtime bytes differ from source startup backing')
 R(not pending and memory==final,'synthetic final RAM differs from independent mutation/native-effect replay')
 return operations

def continuation(records,operations,header,scopes,host,pool,blocks,prefix=0,native=False,initial_blocks=None,initial_transactions=None,cache_lines=None):
 n=header['ncpus'];q31=q33.q31;q32=q33.q32
 metadata=[];apic=[];objects=[];converted={}
 for (variant,rep),scope in scopes.items():
  converted[11,variant,rep]=dict(scope,events=[[] for _ in range(n)])
 for seq,kind,cpu,epoch,d in records:
  rec=(seq,kind,cpu,epoch,d)
  if seq<=prefix:
   if kind==13:
    metadata.append(rec)
    if d[0]==q31.CODE:objects.append(rec)
   continue
  if kind==16 and d[0]==CASE:
   variant,rep,start,serial,publisher,cpus,responders=d[1:]
   metadata.append((seq,13,cpu,epoch,(q31.CASE,11,variant,rep,start,serial,n,0)))
   apic.append((seq,14,cpu,epoch,(q32.CASE,11,variant,rep,start,serial,MASK,0)))
   objects.append((seq,15,cpu,epoch,(q33.CASE,11,variant,rep,start,serial,n,0)))
  elif kind==16 and d[0]==END:
   serial,variant,rep,boot,ready,steps,timer=d[1:]
   metadata.append((seq,13,cpu,epoch,(q31.END,serial,11,variant,rep,MASK,MASK,timer)))
   apic.append((seq,14,cpu,epoch,(q32.END,serial,11,variant,rep,0,0,0)))
  else:
   if kind==13:
    metadata.append(rec)
    if d[0]==q31.CODE:objects.append(rec)
   if kind==14:apic.append(rec)
   if kind==15:objects.append(rec)
  if kind==15:
   scope=next(k for k,x in converted.items() if x['start']<seq<x['stop']);converted[scope]['events'][cpu].append((seq,d[0],d[1:]))
 cases,counts=q31.index_trace(metadata,operations,n,prefix,{11:8},header['initial_q31_counts'],interrupt_mode=True,live_pin_recipes=True,additional_instructions=(17,18))
 apic_cases,apic_states=q32.apic_trace(apic,n,header['timer_intid'],header['spi_base'],header['symbols'],prefix,variant_counts={11:8},initial_states=header.get('initial_q32_states'),scope_end_counters=False,max_native_stage=7,broadcast_groups=())
 del metadata,apic
 bases={(key,cpu):(0x10000000+cpu*0x20000 if cpu==scope['publisher'] else K(0x320000+cpu*0x20000)) for key,scope in converted.items() for cpu in range(n)}
 q32.guest_semantics(cases,apic_cases,n,additional_instructions=(5,13,16,17,18),nonretiring_probes=(5,18),fixture_bases=bases,scalar_fetches=True)
 q33.branches(cases,n,fixture_bases=bases);q33.interrupt_effects(cases,apic_cases,n,fixture_bases=bases)
 transactions=q33.transport(host['transfers'],records,n,prefix,native,header['symbols'],initial_transactions)
 probe_cases={}
 for key,case in cases.items():
  filtered=dict(case,attempts=[list(row) for row in case['attempts']]);probe_cases[key]=filtered
  scope=converted[key]
  for cpu in range(n):
   if cpu not in scope['ready']:continue
   before=scope['continuations'][cpu]
   R(len(before)==2 and [d[0] for _,d in before]==[1,2],'Q34 generated continuation interval')
   first=next(a for a in case['attempts'][cpu] if a['sequence']>before[0][0])
   ops=[s['operation'] for s in case['steps'][cpu] if s['recipe']['attempt']==first['identity']]
   own=[s for s in case['states'][cpu] if s['attempt']==first['identity']]
   pa=0x320000+cpu*0x20000+0x3513
   R((first['instruction'],first['length'],first['width'],first['rip'])==(5,6,1,K(pa))
     and len(ops)==1 and ops[0]['begin'][:3]==(K(pa),4,1) and ops[0]['end'][1:5]==(0x48,0,0,pa)
     and all(s['point']&255 in (1,6) for s in own),'Q34 bootstrap page probe pretends full decoded source or retirement')
   following=next(a for a in case['attempts'][cpu] if a['sequence']>first['sequence'])
   a=next(s['arch'] for s in own if s['point']&255==1)
   b=next(s['arch'] for s in case['states'][cpu] if s['attempt']==following['identity'] and s['point']&255==1)
   R(a==b,'bootstrap source-page probe changed architecture')
   filtered['attempts'][cpu]=[a for a in filtered['attempts'][cpu] if a is not first]
 q33.source_probes(probe_cases,n,converted,transactions)
 published,totals=q33.objects_semantics(transactions,converted,cases,objects,n,pool,blocks,header['symbols'],native,initial_cache_lines=cache_lines or {cpu:(64,64) for cpu in range(n)},initial_blocks=initial_blocks)
 return dict(cases=cases,apic_cases=apic_cases,counts=counts,apic=apic_states,transactions=transactions,published=published,totals=totals,bases=bases)

def bridge(scopes,result,operations,n,native_faults=None):
 q31=q33.q31; opmap={o['sequence']:o for o in operations};gprs=q31.ARCH[:16]
 for key,scope in scopes.items():
  case=result['cases'][(11,)+key];serial=scope['serial']
  allcontrols=[(cpu,seq,d) for cpu,rows in enumerate(scope['raw_controls']) for seq,d in rows]
  split=[(seq,d) for _,seq,d in allcontrols if d[0]==11]
  R(len(split)==1 and split[0][1]==(11,HPD+8,0x3a0000,ROOT,0x200000,0x200000,serial),'runtime split changes imported root or range')
  for cpu in range(n):
   attempts=case['attempts'][cpu];states=case['states'][cpu];steps=case['steps'][cpu]
   if cpu not in scope['ready']:
    R(not any(a['instruction'] in (17,18) for a in attempts),'nonresponder imports or executes startup continuation');continue
   ready=scope['ready'][cpu];b=0x320000+cpu*0x20000;v=K(b)
   def control(action):
    rows=[(seq,d) for seq,d in scope['raw_controls'][cpu] if d[0]==action];R(len(rows)==1,'missing/duplicate startup runtime control');return rows[0]
   c9,d9=control(9);c10,d10=control(10);c12,d12=control(12)
   R(d9==(9,ready[RIP],ready[RSP],ROOT,16,24,serial) and scope['projection'][0]<c9<c10<split[0][0]<c12,'startup import/control ordering or register contract')
   R(d12==(12,ready[GDTR],v+0x10000,ready[IDTR],v+0x1000,16,24),'post-startup descriptor fixture contract')
   imported=next(s for s in states if s['sequence']>c9)
   expected=dict(zip(gprs,ready[:16]));expected.update(rip=ready[RIP],rflags=ready[FLAGS],cs=16,ss=24,cr2=0xdecaf000+cpu)
   R(imported['point']==6 and imported['arch']==expected,'startup canonical context was reset or incompletely imported')
   probe=attempts[0];own=[s for s in states if s['attempt']==probe['identity']];po=[s for s in steps if s['recipe']['attempt']==probe['identity']]
   R((probe['instruction'],probe['rip'],probe['length'],probe['width'])==(18,ready[RIP],1,8) and len(po)==1 and po[0]['action']&255==6,'diagnostic imported stack probe identity/access')
   operation=po[0]['operation'];pa=ready[RSP]-KBASE;value=int.from_bytes(scope['projection'][1][pa-BASE:pa-BASE+8],'little')
   R(d10==(10,ready[RSP],pa,value,operation['sequence'],ROOT,serial) and operation['begin'][:3]==(ready[RSP],0,8) and operation['end'][1:5]==(value,0,0,pa),'imported stack native operation GPA/value/root')
   R(operation['context']==(ROOT,0,1,1,1),'imported execution context flags/CR3')
   R(all(s['arch']==expected and s['point'] in (1,6) for s in own if s['sequence']<c12),'nonretiring stack probe changed canonical state')
   afterjump=dict(expected,rip=v+0x3500)
   R([s for s in own if s['sequence']>c12][0]['arch']==afterjump,'post-startup branch changes registers beyond RIP')
   core=attempts[1:6]
   R([(a['instruction'],a['rip'],a['length'],a['width']) for a in core]==[(17,v+0x3500,3,8),(1,v+0x3503,3,8),(17,v+0x3506,10,8),(1,v+0x3510,3,8),(1,v+0x3510,3,8)],'actual continuation opcode sequence changed')
   current=afterjump
   for j,a in enumerate(core):
    snapshots=[s for s in states if s['attempt']==a['identity']];begin=next(s for s in snapshots if s['point']==1)
    R(begin['arch']==current,'continuation instruction starts from reset/disconnected architecture')
    ownsteps=[s for s in steps if s['recipe']['attempt']==a['identity']]
    fetch=[s['operation'] for s in ownsteps if s['action']&255==7]
    bytes_=b'\x48\x89\xe7' if j==0 else b'\x48\xbf'+(v+0xd000).to_bytes(8,'little') if j==2 else b'\x48\x8b\x07'
    R(len(fetch)==len(bytes_) and all(o['begin'][:3]==(a['rip']+i,4,1) and o['end'][1:5]==(byte,0,0,b+0x3500+(a['rip']-(v+0x3500))+i) for i,(o,byte) in enumerate(zip(fetch,bytes_))),'actual continuation instruction fetch bytes/GPA')
    if j==3:
     reads=[s['operation'] for s in ownsteps if s['action']&255==1]
     R(len(reads)==1 and reads[0]['begin'][:3]==(v+0xd000,0,8) and reads[0]['end'][2:4]==(14,0),'continued instruction must incur actual denied load')
     if native_faults is not None:R(reads[0]['sequence'] in native_faults,'continued denied load lacks native fault/recovery')
     returned=[s for s in snapshots if s['point']==4];R(len(returned)==1,'continued page fault missing IRET')
     current=dict(current,cr2=v+0xd000,rflags=current['rflags']|65536)
     R(returned[0]['arch']==current,'page-fault IRET reset original registers/private stack');continue
    commits=[s for s in snapshots if s['point']==5];R(len(commits)==1,'continued actual instruction retirement count')
    current=dict(current,rip=a['rip']+a['length'],rflags=current['rflags']&~65536)
    if j==0:current['rdi']=current['rsp']
    elif j==2:current['rdi']=v+0xd000
    else:
     reads=[s['operation'] for s in ownsteps if s['action']&255==1];R(len(reads)==1,'continued MOV data read count')
     want=value if j==1 else 0x3400000000000000|serial*16|cpu;gpa=pa if j==1 else b+0xd000
     R(reads[0]['end'][1:5]==(want,0,0,gpa),'continued MOV result/backing identity');current['rax']=want
    R(commits[0]['arch']==current,'continued MOV architectural effect')
   before,after=scope['continuations'][cpu];operand=17+cpu;want=(current['rax']+operand)&MASK
   R(before[1][0:6]==(1,current['rax'],operand,0,1,ready[RSP]) and after[1][0:6]==(2,current['rax'],operand,want,1,ready[RSP]) and before[1][6]==after[1][6]==next(d[3] for _,c,a,d in scope['mail'] if c==cpu and a==3),'generated continuation input/output/private-stack binding')
   add=[a for a in attempts if a['instruction']==13];R(len(add)==1 and before[0]<add[0]['sequence']<after[0],'generated ADD interval/count')
   ab=next(s for s in states if s['attempt']==add[0]['identity'] and s['point']==1);ac=next(s for s in states if s['attempt']==add[0]['identity'] and s['point']==5)
   R(ab['arch']==current and ac['arch']['rax']==want and ac['arch']['rip']==v+0x3519,'generated ADD is disconnected from fault retry')
   tail=[a for a in attempts if after[0]<a['sequence'] and a['rip'] in (v+0x3519,v+0x351a)]
   R([(a['instruction'],a['length']) for a in tail]==[(9,1),(12,1)],'contiguous STI/NOP after generated result')
   cur=ac['arch']
   for a in tail:
    ab=next(s for s in states if s['attempt']==a['identity'] and s['point']==1);ac=next(s for s in states if s['attempt']==a['identity'] and s['point']==5)
    R(ab['arch']==cur,'STI/NOP resets generated canonical state');cur=ac['arch']
   final=states[-1];R(final['point']==6 and final['arch']==cur and cur['rax']==want and cur['rsp']==ready[RSP] and cur['cs']==16 and cur['ss']==24,'post-IRQ final state differs from continued canonical state')
   for o in operations:
    if o['cpu']==cpu and c9<o['sequence']<scope['stop']:R(o['context']==(ROOT,0,1,1,1),'continued execution switched imported root/context')

class CodeState(q33.CodeState):
 q34=True
 def __init__(self,scopes):
  super().__init__();self.projections={v['projection'][0]:v['projection'][1] for v in scopes.values()};self.started=False;self.projected=0;self.projecting=False
 def event(self,sequence,kind,cpu,epoch,data,phase,owner,active):
  if kind==16:
   R(data[0] in range(1,10),'unknown Q34 native metadata')
   self.started=True
   if data[0]==CASE and data[3]==1:self.projected=0;self.projecting=True
   if sequence in self.projections:
    R(self.memory[BASE:BASE+SIZE]==self.projections[sequence] and self.projected==data[4],'native projection differs from startup physical backing or changed-word count');self.projecting=False
   return False
  if self.projecting and kind==q30.MUTATION and BASE<=data[0]<BASE+SIZE:
   R(data[4]==q30.SETUP and data[3]==8 and data[1]!=data[2],'startup projection bypassed retained SETUP word mutation');self.projected+=1
  if kind==13 and data[0]==q33.q31.ATTEMPT and data[4] in (17,18):
   R(self.started,'startup-only recipe before Q34')
   _,attempt,identity,rip,instruction,length,width,generation=data
   R(width==8 and (length==1 if instruction==18 else length in (3,10)),'Q34 native recipe length/width')
   if instruction==17:
    ctx=dict(zip(q30.CONTEXT,self.contexts[cpu]));ctx.pop('pge');actual=bytearray()
    for address in range(rip,rip+length):
     walk=q30.walk(self.memory,gva=address,access='execute',**ctx);R(walk['status']=='ok','MOV RDI has no executable source bytes');actual.append(self.memory[walk['gpa']])
    R(actual==b'\x48\x89\xe7' if length==3 else actual[:2]==b'\x48\xbf','MOV RDI native recipe differs from coherent source')
   return False
  return super().event(sequence,kind,cpu,epoch,data,phase,owner,active)

STATE_OFFSET=0x700000; STATE_BYTES=135168
STATE_HEADER='magic version ncpus failure phase cases serial variant repetition publisher request_sequence request_count responders finish lock_owner contention io_lock diagnostic_request diagnostic_ack diagnostic_reason diagnostic_cpu checkpoint_records checkpoint_epoch'.split()
CPU_HEADER='cases booted cached_id cached_vector seen ignored acknowledged steps lock_attempts lock_acquired stack_private ready continuations failures efer_reads efer_writes protected_steps compat_steps long_steps timer_start timer_end input result'.split()
def decode_state(blob,n,base=0):
 R(len(blob)>=base+STATE_BYTES,'truncated Q34 state')
 h=dict(zip(STATE_HEADER,struct.unpack_from('<23Q',blob,base)))
 R((h['magic'],h['version'],h['ncpus'],h['failure'])==(0x5133345354415254,1,n,0),'Q34 identity/version/failure')
 cpus=[]
 for cpu in range(4):
  words=struct.unpack_from('<59Q',blob,base+184+cpu*472)
  if cpu>=n:R(not any(words),'unused Q34 CPU state')
  else:cpus.append(dict(zip(CPU_HEADER,words[:23]),arch=list(words[23:])))
 R(not any(blob[base+2072:base+4096]),'Q34 state alignment padding changed')
 return h,cpus,bytes(blob[base+4096:base+STATE_BYTES])

def scenario_controls(scopes,result,records,operations,n,logs=None,symbols=None):
 q31,q32=q33.q31,q33.q32
 locks={};gates=[]
 for seq,k,c,e,d in records:
  if k==16 and d[0]==LOCK_EVENT:locks[seq]=(c,d[1:])
  if k==q30.GATE:gates.append((seq,c,d))
 for key,scope in scopes.items():
  variant,rep=key;pub=scope['publisher'];serial=scope['serial'];positive=variant not in (5,6)
  controls=[(c,seq,d) for c,rows in enumerate(scope['raw_controls']) for seq,d in rows]
  R(all(c==pub for c,seq,d in controls if d[0] in (5,6,7)),'publisher control forged by AP')
  mails=scope['mail'];case=result['cases'][(11,)+key];irq=result['apic_cases'][(11,)+key]
  def selected(action,cpu=None):return sorted((seq,c,d) for c,seq,d in controls if d[0]==action and (cpu is None or c==cpu))
  for cpu in range(n):
   if cpu in scope['ready']:
    R(len(selected(4,cpu))==1,'missing/duplicate source initialization control')
    stack=selected(3,cpu);R(len(stack)==1 and stack[0][2]==(3,cpu,K(0x304ff0+cpu*4096),cpu+1,ROOT,1,serial),'private stack source control missing')
   else:R(not selected(3,cpu),'nonresponder private stack')
   arrivals=[(s,d) for s,k,d in irq['events'][cpu] if k==q32.NATIVE]
   required=(n-1 if cpu==pub else 1) if positive else 0
   R(len(arrivals)==required,'Q34 actual native timer count')
   for seq,d in arrivals:
    R(d[0]==q32.TIMER and d[4]==1,'Q34 timer source/interior stage')
    if symbols:R(symbols['_q32_hold_begin']<=d[2]<symbols['_q32_hold_end'],'Q34 timer lacks native interior PC')
   if cpu in scope['ready']:
    R(arrivals[0][0]>scope['continuations'][cpu][-1][0],'Q34 timer predates generated continuation')
  starts,ends=selected(6,pub),selected(7,pub)
  R(len(starts)==len(ends)==(n-1 if positive else 0),'publisher overlap protocol count')
  for index,((begin,c,d),(end,_,f)) in enumerate(zip(starts,ends),1):
   ack=[s for s,c,k,data in mails if k==4 and data[1]==index]
   nextpub=[s for s,c,k,data in mails if k==1 and data[1]==index+1]
   R(len(ack)==1 and ack[0]<begin<end and (not nextpub or end<nextpub[0]) and d[1]==f[1]==index,'publisher activity outside ACK/publication interval')
   ops=[o for o in operations if o['cpu']==pub and begin<o['sequence']<end]
   R(ops and len([a for a in case['attempts'][pub] if begin<a['sequence']<end and a['instruction']==1])==1,'publisher earlier MOV missing')
   first=ops[0];v=0x10000000+pub*0x20000
   R(first['begin'][:3]==(v+0xc000,0,8) and first['end'][1:4]==(0x60c000+pub,0,0) and f[6]==first['end'][1],'publisher MOV wrong earlier fixture value')
   if symbols:
    invalidate=[(s,c,d) for s,c,d in gates if begin<s<first['sequence'] and c==pub and d[0]==1]
    R(invalidate,'publisher omitted actual Q30 invalidate epoch')
   R(len([s for s,k,d in irq['events'][pub] if k==q32.NATIVE and begin<s<end])==1,'publisher timer missing from overlap')
   R(any(s['point']==4 and begin<s['sequence']<end for s in case['states'][pub]),'publisher native timer has no actual guest IRET')
  first=(pub+1)%n
  waitbegin,waitend=selected(1),selected(2)
  if n==4 and positive:
   R(len(waitbegin)==len(waitend)==1,'four-CPU forced contention omitted')
   begin,c,d=waitbegin[0];end,c2,f=waitend[0]
   R(c==c2==first and d==(1,first,first+1,0x2ff0,serial,0,0) and f[0]==2 and f[1]==first and f[3:]==(first+1,serial,0,0) and begin<end,'forced owner/wait context')
   contention=0
   for seq,(cpu,data) in locks.items():
    if scope['start']<seq<end and data[1]==1 and data[4]:
     R(data[5]==first+1 and cpu!=first,'contention before first owner released');contention|=1<<cpu
   R(contention and f[2]==contention,'contention marker lacks real failed BTS')
   R(starts[0][2][2]==first+1 and ends[0][2][2]==first+1 and ends[0][0]<end,'earlier mechanism did not overlap held first AP lock')
   R(not any(begin<s<end and data[1]==2 and cpu==first for s,(cpu,data) in locks.items()),'first AP releases lock before contention')
  else:R(not waitbegin and not waitend,'unexpected forced contention protocol')
  diagnostic=selected(5)
  if variant in (5,6,7):
   timeout=[(s,c,d) for s,c,k,d in mails if k==5]
   R(len(diagnostic)==len(timeout)==1 and diagnostic[0][1]==pub and diagnostic[0][2]==(5,variant,serial,serial,1,serial,0) and timeout[0][0]<diagnostic[0][0],'diagnostic ACK missing/wrong context')
   seq,c,d=timeout[0];lastpub=[s for s,c,k,data in mails if k==1 and data[1]==d[1]][0]
   R({c for s,c,k,data in mails if lastpub<s<seq and k==2 and data[1]==d[1]}==set(range(n))-{pub},'cold diagnostic before all AP ignored observations')
   if logs is not None:
    rows=[r for r in logs if r.get('q34_diagnostic_stop') is True and r['case']==serial]
    expected=dict(q34_diagnostic_stop=True,cpu=pub,case=serial,reason=variant,request_sequence=d[1],command=1,apic_id=d[2],vector=d[3])
    R(rows==[expected],'fabricated/missing actual host diagnostic observation')
  else:R(not diagnostic,'positive boot has cold diagnostic')
 if logs is not None:R(len([r for r in logs if r.get('q34_diagnostic_stop') is True])==48,'extra host startup diagnostic')

def final_startup(h,cpus,memory,scopes,n,logs=None):
 scope=scopes[7,15];pub=15%n;expected_requests=n
 R((h['phase'],h['cases'],h['serial'],h['variant'],h['repetition'],h['publisher'])==(2,128,128,7,15,pub),'final Q34 case/phase')
 R((h['request_sequence'],h['request_count'],h['responders'],h['finish'],h['lock_owner'],h['io_lock'],h['diagnostic_request'],h['diagnostic_ack'],h['diagnostic_reason'],h['diagnostic_cpu'])==(n,n,((1<<n)-1)^(1<<pub),1,0,0,128,128,7,pub),'final Q34 mailbox/ownership state')
 R(memory==scope['memory'],'final whole startup backing mismatch')
 contention=0
 for cpu,c in enumerate(cpus):
  ready=cpu!=pub;rows=scope['steps'][cpu];mail=[d for s,k,a,d in scope['mail'] if k==cpu];lock=list(scope['locks'][cpu].values())
  for d in lock:
   if d[1]==1 and d[4]:contention|=1<<cpu
  arch=scope['snapshots'][cpu][len(rows)] if ready else [0]*36
  expected=dict(cases=128,booted=int(ready),cached_id=0x100+cpu if ready else 0,cached_vector=next((d[3] for d in mail if d[0]==3),0),seen=max((d[1] for d in mail if d[0] in (2,4)),default=0),ignored=sum(d[0]==2 for d in mail),acknowledged=int(ready),steps=len(rows),lock_attempts=sum(d[1]==1 for d in lock),lock_acquired=int(ready),stack_private=int(ready),ready=int(ready),continuations=int(ready),failures=0,efer_reads=int(ready),efer_writes=0,protected_steps=sum(s[MODE]==3 for step,s in scope['snapshots'][cpu].items() if step),compat_steps=sum(s[MODE]==2 for step,s in scope['snapshots'][cpu].items() if step),long_steps=sum(s[MODE]==1 for step,s in scope['snapshots'][cpu].items() if step),input=(0x3400000000000000|128*16|cpu) if ready else 0,result=((0x3400000000000000|128*16|cpu)+17+cpu) if ready else 0,arch=arch)
  R(all(c[k]==v for k,v in expected.items()),'Q34 final per-CPU raw/source replay disagreement')
  R(c['timer_start']<=scope['ends'][cpu][-1]==c['timer_end'] and c['timer_start']<c['timer_end'],'Q34 final timer bounds')
  if logs is not None:
   rows=[r for r in logs if r.get('cpu')==cpu and 'q34_cases' in r]
   R(rows==[dict(cpu=cpu,q34_cases=128,q34_ready=int(ready),q34_continuations=int(ready),q34_failure=0,q34_cr3=arch[CR3],q34_rsp=arch[RSP])],'Q34 host final CPU mismatch')
 R(h['contention']==contention,'Q34 raw contention mask mismatch')

def host_transfers(logs):
 all_transfers={(r['cpu'],r['request'][1]) for r in logs if r.get('q33_transfer') is True}
 R(all((r['cpu'],r['transaction']) in all_transfers for r in logs if r.get('q33_produced') is True),'orphan host production outside Q33/Q34 scopes')
 R(all(r['request'][12] in range(1,12) for r in logs if r.get('q33_transfer') is True),'host transfer outside declared Q33/Q34 scope')
 eligible={(r['cpu'],r['request'][1]) for r in logs if r.get('q33_transfer') is True and r['request'][12]==11}
 produced={}
 for r in logs:
  if r.get('q33_produced') is True and (r['cpu'],r['transaction']) in eligible:
   key=r['cpu'],r['transaction'];R(key not in produced,'duplicate Q34 generated object');produced[key]=r
 consumed=set();transfers=[]
 for row in logs:
  if row.get('q33_transfer') is not True or (row['cpu'],row['request'][1]) not in eligible:continue
  row=dict(row)
  if row['request'][0]==q33.REQUEST_CHUNK and row['request'][2]==64:
   key=row['cpu'],row['request'][1];R(key in produced and key not in consumed,'missing/repeated Q34 host production');consumed.add(key);row.update(produced_hex=produced[key]['bytes_hex'],produced_request=produced[key]['request'])
  transfers.append(row)
 R(consumed==set(produced),'orphan Q34 produced code')
 return dict(transfers=transfers)


def check(artifact,logs,symbols,ram,initial):
 q31,q32=q33.q31,q33.q32
 symbols=dict(symbols,_text_end=min(symbols[k] for k in ('_l1','_l2','_device_l2','_pages','_aliases')))
 saved=q30.snapshot(artifact('q33-checkpoint.bin'),6)
 previous=q33.check(artifact,logs,symbols,saved,initial,version=6,terminal=False,snapshot_only=True)
 R(previous['status']=='PASS','Q34 previous growing program is not accepted by independent checker')
 prefix,prefixcpus,_,_=q30.decode(saved,initial,version=6,records_required=False)
 n,count=prefix['ncpus'],prefix['records']
 old31,old31cpus,oldgens,_,_=q31.decode(saved,n,version=6);old32,old32cpus=q32.decode(saved,n);old33,old33cpus,oldblocks=q33.decode(saved,n,6)
 h,cpus,memory=decode_state(ram,n,STATE_OFFSET);oldh,oldcpus,oldmemory=decode_state(saved,n,STATE_OFFSET)
 R(oldh==dict.fromkeys(STATE_HEADER,0)|dict(magic=h['magic'],version=1,ncpus=n,checkpoint_records=count,checkpoint_epoch=prefix['epoch']) and not any(saved[STATE_OFFSET+184:STATE_OFFSET+STATE_BYTES]),'Q34 started before same-VM Q33 checkpoint')
 header,paging,roles,records=q30.decode(ram,initial,version=6)
 R(header['ncpus']==n and header['records']>count and (h['checkpoint_records'],h['checkpoint_epoch'])==(count,prefix['epoch']),'Q34 checkpoint identity/continued state')
 end=q30.LOG_OFFSET+count*96
 R(memoryview(saved)[q30.LOG_OFFSET:end]==memoryview(ram)[q30.LOG_OFFSET:end],'Q34 changed accepted Q33 trace prefix')
 summaries=[r for r in logs if 'q33_checkpoint_dump_complete' in r]
 R(len(summaries)==1 and summaries[0]['q33_checkpoint_dump_complete'] is True and summaries[0]['q33_checkpoint_host_checks_pass'] is True and tuple(summaries[0]['q33_checkpoint_'+k] for k in ('ram_bytes','records','epoch'))==(len(saved),count,prefix['epoch']),'Q33 all-vCPU checkpoint host evidence')
 for cpu in range(n):
  rows=[r for r in logs if r.get('cpu')==cpu and 'q33_checkpoint_instruction_pc' in r];R(len(rows)==1,'Q33 stopped-owner checkpoint registers');r=rows[0]
  R(r['q33_checkpoint_register_check'] is True and r['q33_checkpoint_actlr']&2 and not r['q33_checkpoint_cpsr']&128 and r['q33_checkpoint_resume_pc']==r['q33_checkpoint_instruction_pc']+4 and struct.unpack_from('<I',saved,r['q33_checkpoint_instruction_pc']-0x40000000)[0]==0xD40006E2 and tuple(r['q33_checkpoint_'+k] for k in ('tcr','ttbr0','ttbr1'))==tuple(prefixcpus[cpu]['final_'+k] for k in ('tcr','ttbr0','ttbr1')),'Q33 native checkpoint TSO/paging/HVC mismatch')
 R(saved[q33.POOL-0x40000000:q33.POOL-0x40000000+old33['pool_bytes']]==ram[q33.POOL-0x40000000:q33.POOL-0x40000000+old33['pool_bytes']],'Q34 overwrote preceding immutable native pool')
 saved.close();del saved,oldmemory,oldcpus
 scopes=startup_trace(records,n);code=CodeState(scopes)
 replay=q30.replay(initial,records,n,version=6,code_state=code)
 q30.foundation(ram,paging,symbols,terminal=True,version=6);q30.validate_native_faults(records,symbols);q30.validate_snapshot(ram,header,paging,roles,replay)
 R(not replay['incomplete'],'unsupported continued paging replay')
 for name,off,size in [('q31.bin',q31.STATE,q31.STATE_BYTES),('q32.bin',q32.STATE,q32.STATE_BYTES),('q33.bin',q33.STATE,q33.STATE_BYTES),('q34.bin',STATE_OFFSET,STATE_BYTES)]:R(artifact(name).read_bytes()==ram[off:off+size],'separate continued dump differs: '+name)
 new33,new33cpus,blocks=q33.decode(ram,n,6)
 R(all(blocks[i]==old for i,old in enumerate(oldblocks) if old['id']),'Q34 deleted or changed earlier immutable descriptors')
 R(new33['allocated']==sum(bool(b['id']) for b in blocks) and new33['pool_bytes']==sum((b['length']+83)&~63 for b in blocks if b['id']),'Q34 allocated native objects/raw pool size')
 settings=dict(ncpus=n,timer_intid=q30.word(ram,0x800000+32),spi_base=old32['spi_base'],symbols=symbols,initial_q31_counts=old31cpus,initial_q32_states=old32cpus)
 cache_lines={c:(4<<((d[2]>>16)&15),4<<(d[2]&15)) for s,k,c,e,d in records if k==15 and d[:2]==(q33.CONTROL,7)}
 result=continuation(records,replay['operations'],settings,scopes,host_transfers(logs),ram[q33.POOL-0x40000000:q33.POOL_END-0x40000000],blocks,count,True,oldblocks,[c['transaction'] for c in old33cpus],cache_lines)
 bridge(scopes,result,replay['operations'],n,{d[0] for s,k,c,e,d in records if k==q30.FAULT})
 scenario_controls(scopes,result,records,replay['operations'],n,logs,symbols);final_startup(h,cpus,memory,scopes,n,logs)
 new31,new31cpus,gens,users,code_roles=q31.decode(ram,n,6);new32,new32cpus=q32.decode(ram,n)
 R(new31==old31 and new32==old32 and tuple(code.generations)==gens and tuple(code.users)==users and bytes(code.roles)==code_roles and not any(users) and not any(code_roles) and not code.pending_generation and not any(code.pins.values()),'continued metadata ownership/phase identity')
 R({k:v for k,v in new33.items() if k not in ('allocated','pool_bytes')}=={k:v for k,v in old33.items() if k not in ('allocated','pool_bytes')} and new33['allocated']==old33['allocated']+len(result['published']) and new33['pool_bytes']==old33['pool_bytes']+len(result['published'])*128,'Q34 code pool/phase accounting')
 for cpu in range(n):
  state,old=new31cpus[cpu],old31cpus[cpu];final=result['cases'][11,7,15]
  R(all(state[k]==old[k] for k in ('started','done','cases','timer_start','timer_end')) and state['depth']==0 and state['code_writes']==code.writes[cpu] and state['arch']==final['states'][cpu][-1]['arch'],'Q34 final canonical state or earlier Q31 completion reset')
  R(all(state[k]==result['counts'][cpu][k] for k in ('attempts','deliveries','commits','snapshots')),'Q34 continued canonical counters')
  attempts=final['attempts'][cpu];last=attempts[-1] if attempts else None;steps=final['steps'][cpu]
  meta=dict(identity=last['rip'],generation=last['generation'],rip=last['rip'],length=last['length'],instruction=last['instruction'],access=0,width=last['width'],substep=0) if last else dict(identity=0x10000000+cpu*0x20000+0x3000,generation=1,rip=0x10000000+cpu*0x20000+0x3000,length=1,instruction=1,access=0,width=8,substep=0)
  if steps and (last is None or steps[-1]['sequence']>last['sequence']):
   recipe=steps[-1]['recipe'];meta.update(identity=recipe['rip'],generation=recipe['generation'],rip=recipe['rip'],instruction=recipe['instruction'],access=recipe['access'],width=recipe['width'],substep=recipe['substep'])
  R(state['current_attempt']==(last['identity'] if last else 0) and state['metadata']==meta,'Q34 final native recovery recipe')
  actual,old,apic=new32cpus[cpu],old32cpus[cpu],result['apic'][cpu]
  R(all(actual[k]==old[k] for k in ('started','done','cases','timer_start','timer_end','diagnostic_request','diagnostic_ack','diagnostic_case')),'Q34 reset accepted Q32 completion')
  R(all(actual[k]==apic[k] for k in ('native_count','guest_count','boundaries','wake_generation')) and actual['handler_effects']==actual['guest_count'] and not q32.bitmap(actual['irr']) and not q32.bitmap(actual['isr']) and actual['stage']==actual['shadow']==actual['tpr']==actual['halted']==actual['hook_stage']==0,'Q34 final native/guest IRQ accounting')
  for name in ('emitted','accepted','vectors'):
   R(all(actual[name][source][sender]==apic[name].get((source+1,sender),0) for source in range(3) for sender in range(4)),'Q34 final interrupt mailbox')
  R(actual['emitted']==actual['accepted'] and actual['sgi_sent']==tuple(apic['sgi_sent']) and actual['raw_irq'][1:]==tuple(apic['raw_irq'][1:]) and actual['empty_irq'][1:]==tuple(apic['empty_irq'][1:]),'Q34 undisclosed source ownership')
  R(all(actual[k]==old[k] for k in ('spi_request','spi_sent','spi_deassert','spi_deasserted','spi_level')),'Q34 changed unrelated completed device notification')
  c,old=new33cpus[cpu],old33cpus[cpu];total=result['totals'][cpu]
  R(all(c[k]==old[k]+v for k,v in total.items()) and c['transaction']==old['transaction']+total['requests'],'Q34 host-production cumulative counters')
  preserved=set(q33.CPU)-set(total)-{'transaction','group','variant','repetition','phase','progress'}
  R(all(c[k]==old[k] for k in preserved) and c['phase']==old['phase'] and c['progress']>=old['progress'],'Q34 reset earlier code state or left new ownership')
  R((c['group'],c['variant'],c['repetition'])==((11,7,15) if cpu!=15%n else (11,7,14)),'Q34 last transaction scope')
  for tag,raw,fields in [('q31',state,q31.CPU[:10]),('q32',actual,('started','done','cases','native_count','guest_count','boundaries','timer_start','timer_end')),('q33',c,('started','done','cases','requests','chunks','entries','effects','timer_start','timer_end'))]:
   rows=[r for r in logs if r.get('cpu')==cpu and tag+'_started' in r]
   R(len(rows)==1 and all(rows[0][tag+'_'+k]==raw[k] for k in fields),'Q34 host/raw continued '+tag+' summary')
 return dict(format='natemu-q34-independent-check-v1',status='PASS',checkpoint=previous,records=len(records),operations=len(replay['operations']),cases=128,checked=['unchanged accepted Q30-Q33 same-VM checkpoints','whole memory/startup source branch replay and mode transitions','concrete context import plus native PF/IRET, host-produced ADD and timer return','SMP lock contention and publisher overlap; actual external cold-context diagnostics'],unsupported=[])


def model(path):
 with path.open() as f:
  header=json.loads(next(f));records=[(r[0],r[1],r[2],r[3],tuple(r[4])) for r in map(json.loads,f)]
 R(header['format']=='natemu-q34-model-trace-v1' and header['ncpus'] in (2,4),'Q34 synthetic trace header')
 scopes=startup_trace(records,header['ncpus'])
 stem=str(path).removesuffix('.jsonl')
 operations=model_replay(records,Path(stem+'-initial-memory.bin').read_bytes(),Path(stem+'-final-memory.bin').read_bytes(),header['ncpus'],scopes)
 _,_,blocks=q33.decode_state(Path(stem+'-q33-state.bin').read_bytes(),header['ncpus'])
 result=continuation(records,operations,header,scopes,json.loads(Path(stem+'-host.json').read_text()),Path(stem+'-pool.bin').read_bytes(),blocks)
 bridge(scopes,result,operations,header['ncpus'])
 scenario_controls(scopes,result,records,operations,header['ncpus'])
 h,c,m=decode_state(Path(stem+'-q34-state.bin').read_bytes(),header['ncpus']);final_startup(h,c,m,scopes,header['ncpus'])
 return header,records,scopes,result

def self_check():
 rejected=[]
 def reject(name,f):
  try:f()
  except ValueError:rejected.append(name)
  else:raise AssertionError('Q34 corruption accepted: '+name)
 rows=[(i+1,16,i%2,0,(CASE,0,0,1,1,0,2,2)) for i in range(3)]
 packed=b''.join(struct.pack('<12Q',*r[:4],*r[4]) for r in rows)
 view=q30.RecordView(packed,0,3)
 assert list(view)==rows and list(view[1:])==rows[1:] and view[-1]==rows[-1]
 reject('strided raw record view',lambda:view[::2])
 metadata=[]
 for ident in (1,2):
  for chunk in range(7):metadata.append((len(metadata)+1,13,0,0,(q33.q31.STATE_EVENT,ident,1,ident,chunk,chunk*3,chunk*3+1,chunk*3+2)))
 assert len(q33.q31.snapshots(metadata,7))==1
 corrupt=list(metadata);seq,k,c,e,d=corrupt[1];corrupt[1]=(seq,k,c,e,(*d[:4],3,*d[5:]))
 reject('discarded-prefix malformed state chunks',lambda:q33.q31.snapshots(corrupt,7))
 reject('orphan host-produced object',lambda:host_transfers([dict(q33_produced=True,cpu=0,transaction=999)]))
 # Native geometry is independently reconstructed, not read back from producer labels.
 memory=bytearray(65536);syms=dict(_l1=0x40001000,_l2=0x40002000,_aliases=0x40004000,_device_l2=0x40005000,_pages=0x40006000)
 def put(p,v):struct.pack_into('<Q',memory,p-0x40000000,v)
 for i,p in ((0,syms['_device_l2']),(1,syms['_l2']),(2,syms['_l2']+4096)):put(syms['_l1']+8*i,p|3)
 for i in range(992):put(syms['_l2']+i*8,(syms['_pages']+i*4096)|3 if i<8 else (0x40000000+i*0x200000)|0x701)
 put(syms['_l2']+992*8,syms['_aliases']|3);put(syms['_aliases'],0x40900703)
 for i in range(4096):put(syms['_pages']+i*8,(0x40000000+i*4096)|0x703)
 put(syms['_device_l2']+128*8,0x10000000|0x405|(3<<53))
 for i in range(8):put(syms['_device_l2']+(136+i)*8,(0x11000000+i*0x200000)|0x405|(3<<53))
 q30.foundation_tables(memory,syms,6)
 for name,address in [('missing second native L1 bank',syms['_l1']+16),('missing final identity block',syms['_l2']+991*8),('missing Q34 alias table',syms['_l2']+992*8)]:
  changed=bytearray(memory);struct.pack_into('<Q',changed,address-0x40000000,0);reject(name,lambda:q30.foundation_tables(changed,syms,6))
 changed=bytearray(memory);struct.pack_into('<Q',changed,syms['_l2']+993*8-0x40000000,0xbc200701)
 reject('native mapping beyond Q34 RAM ceiling',lambda:q30.foundation_tables(changed,syms,6))
 # Q34-only source validation observes actual mapped bytes; no host target substituted.
 memory=bytearray(q30.RAM_SIZE);memory[BASE:BASE+SIZE]=initial_memory(2,1)
 code=CodeState({});code.bind(memory,[(ROOT,0,1,1,1)]*2);code.started=True
 memory[0x320000:0x320003]=b'\x48\x89\xe7'
 recipe=(q33.q31.ATTEMPT,1,K(0x320000),K(0x320000),17,3,8,1)
 code.event(1,13,0,0,recipe,0,None,set())
 memory[0x320002]^=1
 reject('native MOV RDI recipe differs from coherent opcode',lambda:code.event(1,13,0,0,recipe,0,None,set()))
 print(json.dumps(dict(status='Q34 SYNTHETIC PRIMITIVES PASS',rejected=rejected,vm=False)))


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--model-trace',type=Path);p.add_argument('run',type=Path,nargs='?');p.add_argument('--self-check',action='store_true');args=p.parse_args()
 if args.self_check:self_check();return
 if args.model_trace:
  header,records,scopes,result=model(args.model_trace)
  print(json.dumps(dict(status='SYNTHETIC Q34 SEMANTICS PASS',cases=len(scopes),ncpus=header['ncpus'],unsupported=['native protocol and retained native-run envelope'],vm=False)))
 elif args.run:print(json.dumps(q30.check_run(args.run,continuation=check,version=6),indent=2))
 else:p.error('provide retained run or synthetic preparation input')
if __name__=='__main__':main()
