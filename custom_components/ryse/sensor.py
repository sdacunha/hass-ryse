"""The RYSE Battery Sensor."""
from __future__ import annotations

import logging
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from datetime import datetime, timedelta
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .ryse import RyseDevice
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the RYSE battery sensor."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RyseBatterySensor(coordinator, entry)])

class RyseBatterySensor(CoordinatorEntity, SensorEntity, RestoreEntity):
    """Representation of a RYSE battery sensor."""

    def __init__(self, coordinator, entry):
        """Initialize the RYSE battery sensor."""
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._entry = entry
        self._attr_name = f"{entry.data.get('name', entry.data['address'])} Battery"
        self._attr_unique_id = f"{entry.entry_id}_battery"
        self._attr_native_unit_of_measurement = "%"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def available(self):
        if self._entry.options.get("disable_battery_sensor", False):
            return False
        return self._coordinator.available and not self._coordinator.initializing

    @property
    def native_value(self):
        if self._entry.options.get("disable_battery_sensor", False):
            return None
        if self._coordinator.initializing:
            return None
        return self._coordinator.battery

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.device.address)},
            name=self._attr_name,
            manufacturer="RYSE Inc.",
            model="SmartShade",
        )

    @property
    def state(self):
        if self._coordinator.initializing:
            return "initializing"
        return super().state

    async def async_added_to_hass(self) -> None:
        """Set up the battery monitoring."""
        await super().async_added_to_hass()
        _LOGGER.debug(
            "[BatterySensor] async_added_to_hass: available=%s, initializing=%s (coordinator.available=%s, coordinator.initializing=%s)",
            self.available, self._coordinator.initializing, self._coordinator.available, self._coordinator.initializing
        )
        self.async_write_ha_state()
        self._coordinator.device.add_battery_callback(self._handle_battery_update)
        self._coordinator.device.add_unavailable_callback(self._handle_device_unavailable)
        self._coordinator.device.add_adv_callback(self._handle_adv_seen)
        cover_available = False
        device_reg = dr.async_get(self.hass)
        entity_reg = er.async_get(self.hass)
        device = device_reg.async_get_device(identifiers={(DOMAIN, self._coordinator.device.address)})
        if device:
            device_id = device.id
            for entity in entity_reg.entities.values():
                if entity.device_id == device_id and entity.domain == "cover":
                    cover_state = self.hass.states.get(entity.entity_id)
                    if cover_state is not None and cover_state.state != "unavailable":
                        cover_available = True
        # NEW: If the device is not connected or the cover is unavailable, mark battery sensor unavailable
        if not self._coordinator.available or not cover_available:
            _LOGGER.debug("[BatterySensor] Device is offline or cover is unavailable at startup, marking battery sensor unavailable.")
            self.mark_unavailable()
            return
        if self._coordinator.battery is not None and cover_available:
            _LOGGER.debug("[BatterySensor] Immediate battery update from latest advertisement: %s", self._coordinator.battery)
            await self._handle_battery_update(self._coordinator.battery)
        elif cover_available:
            last_state = await self.async_get_last_state()
            if last_state and last_state.state not in (None, "unknown", "unavailable"):
                _LOGGER.debug("[BatterySensor] Restoring last known battery value: %s", last_state.state)
                await self._handle_battery_update(int(last_state.state))
            else:
                _LOGGER.debug("[BatterySensor] No fresh battery value and no last state, marking battery sensor unavailable.")
                self.mark_unavailable()
        else:
            _LOGGER.debug("[BatterySensor] Cover is unavailable at startup, marking battery sensor unavailable and not restoring last state.")
            self.mark_unavailable()

    async def async_will_remove_from_hass(self):
        await super().async_will_remove_from_hass()

    async def _handle_battery_update(self, battery_level: int) -> None:
        """Handle battery level updates."""
        _LOGGER.debug("Battery sensor callback: received battery level %s", battery_level)
        await self._coordinator.async_update_battery(battery_level)

    def mark_unavailable(self):
        """Mark the battery sensor as unavailable and update state."""
        _LOGGER.debug(f"[BatterySensor] mark_unavailable called for {self.entity_id}")
        self.hass.async_create_task(self._coordinator.async_update_battery(None))

    def _handle_device_unavailable(self):
        _LOGGER.warning("[BatterySensor] Device became unavailable, marking battery sensor as unavailable.")
        self.hass.async_create_task(self._coordinator.async_update_battery(None))

    def _handle_adv_seen(self):
        if not self.available:
            self.async_write_ha_state() 