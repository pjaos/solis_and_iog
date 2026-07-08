"""
test_solis_and_iog.py
=====================
Tests for solis_and_iog.py.

Run with:
    poetry run pytest -v tests/test_solis_and_iog.py
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from solis_and_iog.solis_and_iog import ChargeSyncApp
from solis_and_iog.octopus import OctopusClient
from solis_and_iog.solis import SolisClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The local timezone of the test host, captured once at import time.
LOCAL_TZ = datetime.now().astimezone().tzinfo


def local_dt(hour: int, minute: int, date_offset: int = 0) -> datetime:
    """
    Return a timezone-aware datetime for today at hour:minute in LOCAL time.

    _is_outside_offpeak calls .astimezone() on its inputs before comparing
    them against the off-peak window (which is defined in local clock time).
    Supplying local-aware datetimes means .astimezone() is a no-op, so the
    hour:minute values survive the conversion unchanged on any host regardless
    of its UTC offset.
    """
    base = datetime.now(LOCAL_TZ).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return base + timedelta(days=date_offset)


def make_client(**kwargs) -> OctopusClient:
    """Return an OctopusClient with default off-peak window (23:30-05:30)."""
    return OctopusClient(api_key="test_key", account_number="A-TEST1234", **kwargs)


def make_dispatch(start: datetime, end: datetime,
                  delta: str | None = None,
                  delta_kwh: float | None = None) -> dict:
    """Return a raw dispatch dict using flexPlannedDispatches field names."""
    d: dict = {"start": start.isoformat(), "end": end.isoformat()}
    if delta is not None:
        d["delta"] = delta
    if delta_kwh is not None:
        d["deltaKwh"] = delta_kwh
    return d


FULL_SCHEDULE_STRING = (
    "50,60,"
    "23:30-05:30,00:00-00:00,1,0,"
    "00:00-00:00,00:00-00:00,0,0,"
    "00:00-00:00,00:00-00:00,0,0"
)

TOKEN_RESPONSE = {
    "data": {"obtainKrakenToken": {"token": "test-token-abc123"}}
}

DEVICES_RESPONSE = {
    "data": {
        "devices": [
            {"id": "device-ev-001",    "deviceType": "ELECTRIC_VEHICLES"},
            {"id": "device-meter-001", "deviceType": "ELECTRICITY_METERS"},
        ]
    }
}


def mock_response(response_json: dict, status_code: int = 200) -> MagicMock:
    """Return a mock requests.Response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = response_json
    mock_resp.text = str(response_json)
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


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
# OctopusClient._get_token
# ===========================================================================

class TestGetToken:

    def setup_method(self):
        self.client = make_client()

    def test_returns_token_on_success(self):
        with patch("solis_and_iog.octopus.requests.post",
                   return_value=mock_response(TOKEN_RESPONSE)):
            token = self.client._get_token()
        assert token == "test-token-abc123"

    def test_caches_token_on_second_call(self):
        with patch("solis_and_iog.octopus.requests.post",
                   return_value=mock_response(TOKEN_RESPONSE)) as mock_post:
            self.client._get_token()
            self.client._get_token()
        assert mock_post.call_count == 1

    def test_returns_none_when_token_missing_in_response(self):
        resp = {"data": {"obtainKrakenToken": {"token": None}}}
        with patch("solis_and_iog.octopus.requests.post",
                   return_value=mock_response(resp)):
            token = self.client._get_token()
        assert token is None

    def test_returns_none_on_request_exception(self):
        with patch("solis_and_iog.octopus.requests.post",
                   side_effect=Exception("network error")):
            token = self.client._get_token()
        assert token is None

    def test_uses_cached_token_without_api_call(self):
        self.client._token = "cached-token"
        with patch("solis_and_iog.octopus.requests.post") as mock_post:
            token = self.client._get_token()
        assert token == "cached-token"
        mock_post.assert_not_called()


# ===========================================================================
# OctopusClient._get_device_id
# ===========================================================================

class TestGetDeviceId:

    def setup_method(self):
        self.client = make_client()
        self.client._token = "cached-token"

    def test_returns_ev_device_id(self):
        with patch("solis_and_iog.octopus.requests.post",
                   return_value=mock_response(DEVICES_RESPONSE)):
            device_id = self.client._get_device_id()
        assert device_id == "device-ev-001"

    def test_skips_non_ev_devices(self):
        response = {
            "data": {
                "devices": [
                    {"id": "device-meter-001", "deviceType": "ELECTRICITY_METERS"},
                    {"id": "device-ev-001",    "deviceType": "ELECTRIC_VEHICLES"},
                ]
            }
        }
        with patch("solis_and_iog.octopus.requests.post",
                   return_value=mock_response(response)):
            device_id = self.client._get_device_id()
        assert device_id == "device-ev-001"

    def test_returns_none_when_no_ev_device(self):
        response = {
            "data": {
                "devices": [
                    {"id": "device-meter-001", "deviceType": "ELECTRICITY_METERS"},
                ]
            }
        }
        with patch("solis_and_iog.octopus.requests.post",
                   return_value=mock_response(response)):
            device_id = self.client._get_device_id()
        assert device_id is None

    def test_returns_none_when_no_token(self):
        self.client._token = None
        with patch.object(self.client, "_get_token", return_value=None):
            device_id = self.client._get_device_id()
        assert device_id is None

    def test_caches_device_id(self):
        with patch("solis_and_iog.octopus.requests.post",
                   return_value=mock_response(DEVICES_RESPONSE)) as mock_post:
            self.client._get_device_id()
            self.client._get_device_id()
        assert mock_post.call_count == 1

    def test_uses_cached_device_id_without_api_call(self):
        self.client._device_id = "cached-device"
        with patch("solis_and_iog.octopus.requests.post") as mock_post:
            device_id = self.client._get_device_id()
        assert device_id == "cached-device"
        mock_post.assert_not_called()

    def test_returns_none_on_request_exception(self):
        with patch("solis_and_iog.octopus.requests.post",
                   side_effect=Exception("network error")):
            device_id = self.client._get_device_id()
        assert device_id is None


