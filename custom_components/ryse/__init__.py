"""The RYSE BLE Device integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from bleak import BleakClient
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    async_scanner_devices_by_address,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, HARDCODED_UUIDS
from .ryse import RyseDevice

_LOGGER = logging.getLogger(__name__)

SERVICE_ADDRESS_SCHEMA = vol.Schema({vol.Required("address"): str})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register debug services."""
    _LOGGER.info("Setting up RYSE Device integration")

    async def _handle_test_ble_connection(call: ServiceCall) -> None:
        """Probe whether the shade accepts unencrypted GATT operations."""
        address = call.data["address"]
        _LOGGER.info("[RYSE BLE TEST] Testing unencrypted BLE connection to %s", address)

        for coord in hass.data.get(DOMAIN, {}).values():
            if hasattr(coord, "address") and coord.address == address:
                _LOGGER.info("[RYSE BLE TEST] Disconnecting coordinator for %s first", address)
                await coord.device.disconnect()
                break

        ble_device = async_ble_device_from_address(hass, address, connectable=True)
        if not ble_device:
            _LOGGER.error("[RYSE BLE TEST] No BLE device found for %s", address)
            return

        try:
            client = await establish_connection(BleakClient, ble_device, address, max_attempts=2, timeout=15.0)
            _LOGGER.info("[RYSE BLE TEST] Connected to %s (no pair() called)", address)
            try:
                data = await client.read_gatt_char(HARDCODED_UUIDS["rx_uuid"])
                _LOGGER.info("[RYSE BLE TEST] GATT read SUCCESS without pairing: %s (hex=%s)", list(data), data.hex())
            except Exception as err:
                _LOGGER.warning("[RYSE BLE TEST] GATT read FAILED without pairing: %s", err)
            try:
                cmd = bytes([0xF5, 0x03, 0x01, 0x06, 0x00])
                packet = cmd + bytes([sum(cmd[2:]) % 256])
                await client.write_gatt_char(HARDCODED_UUIDS["tx_uuid"], packet)
                _LOGGER.info("[RYSE BLE TEST] GATT write SUCCESS without pairing")
            except Exception as err:
                _LOGGER.warning("[RYSE BLE TEST] GATT write FAILED without pairing: %s", err)
            await client.disconnect()
        except Exception as e:
            _LOGGER.error("[RYSE BLE TEST] Connection failed for %s: %s", address, e)

    async def _handle_inspect_device(call: ServiceCall) -> None:
        """Dump everything we can learn about the shade: services, characteristics, descriptors, reads, notifications, timing.

        Run with the shade already bonded — gathers data the integration's
        normal flow doesn't expose. Use to understand the device protocol.
        """
        import asyncio
        import time

        address = call.data["address"]
        _LOGGER.info("[RYSE INSPECT] ============================================================")
        _LOGGER.info("[RYSE INSPECT] Inspecting %s", address)
        _LOGGER.info("[RYSE INSPECT] ============================================================")

        for coord in hass.data.get(DOMAIN, {}).values():
            if hasattr(coord, "address") and coord.address == address:
                _LOGGER.info("[RYSE INSPECT] Disconnecting coordinator first")
                await coord.device.disconnect()
                break

        ble_device = async_ble_device_from_address(hass, address, connectable=True)
        if not ble_device:
            _LOGGER.error("[RYSE INSPECT] No BLE device found for %s", address)
            return

        # Scanner / adapter info
        scanner_devices = async_scanner_devices_by_address(hass, address, connectable=True)
        _LOGGER.info("[RYSE INSPECT] Reachable via %d scanner(s):", len(scanner_devices))
        for sd in scanner_devices:
            source = getattr(sd.scanner, "source", "?")
            rssi = getattr(sd.advertisement, "rssi", "?")
            name = getattr(sd.advertisement, "local_name", None) or getattr(sd.ble_device, "name", "?")
            mfr = getattr(sd.advertisement, "manufacturer_data", {})
            _LOGGER.info(
                "[RYSE INSPECT]   scanner=%s rssi=%s name=%s mfr=%s",
                source,
                rssi,
                name,
                {hex(k): list(v) for k, v in mfr.items()},
            )

        connect_start = time.monotonic()
        try:
            client = await establish_connection(BleakClient, ble_device, address, max_attempts=2, timeout=15.0)
        except Exception as e:
            _LOGGER.error("[RYSE INSPECT] Connect failed: %s", e)
            return
        connect_ms = (time.monotonic() - connect_start) * 1000
        _LOGGER.info("[RYSE INSPECT] Connected in %.0fms (mtu=%s)", connect_ms, getattr(client, "mtu_size", "?"))

        # Full GATT walk
        try:
            services = client.services
            _LOGGER.info("[RYSE INSPECT] --- GATT services ---")
            for service in services:
                _LOGGER.info("[RYSE INSPECT] service: %s (handle=%s)", service.uuid, service.handle)
                for char in service.characteristics:
                    props = ",".join(char.properties)
                    _LOGGER.info(
                        "[RYSE INSPECT]   char: %s (handle=%s, props=%s)",
                        char.uuid,
                        char.handle,
                        props,
                    )
                    # Try reading every readable characteristic
                    if "read" in char.properties:
                        try:
                            v = await client.read_gatt_char(char.uuid)
                            _LOGGER.info(
                                "[RYSE INSPECT]     read: len=%d hex=%s ascii=%r",
                                len(v),
                                v.hex(),
                                v.decode("ascii", errors="replace"),
                            )
                        except Exception as e:
                            _LOGGER.info("[RYSE INSPECT]     read FAILED: %s", e)
                    for desc in char.descriptors:
                        try:
                            v = await client.read_gatt_descriptor(desc.handle)
                            _LOGGER.info("[RYSE INSPECT]     desc %s (handle=%s): %s", desc.uuid, desc.handle, v.hex())
                        except Exception as e:
                            _LOGGER.info(
                                "[RYSE INSPECT]     desc %s (handle=%s) read FAILED: %s", desc.uuid, desc.handle, e
                            )
        except Exception as e:
            _LOGGER.error("[RYSE INSPECT] GATT walk failed: %s", e)

        # Subscribe to notifications and listen for 5s to see what arrives
        _LOGGER.info("[RYSE INSPECT] --- listening for notifications (5s) ---")
        notifications = []

        def _on_notify(_sender, data: bytearray):
            notifications.append((time.monotonic(), bytes(data)))
            _LOGGER.info("[RYSE INSPECT] NOTIFY len=%d hex=%s", len(data), data.hex())

        try:
            await client.start_notify(HARDCODED_UUIDS["rx_uuid"], _on_notify)
        except Exception as e:
            _LOGGER.info("[RYSE INSPECT] start_notify failed: %s", e)
        await asyncio.sleep(5.0)
        try:
            await client.stop_notify(HARDCODED_UUIDS["rx_uuid"])
        except Exception:
            pass
        _LOGGER.info("[RYSE INSPECT] Received %d notification(s) in 5s", len(notifications))

        # Send a get_position packet, watch what comes back
        _LOGGER.info("[RYSE INSPECT] --- sending get-position, watching for 3s ---")
        get_pos = bytes([0xF5, 0x02, 0x01, 0x03])
        get_pos_pkt = get_pos + bytes([sum(get_pos[2:]) % 256])
        notifications.clear()
        try:
            await client.start_notify(HARDCODED_UUIDS["rx_uuid"], _on_notify)
            t0 = time.monotonic()
            await client.write_gatt_char(HARDCODED_UUIDS["tx_uuid"], get_pos_pkt)
            _LOGGER.info("[RYSE INSPECT] get-position write took %.0fms", (time.monotonic() - t0) * 1000)
            await asyncio.sleep(3.0)
            await client.stop_notify(HARDCODED_UUIDS["rx_uuid"])
        except Exception as e:
            _LOGGER.info("[RYSE INSPECT] get-position probe failed: %s", e)

        await client.disconnect()
        _LOGGER.info("[RYSE INSPECT] ============================================================")
        _LOGGER.info("[RYSE INSPECT] Done")
        _LOGGER.info("[RYSE INSPECT] ============================================================")

    async def _handle_bond_all_proxies(call: ServiceCall) -> None:
        """Force a pair() call on every reachable proxy. Escape hatch — normally the repair flow handles this."""
        address = call.data["address"]
        scanner_devices = async_scanner_devices_by_address(hass, address, connectable=True)
        _LOGGER.info("[RYSE BLE] Bonding %s on %d proxy/proxies", address, len(scanner_devices))
        for sd in scanner_devices:
            source = getattr(sd.scanner, "source", "unknown")
            try:
                client = await establish_connection(BleakClient, sd.ble_device, address, max_attempts=1, timeout=10.0)
                if client.is_connected:
                    try:
                        await client.unpair()
                    except Exception:
                        pass
                    try:
                        await client.pair()
                        _LOGGER.info("[RYSE BLE] Bonded %s via %s", address, source)
                    except Exception as e:
                        _LOGGER.warning("[RYSE BLE] pair() failed on %s: %s", source, e)
                    await client.disconnect()
            except Exception as e:
                _LOGGER.warning("[RYSE BLE] Could not reach %s via %s: %s", address, source, e)

    hass.services.async_register(
        DOMAIN, "test_ble_connection", _handle_test_ble_connection, schema=SERVICE_ADDRESS_SCHEMA
    )
    hass.services.async_register(DOMAIN, "bond_all_proxies", _handle_bond_all_proxies, schema=SERVICE_ADDRESS_SCHEMA)
    hass.services.async_register(DOMAIN, "inspect_device", _handle_inspect_device, schema=SERVICE_ADDRESS_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RYSE from a config entry."""
    _LOGGER.info("Setting up RYSE entry: %s", entry.data)

    address = entry.data["address"]

    if not bluetooth.async_scanner_count(hass, connectable=True):
        raise ConfigEntryNotReady("No connectable Bluetooth adapters or proxies available")

    # Clean up any stale connections left from a previous session
    try:
        from bleak_retry_connector import close_stale_connections_by_address

        await close_stale_connections_by_address(address)
    except ImportError:
        pass

    device = RyseDevice(address)
    device._bonded_source = entry.data.get("bonded_source")
    if device._bonded_source:
        _LOGGER.info(
            "[init] %s pinned to bonded proxy %s",
            entry.data.get("name", address),
            device._bonded_source,
        )
    _LOGGER.info("[init] Created RyseDevice for %s", address)

    # Lazy import — keeps the package import-clean on non-Linux test runners.
    from .coordinator import RyseCoordinator

    coordinator = RyseCoordinator(
        hass,
        address,
        device,
        entry.data.get("name", "SmartShade"),
        entry_id=entry.entry_id,
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, ["cover", "sensor"])
    # Start the coordinator after platforms have subscribed.
    entry.async_on_unload(coordinator.async_start())

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading RYSE entry: %s", entry.data)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["cover", "sensor"])
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.device.disconnect()
        bluetooth.async_rediscover_address(hass, entry.data["address"])
    return unload_ok
