# RYSE Home Assistant Integration

> ⚠️ **Heads up — actively reverse-engineering.** I'm currently experimenting to find the most stable way to handle bonded BLE shades over ESPHome Bluetooth proxies (a topology nothing else upstream solves cleanly). The architecture is changing release-to-release while I work this out. Expect breaking changes, occasional regressions, and config-flow churn until things stabilize. If you need a known-good version, pin to a previous release tag.

This is a complete rewrite of the RYSE Home Assistant integration, inspired by [@mohamedkallel82](https://github.com/mohamedkallel82). This version is built from the ground up for reliability, modern Home Assistant best practices, and robust Bluetooth support (including Bluetooth proxies).

## Features
- Real time updates for position and state
- Works with ESPHome Bluetooth proxies and direct connections
- Easy pairing and setup from the Home Assistant UI
- Battery status

## Installation

### HACS Installation (Recommended)
1. Make sure you have [HACS](https://hacs.xyz/) installed
2. Add this repository to HACS:
   - Go to HACS > Integrations
   - Click the three dots in the top right
   - Click "Custom repositories"
   - Add `https://github.com/sdacunha/hass-ryse` as a repository
   - Select "Integration" as the category
3. Click the "Download" button for the RYSE integration
4. Restart Home Assistant

### Manual Installation
Download the RYSE Home Assistant component from: https://github.com/sdacunha/ryse/archive/refs/heads/main.zip

Unzip it and then copy the folder `custom_components/ryse` to your Home Assistant under `/homeassistant/custom_components`.

The tree in your Home Assistant should look like this:

    /homeassistant
        └── custom_components
            └── ryse
                └── __init__.py
                └── ...

Reboot your Home Assistant instance and you can now pair your RYSE SmartShades.

## Configuration

After pairing a device, click **Configure** on the integration entry to adjust settings per device.

| Setting | Default | Description |
| ------- | ------- | ----------- |
| **Active mode** | Off | Maintain a persistent Bluetooth connection and auto-reconnect on disconnect. Recommended for plugged-in blinds. Not recommended for battery-powered blinds as it will drain the battery. |
| **Poll interval** | 300s | How often to poll the device via Bluetooth when advertisements stop (60–3600s). |
| **Idle disconnect timeout** | 60s | Disconnect the Bluetooth connection after this many seconds of inactivity (10–300s). Ignored when active mode is enabled. |
| **Connection timeout** | 10s | How long to wait for a Bluetooth connection attempt (5–60s). |
| **Max retry attempts** | 3 | Number of times to retry a failed connection (1–10). |
| **Active reconnect delay** | 5s | Seconds to wait before reconnecting after an unexpected disconnect in active mode (1–30s). |

Settings take effect immediately without restarting Home Assistant.

## Development

This repo follows the `ludeeus/integration_blueprint` devcontainer pattern (the de-facto community standard for HA custom integrations).

1. Open the repo in VS Code or Cursor
2. **Dev Containers: Reopen in Container** — `scripts/setup` installs Home Assistant from `requirements.txt`
3. Run **scripts/develop** (or the **Run Home Assistant on port 8123** task)
4. Open http://localhost:8123 and go through HA onboarding
5. Add the **ESPHome** integration and point it at a Bluetooth proxy on your network
6. RYSE shades auto-discover via the proxy

The `custom_components/ryse/` directory is exposed via `PYTHONPATH` — edits take effect on HA restart. HA's persistent state lives in `config/` (gitignored).

## Support & Feedback
If you have questions, suggestions, or want to contribute, please open an issue or pull request on GitHub! My time is limited, so I will do my best to respond to issues and pull requests, I mostly created this integration for my own use.

## TODO
- [ ] Detect if blinds need calibration
- [ ] Add ability to set speed

## Credits
- Inspired by the original [RYSE Home Assistant integration](https://github.com/mohamedkallel82/ryse) by [@mohamedkallel82](https://github.com/mohamedkallel82).
