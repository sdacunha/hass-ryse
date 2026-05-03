"""Repairs flow for RYSE SmartShade integration."""

from __future__ import annotations

import logging

from homeassistant import data_entry_flow
from homeassistant.components.bluetooth import async_scanner_devices_by_address
from homeassistant.components.repairs import RepairsFlow
from homeassistant.helpers import issue_registry as ir

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class BleAuthFailedRepairFlow(RepairsFlow):
    """Guide the user through re-pairing a shade that lost BLE authentication."""

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        """Show the confirm step — tells the user to press the PAIR button."""
        if user_input is not None:
            return await self.async_step_repair()
        return self.async_show_form(step_id="init")

    async def async_step_repair(self, user_input=None) -> data_entry_flow.FlowResult:
        """Pair through the highest-RSSI proxy and pin all future connections to it."""
        # Extract the address from the issue ID: "ble_auth_failed_{address}"
        address = self.issue_id.removeprefix("ble_auth_failed_")

        # Find the coordinator for this address
        coordinators = self.hass.data.get(DOMAIN, {})
        coordinator = None
        for coord in coordinators.values():
            if hasattr(coord, "address") and coord.address == address:
                coordinator = coord
                break

        if not coordinator:
            _LOGGER.error("[Repairs] No coordinator found for address %s", address)
            return self.async_abort(reason="device_not_found")

        try:
            # The shade stores exactly one BLE bond, so we deliberately
            # bond through the strongest-RSSI proxy and pin to it. Bonding
            # through more than one proxy would just race to overwrite.
            scanner_devices = async_scanner_devices_by_address(self.hass, address, connectable=True)
            if not scanner_devices:
                return self.async_abort(reason="device_not_found")
            scanner_devices.sort(key=lambda sd: getattr(sd.advertisement, "rssi", -127), reverse=True)
            best = scanner_devices[0]
            best_source = getattr(best.scanner, "source", None)
            _LOGGER.info(
                "[Repairs] Pairing %s via highest-RSSI proxy %s (rssi=%s)",
                address,
                best_source,
                getattr(best.advertisement, "rssi", "?"),
            )

            # Disconnect any stale coordinator connection first
            await coordinator.device.disconnect()

            client = await establish_connection(
                BleakClient,
                best.ble_device,
                address,
                max_attempts=2,
                timeout=15.0,
            )
            if not client.is_connected:
                return self.async_abort(reason="cannot_connect")

            # Clear any stale bond on this proxy before pairing — a stale
            # LTK would cause the device to drop the link mid-pair.
            try:
                await client.unpair()
            except Exception as unpair_err:
                _LOGGER.debug(
                    "[Repairs] unpair() on %s returned %s (expected if no prior bond)",
                    best_source,
                    unpair_err,
                )
            await client.pair()
            _LOGGER.info("[Repairs] Successfully bonded %s via %s", address, best_source)
            await client.disconnect()

            # Pin future connections to this proxy. Persist on the config
            # entry so the pin survives HA restart.
            if best_source:
                coordinator.device._bonded_source = best_source
                entry = self.hass.config_entries.async_get_entry(coordinator._entry_id)
                if entry is not None:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, "bonded_source": best_source}
                    )

            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            coordinator.device._needs_repair = False

            return self.async_create_entry(data={})

        except Exception as err:
            _LOGGER.error("[Repairs] Re-pair failed for %s: %s", address, err)
            return self.async_abort(reason="repair_failed")


async def async_create_fix_flow(hass, issue_id: str, data: dict | None = None) -> RepairsFlow:
    """Create a fix flow for the given issue."""
    if issue_id.startswith("ble_auth_failed_"):
        return BleAuthFailedRepairFlow()
    return None
