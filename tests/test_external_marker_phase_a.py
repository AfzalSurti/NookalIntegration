"""
Marker wiring only — no real external calls.

Default `pytest` runs with `-m "not external"` (see pyproject.toml).
"""
from __future__ import annotations

import pytest


@pytest.mark.external
def test_external_marker_is_registered() -> None:
    # Would hit live systems in the future; skipped by default addopts.
    pytest.skip("placeholder — no live integration implemented yet")