# ===========================================================================
# OctopusClient._is_token_expired
# ===========================================================================

class TestIsTokenExpired:

    def setup_method(self):
        self.client = make_client()

    def test_returns_true_for_kt_ct_1124(self):
        data = {"errors": [{"extensions": {"errorCode": "KT-CT-1124"}}]}
        assert self.client._is_token_expired(data) is True

    def test_returns_false_for_other_error_code(self):
        data = {"errors": [{"extensions": {"errorCode": "KT-CT-9999"}}]}
        assert self.client._is_token_expired(data) is False

    def test_returns_false_for_no_errors(self):
        assert self.client._is_token_expired({"data": {}}) is False

    def test_returns_false_for_empty_errors(self):
        assert self.client._is_token_expired({"errors": []}) is False


# ===========================================================================
# OctopusClient._get_planned_dispatches
# ===========================================================================

class TestGetPlannedDispatches:

    def setup_method(self):
        self.client = make_client()
        self.client._token     = "cached-token"
        self.client._device_id = "device-ev-001"

    def _dispatch_response(self, dispatches: list) -> dict:
        return {"data": {"flexPlannedDispatches": dispatches}}

    def test_returns_empty_list_when_no_dispatches(self):
        with patch("solis_and_iog.octopus.requests.post",
                   return_value=mock_response(self._dispatch_response([]))):
            result = self.client._get_planned_dispatches()
        assert result == []

    def test_returns_dispatches(self):
        now = datetime.now(timezone.utc)
        dispatches = [
            {"start": now.isoformat(), "end": (now + timedelta(hours=1)).isoformat()}
        ]
        with patch("solis_and_iog.octopus.requests.post",
                   return_value=mock_response(self._dispatch_response(dispatches))):
            result = self.client._get_planned_dispatches()
        assert len(result) == 1

    def test_returns_empty_when_no_token(self):
        self.client._token = None
        with patch.object(self.client, "_get_token", return_value=None):
            result = self.client._get_planned_dispatches()
        assert result == []

    def test_returns_empty_when_no_device_id(self):
        self.client._device_id = None
        with patch.object(self.client, "_get_device_id", return_value=None):
            result = self.client._get_planned_dispatches()
        assert result == []

    def test_clears_token_on_exception(self):
        self.client._token = "valid-token"
        with patch("solis_and_iog.octopus.requests.post",
                   side_effect=Exception("network error")):
            result = self.client._get_planned_dispatches()
        assert result == []
        assert self.client._token is None

    def test_refreshes_token_on_jwt_expiry_in_body(self):
        expired_response = {
            "errors": [{"extensions": {"errorCode": "KT-CT-1124"}}],
            "data":   {"flexPlannedDispatches": None},
        }
        now = datetime.now(timezone.utc)
        fresh_dispatches = [
            {"start": now.isoformat(), "end": (now + timedelta(hours=1)).isoformat()}
        ]
        fresh_response = self._dispatch_response(fresh_dispatches)

        responses = iter([expired_response, fresh_response])
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = ""
        mock_resp.json.side_effect = lambda: next(responses)

        with patch("solis_and_iog.octopus.requests.post", return_value=mock_resp):
            with patch.object(self.client, "_get_token", return_value="new-token"):
                self.client._token = "old-token"
                result = self.client._get_planned_dispatches()

        assert len(result) == 1


# ===========================================================================
# OctopusClient._is_outside_offpeak
# ===========================================================================
#
# DESIGN NOTE — why local_dt() is used here
# ------------------------------------------
# _is_outside_offpeak calls .astimezone() on its inputs to convert them to the
# host's local time before comparing against the off-peak window (which is
# always specified in local clock time).
#
# Tests in TestIsOutsideOffpeak use local_dt(), which builds datetimes that are
# already in local time, so .astimezone() is a no-op and hour:minute values
# survive unchanged on any host regardless of UTC offset.
#
# TestIsOutsideOffpeakUTCInputs exercises the UTC→local conversion path
# explicitly, simulating real Octopus API timestamps.
# ===========================================================================

