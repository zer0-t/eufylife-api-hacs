"""A local web studio for building Eufy light scenes.

A scene saved here is a scene the Eufy app shows under "Persoonlijk" as well: the
integration writes to the same cloud the app does, and it offers whatever is
saved as an effect by name straight away. This is the browser front end for that —
a segment grid, a palette, the catalog's modes, the app's AI LightChat tab and the
live state the light reports (asked for again every minute, so a page left open
shows the lamp rather than the last frame the studio wrote) — and it reuses the
integration's own client, so it drives the very code the services and the light
entity use.

It is a development tool rather than part of the integration: Home Assistant
installs the component's requirements.txt, so Flask lives in
``scripts/requirements-dev.txt`` and the Home Assistant symbols the package
imports are stubbed before the import, exactly as ``scripts/validate_lights.py``
does it.

Usage::

    pip install -r scripts/requirements-dev.txt
    set YOUR_EMAIL=you@example.com
    set YOUR_PASSWORD=...
    set YOUR_COUNTRY=NL
    python scripts/scene_studio.py
    # then open http://127.0.0.1:8765

The password is read from the environment, from the repository's git-ignored
``.env``, or from an interactive prompt — never from the command line. The studio
binds to the loopback address only.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from functools import partial
from typing import Any
from unittest.mock import MagicMock

# The package's __init__ imports Home Assistant, which is not installed here, so
# the names it needs are stubbed before the package is imported.
for _module in (
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.update_coordinator",
    "voluptuous",
):
    sys.modules.setdefault(_module, MagicMock())

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import aiohttp  # noqa: E402

try:
    from flask import Flask, jsonify, render_template, request
except ImportError as err:  # pragma: no cover - only reached without the dev extras
    raise SystemExit(
        "Flask is what serves this studio: pip install -r scripts/requirements-dev.txt"
    ) from err

from custom_components.eufylife_api.cloud import (  # noqa: E402
    EufyLifeCloudError,
    EufyLifeLightCloud,
    EufyLifeLightDevice,
    async_login,
)

_LOGGER = logging.getLogger("scene_studio")
_PORT = 8765
_COLUMNS = 10
_CALL_TIMEOUT = 60
# How long a read waits for a light to report once it has been asked for its
# settings. The report is a round trip through the cloud's own broker and answers
# well inside this window here, so the wait only means a sleeping light cannot hold
# a page load or a poll open for longer than this.
_SETTINGS_TIMEOUT = 2.0



def _read_env_file(path: str) -> dict[str, str]:
    """Read a small ``KEY=VALUE`` file, the way a git-ignored ``.env`` looks."""
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _credentials() -> tuple[str, str, str]:
    """Email, password and country, never taken from ``sys.argv``."""
    stored = _read_env_file(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    )
    for key in ("YOUR_EMAIL", "YOUR_PASSWORD", "YOUR_COUNTRY"):
        if os.environ.get(key):
            stored[key] = os.environ[key]
    email = stored.get("YOUR_EMAIL") or input("EufyLife Email: ")
    password = stored.get("YOUR_PASSWORD") or getpass.getpass("EufyLife Password: ")
    country = (stored.get("YOUR_COUNTRY") or "NL").lower()
    return email, password, country


def _color_hex(color: tuple[int, ...]) -> str:
    """One stored colour as the ``#rrggbb`` the browser works with."""
    return "#" + bytes(tuple(color[:3])).hex()


def _hex_to_color(value: str) -> tuple[int, int, int]:
    """One ``#rrggbb`` from the browser as an RGB tuple."""
    text = str(value).strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"{value!r} is not a #rrggbb colour")
    try:
        return tuple(bytes.fromhex(text))  # type: ignore[return-value]
    except ValueError as err:
        raise ValueError(f"{value!r} is not a #rrggbb colour") from err


def _palette_for(
    payload: dict[str, Any], device: EufyLifeLightDevice
) -> list[tuple[int, ...]]:
    """The palette a request asks for, addressed to every segment of one light.

    A write carries one colour per segment, so a palette shorter than the strip is
    repeated along it and a longer one is cut to it.
    """
    raw = payload.get("colors") or []
    colors = [_hex_to_color(str(value)) for value in raw]
    if not colors:
        raise ValueError("This call needs a palette")
    count = device.lamp_count
    if not count:
        raise ValueError("Waiting for the light's lamp count")
    if len(colors) >= count:
        return colors[:count]
    return [colors[index % len(colors)] for index in range(count)]


