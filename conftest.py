"""Root conftest — patches broken HA Bluetooth imports before test collection.

The homeassistant.components.bluetooth module pulls in bluetooth_adapters and
habluetooth at import time. When those packages are out of sync with the
installed HA version (common on macOS dev environments), every test that
transitively imports coordinator.py fails with an ImportError.

By injecting a stub module into sys.modules *before* pytest collects test
files, the real (broken) import never runs and coordinator.py loads cleanly.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _stub_bluetooth_module():
    """Insert a fake homeassistant.components.bluetooth into sys.modules."""
    key = "homeassistant.components.bluetooth"
    if key in sys.modules:
        return  # Already loaded (or already stubbed)

    mod = ModuleType(key)

    # Attributes used by coordinator.py at import time
    mod.BluetoothScanningMode = MagicMock()
    mod.BluetoothServiceInfoBleak = MagicMock()
    mod.async_ble_device_from_address = MagicMock()
    mod.async_track_unavailable = MagicMock(return_value=MagicMock())
    mod.async_register_callback = MagicMock(return_value=MagicMock())

    # ActiveBluetoothDataUpdateCoordinator lives in a sub-module
    auc_key = f"{key}.active_update_coordinator"
    auc_mod = ModuleType(auc_key)
    auc_mod.ActiveBluetoothDataUpdateCoordinator = type(
        "ActiveBluetoothDataUpdateCoordinator",
        (),
        {
            "__init__": lambda self, *a, **kw: None,
            "async_update_listeners": MagicMock(),
        },
    )

    sys.modules[key] = mod
    sys.modules[auc_key] = auc_mod


_stub_bluetooth_module()
