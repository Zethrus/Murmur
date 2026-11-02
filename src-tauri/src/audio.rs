use crate::config;
use anyhow::{bail, Context, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use serde::Serialize;
use std::{
    collections::VecDeque,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::Duration,
};

const RATE: usize = 16_000;
const CAPACITY: usize = RATE * 60;
#[derive(Debug)]
pub enum AudioEvent {
    Level(f32),
    Endpoint,
    MaxDuration,
    Error(String),
}
type Callback = Arc<dyn Fn(AudioEvent) + Send + Sync>;
#[derive(Serialize)]
pub struct DeviceInfo {
    pub id: String,
    pub name: String,
    pub is_default: bool,
}
pub fn list_devices() -> Result<Vec<DeviceInfo>> {
    let host = cpal::default_host();
    let default = host.default_input_device().and_then(|d| d.name().ok());
    host.input_devices()
        .context("List input devices")?
        .enumerate()
        .map(|(i, d)| {
            let name = d.name().context("Read audio device name")?;
            Ok(DeviceInfo { id: format!("{i}:{name}"), is_default: default.as_ref() == Some(&name), name })
        })
        .collect()
}
#[derive(Default)]
struct Resampler {
    input: VecDeque<f32>,
    position: f64,
}
impl Resampler {
    fn feed(&mut self, samples: &[f32], rate: u32) -> Vec<f32> {
        self.input.extend(samples.iter().copied());
        let step = f64::from(rate) / RATE as f64;
        let mut out = Vec::new();
        while self.position + 1.0 < self.input.len() as f64 {
            let i = self.position.floor() as usize;
            let f = self.position.fract() as f32;
            out.push(self.input[i] * (1.0 - f) + self.input[i + 1] * f);
            self.position += step;
        }
        let consumed = (self.position.floor() as usize).min(self.input.len().saturating_sub(1));
        self.input.drain(..consumed);
        self.position -= consumed as f64;
        out
    }
}
#[derive(Default)]
struct Silence {
    speech_ms: u64,
    silent_ms: u64,
}
impl Silence {
    fn feed(&mut self, rms: f32, threshold: f32, timeout: u64) -> bool {
        if rms >= threshold {
            self.speech_ms += 20;
            self.silent_ms = 0;
        } else {
            self.silent_ms += 20;
        }
        self.speech_ms >= 300 && self.silent_ms >= timeout
    }
}
struct Buffer {
    ring: VecDeque<f32>,
    cursor: u64,
    start: Option<u64>,
    actual_start: u64,
    resampler: Resampler,
    frame: Vec<f32>,
    silence: Silence,
    level_ms: u64,
    endpoint: bool,
    max_sent: bool,
    qa_pos: usize,
}
impl Default for Buffer {
    fn default() -> Self {
        Self {
            ring: VecDeque::with_capacity(CAPACITY),
            cursor: 0,
            start: None,
            actual_start: 0,
            resampler: Resampler::default(),
            frame: Vec::new(),
            silence: Silence::default(),
            level_ms: 0,
            endpoint: false,
            max_sent: false,
            qa_pos: 0,
        }
    }
}
impl Buffer {
    fn push(&mut self, samples: &[f32], rate: u32, cfg: &config::Audio, callback: &Callback) {
        for sample in self.resampler.feed(samples, rate) {
            if self.ring.len() == CAPACITY {
                self.ring.pop_front();
            }
            self.ring.push_back(sample);
            self.cursor += 1;
            if self.start.is_none() {
                continue;
            }
            self.frame.push(sample);
            if self.frame.len() == 320 {
                let rms = rms(&self.frame);
                self.frame.clear();
                self.level_ms += 20;
                if self.level_ms >= 50 {
                    self.level_ms -= 50;
                    callback(AudioEvent::Level((rms * cfg.gain).min(1.0)));
                }
                if self.silence.feed(rms * cfg.gain, cfg.silence_threshold, cfg.silence_timeout_ms) && !self.endpoint {
                    self.endpoint = true;
                    callback(AudioEvent::Endpoint);
                }
            }
            if self.cursor - self.actual_start >= cfg.max_utterance_s * RATE as u64 && !self.max_sent {
                self.max_sent = true;
                callback(AudioEvent::MaxDuration);
            }
        }
    }
    fn pcm(&self, gain: f32) -> Vec<f32> {
        let start = self.start.unwrap_or(self.cursor);
        let first = self.cursor.saturating_sub(self.ring.len() as u64);
        self.ring.iter().skip(start.saturating_sub(first) as usize).map(|x| (x * gain).clamp(-1.0, 1.0)).collect()
    }
}
pub fn rms(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    (samples.iter().map(|x| x * x).sum::<f32>() / samples.len() as f32).sqrt()
}
pub struct AudioCapture {
    buffer: Arc<Mutex<Buffer>>,
    healthy: Arc<AtomicBool>,
    cfg: config::Audio,
    _stream: Option<cpal::Stream>,
    qa: Option<(mpsc::Sender<()>, JoinHandle<()>)>,
}
impl AudioCapture {
    pub fn new(cfg: &config::Audio, callback: impl Fn(AudioEvent) + Send + Sync + 'static) -> Result<Self> {
        let buffer = Arc::new(Mutex::new(Buffer::default()));
        let healthy = Arc::new(AtomicBool::new(true));
        let callback: Callback = Arc::new(callback);
        if let Some(path) = std::env::var_os("MURMUR_QA_WAV") {
            let mut reader = hound::WavReader::open(path).context("Open QA WAV")?;
            let spec = reader.spec();
            if spec.channels == 0 || spec.sample_rate == 0 {
                bail!("Invalid QA WAV format");
            }
            let raw: Vec<f32> = if spec.sample_format == hound::SampleFormat::Float {
                reader.samples::<f32>().collect::<std::result::Result<_, _>>().context("Decode float WAV")?
            } else {
                let scale = 2_f32.powi(i32::from(spec.bits_per_sample) - 1);
                reader
                    .samples::<i32>()
                    .map(|x| x.map(|s| s as f32 / scale))
                    .collect::<std::result::Result<_, _>>()
                    .context("Decode PCM WAV")?
            };
            let mono: Vec<f32> =
                raw.chunks(spec.channels as usize).map(|f| f.iter().sum::<f32>() / f.len() as f32).collect();
            let data = buffer.clone();
            let config = cfg.clone();
            let (tx, rx) = mpsc::channel();
            let job = thread::Builder::new()
                .name("murmur-qa-audio".into())
                .spawn(move || {
                    while matches!(rx.recv_timeout(Duration::from_millis(20)), Err(mpsc::RecvTimeoutError::Timeout)) {
                        let Ok(mut b) = data.lock() else {
                            break;
                        };
                        if b.start.is_none() {
                            continue;
                        }
                        let end = (b.qa_pos + (spec.sample_rate as usize / 50).max(1)).min(mono.len());
                        let pos = b.qa_pos;
                        b.qa_pos = end;
                        b.push(&mono[pos..end], spec.sample_rate, &config, &callback);
                        if end == mono.len() && !b.max_sent {
                            b.max_sent = true;
                            callback(AudioEvent::MaxDuration);
                        }
                    }
                })
                .context("Start QA audio worker")?;
            return Ok(Self { buffer, healthy, cfg: cfg.clone(), _stream: None, qa: Some((tx, job)) });
        }
        let host = cpal::default_host();
        let device = if cfg.device == "default" {
            host.default_input_device()
        } else {
            host.input_devices().context("List microphones")?.enumerate().find_map(|(i, d)| {
                let name = d.name().ok()?;
                (cfg.device == name || cfg.device == format!("{i}:{name}")).then_some(d)
            })
        }
        .context("No matching microphone; choose an available input device")?;
        let supported = device.default_input_config().context("Read default microphone format")?;
        let format = supported.sample_format();
        let stream_config: cpal::StreamConfig = supported.into();
        macro_rules! stream {
            ($ty:ty) => {
                build::<$ty>(&device, &stream_config, buffer.clone(), healthy.clone(), cfg.clone(), callback.clone())?
            };
        }
        let stream = match format {
            cpal::SampleFormat::F32 => stream!(f32),
            cpal::SampleFormat::F64 => stream!(f64),
            cpal::SampleFormat::I8 => stream!(i8),
            cpal::SampleFormat::I16 => stream!(i16),
            cpal::SampleFormat::I32 => stream!(i32),
            cpal::SampleFormat::I64 => stream!(i64),
            cpal::SampleFormat::U8 => stream!(u8),
            cpal::SampleFormat::U16 => stream!(u16),
            cpal::SampleFormat::U32 => stream!(u32),
            cpal::SampleFormat::U64 => stream!(u64),
            _ => bail!("Unsupported microphone format: {format:?}"),
        };
        stream.play().context("Start microphone stream")?;
        Ok(Self { buffer, healthy, cfg: cfg.clone(), _stream: Some(stream), qa: None })
    }
    pub fn healthy(&self) -> bool {
        self.healthy.load(Ordering::Relaxed)
    }
    pub fn start(&mut self) -> Result<()> {
        let mut b = self.buffer.lock().map_err(|_| anyhow::anyhow!("Audio lock poisoned"))?;
        b.start = Some(b.cursor.saturating_sub(4000));
        b.actual_start = b.cursor;
        b.silence = Silence::default();
        b.frame.clear();
        b.level_ms = 0;
        b.endpoint = false;
        b.max_sent = false;
        b.qa_pos = 0;
        if self.qa.is_some() {
            b.ring.clear();
            b.start = Some(b.cursor);
            b.resampler = Resampler::default();
        }
        Ok(())
    }
    pub fn snapshot(&self) -> Result<Vec<f32>> {
        Ok(self.buffer.lock().map_err(|_| anyhow::anyhow!("Audio lock poisoned"))?.pcm(self.cfg.gain))
    }
    pub fn stop(&mut self) -> Result<Vec<f32>> {
        let mut b = self.buffer.lock().map_err(|_| anyhow::anyhow!("Audio lock poisoned"))?;
        let pcm = b.pcm(self.cfg.gain);
        b.start = None;
        Ok(pcm)
    }
    pub fn cancel(&mut self) {
        if let Ok(mut b) = self.buffer.lock() {
            b.start = None;
        }
    }
}
impl Drop for AudioCapture {
    fn drop(&mut self) {
        if let Some((tx, job)) = self.qa.take() {
            let _ = tx.send(());
            let _ = job.join();
        }
    }
}
fn build<T>(
    device: &cpal::Device,
    stream: &cpal::StreamConfig,
    buffer: Arc<Mutex<Buffer>>,
    healthy: Arc<AtomicBool>,
    cfg: config::Audio,
    callback: Callback,
) -> Result<cpal::Stream>
where
    T: cpal::SizedSample,
    f32: cpal::FromSample<T>,
{
    let channels = usize::from(stream.channels);
    let rate = stream.sample_rate.0;
    let errors = callback.clone();
    device
        .build_input_stream(
            stream,
            move |data: &[T], _| {
                let mono: Vec<f32> = data
                    .chunks(channels)
                    .map(|frame| {
                        frame.iter().map(|s| <f32 as cpal::Sample>::from_sample(*s)).sum::<f32>() / frame.len() as f32
                    })
                    .collect();
                if let Ok(mut b) = buffer.lock() {
                    b.push(&mono, rate, &cfg, &callback);
                }
            },
            move |error| {
                healthy.store(false, Ordering::Relaxed);
                errors(AudioEvent::Error(format!("Microphone disconnected: {error}")));
            },
            None,
        )
        .context("Open microphone input stream")
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn silence_requires_speech() {
        let mut s = Silence::default();
        for _ in 0..100 {
            assert!(!s.feed(0.0, 0.012, 1200));
        }
        for _ in 0..15 {
            assert!(!s.feed(0.1, 0.012, 1200));
        }
        for _ in 0..59 {
            assert!(!s.feed(0.0, 0.012, 1200));
        }
        assert!(s.feed(0.0, 0.012, 1200));
    }
    #[test]
    fn rms_math() {
        assert_eq!(rms(&[]), 0.0);
        assert_eq!(rms(&[0.5, -0.5]), 0.5);
    }
    #[test]
    fn resample_chunk_boundaries() {
        let samples: Vec<f32> = (0..4800).map(|i| i as f32 / 4800.0).collect();
        let whole = Resampler::default().feed(&samples, 48000);
        let mut r = Resampler::default();
        let split: Vec<f32> = samples.chunks(137).flat_map(|s| r.feed(s, 48000)).collect();
        assert_eq!(whole, split);
        assert_eq!(whole.len(), 1600);
    }
    #[test]
    fn qa_wav_capture_without_hardware() -> Result<()> {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("target/qa-capture-test.wav");
        let spec = hound::WavSpec {
            channels: 2,
            sample_rate: 48000,
            bits_per_sample: 32,
            sample_format: hound::SampleFormat::Float,
        };
        let mut writer = hound::WavWriter::create(&path, spec)?;
        for _ in 0..4800 {
            writer.write_sample(0.5_f32)?;
            writer.write_sample(0.25_f32)?;
        }
        writer.finalize()?;
        let old = std::env::var_os("MURMUR_QA_WAV");
        std::env::set_var("MURMUR_QA_WAV", &path);
        let (tx, rx) = mpsc::channel();
        let capture = AudioCapture::new(&config::Audio::default(), move |event| {
            let _ = tx.send(event);
        });
        if let Some(old) = old {
            std::env::set_var("MURMUR_QA_WAV", old);
        } else {
            std::env::remove_var("MURMUR_QA_WAV");
        }
        let mut capture = capture?;
        capture.start()?;
        while !matches!(rx.recv_timeout(Duration::from_secs(2))?, AudioEvent::MaxDuration) {}
        let pcm = capture.stop()?;
        assert_eq!(pcm.len(), 1600);
        assert!(pcm.iter().all(|sample| (*sample - 0.375).abs() < 0.00001));
        drop(capture);
        std::fs::remove_file(path)?;
        Ok(())
    }
    #[test]
    fn gain_and_preroll() {
        let b = Buffer { ring: VecDeque::from([0.8, -0.8, 0.2]), cursor: 3, start: Some(1), ..Default::default() };
        assert_eq!(b.pcm(2.0), vec![-1.0, 0.4]);
    }
}
