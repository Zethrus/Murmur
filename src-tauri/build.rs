fn main() {
    // The speech engine is not a Tauri sidecar: release packaging stages the onedir
    // engine (see src/engine.rs for the lookup order), debug builds run the venv.
    tauri_build::build()
}
