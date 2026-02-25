from homeassistant import config_entries
import voluptuous as vol
import logging
from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    async_discovered_service_info,
)
from bleak import BleakClient, BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import callback
from .const import (
    DOMAIN,
    HARDCODED_UUIDS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_IDLE_DISCONNECT_TIMEOUT,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_MAX_RETRY_ATTEMPTS,
)
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

PAIRING_MODE_FLAG = 0x01  # LE Limited Discoverable Mode (standard pairing mode)

class RyseBLEDeviceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RYSE BLE Device."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return RyseOptionsFlow()

    def __init__(self):
        self._discovered_devices = {}
        self._selected_device = None
        self._scan_task = None
        self._scan_timeout = 30  # seconds

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            if user_input.get("cancel"):
                return self.async_abort(reason="user_cancelled")
            return await self.async_step_scan()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
            description_placeholders={"info": "Put your RYSE device in pairing mode (press and hold the PAIR button) and click Next to continue."},
            last_step=False,
            errors={},
        )

    async def async_step_scan(self, user_input=None):
        # Exclude already configured devices
        existing_entries = self.hass.config_entries.async_entries(DOMAIN)
        existing_addresses = {entry.data["address"] for entry in existing_entries if "address" in entry.data}
        self._update_discovered_devices(existing_addresses)
        errors = {}
        selected_label = None
        if user_input is not None:
            if user_input.get("cancel"):
                return self.async_abort(reason="user_cancelled")
            address = user_input.get("device_address")
            if not address or address not in self._discovered_devices:
                errors["base"] = "invalid_device"
            else:
                in_pairing = self._discovered_devices[address]["in_pairing"]
                selected_label = self._discovered_devices[address]["label"]
                if not in_pairing:
                    errors["base"] = "not_in_pairing_mode"
                else:
                    self._selected_device = address
                    return await self.async_step_pair()
        device_options = {
            addr: f"{info['label']}" for addr, info in self._discovered_devices.items()
        }
        data_schema = vol.Schema({
            vol.Required("device_address", description={"suggested_value": None, "translation_key": "device_address"}): vol.In(device_options)
        }) if device_options else vol.Schema({})
        description = (
            "Press the button on your RYSE SmartShade to put it in pairing mode, then select it below and click Pair. "
            "Devices in pairing mode are marked. If you don't see your device in pairing mode, press the button and click Pair again to refresh the list."
        )
        # Set the dynamic title using title_placeholders
        dynamic_title = selected_label or "RYSE SmartShade"
        self.context["title_placeholders"] = {"name": dynamic_title}
        return self.async_show_form(
            step_id="scan",
            data_schema=data_schema,
            description_placeholders={"info": description},
            errors=errors
        )

    def _update_discovered_devices(self, exclude_addresses=None):
        # Only include RYSE devices in the list, excluding already configured ones
        self._discovered_devices = {}
        exclude_addresses = exclude_addresses or set()
        for info in async_discovered_service_info(self.hass):
            if info.address in exclude_addresses:
                continue
            is_ryse = (
                (info.name and info.name.startswith("RZSS")) or
                (0x0409 in info.manufacturer_data or 0x409 in info.manufacturer_data)
            )
            if not is_ryse:
                continue
            mfr_data = info.manufacturer_data
            raw_data = mfr_data.get(0x0409) or mfr_data.get(0x409)
            in_pairing = bool(raw_data and (raw_data[0] & 0x40))
            label = f"{info.name} ({info.address})"
            if in_pairing:
                label += " [Pairing mode]"
            self._discovered_devices[info.address] = {"label": label, "in_pairing": in_pairing}

    async def async_step_pair(self, user_input=None):
        address = self._selected_device
        if not address:
            return self.async_abort(reason="no_device_selected")

        _LOGGER.info(f"Attempting to connect to RYSE device at address: {address}")
        try:
            # Get BLEDevice from Home Assistant (supports Bluetooth proxies)
            ble_device = async_ble_device_from_address(self.hass, address)
            if not ble_device:
                _LOGGER.error(f"Device not found at address: {address}")
                return self.async_abort(reason="device_not_found")

            # Connect using establish_connection for reliability with proxies
            client = await establish_connection(
                BleakClient,
                ble_device,
                address,
                max_attempts=3,
            )

            if not client.is_connected:
                _LOGGER.error(f"Failed to connect to device: {address}")
                return self.async_abort(reason="cannot_connect")

            _LOGGER.info(f"Connected to device: {address}")

            # Verify we can communicate by subscribing to notifications (like original ryseble)
            try:
                await client.start_notify(HARDCODED_UUIDS["rx_uuid"], lambda s, d: None)
                await client.stop_notify(HARDCODED_UUIDS["rx_uuid"])
                _LOGGER.info(f"Verified communication with device: {address}")
            except Exception as e:
                _LOGGER.warning(f"Could not verify notifications for {address}: {e}, proceeding anyway")

            await client.disconnect()
            _LOGGER.info(f"Disconnected from device: {address}")
        except Exception as e:
            _LOGGER.error(f"Failed to pair with RYSE device {address}: {e}")
            return self.async_abort(reason="pairing_failed")

        # Proceed to naming step
        self._pending_entry_data = {
            "address": address,
            "rx_uuid": HARDCODED_UUIDS["rx_uuid"],
            "tx_uuid": HARDCODED_UUIDS["tx_uuid"],
        }
        return await self.async_step_name()

    async def async_step_name(self, user_input=None):
        errors = {}
        if user_input is not None:
            name = user_input.get("name")
            if not name or not name.strip():
                errors["name"] = "Name required"
            else:
                # Save the name and create the entry
                data = dict(self._pending_entry_data)
                data["name"] = name.strip()
                return self.async_create_entry(
                    title=name.strip(),
                    data=data,
                )
        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema({vol.Required("name"): str}),
            description_placeholders={"info": "Enter a name for your SmartShade."},
            errors=errors,
        )

    async def async_step_bluetooth(self, discovery_info):
        """Handle a flow initialized by bluetooth discovery."""
        address = getattr(discovery_info, "address", None)
        # Check if this address is already configured
        existing_entries = self.hass.config_entries.async_entries(DOMAIN)
        if any(entry.data.get("address") == address for entry in existing_entries):
            return self.async_abort(reason="already_configured")
        name = getattr(discovery_info, "name", "RYSE SmartShade")
        display_name = f"{name} ({address})"
        self._discovered_devices[address] = display_name
        # Set the dynamic title using title_placeholders
        self.context["title_placeholders"] = {"name": display_name}
        return self.async_show_form(
            step_id="scan",
            data_schema=vol.Schema({}),
            description_placeholders={"info": f"RYSE SmartShade {display_name} found. Press Next to pair."},
            errors={}
        )

    async def async_step_abort(self, user_input=None):
        """Handle aborting the config flow."""
        if hasattr(self, '_callback') and self._callback:
            self._callback()
            self._callback = None
        return await super().async_step_abort(user_input)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._restored_state = None
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            _LOGGER.debug("[Cover] Storing last known state for later: %s", last_state.state)
            self._restored_state = last_state
        # Always start as unknown until a fresh value is received
        self._state = "unknown"
        self._initialized = False
        self.async_write_ha_state()

    async def _update_position(self, position):
        if 0 <= position <= 100:
            self._current_position = 100 - position
            self._state = "open" if position < 100 else "closed"
            self._is_closing = False
            self._is_opening = False
            self._last_state_update = datetime.now()
            self._initialized = True
            _LOGGER.debug(f"Updated cover position: {position}")
        self.async_write_ha_state()


class RyseOptionsFlow(config_entries.OptionsFlow):
    """Handle options for RYSE integration."""

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "poll_interval",
                        default=options.get("poll_interval", DEFAULT_POLL_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
                    vol.Optional(
                        "idle_disconnect_timeout",
                        default=options.get("idle_disconnect_timeout", DEFAULT_IDLE_DISCONNECT_TIMEOUT),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
                    vol.Optional(
                        "connection_timeout",
                        default=options.get("connection_timeout", DEFAULT_CONNECTION_TIMEOUT),
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=60)),
                    vol.Optional(
                        "max_retry_attempts",
                        default=options.get("max_retry_attempts", DEFAULT_MAX_RETRY_ATTEMPTS),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
                }
            ),
        )