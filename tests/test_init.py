"""Tests for the RYSE integration setup and teardown."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.ryse import _apply_options, async_setup_entry, async_unload_entry
from custom_components.ryse.const import (
    DOMAIN,
    DEFAULT_ACTIVE_MODE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_IDLE_DISCONNECT_TIMEOUT,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_MAX_RETRY_ATTEMPTS,
    DEFAULT_ACTIVE_RECONNECT_DELAY,
)
from custom_components.ryse.ryse import RyseDevice


class TestApplyOptions:
    """Test the _apply_options helper."""

    def test_apply_defaults(self) -> None:
        device = RyseDevice("AA:BB:CC:DD:EE:FF")
        _apply_options(device, {})

        assert device._poll_interval == DEFAULT_POLL_INTERVAL
        assert device._idle_disconnect_timeout == DEFAULT_IDLE_DISCONNECT_TIMEOUT
        assert device._connection_timeout == DEFAULT_CONNECTION_TIMEOUT
        assert device._max_retry_attempts == DEFAULT_MAX_RETRY_ATTEMPTS
        assert device._active_mode == DEFAULT_ACTIVE_MODE
        assert device._active_reconnect_delay == DEFAULT_ACTIVE_RECONNECT_DELAY

    def test_apply_custom_options(self) -> None:
        device = RyseDevice("AA:BB:CC:DD:EE:FF")
        _apply_options(
            device,
            {
                "poll_interval": 600,
                "idle_disconnect_timeout": 120,
                "connection_timeout": 15,
                "max_retry_attempts": 5,
                "active_mode": True,
                "active_reconnect_delay": 10,
            },
        )

        assert device._poll_interval == 600
        assert device._idle_disconnect_timeout == 120
        assert device._connection_timeout == 15
        assert device._max_retry_attempts == 5
        assert device._active_mode is True
        assert device._active_reconnect_delay == 10


@pytest.mark.skipif(sys.platform != "linux", reason="Bluetooth fixtures require Linux dbus")
@pytest.mark.usefixtures("mock_bluetooth")
class TestSetupEntry:
    async def test_setup_creates_coordinator(self, hass: HomeAssistant, mock_entry_factory) -> None:
        """Setup should create a coordinator and store it in hass.data."""
        entry = mock_entry_factory()
        entry.add_to_hass(hass)

        with (
            patch(
                "custom_components.ryse.coordinator.RyseCoordinator",
            ) as mock_coord_cls,
            patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                new_callable=AsyncMock,
            ),
        ):
            mock_coord_cls.return_value = MagicMock()
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert entry.entry_id in hass.data[DOMAIN]


@pytest.mark.skipif(sys.platform != "linux", reason="Bluetooth fixtures require Linux dbus")
@pytest.mark.usefixtures("mock_bluetooth")
class TestUnloadEntry:
    async def test_unload_disconnects_device(self, hass: HomeAssistant, mock_entry_factory) -> None:
        """Unload should disconnect the device and clean up."""
        entry = mock_entry_factory()
        entry.add_to_hass(hass)

        mock_coord = MagicMock()
        mock_coord.device = MagicMock()
        mock_coord.device.disconnect = AsyncMock()

        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][entry.entry_id] = mock_coord

        with patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await async_unload_entry(hass, entry)

        assert result is True
        mock_coord.device.disconnect.assert_awaited_once()
        assert entry.entry_id not in hass.data[DOMAIN]
