"""Test fixtures and constants for the RYSE integration tests."""

RYSE_ADDRESS = "AA:BB:CC:DD:EE:FF"
RYSE_NAME = "RZSS_1234"
RYSE_MANUFACTURER_ID = 0x0409

# Flags byte: bit 6 (0x40) = pairing mode. Position=50, Battery=85.
RYSE_MFR_DATA_PAIRING = bytes([0x40, 50, 85])
# Normal operation (not pairing). Position=30, Battery=72.
RYSE_MFR_DATA_NORMAL = bytes([0x00, 30, 72])
# Incomplete data (only 1 byte)
RYSE_MFR_DATA_SHORT = bytes([0x00])


def _generate_ble_device(address, name):
    """Create a BLEDevice without depending on HA test helpers."""
    from bleak.backends.device import BLEDevice

    return BLEDevice(address=address, name=name, details={}, rssi=-60)


def _generate_advertisement_data(local_name="", manufacturer_data=None, **kwargs):
    """Create an AdvertisementData without depending on HA test helpers."""
    from bleak.backends.scanner import AdvertisementData

    return AdvertisementData(
        local_name=local_name or "",
        manufacturer_data=manufacturer_data or {},
        service_data=kwargs.get("service_data", {}),
        service_uuids=kwargs.get("service_uuids", []),
        rssi=kwargs.get("rssi", -127),
        platform_data=((),),
        tx_power=kwargs.get("tx_power", -127),
    )


def _make_ryse_service_info(
    address: str = RYSE_ADDRESS,
    name: str = RYSE_NAME,
    manufacturer_data: dict[int, bytes] | None = None,
    rssi: int = -60,
    connectable: bool = True,
):
    """Build a BluetoothServiceInfoBleak for a RYSE device."""
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

    if manufacturer_data is None:
        manufacturer_data = {RYSE_MANUFACTURER_ID: RYSE_MFR_DATA_NORMAL}
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        manufacturer_data=manufacturer_data,
        service_data={},
        service_uuids=[],
        rssi=rssi,
        source="local",
        advertisement=_generate_advertisement_data(
            local_name=name,
            manufacturer_data=manufacturer_data,
        ),
        device=_generate_ble_device(address, name),
        time=0,
        connectable=connectable,
        tx_power=-127,
    )


def get_service_info(name: str):
    """Lazily construct service info constants to avoid import-time HA deps."""
    _infos = {
        "RYSE_SERVICE_INFO": lambda: _make_ryse_service_info(),
        "RYSE_SERVICE_INFO_PAIRING": lambda: _make_ryse_service_info(
            manufacturer_data={RYSE_MANUFACTURER_ID: RYSE_MFR_DATA_PAIRING},
        ),
        "RYSE_SERVICE_INFO_2": lambda: _make_ryse_service_info(
            address="11:22:33:44:55:66",
            name="RZSS_5678",
        ),
        "RYSE_SERVICE_INFO_2_PAIRING": lambda: _make_ryse_service_info(
            address="11:22:33:44:55:66",
            name="RZSS_5678",
            manufacturer_data={RYSE_MANUFACTURER_ID: RYSE_MFR_DATA_PAIRING},
        ),
        "NOT_RYSE_WRONG_NAME": lambda: _make_ryse_service_info(
            name="Govee_H5075",
            manufacturer_data={RYSE_MANUFACTURER_ID: b"\x00\x01\x02"},
        ),
        "NOT_RYSE_WRONG_MFR": lambda: _make_ryse_service_info(
            name="RZSS_Fake",
            manufacturer_data={0x1234: b"\x00\x01\x02"},
        ),
        "NOT_RYSE_UNRELATED": lambda: _make_ryse_service_info(
            name="SwitchBot",
            manufacturer_data={89: b"\xfd\x60\x30\x55"},
        ),
        "PHANTOM_RYSE_DEVICE": lambda: _make_ryse_service_info(
            address="FF:EE:DD:CC:BB:AA",
            name="RZSS_9999",
            manufacturer_data={RYSE_MANUFACTURER_ID: RYSE_MFR_DATA_NORMAL},
        ),
    }
    return _infos[name]()
