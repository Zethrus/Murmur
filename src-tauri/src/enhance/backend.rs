//! Inference backend abstraction. The worker owns exactly one backend instance on its own
//! thread; implementations never touch Tauri or the UI.
#![cfg_attr(not(feature = "enhance-llama"), allow(dead_code))]
use super::{prompt::Prompt, EnhanceError, ErrorCode};
use std::{
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Instant,
};

/// Cooperative cancellation + monotonic deadline, checked between native calls.
#[derive(Clone)]
pub struct Budget {
    pub cancel: Arc<AtomicBool>,
    pub deadline: Instant,
    pub timeout_code: ErrorCode,
}
impl Budget {
    pub fn check(&self) -> Result<(), EnhanceError> {
        if self.cancel.load(Ordering::Acquire) {
            return Err(EnhanceError::new(ErrorCode::Cancelled, "Rewrite cancelled"));
        }
        if Instant::now() >= self.deadline {
            return Err(EnhanceError::new(self.timeout_code, "Rewrite timed out"));
        }
        Ok(())
    }
    pub fn alive(&self) -> bool {
        self.check().is_ok()
    }
}

pub trait Backend {
    fn available(&self) -> bool;
    fn loaded(&self) -> bool;
    fn load(&mut self, path: &Path, threads: u32, budget: &Budget) -> Result<(), EnhanceError>;
    /// Must drop all model memory before returning.
    fn unload(&mut self);
    /// Returns the raw generated text (the JSON object). Per-request context, KV cache and
    /// sampler are created and dropped inside this call.
    fn generate(&mut self, prompt: &Prompt, budget: &Budget) -> Result<String, EnhanceError>;
}

/// Used when the crate is built without `enhance-llama`.
#[derive(Default)]
#[allow(dead_code)] // Used when the llama feature is off, and in tests.
pub struct Unavailable;
impl Backend for Unavailable {
    fn available(&self) -> bool {
        false
    }
    fn loaded(&self) -> bool {
        false
    }
    fn load(&mut self, _: &Path, _: u32, _: &Budget) -> Result<(), EnhanceError> {
        Err(EnhanceError::new(
            ErrorCode::BackendUnavailable,
            "This build of Murmur does not include the Enhance engine",
        ))
    }
    fn unload(&mut self) {}
    fn generate(&mut self, _: &Prompt, _: &Budget) -> Result<String, EnhanceError> {
        Err(EnhanceError::new(
            ErrorCode::BackendUnavailable,
            "This build of Murmur does not include the Enhance engine",
        ))
    }
}

pub fn default_backend() -> Box<dyn Backend + Send> {
    #[cfg(feature = "enhance-llama")]
    {
        Box::new(super::llama::Llama::default())
    }
    #[cfg(not(feature = "enhance-llama"))]
    {
        Box::new(Unavailable)
    }
}

pub const fn compiled() -> bool {
    cfg!(feature = "enhance-llama")
}

#[cfg(test)]
pub mod fake {
    //! Deterministic test backend: scripted replies, delays honouring the budget, panics.
    use super::*;
    use std::{collections::VecDeque, sync::Mutex, time::Duration};
    #[derive(Clone)]
    pub enum Script {
        /// Echo the masked transcript with the first letter uppercased and a final period.
        Polish,
        Raw(String),
        Fail(ErrorCode),
        /// Sleep in small steps (honouring cancellation) before polishing.
        Delay(Duration),
        Panic,
    }
    #[derive(Clone, Default)]
    pub struct Log {
        pub loads: Arc<Mutex<u32>>,
        pub unloads: Arc<Mutex<u32>>,
        pub generated: Arc<Mutex<u32>>,
    }
    pub struct Fake {
        pub scripts: Arc<Mutex<VecDeque<Script>>>,
        pub log: Log,
        loaded: bool,
    }
    impl Fake {
        pub fn new(scripts: Arc<Mutex<VecDeque<Script>>>, log: Log) -> Self {
            Self {
                scripts,
                log,
                loaded: false,
            }
        }
    }
    pub fn polish(prompt: &Prompt) -> String {
        let mut text = prompt.masked.trim().to_owned();
        if let Some(first) = text.chars().next() {
            text = first.to_uppercase().collect::<String>() + &text[first.len_utf8()..];
        }
        if !text.ends_with(['.', '?', '!']) {
            text.push('.');
        }
        serde_json::json!({ "text": text }).to_string()
    }
    impl Backend for Fake {
        fn available(&self) -> bool {
            true
        }
        fn loaded(&self) -> bool {
            self.loaded
        }
        fn load(&mut self, _: &Path, _: u32, budget: &Budget) -> Result<(), EnhanceError> {
            budget.check()?;
            *self.log.loads.lock().unwrap() += 1;
            self.loaded = true;
            Ok(())
        }
        fn unload(&mut self) {
            if self.loaded {
                *self.log.unloads.lock().unwrap() += 1;
            }
            self.loaded = false;
        }
        fn generate(&mut self, prompt: &Prompt, budget: &Budget) -> Result<String, EnhanceError> {
            *self.log.generated.lock().unwrap() += 1;
            let script = self
                .scripts
                .lock()
                .unwrap()
                .pop_front()
                .unwrap_or(Script::Polish);
            match script {
                Script::Polish => Ok(polish(prompt)),
                Script::Raw(raw) => Ok(raw),
                Script::Fail(code) => Err(EnhanceError::new(code, "scripted failure")),
                Script::Delay(total) => {
                    let end = Instant::now() + total;
                    while Instant::now() < end {
                        budget.check()?;
                        std::thread::sleep(Duration::from_millis(5));
                    }
                    budget.check()?;
                    Ok(polish(prompt))
                }
                Script::Panic => panic!("scripted backend panic"),
            }
        }
    }
}
