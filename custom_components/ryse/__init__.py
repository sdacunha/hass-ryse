"""The RYSE BLE Device integration."""

from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .ryse import RyseDevice
from .const import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_IDLE_DISCONNECT_TIMEOUT,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_MAX_RETRY_ATTEMPTS,
    DEFAULT_ACTIVE_MODE,
    DEFAULT_ACTIVE_RECONNECT_DELAY,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "ryse"


def _apply_options(device: RyseDevice, options: dict) -> None:
    """Apply config entry options to the device."""
    device._poll_interval = options.get("poll_interval", DEFAULT_POLL_INTERVAL)
    device._idle_disconnect_timeout = options.get("idle_disconnect_timeout", DEFAULT_IDLE_DISCONNECT_TIMEOUT)
    device._connection_timeout = options.get("connection_timeout", DEFAULT_CONNECTION_TIMEOUT)
    device._max_retry_attempts = options.get("max_retry_attempts", DEFAULT_MAX_RETRY_ATTEMPTS)
    device._active_mode = options.get("active_mode", DEFAULT_ACTIVE_MODE)
    device._active_reconnect_delay = options.get("active_reconnect_delay", DEFAULT_ACTIVE_RECONNECT_DELAY)


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the RYSE component."""
    _LOGGER.info("Setting up RYSE Device integration")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RYSE from a config entry."""
    _LOGGER.info("Setting up RYSE entry: %s", entry.data)

    # Create device instance
    device = RyseDevice(entry.data["address"])
    _apply_options(device, entry.options)
    _LOGGER.info("[init] Created RyseDevice (id: %s) for address: %s", id(device), entry.data["address"])

    # Lazy import to avoid pulling in homeassistant.components.bluetooth at
    # package load time (breaks tests on non-Linux / mismatched deps).
    from .coordinator import RyseCoordinator

    # Create coordinator instance
    coordinator = RyseCoordinator(
        hass, entry.data["address"], device, entry.data.get("name", "SmartShade"), entry_id=entry.entry_id
    )

    # Store coordinator in hass data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["cover", "sensor"])

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    coordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        was_active = coordinator.device._active_mode
        _apply_options(coordinator.device, entry.options)
        _LOGGER.info("Updated RYSE options for %s: %s", entry.data.get("name"), entry.options)
        # If active mode was just enabled, establish connection
        if coordinator.device._active_mode and not was_active:
            hass.async_create_task(coordinator._active_reconnect())
        # If active mode was just disabled, let idle timer handle disconnect
        elif was_active and not coordinator.device._active_mode:
            coordinator.device._schedule_idle_disconnect()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading RYSE entry: %s", entry.data)

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["cover", "sensor"])

    if unload_ok:
        # Clean up coordinator and device
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        # Disconnect the device if connected
        await coordinator.device.disconnect()
        _LOGGER.debug("Disconnected device during unload: %s", entry.data["address"])

    return unload_ok
