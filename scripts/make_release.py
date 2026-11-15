"""Build a runnable release ZIP for Sticker Studio.

The archive contains the bot, migrations, tests and docs, but never secrets or
machine-specific artefacts: ``.env``, ``.venv``, ``data``, downloaded FFmpeg,
logs, bytecode and a previous ``dist``.

Usage::

    python scripts/make_release.py            # dist/StickerStudioBot-<version>-full.zip
    python scripts/make_release.py --output custom.zip
"""
from __future__ import annotations

import argparse
import fnmatch
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "data", ".testdata", "dist",
    "ffmpeg", "tools", ".idea", ".vscode", ".pytest_cache", ".ruff_cache",
}
EXCLUDE_FILES = {"*.pyc", "*.pyo", "*.log", "*.sqlite3", "*.sqlite3-wal",
                 "*.sqlite3-shm", "*.zip"}


def _skip(relative: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in relative.parts[:-1]):
        return True
    name = relative.name
    if name in EXCLUDE_DIRS:
        return True
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDE_FILES)


def build(output: Path) -> Path:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "4.0.0"
    output = output if output.is_absolute() else ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(ROOT.rglob("*")):
            relative = path.relative_to(ROOT)
            if path.is_dir() or _skip(relative):
                continue
            archive.write(path, Path(f"StickerStudioBot-{version}") / relative)
            added += 1
    print(f"{output} ({added} files, {output.stat().st_size // 1024} KB)")
    return output


def main(argv: list[str] | None = None) -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "4.0.0"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=Path("dist") / f"StickerStudioBot-{version}-full.zip",
    )
    args = parser.parse_args(argv)
    build(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
