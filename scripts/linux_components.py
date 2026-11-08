"""Install release components into a throwaway data directory through the app itself.

Tests use the app's hidden `--install-component` entry point, so every check runs the same
signature, rollback, size, SHA-256, archive and smoke-test code a user's install does. The
manifest in a pull-request build is signed by a throwaway key; it is accepted only through
MURMUR_DEV_TRUST_ROOT plus the explicit acknowledgement a release binary requires.
"""
import json
import os
from pathlib import Path
import subprocess

MANIFEST = "components-manifest.json"
SIGNATURE = "components-manifest.json.sig"
CERTIFICATE = "murmur-release.asc"
ACK = "I-understand-this-disables-release-trust"


def environment(home, components, base=None):
    """HOME/XDG under `home`, trusting the certificate shipped next to the manifest."""
    home = Path(home)
    env = dict(base if base is not None else os.environ)
    env.update(HOME=str(home), XDG_DATA_HOME=str(home / "data"), XDG_CONFIG_HOME=str(home / "config"),
               XDG_CACHE_HOME=str(home / "cache"), MURMUR_DEV_TRUST_ROOT=str(Path(components) / CERTIFICATE),
               MURMUR_DEV_TRUST_ROOT_ACK=ACK)
    return env


def install(app, components, source, env, check=True):
    """Install one zip or model directory; returns the component ID."""
    components = Path(components)
    result = subprocess.run([str(app), "--install-component", str(source), "--manifest", str(components / MANIFEST),
                             "--manifest-sig", str(components / SIGNATURE)],
                            env=env, capture_output=True, text=True, timeout=600)
    if check and result.returncode != 0:
        raise RuntimeError(f"Installing {Path(source).name} failed: {result.stderr.strip()[-800:]}")
    if result.returncode != 0:
        return None
    return json.loads(result.stdout.strip().splitlines()[-1])["installed"]


def active(env, component):
    """Directory of the active version of `component` in the data directory of `env`."""
    root = Path(env["XDG_DATA_HOME"]) / "murmur/components" / component
    record = json.loads((root / "active.json").read_text())
    return Path(record["location"]) if record.get("location") else root / record["version"]


def install_release(app, components, env, models=()):
    """Engine, CUDA add-on (when present) and the given model directories."""
    components = Path(components)
    installed = {}
    for zip_path in sorted(components.glob("murmur-*-linux-x86_64-*.zip")):
        component = install(app, components, zip_path, env)
        installed[component] = active(env, component)
    for model in models:
        component = install(app, components, model, env)
        installed[component] = active(env, component)
    return installed
