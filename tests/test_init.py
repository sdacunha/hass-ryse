"""Tests for the integration's setup / unload + debug services."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ryse import async_setup, async_setup_entry, async_unload_entry
from custom_components.ryse.const import DOMAIN
from tests import RYSE_ADDRESS


@pytest.mark.asyncio
async def test_async_setup_registers_debug_services(hass):
    """async_setup wires up the test_ble_connection / bond_all_proxies / inspect_device services."""
    assert await async_setup(hass, {}) is True
    for service in ("test_ble_connection", "bond_all_proxies", "inspect_device"):
        assert hass.services.has_service(DOMAIN, service), f"missing service: {service}"


@pytest.mark.asyncio
async def test_async_setup_entry_creates_coordinator(hass, mock_entry_factory):
    """A valid config entry produces a coordinator stored in hass.data."""
    entry = mock_entry_factory(address=RYSE_ADDRESS)
    entry.add_to_hass(hass)

    coord_instance = MagicMock()
    coord_instance.async_start = MagicMock(return_value=lambda: None)

    with (
        patch(
            "custom_components.ryse.bluetooth.async_scanner_count",
            return_value=1,
        ),
        patch("bleak_retry_connector.close_stale_connections_by_address", new=AsyncMock()),
        patch("custom_components.ryse.coordinator.RyseCoordinator", return_value=coord_instance),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ),
    ):
        assert await async_setup_entry(hass, entry) is True
    assert hass.data[DOMAIN][entry.entry_id] is coord_instance


@pytest.mark.asyncio
async def test_async_setup_entry_raises_when_no_bt_scanner(hass, mock_entry_factory):
    """No connectable scanners → ConfigEntryNotReady."""
    from homeassistant.exceptions import ConfigEntryNotReady

    entry = mock_entry_factory(address=RYSE_ADDRESS)
    entry.add_to_hass(hass)

    with patch("custom_components.ryse.bluetooth.async_scanner_count", return_value=0):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)


@pytest.mark.asyncio
async def test_async_unload_entry_disconnects_and_clears_data(hass, mock_entry_factory):
    """Unloading disconnects the device and removes coordinator from hass.data."""
    entry = mock_entry_factory(address=RYSE_ADDRESS)
    entry.add_to_hass(hass)
    coord = MagicMock()
    coord.device.disconnect = AsyncMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(return_value=True),
        ),
        patch("custom_components.ryse.bluetooth.async_rediscover_address"),
    ):
        assert await async_unload_entry(hass, entry) is True
    coord.device.disconnect.assert_awaited()
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