def _design_json(design: dict[str, Any]) -> dict[str, Any]:
    """An AI design as JSON the page can show."""
    return {
        **{key: value for key, value in design.items() if key != "colors"},
        "colors": [_color_hex(color) for color in design["colors"]],
    }



class CloudLink:
    """The integration's cloud client, on a loop of its own.

    The client keeps an aiohttp session and paho's MQTT loop, both bound to one
    event loop, while Flask answers each request on its own thread. So the loop
    runs in a background thread here and every call is handed to it, which also
    means the studio drives exactly the code the services drive.
    """

    def __init__(self, email: str, password: str, country: str) -> None:
        self._email = email
        self._password = password
        self._country = country
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cloud: EufyLifeLightCloud | None = None
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._serve, name="eufylife-cloud", daemon=True
        )

    def start(self) -> None:
        """Start the loop and wait until the account and the lights are connected."""
        self._thread.start()
        if not self._ready.wait(timeout=_CALL_TIMEOUT):
            raise RuntimeError("The cloud client did not come up in time")
        if self._failure is not None:
            raise RuntimeError(f"The cloud client failed to start: {self._failure}")

    def _serve(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as err:  # noqa: BLE001 - reported through start()
            self._failure = err
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        session = aiohttp.ClientSession()
        auth = await async_login(session, self._email, self._password, self._country)
        self._cloud = EufyLifeLightCloud(
            session=session,
            user_id=auth["user_id"],
            user_center_id=auth["user_center_id"],
            user_center_token=auth["user_center_token"],
            openudid=f"scene-studio-{os.getpid()}",
            country=self._country,
            language="en",
            timezone="UTC",
        )
        await self._cloud.async_start()
        await self._await_reports()
        self._ready.set()
        # The MQTT and HTTP work happens in this loop until the process ends.
        await asyncio.Event().wait()

    async def _await_reports(self, timeout: float = 15.0) -> None:
        """Give the lights a moment to report their own settings.

        A light answers with its segment count, brightness and power on its own
        schedule once the MQTT link is up, and the segment count is what a grid
        and a palette write need, so the page is not served against a light that
        has not spoken yet. A light that stays silent is only logged: the rest of
        the account still works.
        """
        assert self._cloud is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(device.lamp_count for device in self._cloud.devices.values()):
                return
            await asyncio.sleep(0.25)
        silent = [
            device.name for device in self._cloud.devices.values() if not device.lamp_count
        ]
        _LOGGER.warning(
            "These lights have not reported their settings yet: %s", ", ".join(silent)
        )

    @property
    def cloud(self) -> EufyLifeLightCloud:
        """The connected client."""
        if self._cloud is None:
            raise RuntimeError("The cloud client is not connected")
        return self._cloud

    def call(
        self,
        action: Callable[..., Coroutine[Any, Any, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run one client call on its own loop and hand back the result."""
        if self._loop is None:
            raise RuntimeError("The cloud client is not connected")
        return asyncio.run_coroutine_threadsafe(
            action(*args, **kwargs), self._loop
        ).result(timeout=_CALL_TIMEOUT)

    def read_lights(self, timeout: float = _SETTINGS_TIMEOUT) -> list[str]:
        """Ask the lights for their settings, then hand back the ones that answered.

        A settings report is the light saying what it is doing right now — its
        power, brightness and effect id — so a page that shows one of those is
        showing the lamp rather than the last frame this studio wrote itself. The
        request is a publish, which belongs on the client's own loop; the answers
        arrive there as frames, and the client's listeners are what says one
        arrived, so a light that stays silent only means the values already known
        stay in place. The serials that were heard from are returned, which is what
        lets the page say whether it actually saw the light.
        """
        cloud = self.cloud
        if self._loop is None:
            raise RuntimeError("The cloud client is not connected")
        answered: set[str] = set()
        heard = threading.Event()

        def listened(serial: str) -> None:
            answered.add(serial)
            heard.set()

        listeners = [
            (serial, partial(listened, serial)) for serial in cloud.devices
        ]
        for serial, listener in listeners:
            cloud.add_listener(serial, listener)
        try:
            for serial in cloud.devices:
                self._loop.call_soon_threadsafe(cloud.request_settings, serial)
            deadline = time.monotonic() + timeout
            while len(answered) < len(cloud.devices):
                if not heard.wait(max(0.0, deadline - time.monotonic())):
                    break
                heard.clear()
        finally:
            for serial, listener in listeners:
                cloud.remove_listener(serial, listener)
        silent = [serial for serial in cloud.devices if serial not in answered]
        if silent:
            _LOGGER.info(
                "These lights did not report their settings on request: %s",
                ", ".join(silent),
            )
        return [serial for serial in cloud.devices if serial in answered]

    def device(self, payload: dict[str, Any]) -> EufyLifeLightDevice:
        """The light a request is about."""
        serial = str(payload.get("serial") or "")
        device = self.cloud.devices.get(serial)
        if device is None:
            raise ValueError(f"No light with serial {serial!r} is in this account")
        return device


_SUGGESTIONS: list[str] = []


def _suggestions(link: CloudLink) -> list[str]:
    """The AI's suggestion chips, read once and remembered for the page."""
    if not _SUGGESTIONS:
        serial = next(iter(link.cloud.devices), None)
        if serial is not None:
            try:
                _SUGGESTIONS.extend(
                    link.call(link.cloud.async_recommend_keywords, serial)
                )
            except (EufyLifeCloudError, RuntimeError, TimeoutError) as err:
                _LOGGER.warning("The AI suggestions are unavailable: %s", err)
    return _SUGGESTIONS


def _state(link: CloudLink) -> dict[str, Any]:
    """Everything the page shows: the lights, their scenes and the suggestions."""
    cloud = link.cloud
    lights: list[dict[str, Any]] = []
    for serial, device in cloud.devices.items():
        scenes = link.call(cloud.async_fetch_personal_scenes, serial)
        lights.append(
            {
                "serial": serial,
                "name": device.name,
                "model": device.model,
                "lamp_count": device.lamp_count,
                "columns": _COLUMNS,
                "power": device.is_on,
                "brightness": device.brightness,
                "effect": device.effect,
                "light_id": device.light_id,
                "speed": device.speed,
                "direction": device.direction,
                "colors": [_color_hex(color) for color in device.colors or []],
                "effect_count": len(device.effects),
                "modes": sorted(
                    {
                        int(entry["dynamic"])
                        for entry in device.effects.values()
                        if "dynamic" in entry
                    }
                ),
                "scenes": [
                    {
                        "name": name,
                        "light_id": preset.get("light_id"),
                        "mode": preset.get("dynamic"),
                        "speed": preset.get("speed"),
                        "light_type": preset.get("light_type"),
                        "colors": [
                            _color_hex(color) for color in preset.get("colors") or []
                        ],
                    }
                    for name, preset in sorted(scenes.items())
                ],
            }
        )
    return {"lights": lights, "keywords": _suggestions(link)}


def create_app(link: CloudLink) -> Flask:
    """The studio's routes: thin wrappers over the integration's own client."""
    app = Flask(__name__)
    # The page and its script change while the studio is being worked on, and
    # neither Jinja nor the browser may then keep serving an older copy: a stale
    # page looks exactly like an answer that never arrived.
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    @app.after_request
    def no_store(response: Any) -> Any:
        response.headers["Cache-Control"] = "no-store"
        return response

    def payload() -> dict[str, Any]:
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}

    @app.get("/")
    def index() -> str:
        return render_template("scene_studio.html")

    @app.get("/api/state")
    def state() -> Any:
        """The lights, their scenes and the suggestions.

        ``?fresh=1`` asks the lights for their settings first, so the page shows
        what the lamp itself reports instead of the last frame this studio wrote.
        A light that stays silent is not an error: the known values are answered
        with the serials that were heard from, and the page says which it got.
        """
        fresh = request.args.get("fresh") in ("1", "true", "yes")
        try:
            answered = link.read_lights() if fresh else []
            return jsonify(**_state(link), fresh=fresh, answered=answered)
        except (EufyLifeCloudError, RuntimeError, TimeoutError, ValueError) as err:
            return jsonify(error=str(err)), 502

    @app.post("/api/apply")
    def apply_palette() -> Any:
        """Show a palette on the light, the way the light entity writes one."""
        body = payload()
        try:
            device = link.device(body)
            link.call(
                link.cloud.async_set_effect,
                device.serial,
                colors=_palette_for(body, device),
                speed=body.get("speed"),
                direction=body.get("direction"),
                refresh=not device.silent_effect,
            )
        except (EufyLifeCloudError, RuntimeError, TimeoutError, ValueError) as err:
            return jsonify(error=str(err)), 400
        return jsonify(ok=True)

    @app.post("/api/effect")
    def apply_effect() -> Any:
        """Apply an effect by name: a preset, or a scene saved earlier."""
        body = payload()
        name = str(body.get("effect") or "").strip()
        if not name:
            return jsonify(error="An effect name is needed"), 400
        try:
            device = link.device(body)
            link.call(
                link.cloud.async_set_effect,
                device.serial,
                effect=name,
                refresh=not device.silent_effect,
            )
        except (EufyLifeCloudError, RuntimeError, TimeoutError, ValueError) as err:
            return jsonify(error=str(err)), 400
        return jsonify(ok=True)

    @app.post("/api/ai")
    def ai_generate() -> Any:
        """Ask the AI for a design, and optionally show it right away."""
        body = payload()
        prompt = str(body.get("prompt") or "").strip() or None
        try:
            device = link.device(body)
            if body.get("apply"):
                design = link.call(
                    link.cloud.async_apply_ai_effect, device.serial, prompt
                )
            else:
                design = link.call(
                    link.cloud.async_generate_ai_effect, device.serial, prompt
                )
        except (EufyLifeCloudError, RuntimeError, TimeoutError, ValueError) as err:
            return jsonify(error=str(err)), 502
        return jsonify(design=_design_json(design))

    @app.post("/api/scenes")
    def create_scene() -> Any:
        """Save a scene to the account, which is what makes it an effect."""
        body = payload()
        try:
            device = link.device(body)
            light_id = link.call(
                link.cloud.async_create_scene,
                device.serial,
                str(body.get("name") or ""),
                colors=[_hex_to_color(value) for value in body.get("colors") or []]
                or None,
                mode=body.get("mode"),
                brightness=body.get("brightness"),
                speed=body.get("speed"),
                direction=body.get("direction"),
                context=str(body.get("context") or ""),
                keywords=str(body.get("keywords") or "").strip() or None,
            )
        except (EufyLifeCloudError, RuntimeError, TimeoutError, ValueError) as err:
            return jsonify(error=str(err)), 400
        return jsonify(ok=True, light_id=light_id)

    @app.post("/api/scenes/delete")
    def delete_scene() -> Any:
        """Remove one of the account's own scenes."""
        body = payload()
        name = str(body.get("name") or "").strip()
        try:
            device = link.device(body)
            light_id = body.get("light_id")
            if light_id is None:
                light_id = link.call(link.cloud.async_find_scene, device.serial, name)
            if light_id is None:
                return jsonify(
                    error=f"{name!r} is not a scene of this account"
                ), 404
            link.call(link.cloud.async_delete_scene, device.serial, int(light_id))
        except (EufyLifeCloudError, RuntimeError, TimeoutError, ValueError) as err:
            return jsonify(error=str(err)), 400
        return jsonify(ok=True)

    return app


def main() -> None:
    """Connect once, then serve the studio on the loopback address."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    email, password, country = _credentials()
    link = CloudLink(email, password, country)
    print("Logging in and connecting to the light cloud...")
    link.start()
    for device in link.cloud.devices.values():
        print(
            f"  {device.name} ({device.model}, {device.serial}): "
            f"{device.lamp_count or '?'} segments, {len(device.effects)} effects"
        )
    print(f"Scene studio on http://127.0.0.1:{_PORT}  (Ctrl+C to stop)")
    create_app(link).run(host="127.0.0.1", port=_PORT, threaded=True)


if __name__ == "__main__":
    main()
