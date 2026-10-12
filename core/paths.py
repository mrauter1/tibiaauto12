"""Application paths that do not depend on the process working directory."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


def project_path(relative: str | Path) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ValueError(f"project path must be relative: {relative!r}")
    resolved = (PROJECT_ROOT / candidate).resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError(f"project path escapes the application directory: {relative!r}") from exc
    return resolved


def scripts_path(filename: str | Path) -> Path:
    """Return a path for one JSON file directly inside ``scripts``."""
    candidate = Path(filename)
    raw = str(candidate)
    if (candidate.is_absolute() or "/" in raw or "\\" in raw or candidate.name != raw
            or candidate.suffix.lower() != ".json"
            or candidate.name in {".json", "..json"}):
        raise ValueError(f"configuration name must be a JSON filename, got {filename!r}")
    return project_path(Path("scripts") / candidate.name)
