"""Exercise the scene studio's web layer without touching the cloud.

Every route is a thin wrapper over the client, so a stub client with canned
answers is enough to check the contract the page depends on: the JSON shapes, the
palette that gets padded to the light's segment count, the record a save sends, and
that a failure comes back as an error message instead of a stack trace. The stub
also answers a settings request the way a light does, so the fresh read the page
polls with can be checked against the values the light reports rather than the ones
this studio wrote itself.

Run it from anywhere: ``python scripts/check_scene_studio.py``.
"""

import asyncio
import importlib.util
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

spec = importlib.util.spec_from_file_location(
    "scene_studio", os.path.join(REPO, "scripts", "scene_studio.py")
)
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)

from custom_components.eufylife_api.cloud import EufyLifeLightDevice  # noqa: E402

SERIAL = "T8L0281024240021"


class StubCloud:
    """A client whose answers are fixed, so nothing leaves the machine."""

    def __init__(self) -> None:
        self.devices = {
            SERIAL: EufyLifeLightDevice(
                serial=SERIAL,
                name="permanent",
                model="T8L02",
                is_on=True,
                online=True,
                brightness=1,
                lamp_count=4,
                light_id=20002,
                effect="kerst",
                speed=4,
                direction=0,
                colors=[(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)],
                effects={"kerst": {}, "Warm White": {}},
            )
        }
        self.deleted: list[int] = []
        self.fail_ai = False
        self.shown_design: dict = {}
        self.asked: list[str] = []  # the lights a settings request was sent to
        self.answer = True  # whether the light reports its settings back
        self._listeners: dict[str, set] = {}

    async def async_fetch_personal_scenes(self, serial):
        return {
            "kerst": {
                "light_id": 171200,
                "dynamic": 20002,
                "direction": 0,
                "speed": 10,
                "colors": [(255, 149, 13), (255, 7, 75)],
                "light_type": 4,
            }
        }

    async def async_recommend_keywords(self, serial):
        return ["Oceanic Opulence Oasis", "Tiki Torch Twilight"]

    async def async_generate_ai_effect(self, serial, keywords=None):
        # The cloud fails a generation on its own side now and then, which is the
        # error the route has to pass on as a message rather than as a stack trace.
        if self.fail_ai:
            raise studio.EufyLifeCloudError(
                "Light API /app/light/aigc/get failed: 190002"
            )
        return {
            "colors": [(255, 130, 0), (255, 182, 0)],
            "dynamic": 30007,
            "direction": 0,
            "speed": 3,
            "brightness": 70,
            "context": "A romantic candlelight dinner walk",
            "cover": "",
        }

    async def async_apply_ai_effect(self, serial, keywords=None):
        self.shown_design = await self.async_generate_ai_effect(serial, keywords)
        return self.shown_design

    async def async_create_scene(self, serial, name, **kwargs):
        # The real client refuses these two, so the stub has to as well for the
        # route's error path to mean anything.
        if not name.strip():
            raise studio.EufyLifeCloudError("A scene needs a name")
        if not kwargs.get("colors"):
            raise studio.EufyLifeCloudError("A scene needs at least one colour")
        self.created = (serial, name, kwargs)
        return 2189268

    async def async_find_scene(self, serial, name):
        return 171200 if name == "kerst" else None

    async def async_delete_scene(self, serial, light_id):
        self.deleted.append(light_id)

    async def async_set_effect(self, serial, **kwargs):
        self.applied = kwargs

    def request_settings(self, serial):
        """The real one publishes this and the light answers on its own loop.

        The stub answers with values of the light's own, which is what makes a fresh
        read stand out from the state this studio last wrote itself.
        """
        self.asked.append(serial)
        device = self.devices[serial]
        device.is_on = True
        device.brightness = 70
        device.light_id = 20006
        if not self.answer:
            return
        for listener in tuple(self._listeners.get(serial, ())):
            listener()

    def add_listener(self, serial, listener):
        self._listeners.setdefault(serial, set()).add(listener)

    def remove_listener(self, serial, listener):
        self._listeners.get(serial, set()).discard(listener)


