import asyncio
import getpass
import json
import aiohttp
import sys
import os
import logging
import argparse
from unittest.mock import MagicMock

# Mock Home Assistant and Voluptuous so the script can run without them
for m in [
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
    sys.modules[m] = MagicMock()

# Add custom_components to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from custom_components.eufylife_api.cloud import (
    _PERSONAL_SCENES_PATH,
    _parse_effects,
    async_login,
    EufyLifeLightCloud,
)

def _preset_command(device, preset: dict) -> str:
    """The command async_set_effect sends for a catalog preset.

    Catalog entries are parsed into two shapes: a layered animation frame with
    the catalog's own params JSON, or a classic dynamic effect with its palette.
    """
    if device.animation_protocol and "params" in preset:
        return "animation frame (0x02 0x0D)"
    if "dynamic" in preset:
        return "dynamic effect (0x02 0x06)"
    return "classic params (0x02 0x10)"


def _print_device_state(device) -> None:
    """Print every cached field, including what the device reported as active.

    The unsolicited (0x02, 0x04) report carries the running effect's cloud id in
    0xA6, which is what the integration maps back to an effect name; printing the
    whole cache makes it possible to compare it with whatever the app just set.
    """
    print(f"\nState for {device.name} ({device.serial}, {device.model}):")
    print(
        f"  power={device.is_on} brightness={device.brightness} "
        f"online={device.online}"
    )
    print(f"  segments={device.lamp_count} mode/light_id={device.light_id}")
    print(f"  effect={device.effect!r}")
    if device.effect and device.effect in device.effects:
        entry = json.dumps(device.effects[device.effect], sort_keys=True)
        print(f"  catalog entry={entry}")
    print(f"  colors={device.colors}")
    print(f"  rgb={device.rgb_color} rgbww={device.rgbww_color}")
    print(f"  speed={device.speed} direction={device.direction}")
    print(f"  known effects={len(device.effects)}")


def _pick_device(cloud, device_list, serial: str | None):
    """The device a menu-free mode works on, or None with the reason printed.

    The serial picks one when the account has several lights; a single light is
    used as it is, which is what the interactive menu does with a chosen number.
    """
    if serial:
        device = cloud.devices.get(serial)
        if device is None:
            print(f"Device with serial {serial} not found.")
        return device
    if len(device_list) == 1:
        return device_list[0]
    print("Multiple devices found; use --test-serial to select one.")
    return None


def _catalog_report(device, raw_catalog) -> None:
    """Report every catalog entry, the command it is sent with, and what is skipped.

    Entries the integration cannot encode are the presets the app shows but Home
    Assistant hides, so their JSON is printed in full for an encoder to be written.
    """
    if not isinstance(raw_catalog, dict) or not isinstance(raw_catalog.get("list"), list):
        print("Unexpected catalog shape:")
        print(json.dumps(raw_catalog, indent=2)[:2000])
        return
    print(f"Catalog top-level keys: {sorted(raw_catalog)}")
    skipped = []
    supported = 0
    for category in raw_catalog["list"]:
        if not isinstance(category, dict):
            continue
        print(
            f"\n[{category.get('name')}] category keys={sorted(category)} "
            f"scenes={len(category.get('scene_info') or [])}"
        )
        for scene in category.get("scene_info") or []:
            if not isinstance(scene, dict):
                continue
            print(
                f"  scene_id={scene.get('scene_id')} name={scene.get('name')!r} "
                f"keys={sorted(scene)}"
            )
            for preset in scene.get("light") or []:
                if not isinstance(preset, dict):
                    print(f"    ! {preset!r}")
                    continue
                name = preset.get("name")
                # Classify with the real parser so this report cannot drift
                # from what the integration does with the same entry.
                parsed = _parse_effects(
                    {
                        "list": [
                            {
                                "scene_info": [
                                    {"scene_id": scene.get("scene_id"), "light": [preset]}
                                ]
                            }
                        ]
                    }
                )
                if isinstance(name, str) and name in parsed:
                    supported += 1
                    print(
                        f"    {name}: light_id={preset.get('light_id')} -> "
                        f"{_preset_command(device, parsed[name])}"
                    )
                else:
                    print(
                        f"    {name!r}: SKIPPED, light_id={preset.get('light_id')}, "
                        f"keys={sorted(preset)}"
                    )
                    skipped.append((name, preset))

    if not skipped:
        print(f"\nAll {supported} catalog entries are supported.")
        return
    print(f"\n{len(skipped)} catalog entry/entries are skipped by the parser:")
    for name, preset in skipped:
        body = json.dumps(preset, indent=2, sort_keys=True)
        if len(body) > 2000:
            body = body[:2000] + "\n...(truncated)"
        print(f"\n--- {name} ---")
        print(body)


