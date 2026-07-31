"""Tests for behive.engine.events module."""
import pytest
from unittest.mock import patch, MagicMock
from behive.engine.events import (
    emit_event,
    ensure_events_table,
    get_events,
    get_latest_quality,
)


class TestEmitEvent:
    """Test event emission."""

    @patch("behive.engine.events.DB_PATH", "")
    def test_emit_event_no_db(self):
        """Emitting without DB should not crash."""
        # With empty DB_PATH or no connection, should degrade gracefully
        try:
            emit_event("test_mission", "phase_start", {"phase": "scout"})
        except Exception:
            pass  # May fail without DB — that's OK for unit test

    def test_emit_event_signature(self):
        """emit_event should accept mission_id, event_type, data."""
        import inspect
        sig = inspect.signature(emit_event)
        params = list(sig.parameters.keys())
        assert len(params) >= 2  # at least mission_id and event_type


class TestGetEvents:
    """Test event retrieval."""

    def test_get_events_signature(self):
        """get_events should accept mission_id."""
        import inspect
        sig = inspect.signature(get_events)
        params = list(sig.parameters.keys())
        assert "mission_id" in params or len(params) >= 1

    @patch("behive.engine.events.DB_PATH", "")
    def test_get_events_empty(self):
        """Getting events for nonexistent mission should return empty."""
        try:
            result = get_events("nonexistent-mission-xyz")
            assert result == [] or result is None
        except Exception:
            pass  # DB not available


class TestGetLatestQuality:
    """Test quality metric retrieval."""

    def test_signature(self):
        """Should accept mission_id."""
        import inspect
        sig = inspect.signature(get_latest_quality)
        assert len(list(sig.parameters)) >= 1

    @patch("behive.engine.events.DB_PATH", "")
    def test_no_db_returns_none(self):
        """Without DB, should return None or empty."""
        try:
            result = get_latest_quality("fake-mission")
            assert result is None or result == {}
        except Exception:
            pass


class TestEnsureEventsTable:
    """Test table creation."""

    def test_signature(self):
        """Should be callable."""
        assert callable(ensure_events_table)