class ImmediateLoop:
    """A stand-in for the client's loop: scheduling a call runs it right away."""

    def call_soon_threadsafe(self, action, *args):
        action(*args)


class StubLink:
    """The studio only ever calls ``link.call``, ``link.device`` and — on a fresh
    read — ``link.read_lights``, so the real reader is bound to the stub and its
    listener wait is exercised for real: what is left out here is the broker, not
    the logic."""

    def __init__(self, cloud: StubCloud) -> None:
        self.cloud = cloud
        self._loop = ImmediateLoop()

    def read_lights(self) -> list[str]:
        # The real reader, with its wait trimmed to what the stub needs: what is
        # checked is that the route asks, and that the answer says which lights were
        # heard from — including none of them.
        return studio.CloudLink.read_lights(self, timeout=0.2)

    def call(self, action, *args, **kwargs):
        return asyncio.run(action(*args, **kwargs))

    def device(self, payload):
        device = self.cloud.devices.get(str(payload.get("serial") or ""))
        if device is None:
            raise ValueError("No light with that serial is in this account")
        return device


cloud = StubCloud()
app = studio.create_app(StubLink(cloud))
client = app.test_client()

page = client.get("/")
assert page.status_code == 200, page.status_code
body = page.get_data(as_text=True)
# A stale page looks exactly like an answer that never arrived, so the page and
# its script are served uncached and the template is reloaded.
assert page.headers["Cache-Control"] == "no-store", page.headers.get("Cache-Control")
for marker in (
    "Segments",
    "AI LightChat",
    "Save as scene",
    "/api/apply",
    "/api/ai",
    'id="design"',
    "showDesignError",
    # The state panel and the minute poll the page runs on it.
    "Current state",
    'id="state-grid"',
    'id="state-when"',
    'id="read-now"',
    "POLL_SECONDS",
    "?fresh=1",
    "visibilitychange",
):
    assert marker in body, f"the page is missing {marker!r}"
print(
    "GET /                       ",
    page.status_code,
    f"({len(body)} bytes, no-store)",
)

state = client.get("/api/state").get_json()
assert len(state["lights"]) == 1, state
light = state["lights"][0]
assert light["name"] == "permanent" and light["lamp_count"] == 4
assert light["colors"] == ["#ff0000", "#00ff00", "#0000ff", "#ffff00"]
assert [scene["name"] for scene in light["scenes"]] == ["kerst"]
assert light["scenes"][0]["colors"] == ["#ff950d", "#ff074b"]
assert light["scenes"][0]["light_id"] == 171200
assert state["keywords"] == ["Oceanic Opulence Oasis", "Tiki Torch Twilight"]
print("GET /api/state              ", json.dumps(state)[:120], "...")

# The page shows the light's own state and polls it. A plain read answers with what
# the client already knows and says so; a fresh read asks the light, so the answer
# follows the lamp rather than the last brightness this studio wrote.
assert light["light_id"] == 20002, light
assert state["fresh"] is False and state["answered"] == [], state
assert cloud.asked == [], cloud.asked

fresh = client.get("/api/state?fresh=1").get_json()
assert fresh["fresh"] is True and fresh["answered"] == [SERIAL], fresh["answered"]
assert cloud.asked == [SERIAL], cloud.asked
read_back = fresh["lights"][0]
assert read_back["brightness"] == 70, read_back
assert read_back["power"] is True and read_back["light_id"] == 20006, read_back
print(
    "GET /api/state?fresh=1      heard from",
    fresh["answered"],
    "->",
    f"{read_back['brightness']}%, id {read_back['light_id']}",
)

# A light that stays silent is not an error: the page is told which lights answered,
# and the values it already has stay in place rather than the read failing.
cloud.answer = False
silent = client.get("/api/state?fresh=1").get_json()
assert silent["fresh"] is True and silent["answered"] == [], silent
assert len(silent["lights"]) == 1 and silent["lights"][0]["brightness"] == 70, silent
print("GET /api/state?fresh=1      a silent light answered:", silent["answered"])
cloud.answer = True