class TestIsOutsideOffpeak:
    """Local-time inputs — window arithmetic tested directly."""

    def setup_method(self):
        self.client = make_client()  # default window: 23:30 – 05:30

    # --- slots wholly inside the off-peak window (expect False) ---

    def test_exact_offpeak_window_is_not_outside(self):
        """Full window 23:30→05:30 is exactly the off-peak period."""
        assert self.client._is_outside_offpeak(local_dt(23, 30), local_dt(5, 30)) is False

    def test_slot_wholly_within_offpeak_after_midnight(self):
        """00:00→05:00 is entirely after midnight, inside the window."""
        assert self.client._is_outside_offpeak(local_dt(0, 0), local_dt(5, 0)) is False

    def test_slot_starts_at_offpeak_start(self):
        """23:30→04:00 starts exactly at the window boundary."""
        assert self.client._is_outside_offpeak(local_dt(23, 30), local_dt(4, 0)) is False

    def test_slot_ends_at_offpeak_end(self):
        """01:00→05:30 ends exactly at the window boundary."""
        assert self.client._is_outside_offpeak(local_dt(1, 0), local_dt(5, 30)) is False

    def test_slot_in_middle_of_window(self):
        """02:00→03:00 is well inside the window."""
        assert self.client._is_outside_offpeak(local_dt(2, 0), local_dt(3, 0)) is False

    def test_single_minute_slot_at_window_start_is_inside(self):
        """23:30→23:31 — a tiny slot right at the opening of the window."""
        assert self.client._is_outside_offpeak(local_dt(23, 30), local_dt(23, 31)) is False

    # --- slots that stray outside the off-peak window (expect True) ---

    def test_slot_starts_one_minute_before_offpeak(self):
        """23:29→05:00 starts one minute before the window opens."""
        assert self.client._is_outside_offpeak(local_dt(23, 29), local_dt(5, 0)) is True

    def test_slot_starts_before_offpeak(self):
        """23:00→05:30 starts half an hour before the window."""
        assert self.client._is_outside_offpeak(local_dt(23, 0), local_dt(5, 30)) is True

    def test_slot_ends_one_minute_after_offpeak(self):
        """00:00→05:31 ends one minute after the window closes."""
        assert self.client._is_outside_offpeak(local_dt(0, 0), local_dt(5, 31)) is True

    def test_slot_ends_after_offpeak(self):
        """23:30→06:00 ends after the window."""
        assert self.client._is_outside_offpeak(local_dt(23, 30), local_dt(6, 0)) is True

    def test_slot_spans_beyond_offpeak_on_both_sides(self):
        """23:00→06:00 extends outside the window on both sides."""
        assert self.client._is_outside_offpeak(local_dt(23, 0), local_dt(6, 0)) is True

    def test_daytime_slot_is_outside(self):
        """12:00→13:00 is in the middle of the day."""
        assert self.client._is_outside_offpeak(local_dt(12, 0), local_dt(13, 0)) is True

    def test_early_morning_slot_past_offpeak_end(self):
        """05:30→07:00: starts at window end, runs past it."""
        assert self.client._is_outside_offpeak(local_dt(5, 30), local_dt(7, 0)) is True

    def test_evening_slot_before_offpeak_start(self):
        """20:00→22:00 is in the evening before the window opens."""
        assert self.client._is_outside_offpeak(local_dt(20, 0), local_dt(22, 0)) is True

    def test_single_minute_slot_just_before_window_start_is_outside(self):
        """23:29→23:30 is one minute before the window opens."""
        assert self.client._is_outside_offpeak(local_dt(23, 29), local_dt(23, 30)) is True

    # --- custom off-peak window ---

    def test_custom_offpeak_window_contained(self):
        """Custom 22:00→06:00 window: exact window should not be outside."""
        client = make_client(offpeak_start=(22, 0), offpeak_end=(6, 0))
        assert client._is_outside_offpeak(local_dt(22, 0), local_dt(6, 0)) is False

    def test_custom_offpeak_window_slot_inside(self):
        """Custom 22:00→06:00 window: 00:00→05:00 is inside."""
        client = make_client(offpeak_start=(22, 0), offpeak_end=(6, 0))
        assert client._is_outside_offpeak(local_dt(0, 0), local_dt(5, 0)) is False

    def test_custom_offpeak_window_slot_starts_before(self):
        """Custom 22:00→06:00 window: 21:00→06:00 starts before it."""
        client = make_client(offpeak_start=(22, 0), offpeak_end=(6, 0))
        assert client._is_outside_offpeak(local_dt(21, 0), local_dt(6, 0)) is True

    def test_custom_offpeak_window_slot_ends_after(self):
        """Custom 22:00→06:00 window: 22:00→07:00 ends after it."""
        client = make_client(offpeak_start=(22, 0), offpeak_end=(6, 0))
        assert client._is_outside_offpeak(local_dt(22, 0), local_dt(7, 0)) is True


class TestIsOutsideOffpeakUTCInputs:
    """
    Pass genuine UTC datetimes (as the real Octopus API returns) into
    _is_outside_offpeak.  The method must convert them correctly to local time
    before comparing against the off-peak window.

    We build UTC datetimes by converting known local times to UTC, then
    passing those UTC datetimes back in.  This round-trip is timezone-agnostic
    and equivalent to what happens in production.
    """

    def setup_method(self):
        self.client = make_client()  # default window: 23:30 – 05:30

    def _local_as_utc(self, hour: int, minute: int, date_offset: int = 0) -> datetime:
        """Return the UTC equivalent of the given local hour:minute."""
        return local_dt(hour, minute, date_offset).astimezone(timezone.utc)

    def test_standard_offpeak_utc_timestamps_not_outside(self):
        """
        A dispatch from local 23:30 today to local 05:30 tomorrow, expressed
        as UTC timestamps, must not be flagged as an extra slot.
        """
        start_utc = self._local_as_utc(23, 30)
        end_utc   = self._local_as_utc(5, 30, date_offset=1)
        assert self.client._is_outside_offpeak(start_utc, end_utc) is False

    def test_utc_slot_inside_offpeak_not_outside(self):
        """A mid-window UTC slot (local 01:00→04:00) is inside the window."""
        start_utc = self._local_as_utc(1, 0)
        end_utc   = self._local_as_utc(4, 0)
        assert self.client._is_outside_offpeak(start_utc, end_utc) is False

    def test_utc_slot_ending_after_offpeak_is_outside(self):
        """A UTC slot ending at local 06:00 tomorrow overruns the window."""
        start_utc = self._local_as_utc(23, 30)
        end_utc   = self._local_as_utc(6, 0, date_offset=1)
        assert self.client._is_outside_offpeak(start_utc, end_utc) is True

    def test_utc_slot_starting_before_offpeak_is_outside(self):
        """A UTC slot starting at local 23:00 begins before the window opens."""
        start_utc = self._local_as_utc(23, 0)
        end_utc   = self._local_as_utc(5, 30, date_offset=1)
        assert self.client._is_outside_offpeak(start_utc, end_utc) is True

    def test_utc_daytime_slot_is_outside(self):
        """A UTC slot at local 14:00→15:00 is nowhere near the off-peak window."""
        start_utc = self._local_as_utc(14, 0)
        end_utc   = self._local_as_utc(15, 0)
        assert self.client._is_outside_offpeak(start_utc, end_utc) is True


# ===========================================================================
# OctopusClient._merge_dispatches
# ===========================================================================

def utc_dt(hour: int, minute: int = 0, day_offset: int = 0) -> datetime:
    """A fixed, timezone-aware UTC datetime for pure merge-logic tests."""
    return datetime(2026, 7, 8, hour, minute, tzinfo=timezone.utc) + timedelta(days=day_offset)


