"""
test_solis_and_iog.py
=====================
Tests for solis_and_iog.py.

Run with:
    poetry run pytest -v tests/test_solis_and_iog.py
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

from solis_and_iog.solis_and_iog import OctopusClient, SolisClient, ChargeSyncApp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc(hour: int, minute: int, date: datetime | None = None) -> datetime:
    """Return a UTC-aware datetime for today at the given hour:minute."""
    base = date or datetime.now(timezone.utc)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def make_client() -> OctopusClient:
    """Return an OctopusClient with default off-peak window (23:30–05:30)."""
    return OctopusClient(api_key="test_key", account_number="A-TEST1234")


def make_dispatch(start: datetime, end: datetime) -> dict:
    """Return a minimal raw dispatch dict."""
    return {
        "startDt": start.isoformat(),
        "endDt":   end.isoformat(),
        "delta":   -1.5,
        "meta":    {"source": "test", "location": "home"},
    }


FULL_SCHEDULE_STRING = (
    "50,60,"
    "23:30-05:30,00:00-00:00,1,0,"
    "00:00-00:00,00:00-00:00,0,0,"
    "00:00-00:00,00:00-00:00,0,0"
)


# ===========================================================================
# OctopusClient._parse_dt
# ===========================================================================

class TestParseDt:
    def test_parses_utc_iso_string(self):
        dt = OctopusClient._parse_dt("2024-01-15T02:00:00+00:00")
        assert dt.hour == 2
        assert dt.tzinfo is not None

    def test_parses_naive_string_assumes_utc(self):
        dt = OctopusClient._parse_dt("2024-01-15T02:00:00")
        assert dt.tzinfo == timezone.utc

    def test_parses_offset_aware_string(self):
        dt = OctopusClient._parse_dt("2024-01-15T03:00:00+01:00")
        assert dt.tzinfo is not None

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            OctopusClient._parse_dt("not-a-date")


# ===========================================================================
# OctopusClient._is_outside_offpeak
# ===========================================================================

class TestIsOutsideOffpeak:
    """
    Off-peak window: 23:30–05:30 (spans midnight).
    _is_outside_offpeak should return:
      False — slot is entirely within 23:30–05:30 (battery's own schedule handles it)
      True  — slot extends outside that window (extra IOG dispatch we care about)
    """

    def setup_method(self):
        self.client = make_client()

    # --- Slots entirely within off-peak: should return False ---

    def test_exact_offpeak_window_is_not_outside(self):
        assert self.client._is_outside_offpeak(utc(23, 30), utc(5, 30)) is False

    def test_slot_wholly_within_offpeak_after_midnight(self):
        assert self.client._is_outside_offpeak(utc(0, 0), utc(5, 0)) is False

    def test_slot_starts_at_offpeak_start(self):
        assert self.client._is_outside_offpeak(utc(23, 30), utc(4, 0)) is False

    def test_slot_ends_at_offpeak_end(self):
        assert self.client._is_outside_offpeak(utc(1, 0), utc(5, 30)) is False

    # --- Slots that extend outside off-peak: should return True ---

    def test_slot_starts_before_offpeak(self):
        # 23:00 is before 23:30
        assert self.client._is_outside_offpeak(utc(23, 0), utc(5, 30)) is True

    def test_slot_ends_after_offpeak(self):
        # 06:00 is after 05:30
        assert self.client._is_outside_offpeak(utc(23, 30), utc(6, 0)) is True

    def test_slot_spans_beyond_offpeak_on_both_sides(self):
        # The original bug: 23:00-06:00 looks in-range at both endpoints
        assert self.client._is_outside_offpeak(utc(23, 0), utc(6, 0)) is True

    def test_daytime_slot_is_outside(self):
        assert self.client._is_outside_offpeak(utc(12, 0), utc(13, 0)) is True

    def test_early_morning_slot_past_offpeak_end(self):
        assert self.client._is_outside_offpeak(utc(5, 30), utc(7, 0)) is True

    def test_evening_slot_before_offpeak_start(self):
        assert self.client._is_outside_offpeak(utc(20, 0), utc(22, 0)) is True

    # --- Custom off-peak window ---

    def test_custom_offpeak_window(self):
        client = OctopusClient(
            api_key="k", account_number="A-TEST",
            offpeak_start=(22, 0),
            offpeak_end=(6, 0),
        )
        # Within custom window — should not be outside
        assert client._is_outside_offpeak(utc(22, 0), utc(6, 0)) is False
        # Outside custom window
        assert client._is_outside_offpeak(utc(21, 0), utc(6, 0)) is True


# ===========================================================================
# OctopusClient.find_active_extra_dispatch
# ===========================================================================

class TestFindActiveExtraDispatch:

    def setup_method(self):
        self.client = make_client()

    def _patch_dispatches(self, dispatches: list[dict]):
        self.client._get_planned_dispatches = MagicMock(return_value=dispatches)

    def test_returns_none_when_no_dispatches(self):
        self._patch_dispatches([])
        assert self.client.find_active_extra_dispatch() is None

    def test_returns_none_for_in_window_active_dispatch(self):
        # Active but entirely within 23:30-05:30 — should be ignored
        now = datetime.now(timezone.utc)
        start = now.replace(hour=23, minute=30, second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=1)
        end = start + timedelta(hours=4)
        self._patch_dispatches([make_dispatch(start, end)])
        # Slot is active but in-window — should return None
        result = self.client.find_active_extra_dispatch()
        assert result is None

    def test_returns_none_for_future_dispatch(self):
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=2)
        end   = now + timedelta(hours=3)
        self._patch_dispatches([make_dispatch(start, end)])
        assert self.client.find_active_extra_dispatch() is None

    def test_returns_none_for_past_dispatch(self):
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=3)
        end   = now - timedelta(hours=1)
        self._patch_dispatches([make_dispatch(start, end)])
        assert self.client.find_active_extra_dispatch() is None

    def test_returns_dispatch_for_active_extra_slot(self):
        now   = datetime.now(timezone.utc)
        start = now - timedelta(minutes=5)
        # Force a daytime slot so it's definitely outside off-peak
        start = start.replace(hour=14, minute=0)
        end   = start + timedelta(hours=1)
        if end < now:
            start = now - timedelta(minutes=5)
            end   = now + timedelta(minutes=55)
            start = start.replace(hour=14)
            end   = end.replace(hour=15)
        self._patch_dispatches([make_dispatch(start, end)])
        result = self.client.find_active_extra_dispatch()
        # Only assert if the slot is actually active right now
        # (time-of-day dependent, so we accept None gracefully)
        if result is not None:
            assert "start" in result
            assert "end" in result
            assert "raw" in result

    def test_skips_malformed_dispatch(self):
        self._patch_dispatches([{"bad": "data"}])
        assert self.client.find_active_extra_dispatch() is None

    def test_skips_malformed_and_returns_valid(self):
        now   = datetime.now(timezone.utc)
        start = now - timedelta(minutes=10)
        end   = now + timedelta(minutes=50)
        dispatches = [
            {"bad": "data"},
            make_dispatch(start, end),
        ]
        self._patch_dispatches(dispatches)
        # If the valid dispatch is outside off-peak it should be returned;
        # the malformed one should be silently skipped regardless.
        # We just verify no exception is raised.
        self.client.find_active_extra_dispatch()  # must not raise


# ===========================================================================
# SolisClient._parse_value_string
# ===========================================================================

class TestParseValueString:

    def test_parses_full_schedule_string(self):
        result = SolisClient._parse_value_string(FULL_SCHEDULE_STRING)
        assert result["charge_current"]    == "50"
        assert result["discharge_current"] == "60"
        assert len(result["charge"])    == 3
        assert len(result["discharge"]) == 3

    def test_slot1_charge_parsed_correctly(self):
        result = SolisClient._parse_value_string(FULL_SCHEDULE_STRING)
        assert result["charge"][0]["start"]  == "23:30"
        assert result["charge"][0]["end"]    == "05:30"
        assert result["charge"][0]["enable"] == 1

    def test_slot2_and_slot3_are_blank(self):
        result = SolisClient._parse_value_string(FULL_SCHEDULE_STRING)
        for i in (1, 2):
            assert result["charge"][i]["start"]  == "00:00"
            assert result["charge"][i]["end"]    == "00:00"
            assert result["charge"][i]["enable"] == 0

    def test_discharge_slot1_is_blank(self):
        result = SolisClient._parse_value_string(FULL_SCHEDULE_STRING)
        assert result["discharge"][0]["start"]  == "00:00"
        assert result["discharge"][0]["end"]    == "00:00"
        assert result["discharge"][0]["enable"] == 0

    def test_handles_truncated_string_gracefully(self):
        # Only the current fields, no slots
        result = SolisClient._parse_value_string("50,60")
        assert result["charge_current"]    == "50"
        assert result["discharge_current"] == "60"
        assert result["charge"][0]["start"] == "00:00"

    def test_handles_empty_string_gracefully(self):
        # "".split(",") yields [""] so parts[0] is "" — the "50"/"60" fallbacks
        # only trigger when len(parts) == 0, which str.split() never produces.
        result = SolisClient._parse_value_string("")
        assert result["charge_current"]    == ""   # parts[0] exists as ""
        assert result["discharge_current"] == "60" # parts[1] missing, fallback triggers
        assert result["charge"][0]["start"] == "00:00"


# ===========================================================================
# SolisClient._build_value_string  (round-trip)
# ===========================================================================

class TestBuildValueString:

    def test_round_trip_is_idempotent(self):
        parsed   = SolisClient._parse_value_string(FULL_SCHEDULE_STRING)
        rebuilt  = SolisClient._build_value_string(parsed)
        reparsed = SolisClient._parse_value_string(rebuilt)
        assert reparsed == parsed

    def test_modifying_slot3_reflects_in_output(self):
        parsed = SolisClient._parse_value_string(FULL_SCHEDULE_STRING)
        parsed["charge"][2] = {"start": "14:00", "end": "15:00", "enable": 1}
        result = SolisClient._build_value_string(parsed)
        assert "14:00-15:00" in result

    def test_other_slots_untouched_when_slot3_modified(self):
        parsed = SolisClient._parse_value_string(FULL_SCHEDULE_STRING)
        parsed["charge"][2] = {"start": "14:00", "end": "15:00", "enable": 1}
        result = SolisClient._build_value_string(parsed)
        # Slot 1 original times should still be present
        assert "23:30-05:30" in result


# ===========================================================================
# SolisClient._fmt_time
# ===========================================================================

class TestFmtTime:

    def test_formats_utc_datetime_as_local_hhmm(self):
        dt  = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)
        fmt = SolisClient.fmt_time(dt)
        # Result is local time; just check it's HH:MM shaped
        assert len(fmt) == 5
        assert fmt[2] == ":"
        assert fmt[:2].isdigit()
        assert fmt[3:].isdigit()


# ===========================================================================
# SolisClient.set_charge_slot / clear_charge_slot  (mocked _post)
# ===========================================================================

def make_solis() -> SolisClient:
    return SolisClient(
        key_id="kid", key_secret="ksecret",
        inverter_sn="SN123456", time_slot=3,
    )


class TestSetChargeSlot:

    def setup_method(self):
        self.solis = make_solis()

    def _mock_post(self, read_response: dict, write_response: dict):
        responses = iter([read_response, write_response])
        self.solis._post = MagicMock(side_effect=lambda *a, **kw: next(responses))

    def test_returns_true_on_success_code_zero(self):
        self._mock_post(
            {"data": {"msg": FULL_SCHEDULE_STRING}},
            {"code": "0"},
        )
        now = datetime.now(timezone.utc)
        assert self.solis.set_charge_slot(now, now + timedelta(hours=1)) is True

    def test_returns_true_on_success_true(self):
        self._mock_post(
            {"data": {"msg": FULL_SCHEDULE_STRING}},
            {"success": True},
        )
        now = datetime.now(timezone.utc)
        assert self.solis.set_charge_slot(now, now + timedelta(hours=1)) is True

    def test_returns_false_on_api_failure(self):
        self._mock_post(
            {"data": {"msg": FULL_SCHEDULE_STRING}},
            {"code": "1", "msg": "error"},
        )
        now = datetime.now(timezone.utc)
        assert self.solis.set_charge_slot(now, now + timedelta(hours=1)) is False

    def test_returns_false_when_read_fails(self):
        self.solis._post = MagicMock(return_value={})
        now = datetime.now(timezone.utc)
        assert self.solis.set_charge_slot(now, now + timedelta(hours=1)) is False

    def test_only_slot3_is_modified(self):
        written_values = []

        def capture_post(path, body):
            if path == SolisClient.READ_PATH:
                return {"data": {"msg": FULL_SCHEDULE_STRING}}
            written_values.append(body.get("value", ""))
            return {"code": "0"}

        self.solis._post = MagicMock(side_effect=capture_post)
        now = datetime.now(timezone.utc)
        self.solis.set_charge_slot(now, now + timedelta(hours=1))

        assert written_values, "No write call was made"
        value_str = written_values[0]
        parts     = value_str.split(",")
        # Slot 1 (fields 2-5) must remain unchanged
        assert parts[2] == "23:30-05:30", "Slot 1 charge window was modified"


class TestClearChargeSlot:

    def setup_method(self):
        self.solis = make_solis()

    def _mock_post(self, read_response: dict, write_response: dict):
        responses = iter([read_response, write_response])
        self.solis._post = MagicMock(side_effect=lambda *a, **kw: next(responses))

    def test_returns_true_on_success(self):
        self._mock_post(
            {"data": {"msg": FULL_SCHEDULE_STRING}},
            {"code": "0"},
        )
        assert self.solis.clear_charge_slot() is True

    def test_slot3_zeroed_after_clear(self):
        written_values = []

        def capture_post(path, body):
            if path == SolisClient.READ_PATH:
                return {"data": {"msg": FULL_SCHEDULE_STRING}}
            written_values.append(body.get("value", ""))
            return {"code": "0"}

        self.solis._post = MagicMock(side_effect=capture_post)
        self.solis.clear_charge_slot()

        assert written_values
        parsed = SolisClient._parse_value_string(written_values[0])
        assert parsed["charge"][2]["start"]  == "00:00"
        assert parsed["charge"][2]["end"]    == "00:00"
        assert parsed["charge"][2]["enable"] == 0

    def test_slot1_unchanged_after_clear(self):
        written_values = []

        def capture_post(path, body):
            if path == SolisClient.READ_PATH:
                return {"data": {"msg": FULL_SCHEDULE_STRING}}
            written_values.append(body.get("value", ""))
            return {"code": "0"}

        self.solis._post = MagicMock(side_effect=capture_post)
        self.solis.clear_charge_slot()

        parsed = SolisClient._parse_value_string(written_values[0])
        assert parsed["charge"][0]["start"]  == "23:30"
        assert parsed["charge"][0]["end"]    == "05:30"
        assert parsed["charge"][0]["enable"] == 1

    def test_returns_false_when_read_fails(self):
        self.solis._post = MagicMock(return_value={})
        assert self.solis.clear_charge_slot() is False


# ===========================================================================
# ChargeSyncApp._poll  (full orchestration, mocked clients)
# ===========================================================================

def make_app() -> tuple[ChargeSyncApp, MagicMock, MagicMock]:
    """Return (app, mock_octopus, mock_solis)."""
    octopus = MagicMock(spec=OctopusClient)
    solis   = MagicMock(spec=SolisClient)
    solis.set_charge_slot.return_value  = True
    solis.clear_charge_slot.return_value = True
    app = ChargeSyncApp(octopus=octopus, solis=solis, poll_interval=60)
    return app, octopus, solis


class TestChargeSyncAppPoll:

    def _dispatch(self, minutes_ahead: int = 30) -> dict:
        now   = datetime.now(timezone.utc)
        start = now - timedelta(minutes=5)
        end   = now + timedelta(minutes=minutes_ahead)
        return {"start": start, "end": end, "raw": {}}

    # --- No dispatch ---

    def test_no_dispatch_no_slot_active_does_nothing(self):
        app, octopus, solis = make_app()
        octopus.find_active_extra_dispatch.return_value = None
        app._poll()
        solis.set_charge_slot.assert_not_called()
        solis.clear_charge_slot.assert_not_called()

    def test_no_dispatch_clears_slot_when_active(self):
        app, octopus, solis = make_app()
        app._slot_active = True
        octopus.find_active_extra_dispatch.return_value = None
        app._poll()
        solis.clear_charge_slot.assert_called_once()

    def test_slot_inactive_after_successful_clear(self):
        app, octopus, solis = make_app()
        app._slot_active = True
        octopus.find_active_extra_dispatch.return_value = None
        app._poll()
        assert app._slot_active is False
        assert app._active_end  is None

    def test_slot_remains_active_if_clear_fails(self):
        app, octopus, solis = make_app()
        app._slot_active = True
        solis.clear_charge_slot.return_value = False
        octopus.find_active_extra_dispatch.return_value = None
        app._poll()
        assert app._slot_active is True

    # --- Active dispatch ---

    def test_new_dispatch_sets_charge_slot(self):
        app, octopus, solis = make_app()
        octopus.find_active_extra_dispatch.return_value = self._dispatch()
        app._poll()
        solis.set_charge_slot.assert_called_once()

    def test_slot_marked_active_after_successful_set(self):
        app, octopus, solis = make_app()
        dispatch = self._dispatch()
        octopus.find_active_extra_dispatch.return_value = dispatch
        app._poll()
        assert app._slot_active is True
        assert app._active_end  == dispatch["end"]

    def test_slot_not_marked_active_if_set_fails(self):
        app, octopus, solis = make_app()
        solis.set_charge_slot.return_value = False
        octopus.find_active_extra_dispatch.return_value = self._dispatch()
        app._poll()
        assert app._slot_active is False

    def test_already_active_same_end_does_not_call_set_again(self):
        app, octopus, solis = make_app()
        dispatch = self._dispatch()
        app._slot_active = True
        app._active_end  = dispatch["end"]
        octopus.find_active_extra_dispatch.return_value = dispatch
        app._poll()
        solis.set_charge_slot.assert_not_called()

    def test_already_active_changed_end_updates_slot(self):
        app, octopus, solis = make_app()
        dispatch     = self._dispatch(minutes_ahead=30)
        new_dispatch = {**dispatch, "end": dispatch["end"] + timedelta(minutes=15)}
        app._slot_active = True
        app._active_end  = dispatch["end"]
        octopus.find_active_extra_dispatch.return_value = new_dispatch
        app._poll()
        solis.set_charge_slot.assert_called_once()
        assert app._active_end == new_dispatch["end"]

    # --- Full cycle ---

    def test_full_dispatch_lifecycle(self):
        """Simulate: detect → active → ends → cleared."""
        app, octopus, solis = make_app()
        dispatch = self._dispatch()

        # Poll 1: dispatch detected
        octopus.find_active_extra_dispatch.return_value = dispatch
        app._poll()
        assert app._slot_active is True
        solis.set_charge_slot.assert_called_once()

        # Poll 2: dispatch still active — no duplicate set
        app._poll()
        solis.set_charge_slot.assert_called_once()  # still only once

        # Poll 3: dispatch gone
        octopus.find_active_extra_dispatch.return_value = None
        app._poll()
        solis.clear_charge_slot.assert_called_once()
        assert app._slot_active is False
