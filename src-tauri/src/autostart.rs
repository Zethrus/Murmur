//! Linux login entries shared by source installs, AppImages, and Debian bundles.
use anyhow::{bail, Context, Result};
use std::{
    fs,
    io::Write,
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
    // Older plugin versions wrote a capitalized filename under HOME/.config,
    // while the installer used lowercase and respected XDG_CONFIG_HOME.
    for root in [config.to_path_buf(), home.join(".config")] {
        for name in ["murmur.desktop", "Murmur.desktop"] {
            let path = root.join("autostart").join(name);
            if enabled && path == target {
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
