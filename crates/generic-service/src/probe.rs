//! Policy-liveness probe.
//!
//! Proves that real RPCs go through Masa's hooks in every tracebench service,
//! with a check that cannot pass in baseline mode:
//!
//! 1. Control: `--control` Root requests through each frontend with a generous
//!    SLO must all succeed in both modes. Each one fans out through the whole
//!    call graph, so the probe is not just rejecting everything.
//! 2. Expired deadline: for each target, send one request whose Masa context
//!    (the `ctx` header written by `RootContext::attach`) has an end-to-end
//!    deadline already in the past. Masa's `abort_slo` deadline guard runs in
//!    the server hooks before the handler is polled and must answer
//!    `DEADLINE_EXCEEDED "/EarlyReturn..."`. With no-op hooks the header is
//!    ignored, the handler runs, and no `/EarlyReturn` can be produced: that
//!    string only exists in Masa's policy modules.
//!
//! `--expect masa` requires an `/EarlyReturn` from every target;
//! `--expect baseline` requires none. Exit status 0 means the expectation held.

use std::time::Duration;

use clap::Parser;
use generic_service::service_stubs::service_client::ServiceClient;
use generic_service::service_stubs::{PingRequest, RootRequest};
use masa::RootContext;
use tonic::transport::Channel;
use tonic::{Request, Status};

#[derive(Parser, Debug)]
struct Args {
    /// `baseline` or `masa` (not needed with `--ready-only`).
    #[arg(long, required_unless_present = "ready_only")]
    expect: Option<String>,
    /// Only wait until every frontend answers a Root request, then exit 0.
    #[arg(long)]
    ready_only: bool,
    /// Root (frontend) service as `graph=http://host:port`. Repeatable. Root
    /// requests (control and expired) are sent here.
    #[arg(long = "frontend")]
    frontends: Vec<String>,
    /// Other services probed directly with Ping, as `name=http://host:port`.
    #[arg(long = "target")]
    targets: Vec<String>,
    /// Number of control Root requests per frontend that must succeed.
    #[arg(long, default_value_t = 20)]
    control: usize,
    /// Before probing, retry a Root request for up to this many seconds until
    /// the stack answers (readiness wait).
    #[arg(long, default_value_t = 0)]
    wait_secs: u64,
    /// Per-request timeout in milliseconds.
    #[arg(long, default_value_t = 5000)]
    timeout_ms: u64,
}

