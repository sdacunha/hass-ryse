"""Shared pytest fixtures for RYSE integration tests."""

from __future__ import annotations

import json
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ryse.const import DOMAIN


# The enable_bluetooth fixture requires Linux dbus access (bluetooth_adapters).
# On macOS/Windows, we skip tests that need the full HA bluetooth stack.
_HAS_BLUETOOTH_FIXTURE = sys.platform == "linux"


@pytest.fixture(autouse=True)
def register_ryse_integration(hass):
    """Register the ryse custom component so HA's loader can find it."""
    from homeassistant import loader

    manifest_path = pathlib.Path(__file__).parent.parent / "custom_components" / "ryse" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    integration = loader.Integration(
        hass,
        f"{loader.PACKAGE_CUSTOM_COMPONENTS}.{DOMAIN}",
        manifest_path.parent,
        manifest,
    )

    hass.data.setdefault(loader.DATA_CUSTOM_COMPONENTS, {})[DOMAIN] = integration


@pytest.fixture
def mock_bluetooth(enable_bluetooth):
    """Mock the Bluetooth stack. Only works on Linux."""


@pytest.fixture
def mock_entry_factory():
    """Return a factory that creates MockConfigEntry for RYSE devices."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    def _create(
        address: str = "AA:BB:CC:DD:EE:FF",
        name: str = "Bedroom Blinds",
        options: dict | None = None,
    ) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            data={
                "address": address,
                "name": name,
                "rx_uuid": "a72f2801-b0bd-498b-b4cd-4a3901388238",
                "tx_uuid": "a72f2802-b0bd-498b-b4cd-4a3901388238",
            },
            unique_id=address,
            options=options or {},
        )

    return _create


@pytest.fixture
def mock_ryse_device():
    """Return a MagicMock for RyseDevice with sensible defaults."""
    device = MagicMock()
    device.address = "AA:BB:CC:DD:EE:FF"
    device.client = MagicMock()
    device.client.is_connected = True
    device.client.pair = AsyncMock()
    device._is_connected = True
    device._connecting = False
    device._active_mode = False
    device._max_retry_attempts = 3
    device._active_reconnect_delay = 5
    device._poll_interval = 300
    device._idle_disconnect_timeout = 60
    device._connection_timeout = 10
    device._battery_callbacks = []
    device._unavailable_callbacks = []
    device._adv_callbacks = []
    device._disconnect_callbacks = []
    device._position_callbacks = []
    device.connect = AsyncMock(return_value=True)
    device.disconnect = AsyncMock()
    device.set_position = AsyncMock()
    device.open = AsyncMock()
    device.close = AsyncMock()
    device.read_gatt = AsyncMock(return_value=bytes([0x00, 50, 85]))
    device.write_gatt = AsyncMock()
    device.set_ble_device = MagicMock()
    device.add_disconnect_callback = MagicMock(side_effect=lambda cb: device._disconnect_callbacks.append(cb))
    device.add_battery_callback = MagicMock(side_effect=lambda cb: device._battery_callbacks.append(cb))
    device.add_unavailable_callback = MagicMock(side_effect=lambda cb: device._unavailable_callbacks.append(cb))
    device.add_adv_callback = MagicMock(side_effect=lambda cb: device._adv_callbacks.append(cb))
    device.add_position_callback = MagicMock(side_effect=lambda cb: device._position_callbacks.append(cb))
    device.parse_advertisement = MagicMock(return_value={"position": 50, "battery": 85})
    device.poll_needed = MagicMock(return_value=False)
    return device


# Lazy service info fixtures — avoids import-time HA dependency issues
@pytest.fixture
def ryse_service_info():
    from . import get_service_info

    return get_service_info("RYSE_SERVICE_INFO")


@pytest.fixture
def ryse_service_info_pairing():
    from . import get_service_info

    return get_service_info("RYSE_SERVICE_INFO_PAIRING")


@pytest.fixture
def ryse_service_info_2_pairing():
    from . import get_service_info

    return get_service_info("RYSE_SERVICE_INFO_2_PAIRING")


@pytest.fixture
def not_ryse_wrong_name():
    from . import get_service_info

    return get_service_info("NOT_RYSE_WRONG_NAME")


@pytest.fixture
def not_ryse_wrong_mfr():
    from . import get_service_info

    return get_service_info("NOT_RYSE_WRONG_MFR")


@pytest.fixture
def not_ryse_unrelated():
    from . import get_service_info

    return get_service_info("NOT_RYSE_UNRELATED")


@pytest.fixture
def phantom_ryse_device():
    from . import get_service_info

    return get_service_info("PHANTOM_RYSE_DEVICE")
