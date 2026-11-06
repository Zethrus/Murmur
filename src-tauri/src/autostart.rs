//! Linux login entries shared by source installs, AppImages, and Debian bundles.
use anyhow::{bail, Context, Result};
use std::{
    fs,
    io::Write,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
};

fn desktop_exec(path: &Path) -> Result<String> {
    let value = path.to_str().context("Autostart path must be UTF-8")?;
    if value.chars().any(char::is_control) {
        bail!("Control characters are not supported in autostart paths");
    }
    // Exec token escaping is followed by desktop-entry string escaping.
    let mut quoted = String::new();
    for ch in value.chars() {
        match ch {
            '%' => quoted.push_str("%%"),
            '\\' | '"' | '`' | '$' => {
                quoted.push('\\');
                quoted.push(ch);
            }
            _ => quoted.push(ch),
        }
    }
    Ok(format!("\"{}\"", quoted.replace('\\', "\\\\")))
}

pub fn launcher(
    appimage: Option<PathBuf>,
    appdir: Option<PathBuf>,
    executable: PathBuf,
) -> PathBuf {
    if let Some(path) = appimage {
        return path;
    }
    if let Some(path) = appdir
        .map(|path| path.join("AppRun"))
        .filter(|path| path.is_file())
    {
        return path;
    }
    executable
}

pub fn update(home: &Path, config: &Path, executable: &Path, enabled: bool) -> Result<()> {
    let directory = config.join("autostart");
    let target = directory.join("murmur.desktop");
    if enabled {
        let contents = format!(
            "[Desktop Entry]\nType=Application\nName=Murmur\nExec={} --background\nIcon=murmur\nTerminal=false\nStartupNotify=false\nX-GNOME-Autostart-enabled=true\n",
            desktop_exec(executable)?
        );
        fs::create_dir_all(&directory).context("Create autostart directory")?;
        let mut random = [0u8; 8];
        getrandom::fill(&mut random).context("Create autostart temporary name")?;
        let temporary = directory.join(format!(".murmur-{:016x}.tmp", u64::from_ne_bytes(random)));
        let result = (|| -> Result<()> {
            let mut file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temporary)?;
            file.write_all(contents.as_bytes())?;
            file.sync_all()?;
            fs::rename(&temporary, &target)?;
            Ok(())
        })();
        let _ = fs::remove_file(&temporary);
        result.context("Write autostart entry")?;
    }
    let active_directory = if enabled {
        Some(fs::metadata(&directory).context("Read active autostart directory")?)
    } else {
        None
    };
    // Older plugin versions wrote a capitalized filename under HOME/.config,
    // while the installer used lowercase and respected XDG_CONFIG_HOME.
    for root in [config.to_path_buf(), home.join(".config")] {
        let candidate_directory = root.join("autostart");
        // Paths can differ while naming the same directory through symlinks,
        // '..' components, or bind mounts. Protect the active entry by identity.
        let is_active_directory = if let Some(active) = &active_directory {
            match fs::metadata(&candidate_directory) {
                Ok(candidate) => candidate.dev() == active.dev() && candidate.ino() == active.ino(),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
                Err(error) => return Err(error).context("Read legacy autostart directory"),
            }
        } else {
            false
        };
        for name in ["murmur.desktop", "Murmur.desktop"] {
            let path = candidate_directory.join(name);
            if is_active_directory && name == "murmur.desktop" {
                continue;
            }
            match fs::remove_file(path) {
                Ok(()) => (),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
                Err(error) => return Err(error).context("Remove old autostart entry"),
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn aliased_config_roots_preserve_enabled_registration() -> Result<()> {
        use std::os::unix::fs::symlink;
        let mut random = [0u8; 8];
        getrandom::fill(&mut random)?;
        let root = std::env::temp_dir().join(format!(
            "murmur-autostart-test-{:016x}",
            u64::from_ne_bytes(random)
        ));
        for layout in ["home-symlink", "xdg-symlink", "dotdot", "distinct"] {
            let base = root.join(layout);
            let home = base.join("home");
            fs::create_dir_all(&home)?;
            let config = match layout {
                "home-symlink" => {
                    let config = base.join("config-store");
                    fs::create_dir_all(&config)?;
                    symlink(&config, home.join(".config"))?;
                    config
                }
                "xdg-symlink" => {
                    fs::create_dir_all(home.join(".config"))?;
                    let config = base.join("config-alias");
                    symlink(home.join(".config"), &config)?;
                    config
                }
                "dotdot" => {
                    fs::create_dir_all(home.join("intermediate"))?;
                    home.join("intermediate/../.config")
                }
                _ => base.join("config-store"),
            };
            for directory in [config.join("autostart"), home.join(".config/autostart")] {
                fs::create_dir_all(&directory)?;
                fs::write(directory.join("murmur.desktop"), "old lowercase")?;
                fs::write(directory.join("Murmur.desktop"), "old capitalized")?;
            }
            update(&home, &config, Path::new("/opt/murmur/AppRun"), true)?;
            let entry = config.join("autostart/murmur.desktop");
            assert!(
                entry.is_file(),
                "Cleanup removed the enabled entry: {layout}"
            );
            assert!(fs::read_to_string(&entry)?.contains(" --background"));
            assert!(!config.join("autostart/Murmur.desktop").exists());
            assert!(!home.join(".config/autostart/Murmur.desktop").exists());
            if layout == "distinct" {
                assert!(!home.join(".config/autostart/murmur.desktop").exists());
            }
            update(&home, &config, Path::new("/opt/murmur/AppRun"), false)?;
            assert!(!entry.exists());
            assert!(!home.join(".config/autostart/murmur.desktop").exists());
        }
        fs::remove_dir_all(root)?;
        Ok(())
    }

    #[test]
    fn quotes_desktop_paths_and_rejects_control_characters() {
        assert_eq!(
            desktop_exec(Path::new("/tmp/Murmur AppImage")).unwrap(),
            "\"/tmp/Murmur AppImage\""
        );
        assert_eq!(
            desktop_exec(Path::new("/tmp/$app%name")).unwrap(),
            "\"/tmp/\\\\$app%%name\""
        );
        assert!(desktop_exec(Path::new("/tmp/bad\npath")).is_err());
    }

    #[test]
    fn appimage_registration_uses_persistent_file_not_mount() {
        assert_eq!(
            launcher(
                Some("/Downloads/Murmur.AppImage".into()),
                Some("/tmp/.mount_Murmur".into()),
                "/tmp/.mount_Murmur/usr/bin/murmur".into()
            ),
            PathBuf::from("/Downloads/Murmur.AppImage")
        );
    }
}
