"""Compatibility facade for the cooperative worker runtime."""

from __future__ import annotations

from typing import Callable, Optional

from core import runtime


# Retained for callers which imported the old module global. Runtime behavior
# no longer depends on this mutable list.
ActivatedThreads = []


class ThreadManager:
    """Manage the single active worker associated with ``Name``."""

    def __init__(self, Name: str):
        self.Name = str(Name)
        self.Target: Optional[Callable[[], None]] = None

    def NewThread(self, _Target: Callable[[], None]):
        """Start a zero-argument target, unless this name is already active."""
        self.Target = _Target
        thread = runtime.start(self.Name, _Target)
        self._refresh_compatibility_list(thread)
        return thread

    def PauseThread(self, timeout: float = 0.5) -> bool:
        """Cooperatively cancel the active generation, retaining its target."""
        return runtime.cancel(self.Name, timeout=timeout)

    def UnPauseThread(self):
        """Start the retained target once the cancelled generation has exited."""
        if self.Target is None:
            return None
        thread = runtime.start(self.Name, self.Target)
        self._refresh_compatibility_list(thread)
        return thread

    def KillThread(self, timeout: float = 0.5) -> bool:
        """Cooperatively cancel the worker and forget this manager's target."""
        stopped = runtime.cancel(self.Name, timeout=timeout, forget=True)
        self.Target = None
        self._remove_compatibility_entries()
        return stopped

    def Status(self):
        return runtime.status(self.Name)

    def _remove_compatibility_entries(self) -> None:
        ActivatedThreads[:] = [
            entry for entry in ActivatedThreads if entry[1] != self.Name
        ]

    def _refresh_compatibility_list(self, thread) -> None:
        self._remove_compatibility_entries()
        ActivatedThreads.append((thread, self.Name))

    def __repr__(self) -> str:
        return self.Name


def Pipeline(*funcs):
    """Retain the old function-composition helper for external callers."""
    def Inner(argument):
        state = argument
        for func in funcs:
            state = func(state)
        return state

    return Inner


__all__ = ["ActivatedThreads", "Pipeline", "ThreadManager"]