# The app's own "Persoonlijk" scenes are account data: the phone builds them and
# applies one as a palette plus a cloud id that the light-mode catalog does not
# own. The search for them started from the catalog call the app's effect list is
# built from, varying both of its filters, and probed the sibling paths a
# user-owned mode list could live on. The route that answered is the app's own
# scene list, which wants a page index and names that missing field when it is
# left out:
#
#   /app/light/diy/list {"sns": [serial], "start_index": 0}
#     -> 400 'field "start_index" is not set'  (route exists, body was short)
#     -> 200 {"is_more": 1, "list": [{"name": "kerst", "light_id": 171200, ...}]}
#
# The integration reads that route itself (cloud.async_fetch_personal_scenes), so
# the effect list carries those scenes by name; this script only reports what the
# call returned and cross-checks it against what the light reported.
_CATALOG_PATH = "/app/light/lightmode/list"
# What the E22 reported while the app applied two of these scenes. In a report A4
# is the mode id the scene was built from (`20002`, `30011`) and A6 the account's
# own cloud id (`171200`, `168622`), which no catalog entry owns and which is the
# only thing that identifies a personal scene: the report carries no palette and
# no name, so the list has to be the source of both.
_PERSONAL_SCENE_IDS = (171200, 168622)
_PERSONAL_MODE_IDS = (20002, 30011)


def _personal_scene_note() -> None:
    """Say where the app's own scenes are loaded from.

    The app's `Persoonlijk` tab looks like part of the same list because it sits
    next to the catalog presets, so a preset listing says out loud that this is
    the catalog and that the other list is read from its own route.
    """
    print(
        "\nThese are the default scenes of the cloud's light-mode catalog. The\n"
        f"app's own (Persoonlijk) scenes are account data from {_PERSONAL_SCENES_PATH},\n"
        "which the integration reads as well, so their names are part of this\n"
        "effect list: 'c' prints them with the cloud id the light reports back\n"
        "while one runs, and 'w' watches the frames the app writes for one.\n"
        "The same two are --personal-scenes and --watch-app without the menu."
    )


def _catalog_entry_names(payload) -> list[str]:
    """Every entry name of a light-mode catalog, in payload order."""
    names = []
    if not isinstance(payload, dict):
        return names
    for category in payload.get("list") or []:
        if not isinstance(category, dict):
            continue
        for scene in category.get("scene_info") or []:
            if not isinstance(scene, dict):
                continue
            for preset in scene.get("light") or []:
                if isinstance(preset, dict) and isinstance(preset.get("name"), str):
                    names.append(preset["name"])
    return names


def _scene_colours(params: dict) -> str:
    """The palette of a parsed scene as hex, so it can be compared with the app."""
    return " ".join(
        "".join(f"{channel:02x}" for channel in color)
        for color in params.get("colors") or []
    )


def _scene_owners(scenes: dict, key: str, value: int) -> str:
    """The names of the scenes carrying an id, or a note that none does."""
    names = sorted(name for name, params in scenes.items() if params.get(key) == value)
    return ", ".join(names) if names else "not in this list"


