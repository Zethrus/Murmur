//! App-independent companion network components, also used by the CLI harness tests.
#[path = "companion/network/auth.rs"]
pub mod auth;
#[path = "companion/network/handshake.rs"]
pub mod handshake;
#[path = "companion/network/lan.rs"]
pub mod lan;
#[path = "companion/network/tls.rs"]
pub mod tls;
#[path = "companion/network/wire.rs"]
pub mod wire;
