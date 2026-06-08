"""Tests for RyseCoordinator behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError

from custom_components.ryse.coordinator import RyseCoordinator
from tests import RYSE_ADDRESS


@pytest.fixture
def mock_coordinator(hass, mock_ryse_device):
    """Build a RyseCoordinator with __init__ bypassed for unit testing."""
    with (
        patch("custom_components.ryse.coordinator.bluetooth.async_set_fallback_availability_interval"),
        patch.object(RyseCoordinator, "__init__", lambda self, *a, **kw: None),
    ):
        coord = RyseCoordinator.__new__(RyseCoordinator)

    coord.hass = hass
    coord.device = mock_ryse_device
    coord._name = "Test Shade"
    coord.address = RYSE_ADDRESS
    coord._position = None
    coord._battery = None
    coord._available = False
    coord._entry_id = "test_entry"
    coord._adv_count = 0
    coord._adv_first_ts = None
    coord._adv_last_ts = None
    coord._adv_last_summary_ts = 0.0
    coord._adv_sources = {}
    coord.async_update_listeners = MagicMock()
    return coord


# -----------------------------------------------------------------------------
# Advertisement handling
# -----------------------------------------------------------------------------


class TestAdvertisementHandling:
    def test_adv_updates_position_and_battery(self, mock_coordinator):
        """Parsed adv data → coordinator position/battery state."""
        mock_coordinator.device.parse_advertisement.return_value = {"position": 33, "battery": 77}
        mock_coordinator._async_handle_bluetooth_event(MagicMock(), MagicMock())
        assert mock_coordinator._position == 33
        assert mock_coordinator._battery == 77
        assert mock_coordinator._available is True

    def test_adv_updates_ble_device(self, mock_coordinator):
        """BLEDevice reference is updated from the advertisement."""
        service_info = MagicMock()
        service_info.device = MagicMock()
        mock_coordinator._async_handle_bluetooth_event(service_info, MagicMock())
        mock_coordinator.device.set_ble_device.assert_called_once_with(service_info.device)

    def test_adv_fires_battery_callbacks(self, mock_coordinator):
        """Battery callbacks are invoked when adv carries battery data."""
        cb = MagicMock()
        mock_coordinator.device._battery_callbacks = [cb]
        mock_coordinator.device.parse_advertisement.return_value = {"position": 0, "battery": 99}
        mock_coordinator._async_handle_bluetooth_event(MagicMock(), MagicMock())
        cb.assert_called_once_with(99)

    def test_adv_skips_battery_callback_when_no_battery(self, mock_coordinator):
        """No battery in adv → no battery callbacks fire."""
        cb = MagicMock()
        mock_coordinator.device._battery_callbacks = [cb]
        mock_coordinator.device.parse_advertisement.return_value = {"position": 50}
        mock_coordinator._async_handle_bluetooth_event(MagicMock(), MagicMock())
        cb.assert_not_called()

    def test_adv_marks_available(self, mock_coordinator):
        """Receiving any adv marks the device available."""
        mock_coordinator._available = False
        mock_coordinator._async_handle_bluetooth_event(MagicMock(), MagicMock())
        assert mock_coordinator._available is True

    def test_adv_tracks_source_counts(self, mock_coordinator):
        """Per-scanner source advertisement count is tracked."""
        info = MagicMock()
        info.source = "AA:BB:CC:DD:EE:01"
        mock_coordinator._async_handle_bluetooth_event(info, MagicMock())
        mock_coordinator._async_handle_bluetooth_event(info, MagicMock())
        info2 = MagicMock()
        info2.source = "AA:BB:CC:DD:EE:02"
        mock_coordinator._async_handle_bluetooth_event(info2, MagicMock())
        assert mock_coordinator._adv_sources == {"AA:BB:CC:DD:EE:01": 2, "AA:BB:CC:DD:EE:02": 1}


class TestPositionNotification:
    def test_position_notification_updates_state(self, mock_coordinator):
        """Position notification updates _position and marks available."""
        mock_coordinator._handle_position_notification(75)
        assert mock_coordinator._position == 75
        assert mock_coordinator._available is True


class TestUnavailable:
    def test_unavailable_clears_available(self, mock_coordinator):
        """The base class's unavailable callback marks us as not available."""
        mock_coordinator._available = True
        mock_coordinator._async_handle_unavailable(MagicMock())
        assert mock_coordinator._available is False


# -----------------------------------------------------------------------------
# Polling — confirm it's truly disabled
# -----------------------------------------------------------------------------


class TestPolling:
    def test_needs_poll_always_false(self, mock_coordinator):
        """Polling is disabled — advertisements carry everything we need."""
        assert mock_coordinator._needs_poll(MagicMock(), 0) is False
        assert mock_coordinator._needs_poll(MagicMock(), 9999) is False

    @pytest.mark.asyncio
    async def test_noop_poll_does_nothing(self, mock_coordinator):
        """Poll method is a no-op and returns None."""
        assert (await mock_coordinator._noop_poll(MagicMock())) is None


# -----------------------------------------------------------------------------
# Command execution
# -----------------------------------------------------------------------------


