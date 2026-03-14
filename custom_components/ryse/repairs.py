"""Repairs flow for RYSE SmartShade integration."""

from __future__ import annotations

import logging

from homeassistant import data_entry_flow
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.components.repairs import RepairsFlow
from homeassistant.helpers import issue_registry as ir


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
        """Attempt to reconnect and re-pair the BLE device."""
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
            ble_device = async_ble_device_from_address(self.hass, address, connectable=True)
            if not ble_device:
                return self.async_abort(reason="device_not_found")

            coordinator.device.set_ble_device(ble_device)

            # Disconnect any stale connection
            await coordinator.device.disconnect()

            # Establish a fresh connection
            if not await coordinator.device.connect():
                return self.async_abort(reason="cannot_connect")

            # Re-pair
            await coordinator.device.client.pair()
            _LOGGER.info("[Repairs] Successfully re-paired %s", address)

            # Clear the repair issue
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)

            return self.async_create_entry(data={})

        except Exception as err:
            _LOGGER.error("[Repairs] Re-pair failed for %s: %s", address, err)
            return self.async_abort(reason="repair_failed")


async def async_create_fix_flow(hass, issue_id: str, data: dict | None = None) -> RepairsFlow:
    """Create a fix flow for the given issue."""
    if issue_id.startswith("ble_auth_failed_"):
        return BleAuthFailedRepairFlow()
    return None