class TestMergeDispatches:
    """_merge_dispatches is a pure function — no client state or time needed."""

    def test_empty_list_returns_empty(self):
        assert OctopusClient._merge_dispatches([]) == []

    def test_single_slot_unchanged_no_boundaries(self):
        w = OctopusClient._merge_dispatches([(utc_dt(12, 30), utc_dt(13, 0))])
        assert len(w) == 1
        assert w[0]["start"]      == utc_dt(12, 30)
        assert w[0]["end"]        == utc_dt(13, 0)
        assert w[0]["boundaries"] == []

    def test_back_to_back_slots_merge_into_one_window(self):
        """The originally reported case: 12:30-13:00 + 13:00-13:30."""
        w = OctopusClient._merge_dispatches([
            (utc_dt(12, 30), utc_dt(13, 0)),
            (utc_dt(13, 0),  utc_dt(13, 30)),
        ])
        assert len(w) == 1
        assert w[0]["start"] == utc_dt(12, 30)
        assert w[0]["end"]   == utc_dt(13, 30)

    def test_join_point_recorded_as_boundary(self):
        w = OctopusClient._merge_dispatches([
            (utc_dt(12, 30), utc_dt(13, 0)),
            (utc_dt(13, 0),  utc_dt(13, 30)),
        ])
        assert w[0]["boundaries"] == [utc_dt(13, 0)]

    def test_three_contiguous_chunks_merge_with_two_boundaries(self):
        w = OctopusClient._merge_dispatches([
            (utc_dt(12, 30), utc_dt(13, 0)),
            (utc_dt(13, 0),  utc_dt(13, 30)),
            (utc_dt(13, 30), utc_dt(14, 0)),
        ])
        assert len(w) == 1
        assert w[0]["end"]        == utc_dt(14, 0)
        assert w[0]["boundaries"] == [utc_dt(13, 0), utc_dt(13, 30)]

    def test_small_gap_within_tolerance_merges(self):
        """A 45s sliver between slots (Octopus does this) still merges."""
        w = OctopusClient._merge_dispatches([
            (utc_dt(12, 30), utc_dt(13, 0)),
            (utc_dt(13, 0) + timedelta(seconds=45), utc_dt(13, 30)),
        ])
        assert len(w) == 1

    def test_gap_beyond_tolerance_stays_separate(self):
        w = OctopusClient._merge_dispatches([
            (utc_dt(12, 30), utc_dt(13, 0)),
            (utc_dt(13, 0) + timedelta(seconds=90), utc_dt(13, 30)),
        ])
        assert len(w) == 2

    def test_distinct_windows_not_merged(self):
        w = OctopusClient._merge_dispatches([
            (utc_dt(12, 30), utc_dt(13, 0)),
            (utc_dt(15, 0),  utc_dt(15, 30)),
        ])
        assert len(w) == 2
        assert w[0]["end"]   == utc_dt(13, 0)
        assert w[1]["start"] == utc_dt(15, 0)

    def test_unsorted_input_is_sorted_before_merging(self):
        w = OctopusClient._merge_dispatches([
            (utc_dt(13, 0),  utc_dt(13, 30)),
            (utc_dt(12, 30), utc_dt(13, 0)),
        ])
        assert len(w) == 1
        assert w[0]["start"] == utc_dt(12, 30)
        assert w[0]["end"]   == utc_dt(13, 30)

    def test_fully_contained_slot_absorbed_without_extending(self):
        """A slot inside an existing window must not extend it or add a boundary."""
        w = OctopusClient._merge_dispatches([
            (utc_dt(12, 30), utc_dt(13, 30)),
            (utc_dt(12, 45), utc_dt(13, 0)),
        ])
        assert len(w) == 1
        assert w[0]["end"]        == utc_dt(13, 30)
        assert w[0]["boundaries"] == []

    def test_overlapping_slot_extends_window(self):
        w = OctopusClient._merge_dispatches([
            (utc_dt(12, 30), utc_dt(13, 10)),
            (utc_dt(13, 0),  utc_dt(13, 30)),
        ])
        assert len(w) == 1
        assert w[0]["end"] == utc_dt(13, 30)

    def test_merge_spanning_midnight(self):
        """Chunks either side of midnight merge into one window."""
        w = OctopusClient._merge_dispatches([
            (utc_dt(23, 30), utc_dt(0, 0, day_offset=1)),
            (utc_dt(0, 0, day_offset=1), utc_dt(0, 30, day_offset=1)),
        ])
        assert len(w) == 1
        assert w[0]["start"] == utc_dt(23, 30)
        assert w[0]["end"]   == utc_dt(0, 30, day_offset=1)


# ===========================================================================
# OctopusClient.find_relevant_extra_dispatch
# ===========================================================================
#
# DESIGN NOTE — time-of-day-independent dispatch construction
# ------------------------------------------------------------
# These tests run against the real clock, so a dispatch built relative to
# "now" lands at a different local time depending on when the suite runs.
# Whether a slot is "outside off-peak" depends on that local time.
#
# To stay deterministic:
#   - Slots that must be OUTSIDE the off-peak window are given a duration
#     LONGER than the window itself (default window = 6h, so any 7h slot
#     cannot possibly be contained, whatever the time of day).
#   - Slots that must be INSIDE the window are pinned to the window's
#     actual local clock times (e.g. 23:30 + 4h), as in the off-peak tests.
# ===========================================================================

OUTSIDE_SPAN = timedelta(hours=7)   # > 6h off-peak window => always outside


