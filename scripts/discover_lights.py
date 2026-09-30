"""List every light the Eufy Life light cloud exposes for an account.

Use this when a light (for example the E22 permanent outdoor lights) does not
show up in Home Assistant. The script prints the raw device-relation response,
the model code of every entry, the lights the integration discovered and the
model codes that still need verification.

Usage:
    python3 scripts/discover_lights.py [--country NL] [--no-raw]
    python3 scripts/discover_lights.py --debug --wait 30

If a light is discovered but shows no power/brightness, the device answered the
cloud inventory but sent no MQTT status frame yet. ``--debug`` prints the raw
MQTT traffic and the integration's log output, ``--wait`` listens longer and
``--handshake`` sends the 0200 session handshake first as an experiment.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import time
from typing import Any
from unittest.mock import MagicMock

# Mock Home Assistant and Voluptuous so the script can run without them
for _module in [
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.config_validation",
    "voluptuous",
]:
    sys.modules[_module] = MagicMock()

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import aiohttp  # noqa: E402

from custom_components.eufylife_api.cloud import (  # noqa: E402
    EufyLifeLightCloud,
    async_login,
)


def _relations(raw: Any) -> list[dict[str, Any]]:
    """Return the device entries of a device-relation response."""
    entries = raw.get("devices") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    devices = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        device = entry.get("device", entry)
        if isinstance(device, dict):
            devices.append(device)
    return devices



def _print_inventory(raw: Any, cloud: EufyLifeLightCloud) -> None:
    """Print the cloud inventory next to what the integration discovered."""
    entries = _relations(raw)
    print("\n=== MODELS IN THE ACCOUNT ===")
    if not entries:
        print("(no light device entries returned)")
    for device in entries:
        name = device.get("device_name") or "?"
        model = device.get("device_model") or "?"
        serial = device.get("device_sn") or "?"
        print(f"- {name}: model={model}, serial={serial}")

    print("\n=== DISCOVERED BY THE INTEGRATION ===")
    if not cloud.devices:
        print("(none)")
    for serial, device in cloud.devices.items():
        protocol = "modern" if device.animation_protocol else "generic"
        print(f"- {device.name} [{device.model}] {serial}")
        print(f"    cloud name: {device.model_name}")
        print(
            f"    protocol={protocol}, verified_model={device.known_model}, "
            f"session_handshake={device.session_handshake}, "
            f"power={device.is_on}, brightness={device.brightness}, "
            f"effect={device.effect}, "
            f"segments={device.lamp_count}, presets={len(device.effects)}"
        )

    unverified = [
        device for device in cloud.devices.values() if not device.known_model
    ]
    if unverified:
        print("\nUnverified model codes (using the generic control path):")
        for device in unverified:
            print(f"- {device.model} on {device.serial}")
        print("Please report these codes so their protocol can be verified.")
        print(
            "Verified family codes: T8L02 (E22 permanent outdoor lights), "
            "T8L30 (outdoor pathway lights), T8L40 (indoor floor lamp)."
        )

    skipped = [
        device
        for device in entries
        if not isinstance(device.get("device_sn"), str)
        or device["device_sn"] not in cloud.devices
    ]
    if skipped:
        print("\nCloud entries without a light entity:")
        for device in skipped:
            print(
                f"- model={device.get('device_model')!r}, "
                f"serial={device.get('device_sn')!r}"
            )
        print("Please report these entries as well.")


def _cloud_status(raw: Any) -> dict[str, str]:
    """Map each serial to the cloud's status flag and last contact age.

    The cloud keeps the last state a light reported, so the age of the newest
    parameter shows how long ago the device was last heard from.
    """
    status: dict[str, str] = {}
    now = time.time()
    for device in _relations(raw):
        serial = device.get("device_sn")
        if not isinstance(serial, str):
            continue
        updates = [
            param["update_time"]
            for param in device.get("params") or []
            if isinstance(param, dict) and isinstance(param.get("update_time"), int)
        ]
        if updates:
            age = f", last cloud update {(now - max(updates)) / 3600:.1f} h ago"
        else:
            age = ""
        status[serial] = f"device_status={device.get('device_status')}{age}"
    return status


async def main() -> int:
    """Discover and report the lights of the configured account."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="EufyLife email")
    parser.add_argument("--password", help="EufyLife password")
    parser.add_argument("--country", help="Country code (e.g. US, DE, NL)")
    parser.add_argument(
        "--no-raw",
        dest="raw",
        action="store_false",
        help="Do not print the raw JSON device list",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=10.0,
        help="Seconds to wait for the first MQTT status reports (default: 10)",
    )
    parser.add_argument(
        "--handshake",
        action="store_true",
        help="Send the 0200 session handshake before waiting (experiment)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print the raw MQTT traffic and the integration's log output",
    )
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger("custom_components.eufylife_api").setLevel(logging.DEBUG)

    email = args.email or os.environ.get("YOUR_EMAIL") or input("EufyLife Email: ")
    password = (
        args.password
        or os.environ.get("YOUR_PASSWORD")
        or getpass.getpass("EufyLife Password: ")
    )
    country = (
        args.country
        or os.environ.get("YOUR_COUNTRY")
        or input("Country Code (e.g. US, DE, NL): ")
        or "US"
    ).lower()

    async with aiohttp.ClientSession() as session:
        print("Logging in...")
        try:
            auth = await async_login(session, email, password, country)
        except Exception as err:
            print(f"Login failed: {err}")
            return 1

        cloud = EufyLifeLightCloud(
            session=session,
            user_id=auth["user_id"],
            user_center_id=auth["user_center_id"],
            user_center_token=auth["user_center_token"],
            openudid=f"discover-{os.getpid()}",
            country=country,
            language="en",
            timezone="UTC",
        )

        print("Discovering devices...")
        try:
            await cloud.async_start()
        except Exception as err:
            print(f"Discovery failed: {err}")
            await cloud.async_close()
            return 1

        # Watch what actually arrives on MQTT so a silent device is visible.
        client = cloud._mqtt
        topics: list[str] = []
        if client is not None:
            handle_message = cloud._on_message

            def _record(_client: Any, _userdata: Any, message: Any) -> None:
                topics.append(f"{message.topic} ({len(message.payload)} bytes)")
                handle_message(_client, _userdata, message)

            client.on_message = _record

        if args.handshake:
            for serial, device in cloud.devices.items():
                if device.animation_protocol:
                    print(f"Sending the 0200 session handshake to {serial}...")
                    await cloud.async_handshake(serial)

        # Wait for the first MQTT status reports so power/brightness show up.
        waited = 0.0
        while waited < args.wait:
            if cloud.connected and all(
                device.is_on is not None for device in cloud.devices.values()
            ):
                break
            await asyncio.sleep(0.5)
            waited += 0.5

        link = client.is_connected() if client is not None else None
        print(
            f"\nMQTT status after {waited:.1f}s: connected={cloud.connected}, "
            f"link={link}, messages={len(topics)}"
        )
        for topic in topics[-10:]:
            print(f"    received: {topic}")
        for serial, device in cloud.devices.items():
            print(
                f"    {serial}: online={device.online}, power={device.is_on}, "
                f"brightness={device.brightness}, segments={device.lamp_count}"
            )

        try:
            raw = await cloud._async_post(
                "/app/devicerelation/get_device_list", {}
            )
        except Exception as err:
            print(f"Could not re-read the raw device list: {err}")
            raw = None

        if args.raw and raw is not None:
            print("\n=== RAW CLOUD DEVICE LIST ===")
            print(json.dumps(raw, indent=2, sort_keys=True))

        _print_inventory(raw, cloud)
        silent = [
            device.serial for device in cloud.devices.values() if device.is_on is None
        ]
        if silent:
            print(
                "\nNo MQTT status report arrived for: " + ", ".join(silent)
                + "\nA light that is switched off at the mains or offline answers"
                " nothing at all, so check that it is powered and online in the"
                " app first. Re-run with --debug to see the raw traffic,"
                " --wait 30 to listen longer, or --handshake to try the 0200"
                " session handshake first."
            )
            status = _cloud_status(raw)
            for serial in silent:
                if serial in status:
                    print(f"    {serial}: cloud {status[serial]}")
        await cloud.async_close()

    if not cloud.devices:
        print(
            "\nNo light entities were created. If your light only appears in the "
            "eufy Security app it is not part of the Eufy Life light cloud, which "
            "this integration talks to."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

