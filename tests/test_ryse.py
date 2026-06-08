"""Tests for the RyseDevice BLE wrapper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError
from bleak_retry_connector import BleakNotFoundError

from custom_components.ryse.ryse import (
    COMMAND_CHAR_UUID,
    POSITION_CHAR_UUID,
    RyseDevice,
)

ADDR = "AA:BB:CC:DD:EE:FF"


def test_init_defaults():
    """Fresh device has no client, empty callback lists."""
    d = RyseDevice(ADDR)
    assert d.address == ADDR
    assert d.client is None
    assert d.ble_device is None
    assert d.is_connected is False
    assert d._position_callbacks == []
    assert d._battery_callbacks == []
    assert d._disconnect_callbacks == []


def test_callback_registration():
    """Add* methods append to their respective lists."""
    d = RyseDevice(ADDR)
    pos_cb, batt_cb, adv_cb, disc_cb = (lambda x: None for _ in range(4))
    d.add_position_callback(pos_cb)
    d.add_battery_callback(batt_cb)
    d.add_adv_callback(adv_cb)
    d.add_disconnect_callback(disc_cb)
    d.add_unavailable_callback(lambda: None)  # no-op shim, just verify no crash
    assert pos_cb in d._position_callbacks
    assert batt_cb in d._battery_callbacks
    assert adv_cb in d._adv_callbacks
    assert disc_cb in d._disconnect_callbacks


def test_is_connected_property():
    """is_connected reflects client.is_connected."""
    d = RyseDevice(ADDR)
    assert d.is_connected is False
    client = MagicMock()
    client.is_connected = True
    d.client = client
    assert d.is_connected is True
    client.is_connected = False
    assert d.is_connected is False


# -----------------------------------------------------------------------------
# Notification parsing
# -----------------------------------------------------------------------------


def test_notification_position_report():
    """0xF5 ... 0x01 0x07 packet fires position callbacks with data[4]."""
    d = RyseDevice(ADDR)
    received = []
    d.add_position_callback(received.append)
    d._handle_notification(None, bytearray([0xF5, 0x00, 0x01, 0x07, 42, 0x00]))
    assert received == [42]


def test_notification_user_target_ignored():
    """0xF5 ... 0x01 0x18 packets are user-target reports — ignored."""
    d = RyseDevice(ADDR)
    received = []
    d.add_position_callback(received.append)
    d._handle_notification(None, bytearray([0xF5, 0x00, 0x01, 0x18, 42, 0x00]))
    assert received == []


def test_notification_wrong_header_ignored():
    """Non-RYSE packets (wrong header byte) ignored."""
    d = RyseDevice(ADDR)
    received = []
    d.add_position_callback(received.append)
    d._handle_notification(None, bytearray([0xAA, 0x00, 0x01, 0x07, 42, 0x00]))
    assert received == []


def test_notification_too_short_ignored():
    """Packets <5 bytes ignored."""
    d = RyseDevice(ADDR)
    received = []
    d.add_position_callback(received.append)
    d._handle_notification(None, bytearray([0xF5, 0x00]))
    assert received == []


def test_notification_callback_exception_does_not_break_others():
    """A failing position callback doesn't prevent later ones from running."""
    d = RyseDevice(ADDR)
    received = []

    def explode(_pos):
        raise RuntimeError("boom")

    d.add_position_callback(explode)
    d.add_position_callback(received.append)
    d._handle_notification(None, bytearray([0xF5, 0x00, 0x01, 0x07, 42, 0x00]))
    assert received == [42]


# -----------------------------------------------------------------------------
# Disconnect callback
# -----------------------------------------------------------------------------


def test_on_disconnected_fires_callbacks():
    """When bleak fires the disconnect callback, we notify all subscribers and clear client."""
    d = RyseDevice(ADDR)
    fired = []
    d.add_disconnect_callback(lambda: fired.append(True))
    client = MagicMock()
    d.client = client
    d._on_disconnected(client)
    assert d.client is None
    assert fired == [True]


def test_on_disconnected_ignores_stale_client():
    """If bleak fires for a client we've already replaced, don't trigger our callbacks."""
    d = RyseDevice(ADDR)
    fired = []
    d.add_disconnect_callback(lambda: fired.append(True))
    current = MagicMock()
    stale = MagicMock()
    d.client = current
    d._on_disconnected(stale)
    assert d.client is current
    assert fired == []


# -----------------------------------------------------------------------------
# Connect / disconnect
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_returns_false_without_ble_device():
    """connect() can't proceed without a BLEDevice reference."""
    d = RyseDevice(ADDR)
    assert d.ble_device is None
    assert (await d.connect()) is False


@pytest.mark.asyncio
async def test_connect_short_circuits_when_already_connected():
    """If already connected, connect() returns True without re-establishing."""
    d = RyseDevice(ADDR)
    client = MagicMock()
    client.is_connected = True
    d.client = client
    with patch("custom_components.ryse.ryse.establish_connection") as m:
        assert (await d.connect()) is True
        m.assert_not_called()


