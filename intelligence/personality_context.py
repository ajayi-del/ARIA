"""
intelligence/personality_context.py — Re-export shim.

PersonalityContextCache lives in intelligence/personality.py to keep the
whole personality system in one file. This module re-exports it so that
tests can import from `intelligence.personality_context` without requiring
a separate file.

Usage:
    from intelligence.personality_context import PersonalityContextCache
"""

from intelligence.personality import PersonalityContextCache, PersonalityContext  # noqa: F401
