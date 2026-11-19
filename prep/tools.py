"""Helpers for locating bundled tools and the ElevenLabs API key."""
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent.parent


def find(name: str) -> str:
    runtime = ROOT / "runtime"
    for candidate in (runtime / (name + ".exe"), runtime / name):
        if candidate.exists():
            return str(candidate)
    path = shutil.which(name)
    if path:
        return path
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            if Path(ffmpeg).exists():
                return ffmpeg
        except ImportError:
            pass
    raise RuntimeError(f"{name} was not found. It should be in the runtime folder next to the tool.")


def key_file() -> Path:
    if os.name == "nt":
        return Path(os.environ["APPDATA"]) / "CurativaProofread" / "elevenlabs.key"
    return Path.home() / "Library" / "Application Support" / "CurativaProofread" / "elevenlabs.key"


def read_key() -> str | None:
    value = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if value:
        return value

    path = key_file()
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value

    env_file = ROOT.parent / "Curativa-launcher" / ".env.local"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.strip().partition("=")
            if name == "VITE_ELEVENLABS_KEY" and value.strip():
                return value.strip().strip('"').strip("'")
    return None


def save_key(value: str) -> None:
    value = value.strip()
    if not value:
        raise ValueError("The key is empty.")
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def has_key() -> bool:
    return bool(read_key())
