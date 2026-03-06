from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN
import logging

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SmartShadeCover(coordinator, entry)])


class SmartShadeCover(CoordinatorEntity, CoverEntity, RestoreEntity):
    def __init__(self, coordinator, entry):
        """Initialize the RYSE SmartShade."""
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._entry = entry
        self._attr_name = entry.data.get("name", f"SmartShade {coordinator.device.address}")
        self._attr_unique_id = f"{entry.entry_id}_cover"
        self._is_closing = False
        self._is_opening = False

    @property
    def available(self):
        return self._coordinator.available and not self._coordinator.initializing

    @property
    def current_cover_position(self):
        if self._coordinator.initializing:
            return None
        pos = self._coordinator.position
        if pos is None:
            return None
        return 100 - pos

    @property
    def is_closed(self):
        if self._coordinator.initializing:
            return None
        pos = self._coordinator.position
        if pos is None:
            return None
        return pos == 100

    @property
    def supported_features(self):
        return CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.device.address)},
            connections={(dr.CONNECTION_BLUETOOTH, self._coordinator.device.address)},
            name=self._attr_name,
            manufacturer="RYSE Inc.",
            model="SmartShade",
        )

    @property
    def state(self):
        if self._coordinator.initializing:
            return "initializing"
        return super().state

    async def async_open_cover(self, **kwargs):
        await self._coordinator.async_open_cover()

    async def async_close_cover(self, **kwargs):
        await self._coordinator.async_close_cover()

    async def async_set_cover_position(self, **kwargs):
        position = 100 - kwargs.get("position", 0)
        await self._coordinator.async_set_position(position)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        _LOGGER.debug(
            "[Cover] async_added_to_hass: available=%s, initializing=%s (coordinator.available=%s, coordinator.initializing=%s)",
            self.available,
            self._coordinator.initializing,
            self._coordinator.available,
            self._coordinator.initializing,
        )
        self.async_write_ha_state()
