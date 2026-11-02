use std::{env, fs, path::PathBuf};
fn main() {
    // Tauri validates externalBin even for plain cargo build. Development uses Python,
    // while release packaging must supply the actual frozen sidecar, never this launcher.
    if env::var("PROFILE").as_deref() == Ok("debug") {
        let root = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("Cargo manifest directory"));
        let target = env::var("TARGET").expect("Cargo target");
        let binary = root
            .join("binaries")
            .join(format!("murmur-engine-{target}{}", if target.contains("windows") { ".exe" } else { "" }));
        if let Err(error) = fs::create_dir_all(root.join("binaries")) {
            panic!("Create dev sidecar directory: {error}");
        }
        if target.contains("windows") {
            if !binary.exists() {
                if let Err(error) = fs::copy(root.join("../engine/.venv/Scripts/python.exe"), &binary) {
                    panic!("Create the engine Windows venv before building: {error}");
                }
            }
        } else if !binary.exists() || fs::read(&binary).map(|data| data.starts_with(b"#!/bin/sh")).unwrap_or(false) {
            let script = root.join("../scripts/engine-sidecar.sh");
            let escaped = script.to_string_lossy().replace('\'', "'\\''");
            if let Err(error) = fs::write(&binary, format!("#!/bin/sh\nexec '{escaped}' \"$@\"\n")) {
                panic!("Write dev sidecar launcher: {error}");
            }
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                if let Err(error) = fs::set_permissions(&binary, fs::Permissions::from_mode(0o755)) {
                    panic!("Set launcher permissions: {error}");
                }
            }
        }
    }
    tauri_build::build()
}
