//! Opt-in scheduler setup shared by the existing native fixture families.
use super::*;
use natemu_abi::families::schedule as s;
use std::sync::OnceLock;

static CONFIG: OnceLock<s::Config> = OnceLock::new();

pub(super) fn configuration() -> Option<s::Config> {
    CONFIG.get().copied()
}

pub(super) fn backing_size(n: usize) -> usize {
    (n + usize::from(CONFIG.get().is_some())) * RAM_SIZE
}

fn parse(args: &[String]) -> Result<s::Config> {
    let family = match args[4].as_str() {
        "stack" => 0,
        "controls" => 1,
        "coherence" => 2,
        "atomic" => 3,
        "partial" => 4,
        _ => return Err("unsupported scheduling family".into()),
    };
    let n = args[5]
        .parse()
        .map_err(|_| "invalid scheduling owner count")?;
    let ordinal = args[6]
        .parse()
        .map_err(|_| "invalid scheduling seed ordinal")?;
    s::Config::new(family, n, ordinal).ok_or("unsupported scheduling configuration".into())
}

pub fn run(args: &[String]) -> Result<()> {
    let config = parse(args)?;
    CONFIG
        .set(config)
        .map_err(|_| "scheduler already configured")?;
    let mut underlying = args.to_vec();
    match config.family {
        0 => {
            underlying = vec![
                args[0].clone(),
                args[1].clone(),
                "i4".into(),
                if config.seed_ordinal % 2 == 0 {
                    "stack-low"
                } else {
                    "stack-high"
                }
                .into(),
                config.n.to_string(),
                args[3].clone(),
                args[8].clone(),
            ];
            super::run(&underlying)
        }
        1 => {
            underlying[2] = "i4v-controls-concurrent".into();
            underlying[4] = if config.seed_ordinal % 2 == 0 {
                "21"
            } else {
                "24"
            }
            .into();
            underlying[6] = config.actor.to_string();
            controls::run(&underlying)
        }
        2 => {
            underlying[2] = "i4v-coherence".into();
            underlying[4] = match config.seed_ordinal {
                1 => "4",
                3 => "5",
                _ => "0",
            }
            .into();
            underlying[6] = config.actor.to_string();
            coherence::run(&underlying)
        }
        3 => {
            underlying = vec![
                args[0].clone(),
                args[1].clone(),
                "i4v-ordering".into(),
                args[3].clone(),
                "9".into(),
                config.n.to_string(),
                config.actor.to_string(),
                config.token_seed().to_string(),
                args[7].clone(),
                args[8].clone(),
            ];
            ordering::run(&underlying)
        }
        4 => partial_progress::run(args),
        _ => unreachable!(),
    }
}

// Called after fixture mappings are installed and before any owner starts.
pub(super) unsafe fn prepare(memory: usize, n: usize) -> Result<()> {
    let Some(config) = CONFIG.get() else {
        return Ok(());
    };
    if config.n != n as u64 {
        return Err("scheduler owner count mismatch".into());
    }
    let offset = n * RAM_SIZE + s::BACKING_OFFSET;
    let attrs = 3 | 1 << 10 | 3 << 8 | 3 << 53;
    unsafe {
        ptr::write(
            (memory + offset) as *mut s::State,
            s::State::new(*config).ok_or("invalid scheduler state")?,
        );
        for id in 0..n {
            let base = memory + id * RAM_SIZE;
            for page in (0..s::BYTES).step_by(4096) {
                ptr::write(
                    (base + 0x14000 + (s::OFFSET + page) / 4096 * 8) as *mut u64,
                    (IPA + (offset + page) as u64) | attrs,
                );
            }
            (*((base + SHARED_OFFSET) as *mut Shared)).schedule = s::MAGIC;
        }
    }
    Ok(())
}

macro_rules! fields {
    ($v:ident; $($name:ident),* $(,)?) => {{
        let mut values = Vec::<String>::new();
        $(values.push(format!("\"{}\":{:?}", stringify!($name), $v.$name));)*
        format!("{{{}}}", values.join(","))
    }};
}

