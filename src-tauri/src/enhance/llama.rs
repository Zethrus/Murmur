//! llama-cpp-2 (exact 0.1.160) integration, CPU only. Isolated here because the binding does
//! not promise API stability. No Tauri/UI dependencies; never logs prompt or output text.
use super::{
    backend::{Backend, Budget},
    prompt::{
        Prompt, CONTEXT_TOKENS, GRAMMAR, MAX_OUTPUT_TOKENS, MAX_PROMPT_TOKENS,
        MAX_TRANSCRIPT_TOKENS,
    },
    EnhanceError, ErrorCode,
};
use llama_cpp_2::{
    context::params::LlamaContextParams,
    llama_backend::LlamaBackend,
    llama_batch::LlamaBatch,
    model::{params::LlamaModelParams, LlamaModel},
    sampling::LlamaSampler,
    token::LlamaToken,
};
use std::{num::NonZeroU32, path::Path};

/// Prefill chunk size bounds the interval between cancellation checks.
const PREFILL_CHUNK: usize = 32;

#[derive(Default)]
pub struct Llama {
    // Field order matters: the model must drop before the backend.
    model: Option<LlamaModel>,
    backend: Option<LlamaBackend>,
    threads: u32,
}

fn fail(code: ErrorCode, message: &str) -> EnhanceError {
    EnhanceError::new(code, message)
}

impl Backend for Llama {
    fn available(&self) -> bool {
        true
    }
    fn loaded(&self) -> bool {
        self.model.is_some()
    }
    fn load(&mut self, path: &Path, threads: u32, budget: &Budget) -> Result<(), EnhanceError> {
        budget.check()?;
        if self.backend.is_none() {
            let mut backend = LlamaBackend::init().map_err(|_| {
                fail(
                    ErrorCode::LoadFailed,
                    "Could not initialise the Enhance engine",
                )
            })?;
            backend.void_logs();
            self.backend = Some(backend);
        }
        let backend = self.backend.as_ref().expect("backend initialised");
        let progress = budget.clone();
        let params = LlamaModelParams::default()
            .with_n_gpu_layers(0)
            .with_use_mmap(true)
            .with_use_mlock(false)
            .with_progress_callback(move |_| progress.alive());
        let model = LlamaModel::load_from_file(backend, path, &params);
        budget.check()?;
        self.model = Some(
            model.map_err(|_| fail(ErrorCode::LoadFailed, "Could not load the Enhance model"))?,
        );
        self.threads = threads.max(1);
        Ok(())
    }
    fn unload(&mut self) {
        self.model = None;
    }
    fn generate(&mut self, prompt: &Prompt, budget: &Budget) -> Result<String, EnhanceError> {
        let model = self
            .model
            .as_ref()
            .ok_or_else(|| fail(ErrorCode::LoadFailed, "Enhance model is not loaded"))?;
        let backend = self
            .backend
            .as_ref()
            .ok_or_else(|| fail(ErrorCode::LoadFailed, "Engine not initialised"))?;
        let vocab = model.vocab();
        let mut tokens: Vec<LlamaToken> = Vec::new();
        for segment in &prompt.segments {
            // Untrusted text is never parsed for special tokens.
            tokens.extend(vocab.tokenize(segment.text.as_bytes(), false, segment.special));
        }
        let transcript_tokens = vocab
            .tokenize(prompt.user_content.as_bytes(), false, false)
            .len();
        if transcript_tokens > MAX_TRANSCRIPT_TOKENS || tokens.len() > MAX_PROMPT_TOKENS {
            return Err(fail(
                ErrorCode::InputTooLong,
                "Dictation is too long to rewrite",
            ));
        }
        let threads = i32::try_from(self.threads).unwrap_or(1);
        let params = LlamaContextParams::default()
            .with_n_ctx(NonZeroU32::new(CONTEXT_TOKENS))
            .with_n_batch(PREFILL_CHUNK as u32)
            .with_n_ubatch(PREFILL_CHUNK as u32)
            .with_n_threads(threads)
            .with_n_threads_batch(threads);
        let mut ctx = model.new_context(backend, params).map_err(|_| {
            fail(
                ErrorCode::InferenceFailed,
                "Could not allocate the rewrite context",
            )
        })?;
        let mut sampler = LlamaSampler::chain_simple([
            LlamaSampler::grammar(model, GRAMMAR, "root")
                .map_err(|_| fail(ErrorCode::InferenceFailed, "Output grammar rejected"))?,
            LlamaSampler::top_k(20),
            LlamaSampler::top_p(0.8, 1),
            LlamaSampler::temp(0.2),
            LlamaSampler::dist(42),
        ]);
        let mut batch = LlamaBatch::new(PREFILL_CHUNK, 1);
        let total = tokens.len();
        let mut pos: i32 = 0;
        for chunk in tokens.chunks(PREFILL_CHUNK) {
            budget.check()?;
            batch.clear();
            for (i, token) in chunk.iter().enumerate() {
                let last = pos as usize + 1 == total;
                batch
                    .add(*token, pos, &[0], last)
                    .map_err(|_| fail(ErrorCode::InferenceFailed, "Prompt batch overflow"))?;
                pos += 1;
                let _ = i;
            }
            ctx.decode(&mut batch)
                .map_err(|_| fail(ErrorCode::InferenceFailed, "Prompt evaluation failed"))?;
        }
        let mut output: Vec<u8> = Vec::new();
        let mut generated = 0;
        loop {
            budget.check()?;
            let token = sampler.sample(&ctx, batch.n_tokens() - 1);
            if vocab.is_eog(token) {
                break;
            }
            generated += 1;
            if generated > MAX_OUTPUT_TOKENS {
                return Err(fail(
                    ErrorCode::InvalidOutput,
                    "Rewrite exceeded the output limit",
                ));
            }
            output.extend(vocab.token_to_piece(token, false, None));
            if pos as u32 + 1 >= CONTEXT_TOKENS {
                return Err(fail(
                    ErrorCode::InvalidOutput,
                    "Rewrite exceeded the context limit",
                ));
            }
            batch.clear();
            batch
                .add(token, pos, &[0], true)
                .map_err(|_| fail(ErrorCode::InferenceFailed, "Generation batch overflow"))?;
            pos += 1;
            ctx.decode(&mut batch)
                .map_err(|_| fail(ErrorCode::InferenceFailed, "Generation failed"))?;
        }
        String::from_utf8(output)
            .map_err(|_| fail(ErrorCode::InvalidOutput, "Rewrite was not valid UTF-8"))
    }
}
