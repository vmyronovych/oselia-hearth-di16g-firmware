"""OSELIA Hearth (RP2040-ETH) provisioning tool + board toolbox.

A Typer-based CLI that flashes MicroPython onto a bare Waveshare RP2040-ETH module
(or reflashes one that already runs MicroPython), provisions it onto a site's MQTT
broker, and wraps the day-to-day mpremote board operations as first-class
subcommands. See PROVISIONING_SPEC.md for the contract and the on-board layout.

The hardware-quirk handling -- watchdog-resets-on-break-in, cooperative MQTT quiesce, BOOTSEL/
flash_nuke flashing, atomic site.json, OTA A/B slot layout -- lives in this package.
"""

__version__ = "0.1.0"
