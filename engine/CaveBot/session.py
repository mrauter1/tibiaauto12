"""Compatibility imports for CaveBot's immutable SDK configuration values.

Route execution moved to :mod:`features.cavebot`.  This module intentionally
contains no blocking session implementation.
"""

from features.cavebot import CaveBotStatus as SessionStatus
from features.cavebot import SessionConfig, TargetSpec

__all__ = ["SessionConfig", "SessionStatus", "TargetSpec"]
