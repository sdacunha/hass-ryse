"""Tests for the RYSE config flow.

These tests require the full HA bluetooth stack and only run on Linux (CI).
On macOS, they are skipped because bluetooth_adapters requires dbus.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ryse.const import DOMAIN

from . import RYSE_ADDRESS

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="Bluetooth fixtures require Linux dbus"),
    pytest.mark.usefixtures("mock_bluetooth"),
]


# ---------------------------------------------------------------------------
# Helper: patch async_setup_entry to skip full setup during config flow tests
# ---------------------------------------------------------------------------
def _patch_setup_entry():
    return patch(
        "custom_components.ryse.async_setup_entry",
        return_value=True,
    )


def _mock_ble_client(connected: bool = True, notify_error: bool = False):
    """Return (ble_device_patch, establish_patch) context managers."""
    mock_client = AsyncMock()
    mock_client.is_connected = connected
    mock_client.start_notify = AsyncMock(side_effect=Exception("GATT error") if notify_error else None)
    mock_client.stop_notify = AsyncMock()
    mock_client.disconnect = AsyncMock()

    return (
        patch(
            "custom_components.ryse.config_flow.async_ble_device_from_address",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.ryse.config_flow.establish_connection",
            return_value=mock_client,
        ),
    )


# ===================================================================
# _is_ryse_device helper tests
# ===================================================================


class TestIsRyseDevice:
    def test_valid_ryse_device(self, ryse_service_info):
        from custom_components.ryse.config_flow import _is_ryse_device

        assert _is_ryse_device(ryse_service_info) is True

    def test_wrong_name_rejected(self, not_ryse_wrong_name):
        from custom_components.ryse.config_flow import _is_ryse_device

        assert _is_ryse_device(not_ryse_wrong_name) is False

    def test_wrong_manufacturer_rejected(self, not_ryse_wrong_mfr):
        from custom_components.ryse.config_flow import _is_ryse_device

        assert _is_ryse_device(not_ryse_wrong_mfr) is False

    def test_unrelated_device_rejected(self, not_ryse_unrelated):
        from custom_components.ryse.config_flow import _is_ryse_device

        assert _is_ryse_device(not_ryse_unrelated) is False

    def test_no_name_attribute(self):
        from custom_components.ryse.config_flow import _is_ryse_device

        info = MagicMock(spec=[])
        assert _is_ryse_device(info) is False

    def test_empty_name(self):
        from custom_components.ryse.config_flow import _is_ryse_device

        info = MagicMock()
        info.name = ""
        info.manufacturer_data = {0x0409: b"\x00\x01\x02"}
        assert _is_ryse_device(info) is False


# ===================================================================
# Bluetooth auto-discovery flow
# ===================================================================


class TestBluetoothDiscoveryFlow:
    async def test_discovery_first_device_not_in_pairing(self, hass: HomeAssistant, ryse_service_info) -> None:
        """First device (no existing entries) surfaced even without pairing mode."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=ryse_service_info,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "bluetooth_confirm"

    async def test_discovery_first_device_in_pairing(self, hass: HomeAssistant, ryse_service_info_pairing) -> None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=ryse_service_info_pairing,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "bluetooth_confirm"

    async def test_discovery_not_ryse_aborts(self, hass: HomeAssistant, not_ryse_wrong_name) -> None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=not_ryse_wrong_name,
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "not_supported"

    async def test_discovery_not_ryse_wrong_mfr_aborts(self, hass: HomeAssistant, not_ryse_wrong_mfr) -> None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=not_ryse_wrong_mfr,
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "not_supported"

    async def test_discovery_already_configured(
        self, hass: HomeAssistant, mock_entry_factory, ryse_service_info_pairing
    ) -> None:
        entry = mock_entry_factory(address=RYSE_ADDRESS)
        entry.add_to_hass(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=ryse_service_info_pairing,
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"

    async def test_phantom_device_rejected_when_existing_entries(
        self, hass: HomeAssistant, mock_entry_factory, phantom_ryse_device
    ) -> None:
        entry = mock_entry_factory(address=RYSE_ADDRESS)
        entry.add_to_hass(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=phantom_ryse_device,
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "not_supported"

    async def test_new_device_in_pairing_mode_allowed_when_existing(
        self, hass: HomeAssistant, mock_entry_factory, ryse_service_info_2_pairing
    ) -> None:
        entry = mock_entry_factory(address=RYSE_ADDRESS)
        entry.add_to_hass(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=ryse_service_info_2_pairing,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "bluetooth_confirm"

    async def test_bluetooth_confirm_proceeds_to_pair(self, hass: HomeAssistant, ryse_service_info_pairing) -> None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=ryse_service_info_pairing,
        )
        ble_patch, conn_patch = _mock_ble_client()
        with ble_patch, conn_patch:
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "name"


# ===================================================================
# Pairing step
# ===================================================================


class TestPairStep:
    async def test_pair_device_not_found(self, hass: HomeAssistant, ryse_service_info_pairing) -> None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=ryse_service_info_pairing,
        )
        with patch(
            "custom_components.ryse.config_flow.async_ble_device_from_address",
            return_value=None,
        ):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "device_not_found"

    async def test_pair_connection_failed(self, hass: HomeAssistant, ryse_service_info_pairing) -> None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=ryse_service_info_pairing,
        )
        ble_patch, conn_patch = _mock_ble_client(connected=False)
        with ble_patch, conn_patch:
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "cannot_connect"

    async def test_pair_exception_aborts(self, hass: HomeAssistant, ryse_service_info_pairing) -> None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=ryse_service_info_pairing,
        )
        with (
            patch(
                "custom_components.ryse.config_flow.async_ble_device_from_address",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.ryse.config_flow.establish_connection",
                side_effect=Exception("BLE error"),
            ),
        ):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "pairing_failed"

    async def test_pair_notification_failure_still_proceeds(
        self, hass: HomeAssistant, ryse_service_info_pairing
    ) -> None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=ryse_service_info_pairing,
        )
        ble_patch, conn_patch = _mock_ble_client(notify_error=True)
        with ble_patch, conn_patch:
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "name"