async def _personal_scene_search(cloud, device) -> None:
    """Print the app's own scenes with the ids the light reports for them.

    This is the call the integration loads its own effect list from, so each scene
    is printed by name, with the cloud id the effect has to be applied with and
    the mode id the light renders it as. Those are the two ids a captured report
    carried, so they are cross-checked against the report here.
    """
    serial = device.serial
    print("\n--- App-built (Persoonlijk) scenes ---")
    print(f"Reading {_PERSONAL_SCENES_PATH} for {serial}...")
    try:
        scenes = await cloud.async_fetch_personal_scenes(serial)
    except Exception as err:
        print(f"{_PERSONAL_SCENES_PATH}: FAILED: {type(err).__name__}: {err}")
        return
    if not scenes:
        print(
            "The account has no scenes of its own, so the cloud catalog is all "
            "this light offers by name."
        )
        return
    try:
        catalog = set(
            _catalog_entry_names(
                await cloud._async_post(
                    _CATALOG_PATH,
                    {"sns": [serial], "light_type": None, "scene_id": None},
                )
            )
        )
    except Exception as err:
        print(f"{_CATALOG_PATH} (catalog) unavailable for comparison: {err}")
        catalog = set()
    for name, params in sorted(scenes.items()):
        where = "catalog name kept" if name in catalog else "account data"
        print(
            f"- {name} [{where}]: cloud id {params['light_id']}, mode "
            f"{params['dynamic']}, type {params['light_type']}, speed "
            f"{params['speed']}, colours {_scene_colours(params)}"
        )
    print(f"\n{len(scenes)} app-built scene(s); the effect list offers them by name.")
    print("Cross-check against the ids the light reported while they ran:")
    for value in _PERSONAL_SCENE_IDS:
        print(f"  cloud id {value}: {_scene_owners(scenes, 'light_id', value)}")
    for value in _PERSONAL_MODE_IDS:
        print(f"  mode id {value}: {_scene_owners(scenes, 'dynamic', value)}")


class _AppFrameWatcher(logging.Handler):
    """Print the phone's frames and the light's own frames while it is attached.

    cloud.py already decodes a frame the Eufy app publishes and logs it as
    ``Eufy app frame for <serial>: opcode=…, payload=…``; the light's own frames
    are logged as ``Frame received for <serial>``. Watching those two lines keeps
    this mode out of the integration and reads the same contract the README
    documents, and the counts it keeps let the watch end with a verdict instead of
    a log that has to be read either way.
    """

    _APP_PREFIX = "Eufy app frame for "
    _LIGHT_PREFIX = "Frame received for "

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.app_frames = 0
        self.light_frames = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # A log handler must never break the logging call.
            return
        if message.startswith(self._APP_PREFIX):
            self.app_frames += 1
            print(f"  >> APP   {message}", flush=True)
        elif message.startswith(self._LIGHT_PREFIX):
            self.light_frames += 1
            print(f"  >> LIGHT {message}", flush=True)


async def _watch_app_frames(cloud, device, seconds: int) -> None:
    """Print what the phone writes while a scene is applied from the app.

    The scene the app builds itself is the one the cloud search has not answered
    for, so the frame the phone publishes for it is the reference for writing it
    from Home Assistant. A tap the app fulfils over BLE or WLAN produces no frame
    at all, so the watch reports how many app frames arrived rather than leaving a
    quiet log to be read either way, and it ends by asking the light for its state
    so the ids it reports for the scene just applied are printed too.
    """
    watcher = _AppFrameWatcher()
    logger = logging.getLogger("custom_components.eufylife_api.cloud")
    # The watched lines are INFO, so the watch raises the logger's own level for
    # its duration: a session that runs with a higher level still prints the frame
    # a capture is after, and the level it found is restored on the way out.
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(watcher)
    print(f"\n--- Watching frames for {seconds}s ---")
    print(
        "On the phone: open this light, switch to the app's own (Persoonlijk)\n"
        "scenes and tap one. '>> APP' lines are what the phone publishes, which is\n"
        "the frame to encode; '>> LIGHT' lines are the light's own frames."
    )
    try:
        await asyncio.sleep(seconds)
        cloud.request_settings(device.serial)
        await asyncio.sleep(3)
    finally:
        logger.removeHandler(watcher)
        logger.setLevel(previous_level)
    print(
        f"--- End of watch: {watcher.app_frames} app frame(s), "
        f"{watcher.light_frames} light frame(s) ---"
    )
    if watcher.app_frames:
        print(
            "The '>> APP' payloads are the frames the integration does not build "
            "itself yet: the opcode and the payload hex are enough to encode one."
        )
    else:
        print(
            "No app frame arrived, so the phone did not command this light over "
            "MQTT for that tap; it used BLE, WLAN or the cloud. A light frame "
            "without an app frame means the scene was applied elsewhere while the "
            "watch ran."
        )
    _print_device_state(device)