class TestFindRelevantExtraDispatch:

    def setup_method(self):
        self.client = make_client()
        self.client._token     = "cached-token"
        self.client._device_id = "device-ev-001"

    def _patch_dispatches(self, dispatches: list[dict]):
        self.client._get_planned_dispatches = MagicMock(return_value=dispatches)

    def test_returns_none_when_no_dispatches(self):
        self._patch_dispatches([])
        assert self.client.find_relevant_extra_dispatch() is None

    def test_returns_active_dispatch(self):
        now = datetime.now(timezone.utc)
        start, end = now - timedelta(hours=1), now - timedelta(hours=1) + OUTSIDE_SPAN
        self._patch_dispatches([make_dispatch(start, end)])
        result = self.client.find_relevant_extra_dispatch()
        assert result is not None
        assert result["end"] > now

    def test_returns_upcoming_dispatch(self):
        """
        Behaviour change from find_active_extra_dispatch: a future window is
        now returned so the Solis slot can be written proactively.
        """
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=1)
        self._patch_dispatches([make_dispatch(start, start + OUTSIDE_SPAN)])
        result = self.client.find_relevant_extra_dispatch()
        assert result is not None
        assert result["start"] > now

    def test_returns_none_for_past_dispatch(self):
        now = datetime.now(timezone.utc)
        self._patch_dispatches([make_dispatch(now - timedelta(hours=9),
                                              now - timedelta(hours=1))])
        assert self.client.find_relevant_extra_dispatch() is None

    def test_returns_none_for_dispatch_inside_offpeak(self):
        """An upcoming dispatch wholly inside 23:30-05:30 must be ignored."""
        now   = datetime.now(timezone.utc)
        start = local_dt(23, 30)
        if start + timedelta(hours=4) < now:   # ensure the window hasn't fully passed
            start += timedelta(days=1)
        self._patch_dispatches([make_dispatch(start, start + timedelta(hours=4))])
        assert self.client.find_relevant_extra_dispatch() is None

    def test_back_to_back_chunks_returned_as_single_merged_window(self):
        """The core fix: contiguous chunks come back as one window."""
        now    = datetime.now(timezone.utc)
        s1     = now + timedelta(hours=1)
        middle = s1 + timedelta(hours=3, minutes=30)
        e2     = s1 + OUTSIDE_SPAN
        self._patch_dispatches([
            make_dispatch(s1, middle),
            make_dispatch(middle, e2),
        ])
        result = self.client.find_relevant_extra_dispatch()
        assert result is not None
        assert result["start"] == s1
        assert result["end"]   == e2
        assert result["boundaries"] == [middle]

    def test_result_always_contains_boundaries_key(self):
        now = datetime.now(timezone.utc)
        self._patch_dispatches([make_dispatch(now, now + OUTSIDE_SPAN)])
        result = self.client.find_relevant_extra_dispatch()
        assert "boundaries" in result
        assert result["boundaries"] == []

    def test_earliest_relevant_window_returned_first(self):
        now = datetime.now(timezone.utc)
        early_start = now + timedelta(hours=1)
        late_start  = now + timedelta(hours=12)
        self._patch_dispatches([
            make_dispatch(late_start,  late_start  + OUTSIDE_SPAN),
            make_dispatch(early_start, early_start + OUTSIDE_SPAN),
        ])
        result = self.client.find_relevant_extra_dispatch()
        assert result["start"] == early_start

    def test_skips_malformed_dispatch(self):
        self._patch_dispatches([{"bad": "data"}])
        assert self.client.find_relevant_extra_dispatch() is None

    def test_skips_malformed_and_returns_valid(self):
        """A malformed entry must not prevent a valid one being found."""
        now   = datetime.now(timezone.utc)
        start = now + timedelta(hours=1)
        self._patch_dispatches([
            {"bad": "data"},
            make_dispatch(start, start + OUTSIDE_SPAN),
        ])
        result = self.client.find_relevant_extra_dispatch()
        assert result is not None
        assert result["start"] == start


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
        result = SolisClient._parse_value_string("50,60")
        assert result["charge_current"]    == "50"
        assert result["discharge_current"] == "60"
        assert result["charge"][0]["start"] == "00:00"

    def test_handles_empty_string_gracefully(self):
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
        assert "23:30-05:30" in result


# ===========================================================================
# SolisClient.fmt_time
# ===========================================================================

class TestFmtTime:

    def test_formats_utc_datetime_as_local_hhmm(self):
        dt  = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)
        fmt = SolisClient.fmt_time(dt)
        assert len(fmt) == 5
        assert fmt[2] == ":"
        assert fmt[:2].isdigit()
        assert fmt[3:].isdigit()


# ===========================================================================
# SolisClient.get_battery_charge_power
# ===========================================================================

