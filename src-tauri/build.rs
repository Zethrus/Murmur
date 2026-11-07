use std::{env, fs, path::PathBuf};

/// Embed the release trust certificate and, when provided, the signed component manifest.
/// `src-tauri/trust/murmur-release.asc` is the public certificate (absent until the owner
/// creates the release key); `MURMUR_EMBED_MANIFEST=<manifest.json>` embeds that file and
/// `<manifest.json>.sig`. Both are verified at runtime exactly like downloaded copies.
fn embed() {
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("OUT_DIR"));
    let root = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("manifest dir"));
    let cert = root.join("trust/murmur-release.asc");
    println!("cargo:rerun-if-changed={}", cert.display());
    println!("cargo:rerun-if-env-changed=MURMUR_EMBED_MANIFEST");
    let literal = |path: &PathBuf| format!("include_bytes!({:?})", path.display().to_string());
    let cert_code = if cert.is_file() {
        format!("Some({})", literal(&cert))
    } else {
        "None".into()
    };
    let manifest_code = match env::var_os("MURMUR_EMBED_MANIFEST").filter(|v| !v.is_empty()) {
        Some(path) => {
            let manifest = PathBuf::from(path);
            let mut signature = manifest.clone().into_os_string();
            signature.push(".sig");
            let signature = PathBuf::from(signature);
            assert!(
                manifest.is_file() && signature.is_file(),
                "MURMUR_EMBED_MANIFEST needs the manifest and its .sig"
            );
            println!("cargo:rerun-if-changed={}", manifest.display());
            println!("cargo:rerun-if-changed={}", signature.display());
            format!("Some(({}, {}))", literal(&manifest), literal(&signature))
        }
        None => "None".into(),
    };
    fs::write(
        out.join("embedded_trust.rs"),
        format!(
            "/// Release certificate compiled into the binary (None until the key exists).\n\
             pub const RELEASE_CERT: Option<&[u8]> = {cert_code};\n\
             /// Signed manifest and detached signature compiled in for offline display.\n\
             pub const EMBEDDED_MANIFEST: Option<(&[u8], &[u8])> = {manifest_code};\n"
        ),
    )
    .expect("write embedded_trust.rs");
}

fn main() {
    embed();
    // The speech engine is not a Tauri sidecar: release packaging stages the onedir
    // engine (see src/engine.rs for the lookup order), debug builds run the venv.
    tauri_build::build()
}