async def validate():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", help="EufyLife email")
    parser.add_argument("--password", help="EufyLife password")
    parser.add_argument("--country", help="Country code (e.g. US, DE)")
    parser.add_argument(
        "--animation",
        help='Animation preset name, for example "Guy Fawkes Night"',
    )
    parser.add_argument(
        "--list-animations",
        action="store_true",
        help="List the named animations available on the selected device",
    )
    parser.add_argument(
        "--personal-scenes",
        action="store_true",
        help="Print the app's own (Persoonlijk) scenes and their cloud ids",
    )
    parser.add_argument(
        "--watch-app",
        nargs="?",
        const=60,
        type=int,
        metavar="SECONDS",
        help="Print the frames the app publishes while a scene is applied (default 60)",
    )
    parser.add_argument("--test-opcode", help=argparse.SUPPRESS)
    parser.add_argument("--test-tags", help=argparse.SUPPRESS)
    parser.add_argument("--test-tlv", help=argparse.SUPPRESS)
    parser.add_argument("--test-version", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--test-serial",
        "--serial",
        dest="test_serial",
        help="Serial of the device to control (required when multiple devices are found)",
    )
    parser.add_argument("--skip-envelope", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--test-compound", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
    
    email = args.email or os.environ.get("YOUR_EMAIL") or input("EufyLife Email: ")
    password = args.password or os.environ.get("YOUR_PASSWORD") or getpass.getpass("EufyLife Password: ")
    country = (
        args.country
        or os.environ.get("YOUR_COUNTRY")
        or input("Country Code (e.g. US, DE, GB): ")
        or "US"
    ).lower()

    async with aiohttp.ClientSession() as session:
        print("\nLogging in...")
        try:
            auth = await async_login(session, email, password, country)
        except Exception as e:
            print(f"Login failed: {e}")
            return

        print("Login successful! Discovering devices...")
        
        cloud = EufyLifeLightCloud(
            session=session,
            user_id=auth["user_id"],
            user_center_id=auth["user_center_id"],
            user_center_token=auth["user_center_token"],
            openudid=f"validate-{os.getpid()}",
            country=country,
            language="en",
            timezone="UTC"
        )
        
        try:
            await cloud.async_start()
        except Exception as e:
            print(f"Discovery failed: {e}")
            return

        if not cloud.devices:
            print("No supported lights found in your account.")
            await cloud.async_close()
            return

        print(f"\nFound {len(cloud.devices)} light(s).")
        print("Connecting to MQTT and fetching status (this may take a few seconds)...")
        
        # Wait up to 10 seconds for MQTT and first status report
        for _ in range(10):
            if cloud.connected and all(d.is_on is not None for d in cloud.devices.values()):
                break
            await asyncio.sleep(1)
            
        device_list = list(cloud.devices.values())
        if not device_list:
            print("No devices found.")
            await cloud.async_close()
            return

        if args.animation or args.list_animations:
            device = None
            if args.test_serial:
                device = cloud.devices.get(args.test_serial)
                if not device:
                    print(f"Device with serial {args.test_serial} not found.")
                    await cloud.async_close()
                    return
            elif len(device_list) == 1:
                device = device_list[0]
            else:
                print("Multiple devices found; use --test-serial to select one.")
                await cloud.async_close()
                return

            if args.list_animations:
                print(f"\nAnimations for {device.name}:")
                for name, preset in device.effects.items():
                    print(f"- {name}  [{_preset_command(device, preset)}]")
                _personal_scene_note()
                await cloud.async_close()
                return

            animation_name = next(
                (name for name in device.effects if name.casefold() == args.animation.casefold()),
                None,
            )
            if animation_name is None:
                print(f'Animation not found: "{args.animation}"')
                print("Available animations:")
                for name, preset in device.effects.items():
                    print(f"- {name}  [{_preset_command(device, preset)}]")
                _personal_scene_note()
                await cloud.async_close()
                return

            print(f"Setting animation: {animation_name} on {device.name}...")
            await cloud.async_set_effect(device.serial, effect=animation_name, refresh=False)
            print("Animation command sent successfully.")
            await cloud.async_close()
            return

        if args.personal_scenes or args.watch_app is not None:
            # The two ways to see the app's own scenes: 'c' prints the account list
            # the integration already loads, the watch reads the frame the phone
            # writes when one is tapped. Both run in one command when both flags
            # are given.
            device = _pick_device(cloud, device_list, args.test_serial)
            if device is None:
                await cloud.async_close()
                return
            if args.personal_scenes:
                await _personal_scene_search(cloud, device)
            if args.watch_app is not None:
                await _watch_app_frames(cloud, device, max(1, args.watch_app))
            await cloud.async_close()
            return

        if args.test_opcode:
            device = None
            if args.test_serial:
                device = cloud.devices.get(args.test_serial)
                if not device:
                    print(f"Device with serial {args.test_serial} not found.")
                    await cloud.async_close()
                    return
            else:
                device = device_list[0]
            
            print(f"\n--- AUTO TEST: {device.name} [{device.serial}] ---")
            opcode = (int(args.test_opcode[:2], 16), int(args.test_opcode[2:], 16))
            tlv_format = args.test_tlv or "1"
            
            def make_tlv(tag, val):
                if tlv_format == '0': return val
                if tlv_format == '1':
                    if len(val) > 255: return bytes([tag, len(val) & 0xFF]) + val
                    return bytes([tag, len(val)]) + val
                if tlv_format == '2L': return bytes([tag]) + len(val).to_bytes(2, "little") + val
                if tlv_format == '2B': return bytes([tag]) + len(val).to_bytes(2, "big") + val
                if tlv_format == 'M':
                    l = len(val)
                    res = b""
                    while True:
                        byte = l & 0x7F
                        l >>= 7
                        if l > 0: byte |= 0x80
                        res += bytes([byte])
                        if l == 0: break
                    return bytes([tag]) + res + val
                if tlv_format == 'A':
                    if len(val) < 256: return bytes([tag, len(val)]) + val
                    return bytes([tag]) + len(val).to_bytes(2, "little") + val
                return b""

            from custom_components.eufylife_api.cloud import _command_payload
            cmd_payload = b""
            for part in args.test_tags.split(';'):
                t_hex, t_type, t_val = part.split('|', 2)
                tag = int(t_hex, 16)
                if t_type == 'hex': val = bytes.fromhex(t_val)
                elif t_type == 'str': val = t_val.encode()
                elif t_type == 'int2L': val = int(t_val).to_bytes(2, "little")
                elif t_type == 'int4L': val = int(t_val).to_bytes(4, "little")
                cmd_payload += make_tlv(tag, val)

            if args.skip_envelope:
                full_payload = cmd_payload
            else:
                full_payload = _command_payload(cloud._user_id, cmd_payload)
            print(f"Sending {opcode} (v{args.test_version or 0}, skip_envelope={args.skip_envelope})...")
            cloud._publish(device.serial, opcode, full_payload, version=args.test_version or 0)

            if args.test_compound:
                print("Waiting 1s after handshake...")
                await asyncio.sleep(1)
                print("Sending Compound Effect (Guy Fawkes Night)...")
                # This will use the logic in cloud.py for T8L40 effects (Nested Envelope)
                await cloud.async_set_effect(device.serial, effect="Guy Fawkes Night", refresh=False)
                print("Compound sequence complete!")

            print("Waiting 5s then closing.")
            await asyncio.sleep(5)
            await cloud.async_close()
            return

        while True:
            print("\n" + "="*40)
            print("DEVICES")
            print("="*40)
            for i, device in enumerate(device_list):
                status = "ON" if device.is_on else "OFF"
                online = "Online" if device.online else "Offline"
                brightness = f"{device.brightness}%" if device.brightness is not None else "??%"
                lamp_info = f", {device.lamp_count} segments" if device.lamp_count else ""
                print(f"{i+1}. {device.name} [{device.model}]")
                print(f"   State: {status}, Brightness: {brightness}, Connectivity: {online}{lamp_info}")
                print(f"   Serial: {device.serial}")
            
            print("\nOptions:")
            print(f"1-{len(device_list)}. Control device (enter its number)")
            print("r.   Refresh status")
            print("q.   Quit")
            
            choice = input("\nChoice: ").strip().lower().rstrip(".)")
            if choice == 'q':
                break
            if choice == 'r':
                for device in device_list:
                    cloud.request_settings(device.serial)
                print("Refresh requested...")
                await asyncio.sleep(1)
                continue
            
            try:
                idx = int(choice) - 1
                if not (0 <= idx < len(device_list)):
                    raise ValueError()
                device = device_list[idx]
            except ValueError:
                print(f"Invalid choice; enter 1-{len(device_list)}, 'r' or 'q'.")
                continue

            while True:
                print(f"\n--- Controlling: {device.name} ---")
                print("1. Toggle Power")
                print("2. Set Brightness (0-100)")
                print("3. Set RGB/RGBWW Color")
                print("4. Set Effect (Preset, Speed, Direction)")
                print("5. Set Segmented Colors (DIY)")
                print("a. Advanced tools")
                print("b. Back to main menu")
                
                action = input("\nAction: ").strip().lower()
                if action == 'b':
                    break
                
                try:
                    if action == '1':
                        new_state = not device.is_on
                        print(f"Turning {'ON' if new_state else 'OFF'}...")
                        cloud.set_power(device.serial, new_state)
                        await asyncio.sleep(1)
                    elif action == 'a':
                        advanced_action = input(
                            "\nAdvanced tools: s=scene ID, d=catalog report, "
                            "c=personal scenes, j=raw catalog JSON, "
                            "p=current device state, t=raw LightShow test, "
                            "w=watch app frames, b=back: "
                        ).strip().lower().rstrip(".)")
                        if advanced_action == 'b':
                            continue
                        if advanced_action == 'p':
                            _print_device_state(device)
                            continue
                        if advanced_action == 'd':
                            print("\nFetching effects catalog...")
                            raw_catalog = await cloud._async_post(
                                _CATALOG_PATH,
                                {"sns": [device.serial], "light_type": None, "scene_id": None},
                            )
                            _catalog_report(device, raw_catalog)
                            print("\n--- End of Catalog ---")
                            continue
                        if advanced_action == 'c':
                            await _personal_scene_search(cloud, device)
                            continue
                        if advanced_action == 'w':
                            # The app's list already names these scenes; this shows
                            # the frame the phone writes for one, which is the
                            # reference for the mode the light renders it with.
                            seconds = input(
                                "Watch the app's frames for how many seconds? "
                                "(default 60): "
                            ).strip()
                            await _watch_app_frames(
                                cloud,
                                device,
                                int(seconds) if seconds.isdigit() else 60,
                            )
                            continue
                        if advanced_action == 'j':
                            print("\nFetching raw effects catalog...")
                            raw_catalog = await cloud._async_post(
                                _CATALOG_PATH,
                                {"sns": [device.serial], "light_type": None, "scene_id": None},
                            )
                            print(json.dumps(raw_catalog, indent=2))
                            print("\n--- End of Catalog ---")
                            continue
                        if advanced_action == 's':
                            # The app's own scenes carry the ids the light reported
                            # for them — mode id in A4, personal cloud id in A6 —
                            # so keep both at hand: a catalog entry is written by
                            # its light id, these are not catalog entries at all.
                            print(
                                "Known personal scene ids — cloud: 171200 (kerst), "
                                "168622 (disco 1); mode: 20002 (kerst), 30011 (disco 1)."
                            )
                            scene_id = int(input("Enter scene ID: "))
                            print(f"Setting scene {scene_id}...")
                            await cloud.async_set_scene(device.serial, scene_id, refresh=False)
                            print("Scene command sent successfully.")
                            continue
                        if advanced_action != 't':
                            print("Invalid advanced action.")
                            continue

                        print("\n--- LightShowCmd Hypothesis Test ---")
                        opcode_input = input("Enter opcode (e.g. 0206, 0210, 0211): ")
                        opcode = (int(opcode_input[:2], 16), int(opcode_input[2:], 16))
                        l_id = input("Enter light_id (e.g. 10494, skip with 's'): ")
                        params = input("Enter params (JSON or hex): ")
                        if params.startswith('{'):
                            val_to_send = params.encode()
                        else:
                            val_to_send = bytes.fromhex(params)

                        tlv_format = input("TLV format? (0=No TLV, 1=1-byte len, 2L=2-byte LE, 2B=2-byte BE, M=MQTT-varint): ")

                        def make_tlv(tag, val):
                            if tlv_format == '0': return val
                            if tlv_format == '1':
                                if len(val) > 255:
                                    print(f"Warning: Tag {tag:02X} value too long for 1-byte length ({len(val)} bytes). Overflowing...")
                                    return bytes([tag, len(val) & 0xFF]) + val
                                return bytes([tag, len(val)]) + val
                            if tlv_format == '2L': return bytes([tag]) + len(val).to_bytes(2, "little") + val
                            if tlv_format == '2B': return bytes([tag]) + len(val).to_bytes(2, "big") + val
                            if tlv_format == 'M':
                                l = len(val)
                                res = b""
                                while True:
                                    byte = l & 0x7F
                                    l >>= 7
                                    if l > 0: byte |= 0x80
                                    res += bytes([byte])
                                    if l == 0: break
                                return bytes([tag]) + res + val
                            return b""

                        from custom_components.eufylife_api.cloud import _command_payload

                        cmd_payload = b""
                        while True:
                            tag_in = input("Enter tag (hex, e.g. A3, or 'done'): ")
                            if tag_in == 'done': break
                            tag = int(tag_in, 16)
                            val_type = input("Value type? (hex, str, int2L, int4L): ")
                            if val_type == 'hex': val = bytes.fromhex(input("Value (hex): "))
                            elif val_type == 'str': val = input("Value (string): ").encode()
                            elif val_type == 'int2L': val = int(input("Value (int): ")).to_bytes(2, "little")
                            elif val_type == 'int4L': val = int(input("Value (int): ")).to_bytes(4, "little")

                            cmd_payload += make_tlv(tag, val)

                        full_payload = _command_payload(cloud._user_id, cmd_payload)
                        print(f"Sending {opcode}...")
                        cloud._publish(device.serial, opcode, full_payload)
                        print("Command sent! Check if the light changed.")
                    elif action == '2':
                        val = input("Enter brightness (0-100): ")
                        brightness = int(val)
                        print(f"Setting brightness to {brightness}%...")
                        cloud.set_power(device.serial, device.is_on, brightness)
                        await asyncio.sleep(1)
                    elif action == '3':
                        if not device.lamp_count:
                            print("Error: Lamp segment count unknown yet. Try refreshing or toggling power first.")
                            continue
                        print("Enter RGB (3 values) or RGBWW (5 values) separated by commas.")
                        val = input("Enter values (e.g., 255,128,0 or 255,0,0,255,0): ")
                        color = tuple(map(int, val.split(',')))
                        if len(color) not in (3, 5):
                            raise ValueError("Need 3 or 5 comma-separated values (0-255)")
                        print(f"Setting color to {color}...")
                        if len(color) == 3:
                            await cloud.async_set_effect(device.serial, rgb_color=color)
                        else:
                            await cloud.async_set_effect(device.serial, rgbww_color=color)
                        print("Success!")
                    elif action == '4':
                        if not device.effects:
                            print("No presets found for this device.")
                            continue
                        effect_list = list(device.effects.keys())
                        print("\nAvailable Presets:")
                        for i, name in enumerate(effect_list):
                            print(f"{i+1}. {name}")
                        _personal_scene_note()
                        e_choice = input("\nSelect preset number: ")
                        e_idx = int(e_choice) - 1
                        effect_name = effect_list[e_idx]

                        speed_val = input("Enter speed (1-10, leave empty for default): ")
                        speed = int(speed_val) if speed_val.strip() else None

                        dir_val = input("Enter direction (0-1, leave empty for default): ")
                        direction = int(dir_val) if dir_val.strip() else None

                        ref = input("Refresh settings after? (y/n): ").lower() == 'y'

                        print(f"Setting preset: {effect_name}...")
                        await cloud.async_set_effect(device.serial, effect=effect_name, speed=speed, direction=direction, refresh=ref)
                        print("Success!")
                    elif action == '5':
                        if not device.lamp_count:
                            print("Error: Lamp count unknown.")
                            continue
                        print(f"Enter {device.lamp_count} colors. Use R,G,B or R,G,B,W,C for each.")
                        colors = []
                        for i in range(device.lamp_count):
                            val = input(f"Segment {i+1}: ")
                            color = tuple(map(int, val.split(',')))
                            colors.append(color)
                        await cloud.async_set_effect(device.serial, colors=colors)
                        print("Success!")
                    else:
                        print("Invalid action.")
                except Exception as e:
                    print(f"Error: {e}")
                
                # Update local device reference to see changes
                device = cloud.devices[device.serial]

        await cloud.async_close()
        print("\nClosed. Goodbye!")

if __name__ == "__main__":
    try:
        asyncio.run(validate())
    except KeyboardInterrupt:
        pass