# ===================================================================
# Name and Settings steps
# ===================================================================


class TestNameAndSettings:
    async def _get_to_name_step(self, hass, ryse_service_info_pairing):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=ryse_service_info_pairing,
        )
        ble_patch, conn_patch = _mock_ble_client()
        with ble_patch, conn_patch:
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "name"
        return result

    async def test_name_empty_shows_error(self, hass: HomeAssistant, ryse_service_info_pairing) -> None:
        result = await self._get_to_name_step(hass, ryse_service_info_pairing)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "   "})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "name"
        assert "name" in result["errors"]

    async def test_name_valid_proceeds_to_settings(self, hass: HomeAssistant, ryse_service_info_pairing) -> None:
        result = await self._get_to_name_step(hass, ryse_service_info_pairing)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Bedroom Blinds"})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "settings"

    async def test_full_flow_creates_entry(self, hass: HomeAssistant, ryse_service_info_pairing) -> None:
        result = await self._get_to_name_step(hass, ryse_service_info_pairing)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Bedroom Blinds"})
        with _patch_setup_entry():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    "active_mode": True,
                    "poll_interval": 300,
                    "idle_disconnect_timeout": 60,
                    "connection_timeout": 10,
                    "max_retry_attempts": 3,
                    "active_reconnect_delay": 5,
                    "disable_battery_sensor": False,
                },
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Bedroom Blinds"
        assert result["data"]["address"] == RYSE_ADDRESS
        assert result["options"]["active_mode"] is True


# ===================================================================
# User-initiated flow (manual scan)
# ===================================================================


class TestUserFlow:
    async def test_user_step_shows_form(self, hass: HomeAssistant) -> None:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

    async def test_user_step_proceeds_to_scan(self, hass: HomeAssistant) -> None:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        with patch(
            "custom_components.ryse.config_flow.async_discovered_service_info",
            return_value=[],
        ):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "scan"

    async def test_scan_shows_ryse_devices(
        self, hass: HomeAssistant, ryse_service_info_pairing, not_ryse_wrong_name
    ) -> None:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        with patch(
            "custom_components.ryse.config_flow.async_discovered_service_info",
            return_value=[ryse_service_info_pairing, not_ryse_wrong_name],
        ):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "scan"
        assert result["data_schema"] is not None

    async def test_scan_not_in_pairing_mode_shows_error(self, hass: HomeAssistant, ryse_service_info) -> None:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        with patch(
            "custom_components.ryse.config_flow.async_discovered_service_info",
            return_value=[ryse_service_info],
        ):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        with patch(
            "custom_components.ryse.config_flow.async_discovered_service_info",
            return_value=[ryse_service_info],
        ):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"device_address": RYSE_ADDRESS})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "scan"
        assert result["errors"]["base"] == "not_in_pairing_mode"


# ===================================================================
# Options flow
# ===================================================================


class TestOptionsFlow:
    async def test_options_flow_init(self, hass: HomeAssistant, mock_entry_factory) -> None:
        entry = mock_entry_factory(
            options={
                "active_mode": True,
                "poll_interval": 300,
                "idle_disconnect_timeout": 60,
                "connection_timeout": 10,
                "max_retry_attempts": 3,
                "active_reconnect_delay": 5,
                "disable_battery_sensor": False,
            }
        )
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

    async def test_options_flow_save(self, hass: HomeAssistant, mock_entry_factory) -> None:
        entry = mock_entry_factory(options={"active_mode": False})
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "active_mode": True,
                "poll_interval": 600,
                "idle_disconnect_timeout": 120,
                "connection_timeout": 15,
                "max_retry_attempts": 5,
                "active_reconnect_delay": 10,
                "disable_battery_sensor": True,
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"]["active_mode"] is True
        assert result["data"]["poll_interval"] == 600
