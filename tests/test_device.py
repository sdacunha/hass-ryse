"""Tests for the RyseDevice class."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ryse.ryse import RyseDevice


@pytest.fixture
def device():
    """Create a RyseDevice instance."""
    return RyseDevice("AA:BB:CC:DD:EE:FF")


# ===================================================================
# Initialization
# ===================================================================


class TestDeviceInit:
    def test_defaults(self, device) -> None:
        assert device.address == "AA:BB:CC:DD:EE:FF"
        assert device.ble_device is None
        assert device.client is None
        assert device._is_connected is False
        assert device._connecting is False
        assert device._active_mode is False
        assert device._battery_callbacks == []
        assert device._disconnect_callbacks == []

    def test_configurable_values(self, device) -> None:
        assert device._connection_timeout == 10
        assert device._max_retry_attempts == 3
        assert device._poll_interval == 300
        assert device._idle_disconnect_timeout == 60
        assert device._active_reconnect_delay == 5


# ===================================================================
# Callbacks
# ===================================================================


class TestCallbacks:
    def test_add_battery_callback(self, device) -> None:
        cb = MagicMock()
        device.add_battery_callback(cb)
        assert cb in device._battery_callbacks

    def test_add_unavailable_callback(self, device) -> None:
        cb = MagicMock()
        device.add_unavailable_callback(cb)
        assert cb in device._unavailable_callbacks

    def test_add_disconnect_callback(self, device) -> None:
        cb = MagicMock()
        device.add_disconnect_callback(cb)
        assert cb in device._disconnect_callbacks

    def test_add_adv_callback(self, device) -> None:
        cb = MagicMock()
        device.add_adv_callback(cb)
        assert cb in device._adv_callbacks


# ===================================================================
# Connection
# ===================================================================


class TestConnection:
    async def test_connect_success(self, device) -> None:
        device.ble_device = MagicMock()
        mock_client = MagicMock()
        mock_client.is_connected = True

        with patch(
            "custom_components.ryse.ryse.establish_connection",
            new_callable=AsyncMock,
            return_value=mock_client,
        ):
            result = await device.connect()

        assert result is True
        assert device._is_connected is True
        assert device._connecting is False
        assert device.client is mock_client

    async def test_connect_already_connected(self, device) -> None:
        mock_client = MagicMock()
        mock_client.is_connected = True
        device.client = mock_client

        result = await device.connect()

        assert result is True

    async def test_connect_no_ble_device_raises(self, device) -> None:
        device.ble_device = None

        with pytest.raises(ConnectionError, match="No BLEDevice"):
            await device.connect()

    async def test_connect_timeout(self, device) -> None:
        device.ble_device = MagicMock()

        with patch(
            "custom_components.ryse.ryse.establish_connection",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError,
        ):
            result = await device.connect()

        assert result is False
        assert device._is_connected is False

    async def test_connect_failure_calls_unavailable_callbacks(self, device) -> None:
        device.ble_device = MagicMock()
        cb = MagicMock()
        device.add_unavailable_callback(cb)

        with patch(
            "custom_components.ryse.ryse.establish_connection",
            new_callable=AsyncMock,
            side_effect=Exception("BLE error"),
        ):
            result = await device.connect()

        assert result is False
        cb.assert_called_once()

    async def test_connect_concurrent_returns_false(self, device) -> None:
        """Second concurrent connect attempt should return False."""
        device.ble_device = MagicMock()
        device._connecting = True

        result = await device.connect()

        assert result is False


# ===================================================================
# Disconnection
# ===================================================================


class TestDisconnection:
    async def test_disconnect(self, device) -> None:
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.disconnect = AsyncMock()
        device.client = mock_client
        device._is_connected = True

        await device.disconnect()

        assert device.client is None
        assert device._is_connected is False
        mock_client.disconnect.assert_awaited_once()

    async def test_disconnect_already_disconnected(self, device) -> None:
        """Disconnecting when not connected should be a no-op."""
        device.client = None
        await device.disconnect()
        assert device._is_connected is False

    def test_on_disconnected_unexpected(self, device) -> None:
        """Unexpected disconnect should clean up state and call callbacks."""
        mock_client = MagicMock()
        device.client = mock_client
        device._is_connected = True
        cb = MagicMock()
        device.add_disconnect_callback(cb)

        device._on_disconnected(mock_client)

        assert device.client is None
        assert device._is_connected is False
        cb.assert_called_once()

    def test_on_disconnected_stale_client_ignored(self, device) -> None:
        """Disconnect callback for an old client should be ignored."""
        old_client = MagicMock()
        new_client = MagicMock()
        device.client = new_client
        device._is_connected = True

        device._on_disconnected(old_client)

        # Should not affect current state
        assert device.client is new_client
        assert device._is_connected is True


# ===================================================================
# GATT operations
# ===================================================================


class TestGATT:
    async def test_read_gatt(self, device) -> None:
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.read_gatt_char = AsyncMock(return_value=b"\x00\x32\x55")
        device.client = mock_client

        result = await device.read_gatt("some-uuid")

        assert result == b"\x00\x32\x55"
        mock_client.read_gatt_char.assert_awaited_once_with("some-uuid")

    async def test_read_gatt_not_connected_raises(self, device) -> None:
        device.client = None

        with pytest.raises(ConnectionError, match="Not connected"):
            await device.read_gatt("some-uuid")

    async def test_write_gatt(self, device) -> None:
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.write_gatt_char = AsyncMock()
        device.client = mock_client

        await device.write_gatt("some-uuid", b"\x01\x02")

        mock_client.write_gatt_char.assert_awaited_once_with("some-uuid", b"\x01\x02")

    async def test_write_gatt_not_connected_raises(self, device) -> None:
        device.client = None

        with pytest.raises(ConnectionError, match="Not connected"):
            await device.write_gatt("some-uuid", b"\x01")


# ===================================================================
# Commands
# ===================================================================


class TestCommands:
    async def test_set_position_builds_correct_packet(self, device) -> None:
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.write_gatt_char = AsyncMock()
        device.client = mock_client

        await device.set_position(50)

        call_args = mock_client.write_gatt_char.call_args
        data = call_args[0][1]
        assert data[0] == 0xF5
        assert data[1] == 0x03
        assert data[4] == 50  # position
        # Verify checksum
        expected_checksum = sum(data[2:5]) % 256
        assert data[5] == expected_checksum

    async def test_set_position_invalid_raises(self, device) -> None:
        with pytest.raises(ValueError, match="position must be between"):
            await device.set_position(101)

        with pytest.raises(ValueError, match="position must be between"):
            await device.set_position(-1)

    async def test_open_sets_position_0(self, device) -> None:
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.write_gatt_char = AsyncMock()
        device.client = mock_client

        await device.open()

        call_args = mock_client.write_gatt_char.call_args
        data = call_args[0][1]
        assert data[4] == 0  # position 0 = open

    async def test_close_sets_position_100(self, device) -> None:
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.write_gatt_char = AsyncMock()
        device.client = mock_client

        await device.close()

        call_args = mock_client.write_gatt_char.call_args
        data = call_args[0][1]
        assert data[4] == 100  # position 100 = closed


# ===================================================================
# Advertisement parsing
# ===================================================================


class TestParseAdvertisement:
    def test_parse_ryse_advertisement(self) -> None:
        """Standard RYSE advertisement with position and battery."""
        service_info = MagicMock()
        service_info.manufacturer_data = {0x0409: bytes([0x00, 50, 85])}

        result = RyseDevice.parse_advertisement(service_info)

        assert result["position"] == 50
        assert result["battery"] == 85

    def test_parse_ignores_non_ryse_manufacturer(self) -> None:
        """Non-RYSE manufacturer IDs should be ignored."""
        service_info = MagicMock()
        service_info.manufacturer_data = {0x2082: bytes([0x00, 99, 99])}

        result = RyseDevice.parse_advertisement(service_info)

        assert "position" not in result
        assert "battery" not in result

    def test_parse_short_data_ignored(self) -> None:
        """Short manufacturer data (< 3 bytes) should be ignored."""
        service_info = MagicMock()
        service_info.manufacturer_data = {0x0409: bytes([0x00])}

        result = RyseDevice.parse_advertisement(service_info)

        assert "position" not in result

    def test_parse_empty_manufacturer_data(self) -> None:
        service_info = MagicMock()
        service_info.manufacturer_data = {}

        result = RyseDevice.parse_advertisement(service_info)

        assert result == {}

    def test_parse_no_manufacturer_data_attr(self) -> None:
        service_info = MagicMock(spec=[])

        result = RyseDevice.parse_advertisement(service_info)

        assert result == {}

    def test_parse_multiple_manufacturer_ids(self) -> None:
        """Only RYSE manufacturer ID should be used, others ignored."""
        service_info = MagicMock()
        service_info.manufacturer_data = {
            0x2082: bytes([0xFF, 99, 99]),  # Should be ignored
            0x0409: bytes([0x00, 30, 72]),  # RYSE data
        }

        result = RyseDevice.parse_advertisement(service_info)

        assert result["position"] == 30
        assert result["battery"] == 72


# ===================================================================
# Idle disconnect
# ===================================================================


class TestIdleDisconnect:
    def test_schedule_idle_disconnect_in_active_mode_is_noop(self, device) -> None:
        """Active mode should not schedule idle disconnect."""
        device._active_mode = True
        device._schedule_idle_disconnect()
        assert device._idle_timer is None

    def test_cancel_idle_timer(self, device) -> None:
        timer = MagicMock()
        device._idle_timer = timer

        device._cancel_idle_timer()

        timer.cancel.assert_called_once()
        assert device._idle_timer is None

    def test_cancel_idle_timer_when_none(self, device) -> None:
        """Cancel when no timer should be a no-op."""
        device._idle_timer = None
        device._cancel_idle_timer()  # Should not raise


# ===================================================================
# Poll needed
# ===================================================================


class TestPollNeeded:
    def test_poll_needed_when_none(self, device) -> None:
        assert device.poll_needed(None) is True

    def test_poll_needed_when_exceeded(self, device) -> None:
        device._poll_interval = 300
        assert device.poll_needed(301) is True

    def test_poll_not_needed_when_recent(self, device) -> None:
        device._poll_interval = 300
        assert device.poll_needed(100) is False
