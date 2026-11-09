//! A real HLT quiescence promise spanning writes, read-only grants, and a canceled request.
use natemu::{Result, execution::{self, Backing, Stops, io::{CopyPermit, Transfer, WindowStatus}}, vm::Machine};
use std::{sync::atomic::{AtomicBool, Ordering::SeqCst}, time::{Duration, Instant}};

pub(super) const BASE: u64 = 0x200000;
const PTE: u64 = 0x503000;
const FUNCTION: u64 = 0x110000;
const A: u64 = 0x1122_3344_5566_7788;
const MARKER: u64 = 0x7175_6965_7400_0000;
const NEW_PTE: u64 = 0x101_0000 | 0x103;

fn status(s: &WindowStatus) -> String {
    format!("{{\"request\":{},\"released\":{},\"written\":{},\"acknowledged\":{:?},\"quiescent\":{:?},\"released_after\":{},\"request_after\":{}}}",
        s.request,s.released,s.written,s.acknowledged,s.quiescent,s.released_after,s.request_after)
}
fn stable(s: &WindowStatus) -> bool {
    s.request==s.request_after && s.request==s.released && s.released==s.released_after && s.quiescent==[1,1]
}
fn unchanged_quiet(s: &WindowStatus, baseline: &WindowStatus) -> Result<()> {
    if !stable(s) || s.acknowledged!=baseline.acknowledged {Err("quiet fixture participant left quiescence or acknowledged a quiet grant".into())}else{Ok(())}
}
fn record(events:&mut Vec<String>, stage:&str, s:&WindowStatus) {
    let row=format!("{{\"stage\":\"{stage}\",\"status\":{}}}",status(s));
    eprintln!("io-quiet-stage: {row}");events.push(row);
}

pub(super) fn run(backing:&Backing,machine:&Machine,stops:&Stops,started:&[AtomicBool],ended:&AtomicBool)->Result<String> {
    use Transfer::{Read,Write};
    let began=Instant::now();
    let live=||->Result<()> {
        if began.elapsed()>Duration::from_secs(30){Err("quiet fixture deadline".into())}
        else if ended.load(SeqCst)||(0..2).any(|v|stops.ended(v)){Err("quiet fixture participant ended before wake".into())}
        else{Ok(())}
    };
    let baseline=loop {
        live()?;
        if started.iter().all(|s|s.load(SeqCst)) {
            let a=backing.io_window_status();
            if stable(&a) && (0..2).all(|v|execution::idle(execution::park_state(backing.memory(),v))) {
                std::thread::sleep(Duration::from_millis(1));
                let b=backing.io_window_status();
                if stable(&b)&&a.request==b.request&&a.acknowledged==b.acknowledged {break b}
            }
        }
        std::thread::sleep(Duration::from_millis(1));
    };
    let mut events=Vec::new();record(&mut events,"quiet",&baseline);
    let copy=|t:&mut [Transfer<'_>]|->Result<()> {live()?;backing.copy_guest(machine,stops,Duration::from_secs(2),t)};
    let mut warm=[0;64];copy(&mut [Read{gpa:BASE,bytes:&mut warm}])?;
    let word=|b:&[u8],i|u64::from_le_bytes(b[i..i+8].try_into().unwrap());
    for v in 0..2 {
        if word(&warm,v*32)!=A || word(&warm,v*32+8)!=1 || word(&warm,v*32+16)!=MARKER+v as u64 {
            return Err("quiet fixture did not warm global mapping and shared code on both CPUs".into());
        }
    }
    let after_warm=backing.io_window_status();unchanged_quiet(&after_warm,&baseline)?;
    if after_warm.request!=baseline.request+1{return Err("quiet warm snapshot did not grant exactly one window".into())}
    record(&mut events,"warm-read",&after_warm);
    let pte=NEW_PTE.to_le_bytes();let immediate=2u32.to_le_bytes();
    copy(&mut [Write{gpa:PTE,bytes:&pte},Write{gpa:FUNCTION+1,bytes:&immediate}])?;
    let after_write=backing.io_window_status();unchanged_quiet(&after_write,&baseline)?;
    if after_write.request!=after_warm.request+1 || after_write.written!=after_write.request{return Err("quiet write epoch not published".into())}
    record(&mut events,"write",&after_write);
    let mut observed_pte=[0;8];let mut observed_code=[0;4];
    copy(&mut [Read{gpa:PTE,bytes:&mut observed_pte},Read{gpa:FUNCTION+1,bytes:&mut observed_code}])?;
    if observed_pte!=pte||observed_code!=immediate{return Err("quiet copied bytes mismatch".into())}
    let after_read=backing.io_window_status();unchanged_quiet(&after_read,&baseline)?;
    if after_read.request!=after_write.request+1||after_read.written!=after_write.written{return Err("quiet read-only successor epoch mismatch".into())}
    record(&mut events,"read-after-write",&after_read);
    let permit=CopyPermit::new();if !permit.cancel(){return Err("new quiet fixture permit could not cancel".into())}
    let canceled=backing.copy_guest_with_permit(machine,stops,Duration::from_secs(2),&mut [Write{gpa:FUNCTION+1,bytes:&3u32.to_le_bytes()}],&permit);
    if canceled.is_ok(){return Err("quiet pre-canceled request wrote guest RAM".into())}
    let after_cancel=backing.io_window_status();unchanged_quiet(&after_cancel,&baseline)?;
    if after_cancel.request!=after_read.request||after_cancel.written!=after_read.written{return Err("pre-canceled quiet request changed epoch".into())}
    record(&mut events,"canceled",&after_cancel);
    for v in 0..2 {if !execution::post(backing.memory(),v,0x40,false){return Err("quiet fixture IRQ refused".into())}}
    for v in 0..2 {if !stops.kick(machine,v)?{return Err("quiet fixture wake target ended".into())}}
    while !(0..2).all(|v|stops.ended(v)) {
        if began.elapsed()>Duration::from_secs(30){return Err("quiet fixture wake/end deadline".into())}
        std::thread::sleep(Duration::from_millis(1));
    }
    let final_status=backing.io_window_status();record(&mut events,"ended",&final_status);
    if final_status.quiescent!=[0,0]{return Err("quiet promise remained after guest reentry/end".into())}
    Ok(format!("{{\"windows\":3,\"cancel_prevented_grant\":true,\"warm\":{:?},\"events\":[{}]}}",warm,events.join(",")))
}
