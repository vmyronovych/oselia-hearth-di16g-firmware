"""OSELIA Hearth (RP2040-ETH) provisioning tool + board toolbox.

A Typer-based CLI that flashes MicroPython onto a bare Waveshare RP2040-ETH module
(or reflashes one that already runs MicroPython), provisions it onto a site's MQTT
broker, and wraps the day-to-day mpremote board operations as first-class
subcommands. See PROVISIONING_SPEC.md for the contract and the on-board layout.

The hardware-quirk handling (USB cold-boot wedge, cooperative MQTT quiesce, BOOTSEL/
flash_nuke flashing, atomic site.json, OTA A/B slot layout) is ported faithfully from
the original single-file wizard -- those are HW-confirmed and must not be re-litigated.
"""

__version__ = "0.1.0"
