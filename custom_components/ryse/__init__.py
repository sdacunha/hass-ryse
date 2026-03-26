"""The RYSE BLE Device integration."""

from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    async_scanner_devices_by_address,
)
import voluptuous as vol

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from .ryse import RyseDevice
from .const import (
    DOMAIN,
    HARDCODED_UUIDS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_IDLE_DISCONNECT_TIMEOUT,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_MAX_RETRY_ATTEMPTS,
    DEFAULT_ACTIVE_MODE,
    DEFAULT_ACTIVE_RECONNECT_DELAY,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_ADDRESS_SCHEMA = vol.Schema(
    {
        vol.Required("address"): str,
    }
)


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

    async def _handle_test_ble_connection(call: ServiceCall) -> None:
        """Test if a shade accepts BLE connections without pairing."""
        address = call.data["address"]
        _LOGGER.info("[Service] Testing unencrypted BLE connection to %s", address)

        ble_device = async_ble_device_from_address(hass, address, connectable=True)
        if not ble_device:
            _LOGGER.error("[Service] No BLE device found for %s", address)
            return

        try:
            client = await establish_connection(
                BleakClient,
                ble_device,
                address,
                max_attempts=2,
                timeout=15.0,
            )
            _LOGGER.info("[Service] Connected to %s (no pair() called)", address)

            # Try reading GATT without pairing
            try:
                data = await client.read_gatt_char(HARDCODED_UUIDS["rx_uuid"])
                _LOGGER.info(
                    "[Service] GATT read SUCCESS without pairing: %s (hex=%s)",
                    list(data),
                    data.hex(),
                )
            except Exception as read_err:
                _LOGGER.warning("[Service] GATT read FAILED without pairing: %s", read_err)

            # Try writing GATT without pairing (read position command)
            try:
                cmd = bytes([0xF5, 0x03, 0x01, 0x06, 0x00])
                checksum = sum(cmd[2:]) % 256
                packet = cmd + bytes([checksum])
                await client.write_gatt_char(HARDCODED_UUIDS["tx_uuid"], packet)
                _LOGGER.info("[Service] GATT write SUCCESS without pairing")
            except Exception as write_err:
                _LOGGER.warning("[Service] GATT write FAILED without pairing: %s", write_err)

            await client.disconnect()
        except Exception as e:
            _LOGGER.error("[Service] Connection failed for %s: %s", address, e)

    async def _handle_bond_all_proxies(call: ServiceCall) -> None:
        """Bond a shade with every proxy that can reach it."""
        address = call.data["address"]
        _LOGGER.info("[Service] Bonding %s with all reachable proxies", address)

        scanner_devices = async_scanner_devices_by_address(hass, address, connectable=True)
        _LOGGER.info(
            "[Service] Found %d proxy/adapter(s) for %s",
            len(scanner_devices),
            address,
        )
        for scanner_device in scanner_devices:
            source = getattr(scanner_device.scanner, "source", "unknown")
            try:
                client = await establish_connection(
                    BleakClient,
                    scanner_device.ble_device,
                    address,
                    max_attempts=1,
                    timeout=10.0,
                )
                if client.is_connected:
                    try:
                        await client.pair()
                        _LOGGER.info("[Service] Bonded %s via proxy %s", address, source)
                    except Exception as pair_err:
                        _LOGGER.warning(
                            "[Service] pair() failed on proxy %s for %s: %s",
                            source,
                            address,
                            pair_err,
                        )
                    await client.disconnect()
            except Exception as e:
                _LOGGER.warning(
                    "[Service] Could not connect to %s via proxy %s: %s",
                    address,
                    source,
                    e,
                )

    hass.services.async_register(
        DOMAIN,
        "test_ble_connection",
        _handle_test_ble_connection,
        schema=SERVICE_ADDRESS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        "bond_all_proxies",
        _handle_bond_all_proxies,
        schema=SERVICE_ADDRESS_SCHEMA,
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RYSE from a config entry."""
    _LOGGER.info("Setting up RYSE entry: %s", entry.data)

    address = entry.data["address"]

    # Verify at least one connectable Bluetooth scanner is available
    if not bluetooth.async_scanner_count(hass, connectable=True):
        raise ConfigEntryNotReady("No connectable Bluetooth adapters or proxies available")

    # Clean up any stale connections left from a previous session
    try:
        from bleak_retry_connector import close_stale_connections_by_address

        await close_stale_connections_by_address(address)
    except ImportError:
        pass  # Older bleak-retry-connector without this API

    # Create device instance
    device = RyseDevice(address)
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

    # Start the coordinator AFTER platforms have subscribed.
    # async_start() returns a cancel callback that cleans up all registrations.
    entry.async_on_unload(coordinator.async_start())

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

    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["cover", "sensor"])

    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator._cancel_reconnect_task()
        await coordinator.device.disconnect()
        # Allow the address to be rediscovered without restart
        bluetooth.async_rediscover_address(hass, entry.data["address"])
        _LOGGER.debug("Unloaded RYSE device: %s", entry.data["address"])

    return unload_ok
