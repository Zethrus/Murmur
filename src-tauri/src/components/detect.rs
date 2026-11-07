//! Hardware facts for the setup recommendation. Local only: /proc, CPUID and `nvidia-smi`.
use serde::Serialize;
use std::{
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Gpu {
    pub name: String,
    pub vram_mb: u64,
    pub driver: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Hardware {
    pub ram_mb: Option<u64>,
    pub avx2: bool,
    pub gpus: Vec<Gpu>,
    /// The CUDA add-on is offered (Linux with an NVIDIA GPU; Windows GPU stays hidden).
    pub cuda_offered: bool,
    pub free_bytes: Option<u64>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Recommendation {
    pub english: String,
    pub multilingual: String,
    pub compute: &'static str,
    pub reason: String,
}

fn ram_mb() -> Option<u64> {
    #[cfg(target_os = "linux")]
    {
        let text = std::fs::read_to_string("/proc/meminfo").ok()?;
        let line = text.lines().find(|l| l.starts_with("MemTotal:"))?;
        let kb: u64 = line.split_whitespace().nth(1)?.parse().ok()?;
        Some(kb / 1024)
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};
        let mut status: MEMORYSTATUSEX = unsafe { std::mem::zeroed() };
        status.dwLength = std::mem::size_of::<MEMORYSTATUSEX>() as u32;
        (unsafe { GlobalMemoryStatusEx(&mut status) } != 0)
            .then(|| status.ullTotalPhys / (1024 * 1024))
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    None
}

fn avx2() -> bool {
    #[cfg(target_arch = "x86_64")]
    {
        std::arch::is_x86_feature_detected!("avx2")
    }
    #[cfg(not(target_arch = "x86_64"))]
    false
}

/// `name, memory.total [MiB], driver_version` per line.
pub fn parse_nvidia_smi(output: &str) -> Vec<Gpu> {
    output
        .lines()
        .filter_map(|line| {
            let mut fields = line.split(',').map(str::trim);
            let name = fields.next()?.to_owned();
            let vram_mb = fields.next()?.parse().ok()?;
            let driver = fields.next()?.to_owned();
            (!name.is_empty() && !driver.is_empty()).then_some(Gpu {
                name,
                vram_mb,
                driver,
            })
        })
        .collect()
}

/// Decision 5 (plan, locked): the Windows CUDA add-on stays hidden until GPU-VALIDATION-windows.json
/// from the Edison runner (windows-build.yml `gpu-validation` job) is tied to the release assets.
/// Flipping this to `true` is the only code change needed to offer it; docs/ci-windows.md has
/// the gate.
pub const WINDOWS_GPU_OFFERED: bool = false;

/// Whether setup offers the CUDA add-on for this platform and set of detected GPUs.
pub fn cuda_offered(linux: bool, windows: bool, gpus: &[Gpu]) -> bool {
    !gpus.is_empty() && (linux || (windows && WINDOWS_GPU_OFFERED))
}

/// `nvidia-smi` locations: PATH first; on Windows the driver installs it in System32, which a
/// service or a trimmed PATH may not list.
fn nvidia_smi_candidates(windows: bool, system_root: Option<&str>) -> Vec<std::path::PathBuf> {
    let mut candidates = vec![std::path::PathBuf::from("nvidia-smi")];
    if windows {
        let root = system_root.unwrap_or("C:\\Windows");
        candidates.push(Path::new(root).join("System32").join("nvidia-smi.exe"));
    }
    candidates
}

fn nvidia_gpus() -> Vec<Gpu> {
    let root = std::env::var("SystemRoot").ok();
    nvidia_smi_candidates(cfg!(windows), root.as_deref())
        .into_iter()
        .map(|program| query_gpus(&program))
        .find(|gpus| !gpus.is_empty())
        .unwrap_or_default()
}

fn query_gpus(program: &Path) -> Vec<Gpu> {
    let mut command = Command::new(program);
    command
        .args([
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let Ok(mut child) = command.spawn() else {
        return Vec::new();
    };
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(50)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Vec::new();
            }
        }
    }
    let mut output = String::new();
    if let Some(mut stdout) = child.stdout.take() {
        let _ = std::io::Read::read_to_string(&mut stdout, &mut output);
    }
    parse_nvidia_smi(&output)
}

pub fn detect(data: &Path) -> Hardware {
    let gpus = nvidia_gpus();
    Hardware {
        ram_mb: ram_mb(),
        avx2: avx2(),
        cuda_offered: cuda_offered(cfg!(target_os = "linux"), cfg!(windows), &gpus),
        gpus,
        free_bytes: crate::platform::available_space(data),
    }
}

/// Model names refer to the speech catalog (`engine.model` values).
pub fn recommend(hardware: &Hardware) -> Recommendation {
    let vram = hardware.gpus.iter().map(|g| g.vram_mb).max().unwrap_or(0);
    let ram = hardware.ram_mb.unwrap_or(0);
    if hardware.cuda_offered && vram >= 4096 {
        return Recommendation {
            english: "distil-large-v3".into(),
            multilingual: "large-v3-turbo".into(),
            compute: "cuda",
            reason: format!(
                "NVIDIA GPU with {} GB of video memory: larger models run fast on it.",
                vram / 1024
            ),
        };
    }
    let (english, reason) = if ram >= 8 * 1024 && hardware.avx2 {
        (
            "distil-small.en",
            format!(
                "{} GB of memory and AVX2: a balanced model runs well on the CPU.",
                ram / 1024
            ),
        )
    } else if ram >= 4 * 1024 {
        (
            "base.en",
            "Limited memory or no AVX2: a smaller model keeps dictation responsive.".to_owned(),
        )
    } else {
        (
            "tiny.en",
            "Little memory available: the smallest model keeps dictation responsive.".to_owned(),
        )
    };
    Recommendation {
        english: english.into(),
        multilingual: "large-v3-turbo".into(),
        compute: "cpu",
        reason: if hardware.cuda_offered {
            format!("{reason} The GPU has under 4 GB of video memory.")
        } else {
            reason
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hardware(ram_gb: u64, avx2: bool, vram_mb: Option<u64>) -> Hardware {
        Hardware {
            ram_mb: Some(ram_gb * 1024),
            avx2,
            gpus: vram_mb
                .map(|v| {
                    vec![Gpu {
                        name: "NVIDIA GeForce RTX 4060 Laptop GPU".into(),
                        vram_mb: v,
                        driver: "580.178.04".into(),
                    }]
                })
                .unwrap_or_default(),
            cuda_offered: vram_mb.is_some(),
            free_bytes: None,
        }
    }

    #[test]
    fn parses_nvidia_smi_csv() {
        let gpus =
            parse_nvidia_smi("NVIDIA GeForce RTX 4060 Laptop GPU, 8188, 580.178.04\n\ngarbage\n");
        assert_eq!(
            gpus,
            vec![Gpu {
                name: "NVIDIA GeForce RTX 4060 Laptop GPU".into(),
                vram_mb: 8188,
                driver: "580.178.04".into()
            }]
        );
        assert!(parse_nvidia_smi("").is_empty());
    }

    #[test]
    fn recommendations_follow_hardware() {
        assert_eq!(
            recommend(&hardware(16, true, Some(8188))).english,
            "distil-large-v3"
        );
        assert_eq!(recommend(&hardware(16, true, Some(8188))).compute, "cuda");
        assert_eq!(
            recommend(&hardware(16, true, None)).english,
            "distil-small.en"
        );
        assert_eq!(recommend(&hardware(16, false, None)).english, "base.en");
        assert_eq!(recommend(&hardware(2, true, None)).english, "tiny.en");
        let small_gpu = recommend(&hardware(16, true, Some(2048)));
        assert_eq!(
            (small_gpu.compute, small_gpu.english.as_str()),
            ("cpu", "distil-small.en")
        );
    }

    #[test]
    fn windows_gpu_stays_hidden_until_validated() {
        let gpu = vec![Gpu {
            name: "RTX".into(),
            vram_mb: 8192,
            driver: "580".into(),
        }];
        assert!(cuda_offered(true, false, &gpu));
        assert!(!cuda_offered(true, false, &[]));
        assert_eq!(cuda_offered(false, true, &gpu), WINDOWS_GPU_OFFERED);
        assert!(
            !WINDOWS_GPU_OFFERED,
            "decision 5: flip only after Edison validation"
        );
        assert!(!cuda_offered(false, false, &gpu));
    }

    #[test]
    fn nvidia_smi_is_also_looked_up_in_system32_on_windows() {
        assert_eq!(nvidia_smi_candidates(false, None).len(), 1);
        let windows = nvidia_smi_candidates(true, Some("D:\\Win"));
        assert_eq!(windows[0], std::path::PathBuf::from("nvidia-smi"));
        assert!(windows[1].ends_with("nvidia-smi.exe"));
        assert!(windows[1].starts_with("D:\\Win"));
    }

    #[test]
    fn detection_runs_locally() {
        let found = detect(&std::env::temp_dir());
        assert!(
            found.ram_mb.is_some_and(|m| m > 0) || cfg!(not(any(target_os = "linux", windows)))
        );
        if !cfg!(target_os = "linux") {
            assert!(
                !found.cuda_offered,
                "Windows GPU stays hidden until validated"
            );
        }
    }
}
