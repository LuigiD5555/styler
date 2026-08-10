use crate::protocol::HostContext;
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

pub fn detect_host() -> HostContext {
    let release = parse_os_release(Path::new("/etc/os-release"));
    let os_id = release.get("ID").cloned().unwrap_or_default();
    let os_name = release
        .get("PRETTY_NAME")
        .or_else(|| release.get("NAME"))
        .cloned()
        .unwrap_or_default();
    let os_version = release
        .get("VERSION_ID")
        .cloned()
        .unwrap_or_default();
    let family = detect_family(&os_id, release.get("ID_LIKE").map(String::as_str));
    let home = env::var("HOME").unwrap_or_else(|_| "/".to_string());
    let desktop = first_non_empty(&[
        env::var("XDG_CURRENT_DESKTOP").ok(),
        env::var("XDG_SESSION_DESKTOP").ok(),
        env::var("DESKTOP_SESSION").ok(),
    ]);
    let session_type = env::var("XDG_SESSION_TYPE").unwrap_or_default();

    let package_managers = ["apt-get", "dnf", "yum", "zypper", "pacman", "apk", "xbps-install"]
        .into_iter()
        .filter(|name| command_exists(name))
        .map(str::to_string)
        .collect();
    let tools = [
        "flatpak", "snap", "nix", "guix", "brew", "appimagetool", "curl", "wget", "tar", "unzip",
        "git", "python3", "pipx", "cargo", "podman", "docker",
    ]
    .into_iter()
    .filter(|name| command_exists(name))
    .map(str::to_string)
    .collect();

    HostContext {
        home,
        os_id,
        os_name,
        os_version,
        family,
        architecture: env::consts::ARCH.to_string(),
        desktop,
        session_type,
        package_managers,
        tools,
    }
}

fn parse_os_release(path: &Path) -> BTreeMap<String, String> {
    let mut values = BTreeMap::new();
    let Ok(content) = fs::read_to_string(path) else {
        return values;
    };
    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, raw)) = line.split_once('=') else {
            continue;
        };
        values.insert(key.to_string(), raw.trim_matches('"').to_string());
    }
    values
}

fn detect_family(id: &str, id_like: Option<&str>) -> String {
    let joined = format!("{} {}", id.to_lowercase(), id_like.unwrap_or("").to_lowercase());
    if joined.contains("ubuntu") || joined.contains("debian") || joined.contains("mint") {
        "debian".to_string()
    } else if joined.contains("arch") || joined.contains("manjaro") {
        "arch".to_string()
    } else if joined.contains("fedora") || joined.contains("rhel") || joined.contains("centos") {
        "fedora".to_string()
    } else if joined.contains("suse") {
        "suse".to_string()
    } else if joined.contains("alpine") {
        "alpine".to_string()
    } else if id.is_empty() {
        "unknown".to_string()
    } else {
        id.to_string()
    }
}

fn first_non_empty(values: &[Option<String>]) -> String {
    values
        .iter()
        .flatten()
        .find(|value| !value.trim().is_empty())
        .cloned()
        .unwrap_or_default()
}

fn command_exists(name: &str) -> bool {
    if name.contains('/') {
        return Path::new(name).is_file();
    }
    let Some(path) = env::var_os("PATH") else {
        return false;
    };
    env::split_paths(&path).any(|directory: PathBuf| directory.join(name).is_file())
}

#[cfg(test)]
mod tests {
    use super::detect_family;

    #[test]
    fn normalizes_distribution_families() {
        assert_eq!(detect_family("linuxmint", Some("ubuntu debian")), "debian");
        assert_eq!(detect_family("manjaro", Some("arch")), "arch");
        assert_eq!(detect_family("opensuse-tumbleweed", Some("suse")), "suse");
    }
}
