# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **The AI tab and the scene editor are on the wire, not in the app**: the light-feature routes of Eufy 3.3.12 are not in the Java dex at all — they live in the Flutter module (`lib/*/libapp.so` inside `split_config.*.apk`), which names 57 `/app/...` routes in plain text, among them the whole AIGC family (`/app/light/aigc/create|get|magic|recommend/list|update`) and the scene editor's own writes (`/app/light/diy/create|update|delete`). Reading them out of the module is what made the feature reachable at all: `E10_Animation_Research.md`'s route table had them only as app behaviour
- **Scenes and AI from Home Assistant**: four light services, backed by `EufyLifeLightCloud` methods, so scenes are no longer read-only account data. `create_scene` saves one to the account, `create_ai_scene` generates a light effect from a description and saves it, `preview_ai_effect` generates and shows one without saving it, and `delete_scene` removes one by name or cloud id. Which of the two save routes is used follows the palette: one that addresses every segment goes to the editor's own route (`/app/light/diy/create`), which keeps that per-segment map and lists the scene as `light_type` 4, while a palette a mode repeats goes to the route the AI's saves use (`/app/light/aigc/create`), which keeps the colours, the mode, the brightness and the speed as given and lists it as `light_type` 1 — the editor's route stores only the segment map, so a palette sent there comes back with an empty `rgb_hex` and never reaches the effect list. Every write re-reads the effect list the way discovery builds it, waits for the service's own scene list to agree (it answers the new id at once but can take a few seconds to list it), and clears the selection when the scene that vanished was the one running
- **Confirmed on hardware, both save shapes**: through the services' own code a two-colour palette grew the live E22's effect list to 14 and the integration's discovery read it back as `{"light_id": 2189276, "light_type": 1, "mode": 18, "speed": 4, "colors": ["#00ff00", "#ffff00"]}`, a fifty-segment grid grew it to 15 and read back as `{"light_id": 2189277, "light_type": 4, "mode": 20006, "speed": 2, "colors": ["#0000ff", "#ff0000"]}`, and both deletes returned the list to exactly the 13 scenes the account started with. The service also refuses a name it already has (`190005 This name is already exist`) and refuses a scene whose `light_detail_info` is nil, both of which the services surface as their own message
- **Scene studio (`scripts/scene_studio.py`)**: a local browser front end for the same client — a segment grid to paint, palette and mode pickers, the AI tab with the cloud's suggestion chips, and the account's scenes with their cloud ids, which is what a scene the app shows but the integration hid looked like. Flask is a development dependency (`scripts/requirements-dev.txt`), never part of the component's own requirements
- **The scene studio shows the light's own state, and keeps it current**: a Current state panel with the power, brightness, effect id, speed, direction and segment colours the light reports, a `Read now` button and a line saying when it last read and when it reads next. The page polls `/api/state?fresh=1` every minute, and at once when the tab comes back into view, while a hidden tab holds its reads: that read asks the light for its settings — the same request the integration makes after a write — and waits for its report on the client's own listeners, so the panel follows the lamp instead of the last frame the studio wrote. A light that stays silent is not an error: the route answers with the values it already had plus the serials that did report, which is what the page's freshness line says. The read is state only, so the palette, the sliders and the mode picker are left alone and a poll cannot undo painting, and the minute tick logs only what moved. The light line above the grids no longer repeats the live values, which the panel owns now. The stub client in `check_scene_studio.py` answers a settings request the way a light does, so the fresh read is checked against the values the light reports — and against none of them
- Route shapes, read off the service itself: an incomplete body is answered with the field it still wants, which named `sn` for all eight routes, then `region` for the suggestion list, `keywords`/`name`/`rgb_hex`/`brightness`/`speed`/`dynamic` for `/app/light/aigc/create` and `name`/`is_group`/`light_effect` (`light_detail_info` may not be nil) for `/app/light/diy/create`. The AI answers a design (`context`, `rgb_hex`, `dynamic`, `brightness`, `speed`, `dynamic_direct`) that is generated afresh per call — two calls with the same description answered differently — while `magic` needs no description at all, which is the app's magic dice
- The round trip is confirmed on hardware: saving through the API grew the live E22's effect list from 13 to 14, the integration's own `async_fetch_personal_scenes` parsed the new scene as `{"light_id": 2189268, "dynamic": 18, "speed": 4, "colors": [[0, 255, 0], [255, 255, 0]], "light_type": 1}`, and deleting it brought the list back to 13. Both the parsed AI design and the record a save has to build are kept as regression cases in the module self-check