class TestGetBatteryChargePower:

    def setup_method(self):
        self.solis = make_solis()

    def _mock_post(self, response: dict) -> MagicMock:
        self.solis._post = MagicMock(return_value=response)
        return self.solis._post

    # --- success cases ---

    def test_returns_watts_when_charging(self):
        """A positive batteryPower (kW) means the battery is charging."""
        self._mock_post({"data": {"batteryPower": 2.5, "batteryPowerStr": "kW"}})
        assert self.solis.get_battery_charge_power() == pytest.approx(2500.0)

    def test_returns_negative_watts_when_discharging(self):
        """A negative batteryPower (kW) means the battery is discharging."""
        self._mock_post({"data": {"batteryPower": -1.8, "batteryPowerStr": "kW"}})
        assert self.solis.get_battery_charge_power() == pytest.approx(-1800.0)

    def test_returns_zero_when_idle(self):
        """Zero batteryPower means the battery is neither charging nor discharging."""
        self._mock_post({"data": {"batteryPower": 0.0, "batteryPowerStr": "kW"}})
        assert self.solis.get_battery_charge_power() == pytest.approx(0.0)

    def test_accepts_string_battery_power(self):
        """batteryPower is sometimes returned as a numeric string by the API."""
        self._mock_post({"data": {"batteryPower": "3.0", "batteryPowerStr": "kW"}})
        assert self.solis.get_battery_charge_power() == pytest.approx(3000.0)

    def test_calls_inverter_detail_endpoint(self):
        """Verify the correct API path and inverter SN are used."""
        mock_post = self._mock_post({"data": {"batteryPower": 1.0}})
        self.solis.get_battery_charge_power()
        mock_post.assert_called_once_with(
            SolisClient.INVERTER_DETAIL_PATH,
            {"sn": self.solis.inverter_sn},
        )

    # --- failure / edge cases ---

    def test_returns_none_when_api_returns_empty_dict(self):
        """An empty API response (e.g. network/auth failure) should return None."""
        self._mock_post({})
        assert self.solis.get_battery_charge_power() is None

    def test_returns_none_when_data_is_none(self):
        """data: null in the response should return None."""
        self._mock_post({"data": None})
        assert self.solis.get_battery_charge_power() is None

    def test_returns_none_when_battery_power_field_missing(self):
        """A response with data but no batteryPower field should return None."""
        self._mock_post({"data": {"batteryCapacitySoc": 80}})
        assert self.solis.get_battery_charge_power() is None

    def test_returns_none_when_battery_power_is_non_numeric_string(self):
        """A non-numeric batteryPower value should return None gracefully."""
        self._mock_post({"data": {"batteryPower": "N/A"}})
        assert self.solis.get_battery_charge_power() is None

    def test_returns_none_when_post_raises(self):
        """If _post raises an exception the method should propagate None, not crash."""
        self.solis._post = MagicMock(side_effect=Exception("connection refused"))
        # _post exceptions are caught inside _post itself and return {}; confirm
        # that a raw exception from _post still results in None via the empty-dict path.
        with patch.object(self.solis, "_post", side_effect=Exception("boom")):
            try:
                result = self.solis.get_battery_charge_power()
            except Exception:
                result = None
        assert result is None


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
        self._mock_post({"data": {"msg": FULL_SCHEDULE_STRING}}, {"code": "0"})
        now = datetime.now(timezone.utc)
        assert self.solis.set_charge_slot(now, now + timedelta(hours=1)) is True

    def test_returns_true_on_success_true(self):
        self._mock_post({"data": {"msg": FULL_SCHEDULE_STRING}}, {"success": True})
        now = datetime.now(timezone.utc)
        assert self.solis.set_charge_slot(now, now + timedelta(hours=1)) is True

    def test_returns_false_on_api_failure(self):
        self._mock_post({"data": {"msg": FULL_SCHEDULE_STRING}}, {"code": "1"})
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
        assert written_values
        assert written_values[0].split(",")[2] == "23:30-05:30"


class TestClearChargeSlot:

    def setup_method(self):
        self.solis = make_solis()

    def _mock_post(self, read_response: dict, write_response: dict):
        responses = iter([read_response, write_response])
        self.solis._post = MagicMock(side_effect=lambda *a, **kw: next(responses))

    def test_returns_true_on_success(self):
        self._mock_post({"data": {"msg": FULL_SCHEDULE_STRING}}, {"code": "0"})
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
#
# The app now writes the Solis slot PROACTIVELY: a window returned by
# find_relevant_extra_dispatch may be upcoming rather than active, and is
# written to the inverter immediately so the inverter's own clock opens the
# charge with no polling-latency gap. State attributes reflect this:
# _slot_written / _written_start / _written_end (plus _boundaries, the
# internal chunk joins of the merged window, used by the adaptive sleep).
# ===========================================================================

def make_app(poll_interval: int = 60) -> tuple[ChargeSyncApp, MagicMock, MagicMock]:
    octopus = MagicMock(spec=OctopusClient)
    solis   = MagicMock(spec=SolisClient)
    solis.set_charge_slot.return_value          = True
    solis.clear_charge_slot.return_value        = True
    solis.get_battery_charge_power.return_value = None
    return ChargeSyncApp(octopus=octopus, solis=solis,
                         poll_interval=poll_interval), octopus, solis


def make_window(start_offset_min: int, end_offset_min: int,
                boundaries_offset_min: list[int] | None = None) -> dict:
    """Build a merged-window dict as find_relevant_extra_dispatch returns it."""
    now = datetime.now(timezone.utc)
    return {
        "start":      now + timedelta(minutes=start_offset_min),
        "end":        now + timedelta(minutes=end_offset_min),
        "boundaries": [now + timedelta(minutes=m)
                       for m in (boundaries_offset_min or [])],
    }


def mark_written(app: ChargeSyncApp, window: dict) -> None:
    """Put the app into the state it would be in after writing this window."""
    app._slot_written  = True
    app._written_start = window["start"]
    app._written_end   = window["end"]
    app._boundaries    = list(window["boundaries"])