@pytest.mark.asyncio
async def test_connect_happy_path():
    """connect() calls establish_connection, subscribes to notifications, returns True."""
    d = RyseDevice(ADDR)
    d.ble_device = MagicMock(details={"source": "proxy-mac"})
    new_client = MagicMock()
    new_client.is_connected = True
    new_client.start_notify = AsyncMock()
    new_client.mtu_size = 247

    with patch("custom_components.ryse.ryse.establish_connection", new=AsyncMock(return_value=new_client)):
        result = await d.connect()

    assert result is True
    assert d.client is new_client
    new_client.start_notify.assert_awaited_once_with(POSITION_CHAR_UUID, d._handle_notification)


@pytest.mark.asyncio
async def test_connect_returns_false_when_not_found():
    """BleakNotFoundError → return False quietly."""
    d = RyseDevice(ADDR)
    d.ble_device = MagicMock(details={})
    with patch(
        "custom_components.ryse.ryse.establish_connection",
        new=AsyncMock(side_effect=BleakNotFoundError("not found")),
    ):
        assert (await d.connect()) is False


@pytest.mark.asyncio
async def test_connect_returns_false_on_generic_error():
    """Any other connection exception → return False."""
    d = RyseDevice(ADDR)
    d.ble_device = MagicMock(details={})
    with patch(
        "custom_components.ryse.ryse.establish_connection",
        new=AsyncMock(side_effect=BleakError("boom")),
    ):
        assert (await d.connect()) is False


@pytest.mark.asyncio
async def test_connect_notify_failure_is_non_fatal():
    """If start_notify fails, connect() still returns True (we only LOG that)."""
    d = RyseDevice(ADDR)
    d.ble_device = MagicMock(details={})
    new_client = MagicMock()
    new_client.is_connected = True
    new_client.start_notify = AsyncMock(side_effect=BleakError("notify denied"))

    with patch("custom_components.ryse.ryse.establish_connection", new=AsyncMock(return_value=new_client)):
        assert (await d.connect()) is True


@pytest.mark.asyncio
async def test_disconnect_clears_client():
    """disconnect() clears the client reference and calls client.disconnect."""
    d = RyseDevice(ADDR)
    client = MagicMock()
    client.is_connected = True
    client.disconnect = AsyncMock()
    d.client = client
    await d.disconnect()
    assert d.client is None
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_when_already_disconnected_is_safe():
    """disconnect() on an already-disconnected client doesn't crash."""
    d = RyseDevice(ADDR)
    d.client = None
    await d.disconnect()  # noop, no exception
    assert d.client is None


# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_position_builds_correct_packet():
    """set_position(50) writes [0xF5,0x03,0x01,0x01,50, checksum]."""
    d = RyseDevice(ADDR)
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    d.client = client

    await d.set_position(50)

    expected_body = bytes([0xF5, 0x03, 0x01, 0x01, 50])
    expected_checksum = sum(expected_body[2:]) % 256
    expected_packet = expected_body + bytes([expected_checksum])
    client.write_gatt_char.assert_awaited_once_with(COMMAND_CHAR_UUID, expected_packet)


@pytest.mark.asyncio
async def test_set_position_rejects_out_of_range():
    """Position must be 0..100."""
    d = RyseDevice(ADDR)
    with pytest.raises(ValueError):
        await d.set_position(150)
    with pytest.raises(ValueError):
        await d.set_position(-1)


@pytest.mark.asyncio
async def test_open_close_call_set_position():
    """open() = position 0, close() = position 100."""
    d = RyseDevice(ADDR)
    with patch.object(d, "set_position", new=AsyncMock()) as m:
        await d.open()
        m.assert_awaited_with(0)
        await d.close()
        m.assert_awaited_with(100)


@pytest.mark.asyncio
async def test_write_gatt_raises_when_not_connected():
    d = RyseDevice(ADDR)
    with pytest.raises(ConnectionError):
        await d.write_gatt(COMMAND_CHAR_UUID, b"\x00")


@pytest.mark.asyncio
async def test_read_gatt_raises_when_not_connected():
    d = RyseDevice(ADDR)
    with pytest.raises(ConnectionError):
        await d.read_gatt(POSITION_CHAR_UUID)


# -----------------------------------------------------------------------------
# Advertisement parsing
# -----------------------------------------------------------------------------


def test_parse_advertisement_extracts_ryse_data():
    """RYSE manufacturer ID (0x0409) carries [flags, position, battery, ...]."""
    info = MagicMock()
    info.manufacturer_data = {0x0409: bytes([0x00, 42, 85])}
    result = RyseDevice.parse_advertisement(info)
    assert result == {"position": 42, "battery": 85}


def test_parse_advertisement_ignores_other_mfr_ids():
    """Non-RYSE manufacturer data → empty result."""
    info = MagicMock()
    info.manufacturer_data = {0x004C: bytes([0x00, 42, 85])}  # Apple's mfr ID
    result = RyseDevice.parse_advertisement(info)
    assert result == {}


def test_parse_advertisement_short_data():
    """RYSE adv with <3 bytes returns empty (no panic)."""
    info = MagicMock()
    info.manufacturer_data = {0x0409: bytes([0x00])}
    result = RyseDevice.parse_advertisement(info)
    assert result == {}


def test_parse_advertisement_missing_manufacturer_data():
    """If service_info has no manufacturer_data attribute → empty result."""
    info = MagicMock(spec=[])  # no manufacturer_data
    result = RyseDevice.parse_advertisement(info)
    assert result == {}