#[derive(Debug)]
enum Outcome {
    Ok,
    EarlyReturn(String),
    Error(#[allow(dead_code)] String),
}

impl Outcome {
    fn of<T>(r: Result<tonic::Response<T>, Status>) -> Self {
        match r {
            Ok(_) => Outcome::Ok,
            Err(s) if s.message().starts_with("/EarlyReturn") => {
                Outcome::EarlyReturn(s.message().to_string())
            }
            Err(s) => Outcome::Error(format!("{:?}: {}", s.code(), s.message())),
        }
    }
}

type Probe = Box<dyn std::error::Error>;

async fn client(addr: &str, timeout: Duration) -> Result<ServiceClient<Channel>, Probe> {
    let ch = Channel::from_shared(addr.to_string())?
        .timeout(timeout)
        .connect_timeout(timeout)
        .connect()
        .await?;
    Ok(ServiceClient::new(ch))
}

fn split(spec: &str, flag: &str) -> Result<(String, String), Probe> {
    spec.split_once('=')
        .map(|(a, b)| (a.to_string(), b.to_string()))
        .ok_or_else(|| format!("--{flag} must be name=addr, got {spec}").into())
}

fn root_request(graph: &str, req_id: u64) -> RootRequest {
    RootRequest {
        req_id,
        start_at: masa::time_now(),
        graph_name: graph.to_string(),
    }
}

/// A Masa root context with the given SLO.
fn context(slo: Duration) -> RootContext {
    RootContext::from(masa::create_context("root", slo))
}

/// A Masa root context whose end-to-end deadline has already passed.
async fn expired() -> RootContext {
    let ctx = context(Duration::from_micros(1));
    tokio::time::sleep(Duration::from_millis(2)).await;
    ctx
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Probe> {
    let args = Args::parse();
    let expect = args.expect.clone().unwrap_or_default();
    let masa_mode = match expect.as_str() {
        "masa" => true,
        "baseline" => false,
        _ if args.ready_only => false,
        other => return Err(format!("--expect must be baseline or masa, got {other}").into()),
    };
    if args.frontends.is_empty() {
        return Err("at least one --frontend graph=addr is required".into());
    }
    let timeout = Duration::from_millis(args.timeout_ms);
    let mut failures = Vec::new();

    let mut frontends = Vec::new();
    for spec in &args.frontends {
        frontends.push(split(spec, "frontend")?);
    }

    // 0. Readiness: wait until every frontend answers a Root request end to end.
    let deadline = std::time::Instant::now() + Duration::from_secs(args.wait_secs);
    let mut roots = Vec::new();
    for (graph, addr) in &frontends {
        let c = loop {
            let attempt = async {
                let mut c = client(addr, timeout).await?;
                let req =
                    context(Duration::from_secs(5)).attach(Request::new(root_request(graph, 0)));
                c.root(req).await?;
                Ok::<_, Probe>(c)
            };
            match attempt.await {
                Ok(c) => break c,
                Err(e) if std::time::Instant::now() < deadline => {
                    println!("waiting for {graph}: {e}");
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
                Err(e) => return Err(format!("frontend {graph} not ready: {e}").into()),
            }
        };
        roots.push((graph.clone(), addr.clone(), c));
    }

    if args.ready_only {
        println!("ready: {} frontend(s) answer Root requests", roots.len());
        return Ok(());
    }

    // 1. Control: generous SLO, must succeed in both modes.
    for (graph, _, c) in &mut roots {
        let mut ok = 0;
        for i in 0..args.control {
            let req = context(Duration::from_secs(5))
                .attach(Request::new(root_request(graph, 1 + i as u64)));
            match c.root(req).await {
                Ok(_) => ok += 1,
                Err(s) => println!("control: error {:?}: {}", s.code(), s.message()),
            }
        }
        println!(
            "control {graph}: {ok}/{} Root requests with a 5 s SLO succeeded",
            args.control
        );
        if ok != args.control {
            failures.push(format!(
                "control {graph}: only {ok}/{} succeeded",
                args.control
            ));
        }
    }

    // 2. Expired deadlines, one per service.
    let mut outcomes: Vec<(String, Outcome)> = Vec::new();
    for (graph, _, c) in &mut roots {
        let req = expired()
            .await
            .attach(Request::new(root_request(graph, 9_999)));
        outcomes.push((format!("frontend {graph}"), Outcome::of(c.root(req).await)));
    }
    for spec in &args.targets {
        let (name, addr) = split(spec, "target")?;
        let mut c = client(&addr, timeout).await?;
        let req = expired().await.attach(Request::new(PingRequest {}));
        outcomes.push((name, Outcome::of(c.ping(req).await)));
    }
    let total = outcomes.len();
    let mut early_returns = 0;
    for (name, outcome) in &outcomes {
        println!("expired-deadline {name:<24} -> {outcome:?}");
        match (outcome, masa_mode) {
            (Outcome::EarlyReturn(_), true) => early_returns += 1,
            (Outcome::EarlyReturn(msg), false) => {
                early_returns += 1;
                failures.push(format!(
                    "{name}: baseline produced a Masa early return: {msg}"
                ));
            }
            (_, true) => failures.push(format!("{name}: expected /EarlyReturn, got {outcome:?}")),
            (_, false) => {}
        }
    }
    println!(
        "policy-live: {early_returns}/{total} services rejected an expired Masa deadline (expect {})",
        expect
    );

    if failures.is_empty() {
        println!("PROBE PASS ({expect})");
        Ok(())
    } else {
        for f in &failures {
            println!("FAIL: {f}");
        }
        println!("PROBE FAIL ({expect})");
        std::process::exit(1);
    }
}