applied = client.post(
    "/api/apply",
    json={"serial": SERIAL, "colors": ["#ff0000", "#00ff00"], "speed": 7, "direction": 1},
)
assert applied.status_code == 200, applied.get_json()
colors = cloud.applied["colors"]
assert colors == [(255, 0, 0), (0, 255, 0), (255, 0, 0), (0, 255, 0)], colors
assert cloud.applied["speed"] == 7 and cloud.applied["direction"] == 1
print("POST /api/apply             padded to", len(colors), "segments, speed", cloud.applied["speed"])

design = client.post(
    "/api/ai", json={"serial": SERIAL, "prompt": "a quiet snowy evening"}
).get_json()["design"]
assert design["dynamic"] == 30007 and design["context"].startswith("A romantic")
assert design["colors"] == ["#ff8200", "#ffb600"]
# The design travels as JSON the page can draw: every field the design panel shows
# has to be in it, the brightness included.
assert sorted(design) == [
    "brightness",
    "colors",
    "context",
    "cover",
    "direction",
    "dynamic",
    "speed",
]
print("POST /api/ai                ", design["dynamic"], design["colors"])

shown = client.post(
    "/api/ai",
    json={"serial": SERIAL, "prompt": "a quiet snowy evening", "apply": True},
)
assert shown.status_code == 200, shown.get_json()
assert cloud.shown_design["brightness"] == 70
print("POST /api/ai (apply)        showed the design at", cloud.shown_design["brightness"], "%")

# A generation the cloud fails is handed to the page as a message it shows next to
# the answers that do arrive, not as a stack trace.
cloud.fail_ai = True
broken = client.post(
    "/api/ai", json={"serial": SERIAL, "prompt": "a quiet snowy evening"}
)
assert broken.status_code == 502, broken.status_code
assert "190002" in broken.get_json()["error"], broken.get_json()
print("POST /api/ai (failed)       ->", broken.get_json()["error"])
cloud.fail_ai = False

saved = client.post(
    "/api/scenes",
    json={
        "serial": SERIAL,
        "name": "studio test",
        "colors": ["#ff0000", "#00ff00"],
        "mode": 20006,
        "brightness": 60,
        "speed": 4,
        "direction": 1,
        "context": "from the studio",
        "keywords": "a quiet snowy evening",
    },
).get_json()
assert saved == {"ok": True, "light_id": 2189268}, saved
serial, name, kwargs = cloud.created
assert (serial, name) == (SERIAL, "studio test")
assert kwargs["colors"] == [(255, 0, 0), (0, 255, 0)]
assert kwargs["keywords"] == "a quiet snowy evening" and kwargs["context"] == "from the studio"
print("POST /api/scenes            saved", name, "as", saved["light_id"])

deleted = client.post(
    "/api/scenes/delete", json={"serial": SERIAL, "name": "kerst"}
)
assert deleted.status_code == 200 and cloud.deleted == [171200], deleted.get_json()
print("POST /api/scenes/delete     removed", cloud.deleted)

missing = client.post("/api/scenes/delete", json={"serial": SERIAL, "name": "Warm White"})
assert missing.status_code == 404, missing.status_code
print("POST /api/scenes/delete     refused a catalog preset:", missing.get_json()["error"])

effect = client.post("/api/effect", json={"serial": SERIAL, "effect": "Warm White"})
assert effect.status_code == 200 and cloud.applied["effect"] == "Warm White"
print("POST /api/effect            applied", cloud.applied["effect"])

for path, payload in (
    ("/api/apply", {"serial": SERIAL}),
    ("/api/apply", {"serial": "nope", "colors": ["#ff0000"]}),
    ("/api/apply", {"serial": SERIAL, "colors": ["nonsense"]}),
    ("/api/scenes", {"serial": SERIAL, "name": "", "colors": ["#ff0000"]}),
):
    failed = client.post(path, json=payload)
    assert failed.status_code == 400, (payload, failed.status_code)
    print(f"{path:28}", str(payload)[:44], "->", failed.get_json()["error"][:60])

print("\nscene studio: all checks passed")
