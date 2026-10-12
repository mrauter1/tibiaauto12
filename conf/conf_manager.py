"""Safe JSON configuration storage."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Iterator, Mapping, MutableMapping, Sequence

from core.paths import SCRIPTS_DIR, scripts_path


class ConfigError(RuntimeError):
    """A configuration could not be read or written safely."""


_locks_guard = threading.Lock()
_locks: dict[Path, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(path, threading.RLock())


@contextmanager
def _process_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as descriptor:
        descriptor.seek(0)
        if descriptor.read(1) == b"":
            descriptor.write(b"0")
            descriptor.flush()
        descriptor.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(descriptor.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                descriptor.seek(0)
                msvcrt.locking(descriptor.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    with _thread_lock(path):
        with _process_lock(path):
            yield


def _read(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as descriptor:
            return json.load(descriptor)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"could not read configuration {path}: {exc}") from exc


def _write_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as descriptor:
            temporary = Path(descriptor.name)
            json.dump(data, descriptor, indent=2)
            descriptor.write("\n")
            descriptor.flush()
            os.fsync(descriptor.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigError(f"could not atomically write configuration {path}: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _merge_missing(saved: Any, defaults: Any) -> Any:
    if not isinstance(saved, MutableMapping) or not isinstance(defaults, Mapping):
        return saved
    for key, value in defaults.items():
        if key not in saved:
            saved[key] = deepcopy(value)
        else:
            saved[key] = _merge_missing(saved[key], value)
    return saved


class ConfManager:
    @staticmethod
    def path(file: str | Path) -> Path:
        try:
            return scripts_path(file)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid configuration filename {file!r}: {exc}") from exc

    @classmethod
    def get(cls, file: str | Path, default: Any = None) -> Any:
        path = cls.path(file)
        with _locked(path):
            if not path.exists() and default is not None:
                return deepcopy(default)
            return _read(path)

    @classmethod
    def set(cls, data: Any, file: str | Path) -> None:
        path = cls.path(file)
        with _locked(path):
            _write_atomic(path, data)

    @classmethod
    def update(cls, file: str | Path, updater: Callable[[Any], Any]) -> Any:
        """Perform a locked read-modify-write and return updated data/result."""
        if not callable(updater):
            raise TypeError("updater must be callable")
        path = cls.path(file)
        with _locked(path):
            data = _read(path)
            result = updater(data)
            replacement = result if isinstance(result, (dict, list)) else data
            _write_atomic(path, replacement)
            return replacement if result is None or isinstance(result, (dict, list)) else result

    @classmethod
    def update_section(cls, file: str | Path, path: Sequence[str | int], value: Any) -> Any:
        if not path:
            raise ValueError("configuration section path cannot be empty")

        def assign(data: Any) -> None:
            current = data
            for part in path[:-1]:
                current = current[part]
            current[path[-1]] = value

        return cls.update(file, assign)

    @classmethod
    def ensure_defaults(cls, defaults: Mapping[str, Any], file: str | Path) -> Any:
        """Add missing schema keys without changing existing saved values."""
        path = cls.path(file)
        with _locked(path):
            if path.exists():
                data = _read(path)
                if not isinstance(data, MutableMapping):
                    raise ConfigError(f"configuration root must be an object: {path}")
                original = deepcopy(data)
                merged = _merge_missing(data, defaults)
                if merged == original:
                    return merged
            else:
                merged = deepcopy(defaults)
            _write_atomic(path, merged)
            return merged


__all__ = ["ConfManager", "ConfigError", "SCRIPTS_DIR"]