// Read only after run_owners has joined every native owner, including failures.
pub(super) unsafe fn report(
    memory: usize,
    n: usize,
    completed: usize,
    destroyed: &[(usize, i32)],
    cancellations: &[i32],
    error: Option<&str>,
    elapsed: Duration,
) {
    if CONFIG.get().is_none() {
        return;
    }
    let state = unsafe { &*((memory + n * RAM_SIZE + s::BACKING_OFFSET) as *const s::State) };
    let config = &state.config;
    let token_seed = config.token_seed();
    let mut config = fields!(config; family,n,actor,seed_ordinal,seed,pool_limit);
    config.pop();
    config.push_str(&format!(",\"token_seed\":{token_seed}}}"));
    let cuts = state.cuts[..(state.cut_count as usize).min(s::MAX_CUTS)]
        .iter()
        .map(|v| fields!(v; key,cohort,arrived,completed,chosen,decision))
        .collect::<Vec<_>>()
        .join(",");
    let decisions = state.decisions[..(state.decision_count as usize).min(s::MAX_DECISIONS)].iter()
        .map(|v|fields!(v; cut,phase,epoch,cohort,eligible,arrived,done_before,prng_before,prng_after,draw,chosen,claimed,trace_before,claim_trace_before))
        .collect::<Vec<_>>().join(",");
    let pool = state.pool_events[..(state.pool_event_count as usize).min(s::MAX_POOL_EVENTS)].iter()
        .map(|v|fields!(v; owner,ordinal,capacity,depth,epoch,result,serial,before_digest,after_digest,trace_before))
        .collect::<Vec<_>>().join(",");
    let summary = fields!(state; magic,version,prng,draws,cut_count,decision_count,
        pool_event_count,full_count,readmitted,full,retried);
    let destroyed = destroyed
        .iter()
        .map(|(id, status)| format!("[{id},{status}]"))
        .collect::<Vec<_>>()
        .join(",");
    let statuses = (0..n)
        .map(|id| {
            let shared = unsafe { &*((memory + id * RAM_SIZE + SHARED_OFFSET) as *const Shared) };
            format!("[{id},{},{}]", shared.status, shared.detail)
        })
        .collect::<Vec<_>>()
        .join(",");
    let error = error.map_or_else(|| "null".into(), json_string);
    let milliseconds = elapsed.as_millis();
    let host_pid = std::process::id();
    println!(
        "{{\"case\":\"schedule\",\"config\":{config},\"summary\":{summary},\"cuts\":[{cuts}],\"decisions\":[{decisions}],\"pool_events\":[{pool}],\"supervision\":{{\"host_pid\":{host_pid},\"joined\":{n},\"completions\":{completed},\"destroyed\":[{destroyed}],\"statuses\":[{statuses}],\"cancellations\":{cancellations:?},\"error\":{error},\"elapsed_ms\":{milliseconds}}}}}"
    );
}

pub(super) fn json_string(value: &str) -> String {
    let mut result = String::from("\"");
    for ch in value.chars() {
        match ch {
            '\\' => result.push_str("\\\\"),
            '"' => result.push_str("\\\""),
            ch if ch < ' ' => result.push_str(&format!("\\u{:04x}", ch as u32)),
            ch => result.push(ch),
        }
    }
    result.push('"');
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn actor_rotation_covers_all_owners_even_when_seeds_share_residues() {
        for family in ["stack", "controls", "coherence"] {
            for n in 1..=4 {
                let mut seen = 0;
                for ordinal in 0..4 {
                    let args = [
                        "host",
                        "runtime",
                        "i4v-progress",
                        "producer",
                        family,
                        &n.to_string(),
                        &ordinal.to_string(),
                        "worker",
                        "output",
                    ]
                    .map(str::to_string);
                    seen |= 1 << parse(&args).unwrap().actor;
                }
                assert_eq!(seen, (1 << n) - 1);
            }
        }
    }
}
