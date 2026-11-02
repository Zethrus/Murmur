use crate::config::Sounds;
use anyhow::{Context, Result};
use std::{
    f32::consts::TAU,
    sync::mpsc,
    thread::{self, JoinHandle},
};
#[derive(Clone, Copy)]
pub enum Cue {
    Start,
    Stop,
    Error,
}
pub fn synthesize(cue: Cue, volume: f32) -> Vec<f32> {
    let rate = 44_100.0;
    let (tones, seconds): ([f32; 2], f32) = match cue {
        Cue::Start => ([660.0, 880.0], 0.045),
        Cue::Stop => ([880.0, 660.0], 0.045),
        Cue::Error => ([180.0, 180.0], 0.09),
    };
    let samples = (seconds * rate) as usize;
    let envelope = 0.008 * rate;
    let mut out = Vec::with_capacity(samples * 2);
    for frequency in tones {
        for i in 0..samples {
            let fade = ((i as f32 / envelope).min((samples - 1 - i) as f32 / envelope)).min(1.0);
            let phase = TAU * frequency * i as f32 / rate;
            let wave = match cue {
                Cue::Error => phase.sin() * 0.75 + (phase * 3.0).sin() * 0.25,
                _ => phase.sin(),
            };
            out.push(wave * fade * volume.clamp(0.0, 1.0) * 0.3);
        }
        if matches!(cue, Cue::Error) {
            out.extend(std::iter::repeat_n(0.0, 1764));
        }
    }
    out
}
pub struct SoundPlayer {
    sender: mpsc::Sender<Option<(Cue, f32)>>,
    job: Option<JoinHandle<()>>,
}
impl SoundPlayer {
    pub fn new() -> Result<Self> {
        let (sender, receiver) = mpsc::channel();
        let job = thread::Builder::new()
            .name("murmur-cues".into())
            .spawn(move || {
                let mut output = None;
                while let Ok(Some((cue, volume))) = receiver.recv() {
                    if output.is_none() {
                        match rodio::OutputStream::try_default() {
                            Ok(pair) => output = Some(pair),
                            Err(error) => {
                                tracing::warn!(%error, "Cue output unavailable");
                                continue;
                            }
                        }
                    }
                    if let Some((_, handle)) = &output {
                        match rodio::Sink::try_new(handle) {
                            Ok(sink) => {
                                sink.append(rodio::buffer::SamplesBuffer::new(1, 44_100, synthesize(cue, volume)));
                                sink.sleep_until_end();
                            }
                            Err(error) => {
                                tracing::warn!(%error, "Cue playback failed");
                                output = None;
                            }
                        }
                    }
                }
            })
            .context("Start cue worker")?;
        Ok(Self { sender, job: Some(job) })
    }
    pub fn play(&self, cue: Cue, cfg: &Sounds) {
        if cfg.enabled && !cfg.muted {
            let _ = self.sender.send(Some((cue, cfg.volume)));
        }
    }
    pub fn shutdown(&mut self) {
        let _ = self.sender.send(None);
        if let Some(job) = self.job.take() {
            let _ = job.join();
        }
    }
}
impl Drop for SoundPlayer {
    fn drop(&mut self) {
        self.shutdown();
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn amplitude_envelope() {
        for cue in [Cue::Start, Cue::Stop, Cue::Error] {
            let samples = synthesize(cue, 0.6);
            assert_eq!(samples[0], 0.0);
            assert_eq!(samples.last(), Some(&0.0));
            assert!(samples.iter().all(|x| x.abs() <= 0.18001));
            assert!(samples.iter().any(|x| x.abs() > 0.1));
        }
    }
    #[test]
    fn muted_amplitude() {
        assert!(synthesize(Cue::Start, 0.0).iter().all(|x| *x == 0.0));
    }
}
