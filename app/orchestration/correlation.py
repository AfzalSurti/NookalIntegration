"""Correlation IDs for tracing a single workflow run across audit events."""
from __future__ import annotations

import uuid


def new_correlation_id() -> str:
    """Return a new opaque correlation id (UUID4 string)."""
    return str(uuid.uuid4())