class TestExecuteCommand:
    @pytest.mark.asyncio
    async def test_happy_path_connects_writes_disconnects(self, mock_coordinator):
        """A successful command path: connect → write → disconnect."""
        op = AsyncMock()
        ble_device = MagicMock()
        with (
            patch(
                "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
                return_value=ble_device,
            ),
            patch("custom_components.ryse.coordinator.ir.async_delete_issue") as mock_delete,
        ):
            await mock_coordinator._execute_command(op, 42)
        op.assert_awaited_once_with(42)
        mock_coordinator.device.disconnect.assert_awaited()
        mock_delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_ble_device_marks_unavailable(self, mock_coordinator):
        """No reachable BLE device → mark unavailable, return early."""
        op = AsyncMock()
        with patch(
            "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
            return_value=None,
        ):
            await mock_coordinator._execute_command(op)
        op.assert_not_awaited()
        assert mock_coordinator._available is False

    @pytest.mark.asyncio
    async def test_connect_failure_retries_then_gives_up(self, mock_coordinator):
        """Failed connect on attempt 1 → sleep + retry; both fail → unavailable."""
        op = AsyncMock()
        mock_coordinator.device.connect = AsyncMock(return_value=False)
        with (
            patch(
                "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
                return_value=MagicMock(),
            ),
            patch("custom_components.ryse.coordinator.asyncio.sleep", new=AsyncMock()),
        ):
            await mock_coordinator._execute_command(op)
        op.assert_not_awaited()
        assert mock_coordinator._available is False
        assert mock_coordinator.device.connect.await_count == 2

    @pytest.mark.asyncio
    async def test_bleak_error_retries_once(self, mock_coordinator):
        """First op raises BleakError, retry succeeds."""
        op = AsyncMock(side_effect=[BleakError("transient"), None])
        with (
            patch(
                "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
                return_value=MagicMock(),
            ),
            patch("custom_components.ryse.coordinator.asyncio.sleep", new=AsyncMock()),
        ):
            await mock_coordinator._execute_command(op)
        assert op.await_count == 2

    @pytest.mark.asyncio
    async def test_auth_5_creates_repair_issue(self, mock_coordinator):
        """Insufficient authentication error surfaces a repair issue."""
        op = AsyncMock(side_effect=BleakError("Insufficient authentication (5)"))
        with (
            patch(
                "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
                return_value=MagicMock(),
            ),
            patch("custom_components.ryse.coordinator.asyncio.sleep", new=AsyncMock()),
            patch("custom_components.ryse.coordinator.ir.async_create_issue") as mock_create,
        ):
            await mock_coordinator._execute_command(op)
        mock_create.assert_called()
        # Issue id contains the address
        call_kwargs = mock_create.call_args
        assert RYSE_ADDRESS in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_both_attempts_fail_marks_unavailable(self, mock_coordinator):
        """Two consecutive BleakErrors → marked unavailable."""
        op = AsyncMock(side_effect=BleakError("broken"))
        with (
            patch(
                "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
                return_value=MagicMock(),
            ),
            patch("custom_components.ryse.coordinator.asyncio.sleep", new=AsyncMock()),
        ):
            await mock_coordinator._execute_command(op)
        assert mock_coordinator._available is False
        assert op.await_count == 2

    @pytest.mark.asyncio
    async def test_success_clears_existing_repair_issue(self, mock_coordinator):
        """A successful command clears any outstanding repair issue."""
        op = AsyncMock()
        with (
            patch(
                "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
                return_value=MagicMock(),
            ),
            patch("custom_components.ryse.coordinator.ir.async_delete_issue") as mock_delete,
        ):
            await mock_coordinator._execute_command(op)
        mock_delete.assert_called_once()


# -----------------------------------------------------------------------------
# Cover command wrappers — short-circuits on already-at-target
# -----------------------------------------------------------------------------


class TestCoverCommands:
    @pytest.mark.asyncio
    async def test_set_position_dispatches(self, mock_coordinator):
        """async_set_position → _execute_command(set_position, pos)."""
        with patch.object(mock_coordinator, "_execute_command", new=AsyncMock()) as m:
            await mock_coordinator.async_set_position(60)
            m.assert_awaited_once_with(mock_coordinator.device.set_position, 60)

    @pytest.mark.asyncio
    async def test_open_skips_when_already_open(self, mock_coordinator):
        """open() is a no-op when position is already 0."""
        mock_coordinator._position = 0
        with patch.object(mock_coordinator, "_execute_command", new=AsyncMock()) as m:
            await mock_coordinator.async_open_cover()
            m.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_open_runs_when_not_open(self, mock_coordinator):
        """open() dispatches when position != 0."""
        mock_coordinator._position = 50
        with patch.object(mock_coordinator, "_execute_command", new=AsyncMock()) as m:
            await mock_coordinator.async_open_cover()
            m.assert_awaited_once_with(mock_coordinator.device.open)

    @pytest.mark.asyncio
    async def test_close_skips_when_already_closed(self, mock_coordinator):
        """close() is a no-op when position is already 100."""
        mock_coordinator._position = 100
        with patch.object(mock_coordinator, "_execute_command", new=AsyncMock()) as m:
            await mock_coordinator.async_close_cover()
            m.assert_not_awaited()


# -----------------------------------------------------------------------------
# Properties
# -----------------------------------------------------------------------------


class TestProperties:
    def test_properties_expose_state(self, mock_coordinator):
        mock_coordinator._position = 25
        mock_coordinator._battery = 88
        mock_coordinator._available = True
        assert mock_coordinator.position == 25
        assert mock_coordinator.battery == 88
        assert mock_coordinator.available is True
        assert mock_coordinator.initializing is False
        assert mock_coordinator.name == "Test Shade"

    @pytest.mark.asyncio
    async def test_async_update_battery_sets_value_and_notifies(self, mock_coordinator):
        await mock_coordinator.async_update_battery(73)
        assert mock_coordinator._battery == 73
        mock_coordinator.async_update_listeners.assert_called()