### Changed
- **HACS onboarding**: `hacs.json` now renders the README in HACS, the install steps in the README and in the release workflow's notes say that the repository has to be added to HACS as a custom repository before the integration can be found there, and `.gitignore` excludes app dumps (`*.apk`, `*.apkm`, `*.apks`, `*.xapk`) and local credential files (`eufy_creds.json`, `*creds*.json`, `scripts/.env`), so the 120 MB `com.oceanwing.smarthome_3.3.12…apkm` research dump and any password file stay out of the repository — GitHub refuses files larger than 100 MB, so that dump alone would have blocked the first push. `CONTRIBUTING.md` gained a "Publishing a fork to HACS" section with the layout HACS expects, the version/tag rule that makes a release installable, and the custom-repository checklist. The integration itself is unchanged: the config flow keeps asking for the EufyLife email, password and country in the Home Assistant UI and stores them in the config entry, and nothing in the repository holds a credential
- Unused imports dropped (`Any` in `number.py` and `select.py`; the `TYPE_CHECKING`-only `ConfigEntry` in `models.py` now says why it is there), so `ruff check custom_components` reports nothing
- Repository metadata points at the publishing account (`zer0-t`): the manifest's `codeowners`, `documentation` and `issue_tracker`, the README's HACS custom-repository URL, release and license shields and maintainer badge, and the two issue templates' Code of Conduct link — the templates previously mixed the two older owner names
- The release workflow builds its notes in a file and publishes with the runner's own `gh` CLI instead of the archived `actions/create-release@v1` (Node 12, unmaintained since 2021), so bumping `version.txt` really does produce the GitHub release HACS needs before it will offer a version: the notes still come from `version.txt` plus the commit subjects between the last stable tag and `HEAD`, and a `2.4.0-beta1`-style version is still marked a pre-release
- The publishing checklist asks for the repository **topics** as well: the `hacs/action` `repository` check in the Validate workflow fails on a repository that has none ("The repository has no valid topics"), and `hacs` plus `integration` are the two HACS uses for an integration

## [2.4.0] - 2026-09-29

