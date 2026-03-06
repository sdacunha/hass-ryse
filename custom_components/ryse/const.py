DOMAIN = "ryse"

HARDCODED_UUIDS = {
    "rx_uuid": "a72f2801-b0bd-498b-b4cd-4a3901388238",
    "tx_uuid": "a72f2802-b0bd-498b-b4cd-4a3901388238",
}

# Connection and polling configuration
DEFAULT_CONNECTION_TIMEOUT = 10  # seconds (initial attempt)
DEFAULT_MAX_RETRY_ATTEMPTS = 3
DEFAULT_POLL_INTERVAL = 300  # seconds (5 minutes - rely on advertisements primarily)
DEFAULT_INIT_TIMEOUT = 30  # seconds (wait for first advertisement)
DEFAULT_IDLE_DISCONNECT_TIMEOUT = 60  # seconds - disconnect BLE after inactivity
DEFAULT_ACTIVE_MODE = False  # keep persistent BLE connection (for plugged-in blinds)
DEFAULT_ACTIVE_RECONNECT_DELAY = 5  # seconds before reconnecting in active mode