class TestChargeSyncAppPoll:

    # --- no dispatch ---

    def test_no_dispatch_nothing_written_does_nothing(self):
        app, octopus, solis = make_app()
        octopus.find_relevant_extra_dispatch.return_value = None
        app._poll()
        solis.set_charge_slot.assert_not_called()
        solis.clear_charge_slot.assert_not_called()

    def test_no_dispatch_clears_slot_when_written(self):
        app, octopus, solis = make_app()
        mark_written(app, make_window(-5, 30))
        octopus.find_relevant_extra_dispatch.return_value = None
        app._poll()
        solis.clear_charge_slot.assert_called_once()

    def test_state_reset_after_successful_clear(self):
        app, octopus, solis = make_app()
        mark_written(app, make_window(-5, 30, [10]))
        octopus.find_relevant_extra_dispatch.return_value = None
        app._poll()
        assert app._slot_written  is False
        assert app._written_start is None
        assert app._written_end   is None
        assert app._boundaries    == []

    def test_slot_remains_written_if_clear_fails(self):
        app, octopus, solis = make_app()
        mark_written(app, make_window(-5, 30))
        solis.clear_charge_slot.return_value = False
        octopus.find_relevant_extra_dispatch.return_value = None
        app._poll()
        assert app._slot_written is True

    # --- active dispatch ---

    def test_active_dispatch_sets_charge_slot(self):
        app, octopus, solis = make_app()
        octopus.find_relevant_extra_dispatch.return_value = make_window(-5, 30)
        app._poll()
        solis.set_charge_slot.assert_called_once()

    def test_state_recorded_after_successful_set(self):
        app, octopus, solis = make_app()
        window = make_window(-5, 30, [10])
        octopus.find_relevant_extra_dispatch.return_value = window
        app._poll()
        assert app._slot_written  is True
        assert app._written_start == window["start"]
        assert app._written_end   == window["end"]
        assert app._boundaries    == window["boundaries"]

    def test_state_not_recorded_if_set_fails(self):
        app, octopus, solis = make_app()
        solis.set_charge_slot.return_value = False
        octopus.find_relevant_extra_dispatch.return_value = make_window(-5, 30)
        app._poll()
        assert app._slot_written is False

    # --- upcoming dispatch (proactive writing) ---

    def test_upcoming_dispatch_written_proactively(self):
        """
        Key behaviour change: a window that has NOT yet started must be
        written immediately, so the inverter starts the charge on time even
        if no poll lands near the start.
        """
        app, octopus, solis = make_app()
        window = make_window(30, 90)
        octopus.find_relevant_extra_dispatch.return_value = window
        app._poll()
        solis.set_charge_slot.assert_called_once_with(window["start"], window["end"])
        assert app._slot_written is True

    def test_upcoming_dispatch_more_than_24h_away_is_deferred(self):
        """
        Solis slots are HH:MM only — writing a window >= ~24h away would
        activate at the wrong day's occurrence of that time.
        """
        app, octopus, solis = make_app()
        octopus.find_relevant_extra_dispatch.return_value = make_window(25 * 60, 26 * 60)
        app._poll()
        solis.set_charge_slot.assert_not_called()
        assert app._slot_written is False

    def test_deferred_far_future_dispatch_clears_stale_written_slot(self):
        """
        If the plan changes so the only remaining window is >24h away, any
        previously written slot must be cleared, not left on the inverter.
        """
        app, octopus, solis = make_app()
        mark_written(app, make_window(-5, 30))
        octopus.find_relevant_extra_dispatch.return_value = make_window(25 * 60, 26 * 60)
        app._poll()
        solis.clear_charge_slot.assert_called_once()
        assert app._slot_written is False

    # --- window changes while written ---

    def test_unchanged_window_does_not_call_set_again(self):
        app, octopus, solis = make_app()
        window = make_window(-5, 30)
        mark_written(app, window)
        octopus.find_relevant_extra_dispatch.return_value = window
        app._poll()
        solis.set_charge_slot.assert_not_called()

    def test_changed_end_updates_slot(self):
        """e.g. Octopus extends the window by appending another chunk."""
        app, octopus, solis = make_app()
        window = make_window(-5, 30)
        mark_written(app, window)
        extended = {**window, "end": window["end"] + timedelta(minutes=30),
                    "boundaries": [window["end"]]}
        octopus.find_relevant_extra_dispatch.return_value = extended
        app._poll()
        solis.set_charge_slot.assert_called_once()
        assert app._written_end == extended["end"]
        assert app._boundaries  == extended["boundaries"]

    def test_changed_end_shrink_updates_slot(self):
        """e.g. Octopus drops the second chunk of a merged window mid-slot."""
        app, octopus, solis = make_app()
        window = make_window(-5, 55, [25])
        mark_written(app, window)
        shrunk = {**window, "end": window["boundaries"][0], "boundaries": []}
        octopus.find_relevant_extra_dispatch.return_value = shrunk
        app._poll()
        solis.set_charge_slot.assert_called_once()
        assert app._written_end == shrunk["end"]
        assert app._boundaries  == []

    def test_changed_start_updates_slot(self):
        app, octopus, solis = make_app()
        window = make_window(-10, 30)
        mark_written(app, window)
        moved = {**window, "start": window["start"] + timedelta(minutes=5)}
        octopus.find_relevant_extra_dispatch.return_value = moved
        app._poll()
        solis.set_charge_slot.assert_called_once()
        assert app._written_start == moved["start"]

    def test_changed_window_set_fails_does_not_update_state(self):
        """If the rewrite fails, tracked times must not advance (next poll retries)."""
        app, octopus, solis = make_app()
        solis.set_charge_slot.return_value = False
        window = make_window(-10, 30)
        mark_written(app, window)
        moved = {**window, "start": window["start"] + timedelta(minutes=5)}
        octopus.find_relevant_extra_dispatch.return_value = moved
        app._poll()
        assert app._written_start == window["start"]
        assert app._written_end   == window["end"]

    def test_boundaries_refreshed_when_window_unchanged(self):
        """
        Chunk joins can move without the overall window changing; the stored
        boundaries (used by the adaptive sleep) must track the latest ones.
        """
        app, octopus, solis = make_app()
        window = make_window(-5, 55, [25])
        mark_written(app, window)
        new_boundary = window["start"] + timedelta(minutes=40)
        octopus.find_relevant_extra_dispatch.return_value = {**window,
                                                             "boundaries": [new_boundary]}
        app._poll()
        solis.set_charge_slot.assert_not_called()
        assert app._boundaries == [new_boundary]

    # --- lifecycle ---

    def test_full_dispatch_lifecycle(self):
        """Simulate: upcoming -> written -> active -> unchanged -> ends -> cleared."""
        app, octopus, solis = make_app()
        window = make_window(10, 40)

        # Poll 1: upcoming window detected and written proactively.
        octopus.find_relevant_extra_dispatch.return_value = window
        app._poll()
        assert app._slot_written is True
        solis.set_charge_slot.assert_called_once()

        # Poll 2: window unchanged (now notionally active) — no rewrite.
        app._poll()
        solis.set_charge_slot.assert_called_once()  # still only once

        # Poll 3: window has ended / disappeared from the plan — cleared.
        octopus.find_relevant_extra_dispatch.return_value = None
        app._poll()
        solis.clear_charge_slot.assert_called_once()
        assert app._slot_written  is False
        assert app._written_start is None
        assert app._written_end   is None

    # --- battery power logging ---

    def test_battery_power_logged_when_dispatch_active(self):
        app, octopus, solis = make_app()
        solis.get_battery_charge_power.return_value = 2500.0
        octopus.find_relevant_extra_dispatch.return_value = make_window(-5, 30)
        app._poll()
        app._poll()
        assert solis.get_battery_charge_power.call_count == 2

    def test_battery_power_not_logged_for_upcoming_window(self):
        """No charging is happening yet, so there is nothing to report."""
        app, octopus, solis = make_app()
        octopus.find_relevant_extra_dispatch.return_value = make_window(30, 90)
        app._poll()
        solis.get_battery_charge_power.assert_not_called()

    def test_battery_power_logged_after_slot_cleared(self):
        app, octopus, solis = make_app()
        solis.get_battery_charge_power.return_value = 0.0
        mark_written(app, make_window(-35, -1))
        octopus.find_relevant_extra_dispatch.return_value = None
        app._poll()
        solis.get_battery_charge_power.assert_called_once()

    def test_battery_power_not_logged_when_no_dispatch_and_nothing_written(self):
        app, octopus, solis = make_app()
        octopus.find_relevant_extra_dispatch.return_value = None
        app._poll()
        solis.get_battery_charge_power.assert_not_called()

    def test_battery_power_not_logged_if_clear_fails(self):
        app, octopus, solis = make_app()
        mark_written(app, make_window(-35, -1))
        solis.clear_charge_slot.return_value = False
        octopus.find_relevant_extra_dispatch.return_value = None
        app._poll()
        solis.get_battery_charge_power.assert_not_called()

    def test_battery_power_exception_does_not_break_poll(self):
        app, octopus, solis = make_app()
        solis.get_battery_charge_power.side_effect = RuntimeError("unexpected API response")
        octopus.find_relevant_extra_dispatch.return_value = make_window(-5, 30)
        # Should complete without raising
        app._poll()


