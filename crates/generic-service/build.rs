/// Every generated client and server must default to Masa's hooks. Without
/// this, rpcstack's tonic-build falls back to `tonic::rpcstack::noop::NoopHooks`
/// and the policy silently does nothing. `tests/hooks.rs` guards this.
const DEFAULT_HOOKS: &str = "masa::DefaultHooks";

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Vendored protoc: no system protobuf-compiler needed.
    std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path().unwrap());

    println!("cargo:rerun-if-changed=proto/service.proto");
    tonic_build::configure()
        .default_hooks_path(DEFAULT_HOOKS)
        .compile(&["proto/service.proto"], &["proto"])?;
    Ok(())
}
