"""Build a Windows onedir executable with PyInstaller.

Run ``python build.py`` for the windowed application, or ``python build.py --console``
to produce a console-enabled diagnostic build.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
APP_DIST_DIR = DIST_DIR / "Jarvis"

HIDDEN_IMPORTS = (
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "whisper",
    "torch",
    "sounddevice",
    "edge_tts",
    "telethon",
    "instagrapi",
    "googleapiclient",
    "google.oauth2",
    "google_auth_oauthlib",
    "google.genai",
    "pydantic",
)

DATA_DIRECTORIES = (
    "screenshots",
    "models",
    "downloads",
    "avatars",
)


def ensure_pyinstaller() -> None:
    """Install PyInstaller into the active interpreter when it is unavailable."""
    if importlib.util.find_spec("PyInstaller") is None:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller"],
            check=True,
        )


def clean_build_directories() -> None:
    """Remove only this project's generated PyInstaller output directories."""
    for path in (BUILD_DIR, DIST_DIR):
        if path.is_symlink():
            raise RuntimeError(f"Refusing to clean symlinked build directory: {path}")
        if path.exists():
            if path.resolve().parent != ROOT:
                raise RuntimeError(f"Refusing to clean path outside project root: {path}")
            shutil.rmtree(path)


def create_distribution_scaffolds() -> None:
    """Copy config sidecars, never runtime data, into the output directory."""
    APP_DIST_DIR.mkdir(parents=True, exist_ok=True)
    for filename in (".env", "credentials.json"):
        source = ROOT / filename
        if source.is_file():
            shutil.copy2(source, APP_DIST_DIR / filename)
        elif filename == ".env":
            (APP_DIST_DIR / filename).touch()

    data_dir = APP_DIST_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    for directory in DATA_DIRECTORIES:
        (data_dir / directory).mkdir(exist_ok=True)


def build(console: bool = False) -> None:
    ensure_pyinstaller()
    clean_build_directories()

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "Jarvis",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
        "--paths",
        str(ROOT),
        "--add-data",
        f"{ROOT / 'ui' / 'styles.qss'};ui",
        "--collect-data",
        "googleapiclient",
    ]
    command.append("--console" if console else "--windowed")
    for module in HIDDEN_IMPORTS:
        command.extend(("--hidden-import", module))
    command.append(str(ROOT / "main.py"))

    subprocess.run(command, cwd=ROOT, check=True)
    create_distribution_scaffolds()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Jarvis Windows executable.")
    parser.add_argument(
        "--console",
        action="store_true",
        help="build with a console for diagnostic logging (default: windowed GUI)",
    )
    args = parser.parse_args()

    build(console=args.console)
    print(f"Build complete: {APP_DIST_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
