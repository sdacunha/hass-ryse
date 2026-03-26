from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
    async_scanner_devices_by_address,
)
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
import voluptuous as vol
import logging

from bleak import BleakClient
from bleak_retry_connector import establish_connection

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

# Manufacturer ID registered to RYSE Inc. with the Bluetooth SIG
RYSE_MANUFACTURER_ID = 0x0409


def _is_ryse_device(service_info) -> bool:
    """Return True if the service_info belongs to a RYSE SmartShade.

    Requires BOTH the RZSS name prefix AND the RYSE manufacturer ID.
    """
    has_name = bool(getattr(service_info, "name", None) and service_info.name.startswith("RZSS"))
    has_mfr = RYSE_MANUFACTURER_ID in getattr(service_info, "manufacturer_data", {})
    return has_name and has_mfr


SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Optional("active_mode", default=DEFAULT_ACTIVE_MODE): bool,
        vol.Optional("poll_interval", default=DEFAULT_POLL_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=60, max=3600)
        ),
        vol.Optional("idle_disconnect_timeout", default=DEFAULT_IDLE_DISCONNECT_TIMEOUT): vol.All(
            vol.Coerce(int), vol.Range(min=10, max=300)
        ),
        vol.Optional("connection_timeout", default=DEFAULT_CONNECTION_TIMEOUT): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=60)
        ),
        vol.Optional("max_retry_attempts", default=DEFAULT_MAX_RETRY_ATTEMPTS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=10)
        ),
        vol.Optional("active_reconnect_delay", default=DEFAULT_ACTIVE_RECONNECT_DELAY): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=30)
        ),
        vol.Optional("disable_battery_sensor", default=False): bool,
    }
)


class RyseBLEDeviceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RYSE BLE Device."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return RyseOptionsFlow()

    def __init__(self):
        self._discovered_devices: dict[str, dict] = {}
        self._selected_device: str | None = None
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._pending_entry_data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Manual flow: user → scan → pair → name → settings
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_scan()
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        existing = self.hass.config_entries.async_entries(DOMAIN)
        exclude = set()
        for entry in existing:
            if "address" in entry.data:
                exclude.add(entry.data["address"].upper())
            if entry.unique_id:
                exclude.add(entry.unique_id.upper())

        self._refresh_discovered_devices(exclude)

        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input.get("device_address")
            if not address or address not in self._discovered_devices:
                errors["base"] = "no_devices_found"
            else:
                dev = self._discovered_devices[address]
                if not dev["in_pairing"]:
                    errors["base"] = "not_in_pairing_mode"
                else:
                    self._selected_device = address
                    return await self.async_step_pair()

        device_options = {addr: info["label"] for addr, info in self._discovered_devices.items()}

        if device_options:
            data_schema = vol.Schema({vol.Required("device_address"): vol.In(device_options)})
        else:
            data_schema = vol.Schema({})

        return self.async_show_form(
            step_id="scan",
            data_schema=data_schema,
            errors=errors,
        )

    def _refresh_discovered_devices(self, exclude_addresses: set) -> None:
        """Populate self._discovered_devices with currently visible RYSE devices."""
        self._discovered_devices = {}
        for info in async_discovered_service_info(self.hass):
            addr_upper = info.address.upper()

            # Skip already configured
            if addr_upper in exclude_addresses:
                continue

            # Require BOTH RZSS name AND RYSE manufacturer ID
            if not _is_ryse_device(info):
                continue

            mfr_data = info.manufacturer_data.get(RYSE_MANUFACTURER_ID)
            in_pairing = bool(mfr_data and len(mfr_data) >= 1 and (mfr_data[0] & 0x40))

            label = f"{info.name} ({info.address})"
            if in_pairing:
                label += " [Pairing mode]"

            self._discovered_devices[info.address] = {
                "label": label,
                "in_pairing": in_pairing,
            }

    # ------------------------------------------------------------------
    # Bluetooth auto-discovery: bluetooth → bluetooth_confirm → pair → …
    # ------------------------------------------------------------------

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """Handle a flow initialized by bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        # Validate this is actually a RYSE device
        if not _is_ryse_device(discovery_info):
            return self.async_abort(reason="not_supported")

        # If we already have configured RYSE devices, only surface new
        # discoveries that are in pairing mode.  Phantom devices from
        # ESPHome proxy MAC corruption will never be in pairing mode.
        existing = self.hass.config_entries.async_entries(DOMAIN)
        if existing:
            mfr_data = discovery_info.manufacturer_data.get(RYSE_MANUFACTURER_ID)
            in_pairing = bool(mfr_data and len(mfr_data) >= 1 and (mfr_data[0] & 0x40))
            if not in_pairing:
                _LOGGER.debug(
                    "[ConfigFlow] Ignoring non-pairing RZSS discovery %s (likely phantom)",
                    discovery_info.address,
                )
                return self.async_abort(reason="not_supported")

        self._discovery_info = discovery_info
        title = f"{discovery_info.name} ({discovery_info.address})"
        self.context["title_placeholders"] = {"name": title}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm the discovered device before pairing."""
        if user_input is not None:
            self._selected_device = self._discovery_info.address
            return await self.async_step_pair()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=self.context.get("title_placeholders", {}),
        )

    # ------------------------------------------------------------------
    # Shared steps: pair → name → settings → create entry
    # ------------------------------------------------------------------

    async def async_step_pair(self, user_input=None) -> ConfigFlowResult:
        address = self._selected_device
        if not address:
            return self.async_abort(reason="no_device_selected")

        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()

        _LOGGER.info("Attempting to connect to RYSE device at %s", address)
        try:
            ble_device = async_ble_device_from_address(self.hass, address)
            if not ble_device:
                _LOGGER.error("Device not found at address: %s", address)
                return self.async_abort(reason="device_not_found")

            client = await establish_connection(BleakClient, ble_device, address, max_attempts=3)

            if not client.is_connected:
                _LOGGER.error("Failed to connect to device: %s", address)
                return self.async_abort(reason="cannot_connect")

            _LOGGER.info("Connected to device: %s", address)

            try:
                await client.start_notify(HARDCODED_UUIDS["rx_uuid"], lambda s, d: None)
                await client.stop_notify(HARDCODED_UUIDS["rx_uuid"])
                _LOGGER.info("Verified communication with device: %s", address)
            except Exception as e:
                _LOGGER.warning(
                    "Could not verify notifications for %s: %s, proceeding anyway",
                    address,
                    e,
                )

            # Pair on this proxy to establish bonding keys
            try:
                await client.pair()
                _LOGGER.info("Paired with device on primary proxy: %s", address)
            except Exception as e:
                _LOGGER.warning("Pairing on primary proxy failed (may not be required): %s", e)

            await client.disconnect()

            # Bond with all other proxies that can reach this device
            await self._bond_all_proxies(address)

        except Exception as e:
            _LOGGER.error("Failed to pair with RYSE device %s: %s", address, e)
            return self.async_abort(reason="pairing_failed")

        self._pending_entry_data = {
            "address": address,
            "rx_uuid": HARDCODED_UUIDS["rx_uuid"],
            "tx_uuid": HARDCODED_UUIDS["tx_uuid"],
        }
        return await self.async_step_name()

    async def _bond_all_proxies(self, address: str) -> None:
        """Bond the shade with every ESPHome proxy that can reach it.

        Each proxy stores bonding keys independently.  By pairing through
        each one during setup, HA can route connections through any proxy
        without hitting 'Insufficient authentication' errors.
        """
        scanner_devices = async_scanner_devices_by_address(self.hass, address, connectable=True)
        _LOGGER.info(
            "[ConfigFlow] Found %d proxy/adapter(s) that can reach %s — bonding with each",
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
                    max_attempts=2,
                    timeout=15.0,
                )
                if client.is_connected:
                    try:
                        await client.pair()
                        _LOGGER.info("[ConfigFlow] Bonded %s via proxy %s", address, source)
                    except Exception as pair_err:
                        _LOGGER.warning(
                            "[ConfigFlow] pair() failed on proxy %s for %s: %s (may already be bonded)",
                            source,
                            address,
                            pair_err,
                        )
                    await client.disconnect()
            except Exception as e:
                _LOGGER.warning(
                    "[ConfigFlow] Could not bond %s via proxy %s: %s",
                    address,
                    source,
                    e,
                )

    async def async_step_name(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input.get("name")
            if not name or not name.strip():
                errors["name"] = "Name required"
            else:
                self._pending_entry_data["name"] = name.strip()
                return await self.async_step_settings()
        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema({vol.Required("name"): str}),
            errors=errors,
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = dict(self._pending_entry_data)
            return self.async_create_entry(
                title=data["name"],
                data=data,
                options=user_input,
            )
        return self.async_show_form(step_id="settings", data_schema=SETTINGS_SCHEMA)


class RyseOptionsFlow(config_entries.OptionsFlow):
    """Handle options for RYSE integration."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "active_mode",
                        default=options.get("active_mode", DEFAULT_ACTIVE_MODE),
                    ): bool,
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
                    vol.Optional(
                        "active_reconnect_delay",
                        default=options.get("active_reconnect_delay", DEFAULT_ACTIVE_RECONNECT_DELAY),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
                    vol.Optional(
                        "disable_battery_sensor",
                        default=options.get("disable_battery_sensor", False),
                    ): bool,
                }
            ),
        )
