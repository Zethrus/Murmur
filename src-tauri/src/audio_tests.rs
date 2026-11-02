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
    while !matches!(
        rx.recv_timeout(Duration::from_secs(2))?,
        AudioEvent::MaxDuration
    ) {}
    let pcm = capture.stop()?;
    assert_eq!(pcm.len(), 1600);
    assert!(pcm.iter().all(|sample| (*sample - 0.375).abs() < 0.00001));
    drop(capture);
    std::fs::remove_file(path)?;
    Ok(())
}
#[test]
fn gain_and_preroll() {
    let b = Buffer {
        ring: VecDeque::from([0.8, -0.8, 0.2]),
        cursor: 3,
        start: Some(1),
        ..Default::default()
    };
    assert_eq!(b.pcm(2.0), vec![-1.0, 0.4]);
}

#[test]
fn ninety_seconds_and_preroll_fit_without_truncation() {
    let mut b = Buffer::default();
    let cfg = config::Audio::default();
    let callback: Callback = Arc::new(|_| {});
    b.push(&vec![0.25; 4001], RATE as u32, &cfg, &callback);
    b.start = Some(b.cursor.saturating_sub(4000));
    b.actual_start = b.cursor;
    b.push(&vec![0.5; 90 * RATE], RATE as u32, &cfg, &callback);
    let pcm = b.pcm(1.0);
    assert_eq!(pcm.len(), 90 * RATE + 4000);
    assert_eq!(pcm[0], 0.25);
    assert_eq!(pcm[pcm.len() - 1], 0.5);
    assert!(b.max_sent);
}
#[test]
fn idle_meter_emits_without_endpointing() {
    let mut b = Buffer::default();
    let events = Arc::new(Mutex::new(Vec::new()));
    let collected = events.clone();
    let callback: Callback = Arc::new(move |event| collected.lock().unwrap().push(event));
    b.push(
        &vec![0.5; RATE],
        RATE as u32,
        &config::Audio::default(),
        &callback,
    );
    let events = events.lock().unwrap();
    assert!(!events.is_empty());
    assert!(events.iter().all(|e| matches!(e, AudioEvent::Level(_))));
}