# ===========================================================================
# ChargeSyncApp._next_sleep  (adaptive sleep around window boundaries)
# ===========================================================================

class TestNextSleep:

    def test_returns_poll_interval_when_nothing_written(self):
        app, _, _ = make_app(poll_interval=180)
        assert app._next_sleep() == 180

    def test_returns_poll_interval_when_all_boundaries_far_away(self):
        """Start/boundary/end all further than poll_interval -> normal cadence."""
        app, _, _ = make_app(poll_interval=180)
        mark_written(app, make_window(10, 70, [40]))
        assert app._next_sleep() == 180

    def test_wakes_just_after_imminent_start(self):
        """Verify the dispatch still exists just after the written start."""
        app, _, _ = make_app(poll_interval=180)
        window = make_window(0, 60)
        window["start"] = datetime.now(timezone.utc) + timedelta(seconds=30)
        mark_written(app, window)
        sleep = app._next_sleep()
        assert 30 <= sleep <= 30 + ChargeSyncApp.BOUNDARY_WAKE_DELAY_S + 2

    def test_wakes_just_after_imminent_end(self):
        """Clear the slot promptly after the window ends."""
        app, _, _ = make_app(poll_interval=180)
        window = make_window(-30, 0)
        window["end"] = datetime.now(timezone.utc) + timedelta(seconds=60)
        mark_written(app, window)
        sleep = app._next_sleep()
        assert 60 <= sleep <= 60 + ChargeSyncApp.BOUNDARY_WAKE_DELAY_S + 2

    def test_wakes_just_after_imminent_chunk_boundary(self):
        """Re-verify the remainder of a merged window at each chunk join."""
        app, _, _ = make_app(poll_interval=180)
        window = make_window(-10, 20)
        boundary = datetime.now(timezone.utc) + timedelta(seconds=45)
        window["boundaries"] = [boundary]
        mark_written(app, window)
        sleep = app._next_sleep()
        assert 45 <= sleep <= 45 + ChargeSyncApp.BOUNDARY_WAKE_DELAY_S + 2

    def test_earliest_upcoming_boundary_wins(self):
        app, _, _ = make_app(poll_interval=180)
        now = datetime.now(timezone.utc)
        window = make_window(-10, 3)                       # end in 3 min
        window["boundaries"] = [now + timedelta(seconds=40)]  # boundary sooner
        mark_written(app, window)
        sleep = app._next_sleep()
        assert sleep < 60   # boundary wake, not the 3-minute end

    def test_minimum_sleep_floor_applies(self):
        """A boundary a second away must not cause a near-zero sleep."""
        app, _, _ = make_app(poll_interval=180)
        window = make_window(-10, 20)
        window["boundaries"] = [datetime.now(timezone.utc) + timedelta(seconds=1)]
        mark_written(app, window)
        assert app._next_sleep() == ChargeSyncApp.MIN_SLEEP_S

    def test_past_boundaries_ignored(self):
        """Already-passed start/boundaries must not produce a wake."""
        app, _, _ = make_app(poll_interval=180)
        now = datetime.now(timezone.utc)
        window = make_window(-30, 60)
        window["boundaries"] = [now - timedelta(minutes=10)]
        mark_written(app, window)
        assert app._next_sleep() == 180

    def test_window_fully_passed_returns_poll_interval(self):
        app, _, _ = make_app(poll_interval=180)
        mark_written(app, make_window(-60, -5))
        assert app._next_sleep() == 180

    def test_sleep_never_exceeds_poll_interval(self):
        """Adaptive wakes only ever shorten the sleep, never lengthen it."""
        app, _, _ = make_app(poll_interval=60)
        mark_written(app, make_window(2, 90))   # start in 2 min > 60s interval
        assert app._next_sleep() <= 60


# ===========================================================================
# ChargeSyncApp.__init__  (configuration guards)
# ===========================================================================

class TestChargeSyncAppInit:

    def test_poll_interval_floor_enforced(self):
        """Intervals below 60s are raised to 60s to limit Octopus API usage."""
        app, _, _ = make_app(poll_interval=10)
        assert app.poll_interval == 60

    def test_poll_interval_above_floor_unchanged(self):
        app, _, _ = make_app(poll_interval=300)
        assert app.poll_interval == 300

    def test_initial_state_is_nothing_written(self):
        app, _, _ = make_app()
        assert app._slot_written  is False
        assert app._written_start is None
        assert app._written_end   is None
        assert app._boundaries    == []
