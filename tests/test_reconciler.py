"""Tests for daily reconciler."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import FakeHass

from custom_components.volcast.reconciler import (
    DailyReconciler,
    ReconcileResult,
)


# ---------------------------------------------------------------------------
# ReconcileResult dataclass (Task 16)
# ---------------------------------------------------------------------------


def test_reconcile_result_defaults():
    r = ReconcileResult()
    assert r.success is False
    assert r.submitted == 0
    assert r.accepted == 0
    assert r.skipped is False
    assert r.reason is None
    assert r.error is None
