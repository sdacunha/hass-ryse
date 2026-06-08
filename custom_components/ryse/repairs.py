"""Repairs flow for RYSE SmartShade — re-pair through the strongest proxy."""

from __future__ import annotations

import logging

from bleak import BleakClient
from bleak_retry_connector import establish_connection
from homeassistant import data_entry_flow
from homeassistant.components.bluetooth import async_scanner_devices_by_address
from homeassistant.components.repairs import RepairsFlow
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class BleAuthFailedRepairFlow(RepairsFlow):
    """Guide the user through re-pairing a shade that's lost its BLE bond."""

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            return await self.async_step_repair()
        return self.async_show_form(step_id="init")

    async def async_step_repair(self, user_input=None) -> data_entry_flow.FlowResult:
        """Pair through the highest-RSSI proxy that can reach the shade."""
        address = self.issue_id.removeprefix("ble_auth_failed_")

        coordinators = self.hass.data.get(DOMAIN, {})
        coordinator = None
        for coord in coordinators.values():
            if hasattr(coord, "address") and coord.address == address:
                coordinator = coord
                break
        if not coordinator:
            _LOGGER.error("[Repairs] No coordinator found for %s", address)
            return self.async_abort(reason="device_not_found")

        scanner_devices = async_scanner_devices_by_address(self.hass, address, connectable=True)
        if not scanner_devices:
            return self.async_abort(reason="device_not_found")

        # Highest RSSI proxy first
        scanner_devices.sort(
            key=lambda sd: getattr(sd.advertisement, "rssi", -127),
            reverse=True,
        )
        best = scanner_devices[0]
        best_source = getattr(best.scanner, "source", None)
        _LOGGER.info(
            "[Repairs] Pairing %s via proxy %s (rssi=%s)",
            address,
            best_source,
            getattr(best.advertisement, "rssi", "?"),
        )

        await coordinator.device.disconnect()

        try:
            client = await establish_connection(
                BleakClient,
                best.ble_device,
                address,
                max_attempts=2,
                timeout=15.0,
            )
        except Exception as err:
            _LOGGER.error("[Repairs] Could not connect to %s: %s", address, err)
            return self.async_abort(reason="cannot_connect")

        if not client.is_connected:
            return self.async_abort(reason="cannot_connect")

        # Clear any stale bond on this proxy before pairing — a stale LTK
        # would cause the device to drop the link mid-pair with auth-5.
        try:
            await client.unpair()
        except Exception as e:
            _LOGGER.debug("[Repairs] unpair() returned %s (often expected)", e)

        try:
            await client.pair()
            _LOGGER.info("[Repairs] Successfully paired %s via %s", address, best_source)
        except Exception as err:
            _LOGGER.error("[Repairs] Pair failed for %s: %s", address, err)
            try:
                await client.disconnect()
            except Exception:
                pass
            return self.async_abort(reason="repair_failed")

        try:
            await client.disconnect()
        except Exception:
            pass

        # Pin all future connections to this proxy and persist on the
        # config entry so the pin survives HA restart.
        if best_source:
            coordinator.device._bonded_source = best_source
            entry = self.hass.config_entries.async_get_entry(coordinator._entry_id)
            if entry is not None:
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, "bonded_source": best_source})
            _LOGGER.info("[Repairs] Pinned %s to bonded proxy %s", address, best_source)

        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_create_entry(data={})


async def async_create_fix_flow(hass, issue_id: str, data: dict | None = None) -> RepairsFlow | None:
    if issue_id.startswith("ble_auth_failed_"):
        return BleAuthFailedRepairFlow()
    return None
