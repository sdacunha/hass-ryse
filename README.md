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
| **Disable battery sensor** | Off | Hide the battery sensor for this device. Useful for plugged-in blinds that report unreliable battery values. |

The integration uses a connect-on-demand model — there's no persistent connection to tune, no keepalive, no polling interval. Commands connect, write, and disconnect immediately. Position and battery come from BLE advertisements.

### How bonding works

The RYSE shade stores exactly one BLE bond at a time. With multiple ESPHome Bluetooth proxies in range, the integration pins each device to whichever proxy was used during pairing (`bonded_source` saved on the config entry) so reconnects route through the right adapter. If you ever move the shade closer to a different proxy, just run the repair flow to re-pair through the new strongest-RSSI proxy.

## Development

Local dev runs Home Assistant directly against this checkout — no Docker required. The layout follows [`ludeeus/integration_blueprint`](https://github.com/ludeeus/integration_blueprint), the community-standard pattern for HA custom integrations.

You need Python 3.13+. Inside a fresh venv:

    python3 -m venv .venv
    source .venv/bin/activate
    scripts/setup        # installs HA + deps from requirements.txt
    scripts/develop      # starts HA against ./config with this repo's custom_component on PYTHONPATH

Then open <http://localhost:8123> and go through onboarding. Add the **ESPHome** integration, point it at one of your Bluetooth proxies, and RYSE shades auto-discover.

Edits to `custom_components/ryse/` take effect on HA restart (Ctrl-C the running `hass` and re-run `scripts/develop`). HA's state persists in `./config/` (gitignored).

### Tests

    pip install -r requirements_test.txt
    python -m pytest tests/ -v

## Support & Feedback
If you have questions, suggestions, or want to contribute, please open an issue or pull request on GitHub! My time is limited, so I will do my best to respond to issues and pull requests, I mostly created this integration for my own use.

## TODO
- [ ] Detect if blinds need calibration
- [ ] Add ability to set speed

## Credits
- Inspired by the original [RYSE Home Assistant integration](https://github.com/mohamedkallel82/ryse) by [@mohamedkallel82](https://github.com/mohamedkallel82).
