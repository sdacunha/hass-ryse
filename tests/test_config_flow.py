"""Tests for the config flow + options flow."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.ryse.config_flow import (
    RyseBLEDeviceConfigFlow,
    RyseOptionsFlow,
    _is_ryse_device,
)


# -----------------------------------------------------------------------------
# Device filter — pure function, no HA needed
# -----------------------------------------------------------------------------


class TestIsRyseDevice:
    def test_accepts_real_ryse(self, ryse_service_info):
        assert _is_ryse_device(ryse_service_info) is True

    def test_rejects_wrong_name(self, not_ryse_wrong_name):
        assert _is_ryse_device(not_ryse_wrong_name) is False

    def test_rejects_wrong_mfr_id(self, not_ryse_wrong_mfr):
        assert _is_ryse_device(not_ryse_wrong_mfr) is False

    def test_rejects_unrelated_device(self, not_ryse_unrelated):
        assert _is_ryse_device(not_ryse_unrelated) is False


# -----------------------------------------------------------------------------
# Bluetooth auto-discovery — exercise flow methods directly to avoid invoking
# HA's full bluetooth subsystem (which can't start on non-Linux).
# -----------------------------------------------------------------------------


class TestBluetoothAutoDiscovery:
    @pytest.mark.asyncio
    async def test_aborts_for_non_ryse(self, hass, not_ryse_wrong_name):
        """A non-RYSE BLE device → not_supported abort."""
        flow = RyseBLEDeviceConfigFlow()
        flow.hass = hass
        # mimic ConfigFlow harness wiring
        flow.context = {}
        with (
            patch.object(flow, "async_set_unique_id", return_value=None),
            patch.object(flow, "_abort_if_unique_id_configured", return_value=None),
        ):
            result = await flow.async_step_bluetooth(not_ryse_wrong_name)
        assert result["type"] == "abort"
        assert result["reason"] == "not_supported"

    @pytest.mark.asyncio
    async def test_shows_confirm_for_pairing_ryse(self, hass, ryse_service_info_pairing):
        """First-time discovery of a pairing-mode RYSE → bluetooth_confirm form."""
        flow = RyseBLEDeviceConfigFlow()
        flow.hass = hass
        flow.context = {}
        with (
            patch.object(flow, "async_set_unique_id", return_value=None),
            patch.object(flow, "_abort_if_unique_id_configured", return_value=None),
        ):
            result = await flow.async_step_bluetooth(ryse_service_info_pairing)
        assert result["type"] == "form"
        assert result["step_id"] == "bluetooth_confirm"


# -----------------------------------------------------------------------------
# Options flow — instantiate directly, validate the schema shape.
# -----------------------------------------------------------------------------


class TestOptionsFlow:
    @pytest.mark.asyncio
    async def test_options_form_has_only_battery_toggle(self, hass, mock_entry_factory):
        """The simplified options flow exposes exactly one field."""
        entry = mock_entry_factory(options={})
        flow = RyseOptionsFlow()
        flow.hass = hass
        flow.handler = entry.entry_id
        # The options flow reads config_entry via property; inject directly.
        flow._config_entry = entry
        with patch.object(
            RyseOptionsFlow,
            "config_entry",
            new=MagicMock(return_value=entry),
        ):
            # config_entry is a property — patch the attribute instead
            flow.__dict__["config_entry"] = entry
            result = await flow.async_step_init()
        assert result["type"] == "form"
        assert result["step_id"] == "init"
        schema_keys = {str(k) for k in result["data_schema"].schema}
        assert schema_keys == {"disable_battery_sensor"}

    @pytest.mark.asyncio
    async def test_options_save_creates_entry(self, hass, mock_entry_factory):
        """Submitting options input creates a result with the new data."""
        entry = mock_entry_factory(options={})
        flow = RyseOptionsFlow()
        flow.hass = hass
        flow.handler = entry.entry_id
        flow.__dict__["config_entry"] = entry
        result = await flow.async_step_init(user_input={"disable_battery_sensor": True})
        assert result["type"] == "create_entry"
        assert result["data"] == {"disable_battery_sensor": True}
