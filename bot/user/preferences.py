"""Validation helpers for user-selectable bot preferences."""
from __future__ import annotations

ALLOWED_LANGUAGES = {"ar", "en"}
ALLOWED_QUALITIES = {"small", "balanced", "high"}


def normalize_language(value: str) -> str:
    value = value.strip().lower()
    if value not in ALLOWED_LANGUAGES:
        raise ValueError("اختر ar أو en.")
    return value


def normalize_quality(value: str) -> str:
    value = value.strip().lower()
    if value not in ALLOWED_QUALITIES:
        raise ValueError("الجودة يجب أن تكون small أو balanced أو high.")
    return value