### Added
- **E22 permanent outdoor lights (`T8L02`)**: the E22's real model code is now known, so it is a verified model instead of an unlisted one, driven through the family's binary `0x020D` animation protocol
- **E22 verified on real hardware**: a live `T8L02` answers the classic `0x0A00` settings request with power, brightness and its 50-segment count, needs no 0200 session handshake and sends that reply on the iOS-style `cmd/…/app/res` topic, so the `T8L02` entry no longer rests on captured frames alone. Switching the lights on at 1 % in the app confirmed the brightness field really is a 0-100 percentage and that a preset reported by the light resolves to its catalog name, while the colour blob stays empty. Both captured frames are kept as regression cases in the module self-check
- **Preset list shows the send path**: `--list-animations` tags every preset with the command family the integration writes it with — laid-out animation frame, dynamic effect or classic params — so it is visible up front which frame a hardware run will send
- **E22 preset write verified on hardware**: selecting `Rainbow Bridge` from Home Assistant sends the catalog's own `0x020D` layered animation frame and the `T8L02` answers `0x0A 0x0D` with command result `A1`=0 (`ff090e000300020a0d00a101005e`), so a preset write is acknowledged on the opcode the write already waits for — no change to the accepted-opcode set — and the Eufy app followed the new scene. The captured ack is kept as a regression case in the module self-check
- **Catalog report in the validation script**: `d` in the control menu now reports every catalog entry with the command it is written with and prints the JSON of the entries the parser skips, so presets the app shows but the integration hides (such as app-built personal scenes) are visible with their real shape; `j` dumps the untouched catalog JSON and `p` prints the cached state that feeds Home Assistant (power, brightness, reported mode/light id, effect name and its catalog entry, colors, speed, direction), which is what the device's own report is parsed into
- **App frames are decoded instead of dropped**: the Eufy app writes the same head `client_id` as this integration (`android-eufy_life-<user id>`), so an echo filter that keyed off it swallowed everything the phone sent. Traffic is now told apart by the payload the integration published, and a frame the app publishes is decoded and logged as `Eufy app frame for <serial>: opcode=…, payload=…`, which is the reference for the frames the integration cannot build itself yet. The app's periodic `0x0200` polls stay at debug level and the raw dump of a request frame is now debug too, so a capture that watches the app work is not buried under the app's own polling
- **Personal scene report captured**: applying the app's own `Persoonlijk` scenes on the E22 (`kerst`, `disco 1`) showed how they reach a light — a mode id and a cloud id of the app's own (`A4`=20002/30011, `A6`=171200/168622) for a three-colour palette and no `A9` layer blob, unlike the catalog presets, which report the catalog's cloud id and their layer blob. They are therefore not catalog entries, so the catalog cannot name them: the account's own scene list carries that cloud id and is what names them (see below). Both reports are kept as regression cases in the module self-check
- **The app's own scenes are found, named and offered as effects**: the list behind the `Persoonlijk` tab is `/app/light/diy/list` — the route is named in the app's own code, and of the 16 sibling paths a user-owned mode list could live on only this one answers `400 field "start_index" is not set`, which is what a missing page index looks like on a route that exists. Read as `{"sns": [serial], "start_index": N}` it pages through the account's own scenes (`is_more`), and each entry carries the name, the `light_id` the light reports back in `A6` while it runs, the `light_type` (1 for a scene built on a catalog mode's palette, 4 for one the app renders segment by segment), the mode it was built from and its palette. The E22's account returned 13, and `kerst` = 171200 and `disco 1` = 168622 are exactly the ids the captured reports carried. Discovery now reads that route for every light (`EufyLifeLightCloud.async_fetch_personal_scenes`) and merges it into the effect list, so those scenes are selectable by name next to the catalog presets and `async_set_effect` writes one as a dynamic effect with its palette and the scene's own cloud id in `0xAC` (`_parse_personal_scenes`, `_scene_palette`), all covered by regression checks in the module self-check. The round trip is confirmed on hardware: `--animation kerst` is acknowledged on `0x0A06`, the light's next `0x0A00` report carries `A4`=20002 and `A6`=171200, and the integration resolves that id back to `kerst` — the effect list of the live E22 grew to 102 names, 89 catalog entries plus the 13 scenes of the account
- **`--personal-scenes` reports that list instead of hunting for it**: `c` in the control menu (and `--personal-scenes` without the menu) prints every app-built scene of the selected light by name with the cloud id it is applied with, the mode id the light renders it as, its type, speed and palette, marks each one that is account data rather than a catalog name, and cross-checks those ids against the ones a real light reported for `kerst` and `disco 1` — so a scene the app shows but the effect list lacks is visible per scene instead of inferred from a diff
- **The frame the app writes is one command away**: `--watch-app SECONDS` (menu `w`) prints every frame the phone publishes as `>> APP …` and every light frame as `>> LIGHT …`, raises the cloud logger's own level for the duration so the watched lines cannot be filtered out of a capture, and ends with how many of each arrived, the state the light reports and a verdict — a watch without a single `>> APP` line means that tap was applied over BLE, WLAN or the cloud rather than MQTT
- **Preset listings name their source**: every preset list (`--list-animations`, the preset menu and the not-found path) ends by saying that it is the cloud's default catalog and that the app's own `Persoonlijk` scenes come from their own route and are part of the same effect list, with the command that prints them and the one that captures the frame the app writes — the tab sits next to the presets in the app, so a preset listing on its own looked like it had answered the question about them
- Device selection is shared by the menu-free modes (`_pick_device`), so `--personal-scenes` and `--watch-app` report a missing `--test-serial` the same way `--animation` does
- **`scripts/discover_lights.py`**: diagnostic report that prints the raw device-relation response, the model code of every entry, the retail name of each model and the lights the integration created
- The discovery report also prints the MQTT connection state, the number of received messages and the last topics seen, plus `--debug` (raw MQTT traffic and the integration's log output), `--wait` (listen longer) and `--handshake` (try the 0200 session handshake first) for lights that answer the cloud inventory but stay silent on MQTT. A silent light is reported with the cloud's own `device_status` and the age of its newest parameter, so an offline light is told apart from a protocol mismatch
- `protocol` (`modern`/`generic`), `model_name`, `speed`, `direction`, `lamp_count` and `known_model` light attributes
- Verified-model, animation-protocol, session-handshake and silent-effect model sets (`_KNOWN_MODELS`, `_ANIMATION_PROTOCOL_MODELS`, `_SESSION_HANDSHAKE_MODELS`, `_SILENT_EFFECT_MODELS`) plus a retail-name table, so model-specific behaviour is data driven
- **`E10_Animation_Research.md`** now cross-checks this integration's frames against the independent [`mega-yfue/eufy-sdk`](https://github.com/mega-yfue/eufy-sdk) captures (model registry and the 0x0206/0x020D layouts)

### Changed
- Model-specific behaviour no longer hardcodes `T8L40`: the session handshake, the binary animation opcode (`0x020D`) and the settings-refresh handling follow separate device traits, because only the handshake and the post-effect silence are T8L40 quirks
- The captured T8L40 animation layer blobs are only replayed on the T8L40; other animation-protocol lights render the catalog's own params JSON, so the segment geometry comes from the model in front of them
- Per-segment light entities are limited to `MAX_SEGMENT_ENTITIES` (32) segments, deliberately below the 50 a palette frame can carry; long strips stay on their main light entity
- Oversized palettes now raise a readable error that names the limit instead of a generic TLV length error
- The validation script's menus no longer print the literal range notation `1-N`: they name the real range of the devices found, accept `1.`/`1)` style input and repeat the accepted values when a choice is invalid, so a pasted menu line is no longer typed by accident

### Fixed
- **Own commands counted as device traffic**: the broker delivers a client its own QoS 1 publish back on `cmd/…/req`, the topic the integration subscribes to, so its requests were parsed as if they arrived from the light. Those echoes are now recognised by the payload that was published — not by `head.client_id`, which the Eufy app writes identically to this integration, so keying off it also discarded every frame the phone sent — and ignored, which keeps the MQTT log and the discovery report from reporting contact that did not happen
- **paho-mqtt 1.x crash**: `mqtt.CallbackAPIVersion` does not exist in paho-mqtt 1.6.1 — the version Home Assistant pinned up to 2024.10 and the one the dev environment installs — so MQTT setup failed with `module 'paho.mqtt.client' has no attribute 'CallbackAPIVersion'`. The client is now built for whichever paho-mqtt API is installed and both callback argument lists are accepted, so `paho-mqtt>=1.6.1` is enough again instead of forcing an upgrade of a package Home Assistant itself pins
- **A generated AI design that never showed on the light**: `preview_ai_effect` wrote the palette alone, and the palette frame (`0x0206`) carries only the effect layer's own level â€” the strip's brightness is set separately (`SetUpDeviceCmd`). A light that had been left dim therefore showed nothing recognisable of the design: on the live E22 the light kept reporting `brightness=1` after the write, the design's own 70 % ignored. The service now sends the design's speed and direction with the palette and then switches the light on at the design's brightness, which is the order the light entity itself uses (acknowledged effect first, then requested power and brightness); the light answers with `brightness=70` and the fifty written segments
- **A failed AI generation looked like a button that did nothing**: the cloud fails a generation on its own side every now and then (`190002`, seen live for a description that answered fine a moment later), which reached the studio as a 502 and a single log line. The generation is now asked for again before its failure is passed on (only the transport failure is retried â€” a malformed answer is not something a second attempt fixes), and the studio shows the error in the design panel itself, beside the answers that do arrive. The studio also serves its page with `Cache-Control: no-store` and reloads the template, so a stale page can no longer be mistaken for an answer that never came

## [1.1.1] - 2025-01-09

### Fixed
- **Deprecation Warning**: Removed explicit config_entry assignment in OptionsFlow to fix deprecation warning in Home Assistant 2025.12+
- **Future Compatibility**: Integration now follows current Home Assistant best practices for options flow

### Technical Changes
- Simplified EufyLifeAPIOptionsFlow class initialization
- Removed deprecated explicit config_entry property assignment

## [1.1.0] - 2025-01-09

### Added
- **Configurable Update Intervals**: Users can now choose update frequency from 1 minute to 12 hours
- **Options Flow Support**: Change update interval after setup without recreating the integration
- **Dynamic Interval Updates**: Changes take effect immediately without restarting Home Assistant
- **Interval Display**: Current update interval shown in sensor attributes
- **Re-authentication Support**: Improved handling for expired tokens

### Changed
- **Default Update Interval**: Still 5 minutes but now user-configurable
- **Data Coordinator**: Enhanced to support dynamic interval changes
- **Sensor Attributes**: Added update interval information to all sensors
- **Logging**: Improved logging for interval changes and coordinator status

### Available Update Intervals
- 1 minute (for frequent weighing sessions)
- 2 minutes  
- 5 minutes (recommended default)
- 10, 15, 30 minutes (moderate usage)
- 1, 2, 6, 12 hours (occasional use)

### Technical Changes
- Enhanced config flow with update interval selection
- Added options flow for post-setup configuration
- Improved data coordinator with configurable intervals
- Updated strings.json with interval option translations
- Added config entry update listener for dynamic changes

## [1.0.0] - 2025-01-09

### Added
- Initial release of EufyLife API integration for Home Assistant
- Email/password authentication through Home Assistant UI
- Multi-user support for family members on the same scale
- Weight tracking sensors (current weight, target weight)
- Body composition sensors (body fat, muscle mass, BMI)
- Automatic device discovery and setup
- Data coordinator for efficient API polling every 5 minutes
- Token management with 30-day expiry handling
- HACS compatibility for easy installation
- Comprehensive error handling and logging
- Support for EufyLife Smart Scale P3 and other models

### Features
- Native Home Assistant integration with config flow
- Individual devices for each family member
- Sensor entities with proper device classes and units
- State attributes including last update timestamp
- Secure credential storage in Home Assistant
- Real-time weight and body composition monitoring

### API Endpoints
- Authentication via `/v1/user/v2/email/login`
- Weight data from `/v1/customer/all_target`
- Detailed customer data from `/v1/customer/target/{customer_id}`

## [Unreleased]

### Planned
- Automatic token refresh functionality
- Historical weight data trends
- Additional body composition metrics
- Goal tracking and notifications
- Enhanced error recovery mechanisms 