"""Tests for the RYSE coordinator."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.ryse.coordinator import RyseCoordinator

from . import RYSE_ADDRESS


@pytest.fixture
def mock_coordinator(hass: HomeAssistant, mock_ryse_device):
    """Create a RyseCoordinator with mocked dependencies."""
    with (
        patch(
            "custom_components.ryse.coordinator.bluetooth.async_track_unavailable",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.ryse.coordinator.bluetooth.async_register_callback",
            return_value=MagicMock(),
        ),
        patch.object(
            RyseCoordinator,
            "__init__",
            lambda self, *a, **kw: None,
        ),
    ):
        coord = RyseCoordinator.__new__(RyseCoordinator)

    # Manually set up the state that __init__ would create
    coord.hass = hass
    coord.device = mock_ryse_device
    coord._name = "Test Shade"
    coord.address = RYSE_ADDRESS
    coord._position = None
    coord._battery = None
    coord._last_adv = None
    coord._available = False
    coord._initializing = True
    coord._ready_event = MagicMock()
    coord._was_unavailable = True
    coord._reconnect_task = None
    coord._last_warm_connect_attempt = None
    coord.async_update_listeners = MagicMock()
    return coord


# ===================================================================
# Advertisement handling
# ===================================================================


class TestAdvertisementHandling:
    """Tests for _handle_adv callback."""

    def test_adv_updates_position_and_battery(self, mock_coordinator) -> None:
        """Advertisement with valid data should update position and battery."""
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 50,
            "battery": 85,
        }
        service_info = MagicMock()
        service_info.device = MagicMock()

        mock_coordinator._handle_adv(service_info, MagicMock())

        assert mock_coordinator._position == 50
        assert mock_coordinator._battery == 85
        assert mock_coordinator._available is True
        assert mock_coordinator._initializing is False

    def test_adv_sets_ready_event(self, mock_coordinator) -> None:
        """Advertisement with position should set the ready event."""
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 30,
            "battery": 72,
        }

        mock_coordinator._handle_adv(MagicMock(), MagicMock())

        mock_coordinator._ready_event.set.assert_called_once()

    def test_adv_no_position_does_not_set_ready(self, mock_coordinator) -> None:
        """Advertisement without position should not set ready."""
        mock_coordinator.device.parse_advertisement.return_value = {
            "battery": 72,
        }

        mock_coordinator._handle_adv(MagicMock(), MagicMock())

        mock_coordinator._ready_event.set.assert_not_called()

    def test_adv_calls_battery_callbacks(self, mock_coordinator) -> None:
        """Battery callbacks should be called with the new value."""
        mock_cb = MagicMock()
        mock_coordinator.device._battery_callbacks = [mock_cb]
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 50,
            "battery": 90,
        }

        mock_coordinator._handle_adv(MagicMock(), MagicMock())

        mock_cb.assert_called_once_with(90)

    def test_adv_calls_adv_callbacks(self, mock_coordinator) -> None:
        """Advertisement callbacks should be called."""
        mock_cb = MagicMock()
        mock_coordinator.device._adv_callbacks = [mock_cb]
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 50,
            "battery": 90,
        }

        mock_coordinator._handle_adv(MagicMock(), MagicMock())

        mock_cb.assert_called_once()

    def test_adv_updates_ble_device(self, mock_coordinator) -> None:
        """BLE device reference should be updated from advertisement."""
        service_info = MagicMock()
        service_info.device = MagicMock()
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 50,
            "battery": 85,
        }

        mock_coordinator._handle_adv(service_info, MagicMock())

        mock_coordinator.device.set_ble_device.assert_called_once_with(service_info.device)

    def test_adv_marks_online_after_unavailable(self, mock_coordinator) -> None:
        """Device should be marked online after receiving an adv while unavailable."""
        mock_coordinator._was_unavailable = True
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 50,
            "battery": 85,
        }

        mock_coordinator._handle_adv(MagicMock(), MagicMock())

        assert mock_coordinator._was_unavailable is False
        assert mock_coordinator._available is True


# ===================================================================
# Warm connect behavior
# ===================================================================


class TestWarmConnect:
    """Tests for warm connect logic in _handle_adv."""

    def test_passive_mode_no_warm_connect(self, mock_coordinator) -> None:
        """Passive mode should never warm connect."""
        mock_coordinator.device._active_mode = False
        mock_coordinator.device._is_connected = False
        mock_coordinator.device._connecting = False
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 50,
            "battery": 85,
        }

        with patch.object(mock_coordinator.hass, "async_create_task") as mock_task:
            mock_coordinator._handle_adv(MagicMock(), MagicMock())

        # Should NOT schedule warm connect in passive mode
        for call in mock_task.call_args_list:
            # Check that _warm_connect was not called
            assert "_warm_connect" not in str(call)

    def test_active_mode_warm_connect_on_disconnect(self, mock_coordinator) -> None:
        """Active mode should trigger warm connect when disconnected."""
        mock_coordinator.device._active_mode = True
        mock_coordinator.device._is_connected = False
        mock_coordinator.device._connecting = False
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 50,
            "battery": 85,
        }

        with patch.object(mock_coordinator.hass, "async_create_task") as mock_task:
            mock_coordinator._handle_adv(MagicMock(), MagicMock())

        # Should schedule warm connect
        assert mock_task.called

    def test_active_mode_warm_connect_cooldown(self, mock_coordinator) -> None:
        """Active mode should respect 60s cooldown between warm connect attempts."""
        mock_coordinator.device._active_mode = True
        mock_coordinator.device._is_connected = False
        mock_coordinator.device._connecting = False
        mock_coordinator._last_warm_connect_attempt = datetime.now()
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 50,
            "battery": 85,
        }

        with patch.object(mock_coordinator.hass, "async_create_task") as mock_task:
            mock_coordinator._handle_adv(MagicMock(), MagicMock())

        # Should NOT warm connect (cooldown not expired)
        for call in mock_task.call_args_list:
            assert "_warm_connect" not in str(call)

    def test_active_mode_warm_connect_after_cooldown(self, mock_coordinator) -> None:
        """Active mode should warm connect after cooldown expires."""
        mock_coordinator.device._active_mode = True
        mock_coordinator.device._is_connected = False
        mock_coordinator.device._connecting = False
        mock_coordinator._last_warm_connect_attempt = datetime.now() - timedelta(seconds=61)
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 50,
            "battery": 85,
        }

        with patch.object(mock_coordinator.hass, "async_create_task") as mock_task:
            mock_coordinator._handle_adv(MagicMock(), MagicMock())

        assert mock_task.called

    def test_skip_warm_connect_if_reconnect_running(self, mock_coordinator) -> None:
        """Should skip warm connect if _active_reconnect task is running."""
        mock_coordinator.device._active_mode = True
        mock_coordinator.device._is_connected = False
        mock_coordinator.device._connecting = False
        mock_coordinator._reconnect_task = MagicMock()
        mock_coordinator._reconnect_task.done.return_value = False
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 50,
            "battery": 85,
        }

        with patch.object(mock_coordinator.hass, "async_create_task") as mock_task:
            mock_coordinator._handle_adv(MagicMock(), MagicMock())

        # async_create_task should only be called for callbacks, not warm connect
        # If it was called, it should not be for _warm_connect
        for call in mock_task.call_args_list:
            assert "_warm_connect" not in str(call)

    def test_no_warm_connect_when_already_connected(self, mock_coordinator) -> None:
        """Should not warm connect if device is already connected."""
        mock_coordinator.device._active_mode = True
        mock_coordinator.device._is_connected = True
        mock_coordinator.device.parse_advertisement.return_value = {
            "position": 50,
            "battery": 85,
        }

        with patch.object(mock_coordinator.hass, "async_create_task") as mock_task:
            mock_coordinator._handle_adv(MagicMock(), MagicMock())

        for call in mock_task.call_args_list:
            assert "_warm_connect" not in str(call)


# ===================================================================
# Warm connect method
# ===================================================================


class TestWarmConnectMethod:
    async def test_warm_connect_success_resets_cooldown(self, mock_coordinator) -> None:
        mock_coordinator._last_warm_connect_attempt = datetime.now()
        mock_coordinator.device.connect = AsyncMock(return_value=True)

        await mock_coordinator._warm_connect()

        assert mock_coordinator._last_warm_connect_attempt is None

    async def test_warm_connect_failure_keeps_cooldown(self, mock_coordinator) -> None:
        ts = datetime.now()
        mock_coordinator._last_warm_connect_attempt = ts
        mock_coordinator.device.connect = AsyncMock(return_value=False)

        await mock_coordinator._warm_connect()

        assert mock_coordinator._last_warm_connect_attempt == ts

    async def test_warm_connect_exception_does_not_raise(self, mock_coordinator) -> None:
        mock_coordinator.device.connect = AsyncMock(side_effect=Exception("BLE error"))

        # Should not raise
        await mock_coordinator._warm_connect()


# ===================================================================
# Polling (_needs_poll)
# ===================================================================


class TestNeedsPoll:
    """Tests for the _needs_poll callback."""

    def test_active_mode_never_polls(self, mock_coordinator) -> None:
        """Active mode should always return False for polling."""
        mock_coordinator.device._active_mode = True
        assert mock_coordinator._needs_poll(MagicMock(), 600) is False

    def test_passive_mode_polls_when_needed(self, mock_coordinator) -> None:
        """Passive mode should poll when interval exceeded."""
        mock_coordinator.device._active_mode = False
        mock_coordinator.device.poll_needed.return_value = True
        mock_hass = MagicMock()
        mock_hass.state = mock_hass.CoreState.running
        mock_coordinator.hass = mock_hass

        with patch(
            "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ):
            result = mock_coordinator._needs_poll(MagicMock(), 600)

        assert result is True

    def test_passive_mode_no_poll_when_not_needed(self, mock_coordinator) -> None:
        mock_coordinator.device._active_mode = False
        mock_coordinator.device.poll_needed.return_value = False
        mock_hass = MagicMock()
        mock_hass.state = mock_hass.CoreState.running
        mock_coordinator.hass = mock_hass

        with patch(
            "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ):
            result = mock_coordinator._needs_poll(MagicMock(), 60)

        assert result is False


# ===================================================================
# _async_update (GATT poll)
# ===================================================================


class TestAsyncUpdate:
    """Tests for the GATT polling method."""

    async def test_poll_updates_position_battery(self, mock_coordinator) -> None:
        """Successful GATT poll should update position and battery."""
        mock_coordinator.device.read_gatt = AsyncMock(return_value=bytes([0x00, 45, 78]))
        mock_coordinator.device.connect = AsyncMock(return_value=True)

        with patch(
            "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ):
            await mock_coordinator._async_update(MagicMock())

        assert mock_coordinator._position == 45
        assert mock_coordinator._battery == 78
        assert mock_coordinator._available is True

    async def test_poll_no_ble_device_marks_unavailable(self, mock_coordinator) -> None:
        with patch(
            "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
            return_value=None,
        ):
            await mock_coordinator._async_update(MagicMock())

        assert mock_coordinator._available is False
        assert mock_coordinator._was_unavailable is True

    async def test_poll_connect_fails_marks_unavailable(self, mock_coordinator) -> None:
        mock_coordinator.device.connect = AsyncMock(return_value=False)

        with patch(
            "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ):
            await mock_coordinator._async_update(MagicMock())

        assert mock_coordinator._available is False

    async def test_poll_gatt_error_marks_unavailable(self, mock_coordinator) -> None:
        mock_coordinator.device.connect = AsyncMock(return_value=True)
        mock_coordinator.device.read_gatt = AsyncMock(side_effect=Exception("GATT read failed"))

        with patch(
            "custom_components.ryse.coordinator.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ):
            await mock_coordinator._async_update(MagicMock())

        assert mock_coordinator._available is False


# ===================================================================
# Unavailable handling
# ===================================================================


class TestUnavailableHandling:
    def test_handle_unavailable_recent_adv_keeps_available(self, mock_coordinator) -> None:
        """If we received an adv recently, stay available."""
        mock_coordinator._last_adv = datetime.now()
        mock_coordinator._available = True

        mock_coordinator._handle_unavailable(MagicMock())

        assert mock_coordinator._available is True

    def test_handle_unavailable_old_adv_marks_unavailable(self, mock_coordinator) -> None:
        """If last adv is old (>15min), mark unavailable."""
        mock_coordinator._last_adv = datetime.now() - timedelta(minutes=20)
        mock_coordinator._available = True

        mock_coordinator._handle_unavailable(MagicMock())

        assert mock_coordinator._available is False
        assert mock_coordinator._was_unavailable is True

    def test_handle_unavailable_no_adv_marks_unavailable(self, mock_coordinator) -> None:
        """If we never received an adv, mark unavailable."""
        mock_coordinator._last_adv = None
        mock_coordinator._available = True

        mock_coordinator._handle_unavailable(MagicMock())

        assert mock_coordinator._available is False


# ===================================================================
# Disconnect handling
# ===================================================================


class TestDisconnectHandling:
    def test_passive_mode_no_reconnect(self, mock_coordinator) -> None:
        """Passive mode should not trigger reconnect on disconnect."""
        mock_coordinator.device._active_mode = False

        mock_coordinator._handle_device_disconnected()

        assert mock_coordinator._reconnect_task is None

    def test_active_mode_triggers_reconnect(self, mock_coordinator) -> None:
        """Active mode should schedule reconnect on disconnect."""
        mock_coordinator.device._active_mode = True

        with patch.object(mock_coordinator.hass, "async_create_task", return_value=MagicMock()) as mock_task:
            mock_coordinator._handle_device_disconnected()

        assert mock_task.called

    def test_active_mode_no_stacking_reconnects(self, mock_coordinator) -> None:
        """Should not stack reconnect tasks."""
        mock_coordinator.device._active_mode = True
        mock_coordinator._reconnect_task = MagicMock()
        mock_coordinator._reconnect_task.done.return_value = False

        with patch.object(mock_coordinator.hass, "async_create_task") as mock_task:
            mock_coordinator._handle_device_disconnected()

        mock_task.assert_not_called()


# ===================================================================
# Command execution (_execute_command)
# ===================================================================


class TestExecuteCommand:
    async def test_execute_command_success(self, mock_coordinator) -> None:
        """Successful command should execute without retry."""
        operation = AsyncMock()

        with patch.object(mock_coordinator, "_ensure_connected", return_value=True):
            await mock_coordinator._execute_command(operation, 50)

        operation.assert_awaited_once_with(50)

    async def test_execute_command_not_connected(self, mock_coordinator) -> None:
        """Command should return without executing if not connected."""
        operation = AsyncMock()

        with patch.object(mock_coordinator, "_ensure_connected", return_value=False):
            await mock_coordinator._execute_command(operation)

        operation.assert_not_awaited()

    async def test_execute_command_retries_on_bleak_error(self, mock_coordinator) -> None:
        """Should retry once on BleakError."""
        from bleak.exc import BleakError

        operation = AsyncMock(side_effect=[BleakError("connection lost"), None])

        with patch.object(mock_coordinator, "_ensure_connected", return_value=True):
            await mock_coordinator._execute_command(operation)

        assert operation.await_count == 2

    async def test_execute_command_auth_failure_triggers_repare(self, mock_coordinator) -> None:
        """Insufficient authentication should trigger BLE re-pair."""
        from bleak.exc import BleakError

        operation = AsyncMock(
            side_effect=[
                BleakError("Insufficient authentication"),
                None,
            ]
        )
        mock_coordinator.device.client.pair = AsyncMock()

        with patch.object(mock_coordinator, "_ensure_connected", return_value=True):
            await mock_coordinator._execute_command(operation)

        mock_coordinator.device.client.pair.assert_awaited_once()
        assert operation.await_count == 2

    async def test_execute_command_auth_repare_fails(self, mock_coordinator) -> None:
        """If re-pair fails, should still retry with disconnect."""
        from bleak.exc import BleakError

        operation = AsyncMock(
            side_effect=[
                BleakError("Insufficient authentication"),
                BleakError("Still broken"),
            ]
        )
        mock_coordinator.device.client.pair = AsyncMock(side_effect=Exception("Pair failed"))

        with patch.object(mock_coordinator, "_ensure_connected", return_value=True):
            await mock_coordinator._execute_command(operation)

        # Both attempts failed → marked unavailable
        assert mock_coordinator._available is False

    async def test_execute_command_both_attempts_fail(self, mock_coordinator) -> None:
        """Two failures should mark device unavailable."""
        from bleak.exc import BleakError

        operation = AsyncMock(side_effect=BleakError("broken"))

        with patch.object(mock_coordinator, "_ensure_connected", return_value=True):
            await mock_coordinator._execute_command(operation)

        assert mock_coordinator._available is False
        assert mock_coordinator._was_unavailable is True
        mock_coordinator.async_update_listeners.assert_called()


# ===================================================================
# Cover command wrappers
# ===================================================================


class TestCoverCommands:
    async def test_async_set_position(self, mock_coordinator) -> None:
        with patch.object(mock_coordinator, "_execute_command", new_callable=AsyncMock) as mock_exec:
            await mock_coordinator.async_set_position(50)
        mock_exec.assert_awaited_once_with(mock_coordinator.device.set_position, 50)

    async def test_async_open_cover(self, mock_coordinator) -> None:
        mock_coordinator._position = 50  # Not already open
        with patch.object(mock_coordinator, "_execute_command", new_callable=AsyncMock) as mock_exec:
            await mock_coordinator.async_open_cover()
        mock_exec.assert_awaited_once()

    async def test_async_open_cover_already_open(self, mock_coordinator) -> None:
        """Open when already at position 0 should be a no-op."""
        mock_coordinator._position = 0
        with patch.object(mock_coordinator, "_execute_command", new_callable=AsyncMock) as mock_exec:
            await mock_coordinator.async_open_cover()
        mock_exec.assert_not_awaited()

    async def test_async_close_cover(self, mock_coordinator) -> None:
        mock_coordinator._position = 50
        with patch.object(mock_coordinator, "_execute_command", new_callable=AsyncMock) as mock_exec:
            await mock_coordinator.async_close_cover()
        mock_exec.assert_awaited_once()

    async def test_async_close_cover_already_closed(self, mock_coordinator) -> None:
        """Close when already at position 100 should be a no-op."""
        mock_coordinator._position = 100
        with patch.object(mock_coordinator, "_execute_command", new_callable=AsyncMock) as mock_exec:
            await mock_coordinator.async_close_cover()
        mock_exec.assert_not_awaited()
