"""Eufy lights (E10, E22, ...) for EufyLife API."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ATTR_RGBWW_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_platform
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from .cloud import EufyLifeCloudError, EufyLifeLightCloud, EufyLifeLightDevice
from .const import DOMAIN, MAX_SEGMENT_ENTITIES
from .models import EufyLifeConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: EufyLifeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up cloud-discovered Eufy lights."""
    cloud = entry.runtime_data.light_cloud
    if cloud is not None:
        entities: list[LightEntity] = []
        for device in cloud.devices.values():
            entities.append(EufyLifeLight(cloud, device))
            if not device.lamp_count or device.lamp_count <= 1:
                continue
            if device.lamp_count > MAX_SEGMENT_ENTITIES:
                # Long strips (for example the E22) report dozens of light
                # points; a palette for every one of them has to fit one frame,
                # so they stay on the main light entity instead of flooding HA
                # with entities that cannot be expressed anyway.
                _LOGGER.info(
                    "%s reports %d segments; per-segment entities are limited "
                    "to %d segments",
                    device.name,
                    device.lamp_count,
                    MAX_SEGMENT_ENTITIES,
                )
                continue
            for i in range(device.lamp_count):
                entities.append(EufyLifeSegmentLight(cloud, device, i))
        async_add_entities(entities)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "set_light_settings",
        {
            vol.Optional("effect"): cv.string,
            vol.Optional("colors"): vol.All(cv.ensure_list, [vol.All(cv.ensure_list, [vol.Coerce(int)])]),
            vol.Optional("speed"): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
            vol.Optional("direction"): vol.All(vol.Coerce(int), vol.Range(min=0, max=1)),
        },
        "async_set_light_settings",
    )
    platform.async_register_entity_service(
        "set_light_show",
        {
            vol.Required("params"): cv.string,
            vol.Optional("use_ai_opcode"): cv.boolean,
        },
        "async_set_light_show",
    )
    platform.async_register_entity_service(
        "set_scene",
        {
            vol.Required("scene_id"): vol.Coerce(int),
        },
        "async_set_scene",
    )
    platform.async_register_entity_service(
        "create_scene",
        {
            vol.Required("name"): cv.string,
            vol.Optional("colors"): vol.All(
                cv.ensure_list, [vol.All(cv.ensure_list, [vol.Coerce(int)])]
            ),
            vol.Optional("mode"): vol.Coerce(int),
            vol.Optional("brightness"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
            vol.Optional("speed"): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
            vol.Optional("direction"): vol.All(vol.Coerce(int), vol.Range(min=0, max=1)),
            vol.Optional("context"): cv.string,
        },
        "async_create_scene",
    )
    platform.async_register_entity_service(
        "create_ai_scene",
        {
            vol.Required("name"): cv.string,
            vol.Optional("prompt"): cv.string,
        },
        "async_create_ai_scene",
    )
    platform.async_register_entity_service(
        "preview_ai_effect",
        {
            vol.Optional("prompt"): cv.string,
        },
        "async_preview_ai_effect",
    )
    platform.async_register_entity_service(
        "delete_scene",
        {
            vol.Optional("effect"): cv.string,
            vol.Optional("light_id"): vol.Coerce(int),
        },
        "async_delete_scene",
    )


class EufyLifeLight(LightEntity):
    """A Eufy E10 light string or lamp."""

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.RGBWW
    _attr_supported_color_modes = {ColorMode.RGBWW}
    _attr_supported_features = LightEntityFeature.EFFECT
    # Power/brightness are reported; color/effect are last device-acknowledged values.
    _attr_assumed_state = True

    def __init__(self, cloud: EufyLifeLightCloud, device: EufyLifeLightDevice) -> None:
        self._cloud = cloud
        self._device = device
        self._attr_unique_id = device.serial
        self._attr_name = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.serial)},
            manufacturer="Eufy",
            model=device.model,
            name=device.name,
        )

    @property
    def is_on(self) -> bool | None:
        """Return the last device-confirmed power state."""
        return self._device.is_on

    @property
    def brightness(self) -> int | None:
        """Convert the device's 0–100 percentage to HA's 0–255 scale."""
        value = self._device.brightness
        return round(value * 255 / 100) if value is not None else None

    @property
    def available(self) -> bool:
        """Return whether the cloud link and device are available."""
        return self._cloud.connected and self._device.online is not False

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        if self._device.effect is not None:
            return None
        if self._device.colors and len(self._device.colors) > 0:
            return self._device.colors[0][:3]
        return self._device.rgb_color

    @property
    def rgbww_color(self) -> tuple[int, int, int, int, int] | None:
        if self._device.effect is not None:
            return None
        if self._device.colors and len(self._device.colors) > 0:
            color = self._device.colors[0]
            if len(color) == 5:
                return color
            return (*color, 0, 0)
        return self._device.rgbww_color

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return device-specific state attributes.

        The ids and the moment are what the light itself reported: ``light_id``
        is the mode it renders (the A4 of its report) and ``effect_id`` is the
        cloud id (A6) that the effect name was resolved from, so an effect this
        account cannot name is still readable as the id the light showed.
        """
        attributes: dict[str, Any] = {
            "speed": self._device.speed,
            "direction": self._device.direction,
            "lamp_count": self._device.lamp_count,
            "model": self._device.model,
            "model_name": self._device.model_name,
            "protocol": "modern" if self._device.animation_protocol else "generic",
            "light_id": self._device.light_id,
            "effect_id": self._device.effect_id,
            "online": self._device.online,
        }
        if self._device.effect is not None:
            # The account's own scenes carry the app's own type; catalog presets
            # are shared by every account and do not.
            preset = self._device.effects.get(self._device.effect) or {}
            attributes["effect_source"] = (
                "app" if "light_type" in preset else "catalog"
            )
        if self._device.colors:
            # One colour per lamp of the last palette the light acknowledged.
            attributes["palette"] = [
                f"#{bytes(color[:3]).hex()}"
                for color in dict.fromkeys(
                    tuple(color) for color in self._device.colors
                )
            ]
        if self._device.last_report is not None:
            attributes["last_report"] = datetime.fromtimestamp(
                self._device.last_report, tz=timezone.utc
            ).isoformat(timespec="seconds")
        return attributes

    @property
    def effect(self) -> str | None:
        return self._device.effect

    @property
    def effect_list(self) -> list[str]:
        return list(self._device.effects)

    async def async_added_to_hass(self) -> None:
        """Subscribe to MQTT-backed state changes."""
        await super().async_added_to_hass()
        self._cloud.add_listener(self._device.serial, self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from state changes."""
        self._cloud.remove_listener(self._device.serial, self.async_write_ha_state)
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set a device-acknowledged color/preset, then requested power/brightness."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if ATTR_RGB_COLOR in kwargs or ATTR_RGBWW_COLOR in kwargs or ATTR_EFFECT in kwargs:
            try:
                # The T8L40 acks without echoing settings; the E22 reports back.
                refresh = not self._device.silent_effect

                await self._cloud.async_set_effect(
                    self._device.serial,
                    rgb_color=kwargs.get(ATTR_RGB_COLOR),
                    rgbww_color=kwargs.get(ATTR_RGBWW_COLOR),
                    effect=kwargs.get(ATTR_EFFECT),
                    refresh=refresh,
                )
            except EufyLifeCloudError as err:
                raise HomeAssistantError(str(err)) from err
        self._set_power(
            True, round(brightness * 100 / 255) if brightness is not None else None
        )

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Ask the device to turn off."""
        self._set_power(False)

    async def async_set_light_settings(
        self,
        effect: str | None = None,
        colors: list[list[int]] | None = None,
        speed: int | None = None,
        direction: int | None = None,
    ) -> None:
        """Advanced control service: set effect, segmented colors, speed, or direction."""
        try:
            target_colors = None
            if colors is not None:
                target_colors = [tuple(c) for c in colors]

            refresh = not self._device.silent_effect

            await self._cloud.async_set_effect(
                self._device.serial,
                effect=effect,
                colors=target_colors,
                speed=speed,
                direction=direction,
                refresh=refresh,
            )
        except EufyLifeCloudError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_light_show(
        self,
        params: str,
        use_ai_opcode: bool = False,
    ) -> None:
        """Advanced control service: set a custom JSON LightShow animation."""
        try:
            refresh = not self._device.silent_effect

            await self._cloud.async_set_effect(
                self._device.serial,
                params=params,
                use_ai_opcode=use_ai_opcode,
                refresh=refresh,
            )
        except EufyLifeCloudError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_scene(
        self,
        scene_id: int,
    ) -> None:
        """Advanced control service: set a specific cloud scene by ID."""
        try:
            await self._cloud.async_set_scene(
                self._device.serial,
                scene_id=scene_id,
            )
        except EufyLifeCloudError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_create_scene(
        self,
        name: str,
        colors: list[list[int]] | None = None,
        mode: int | None = None,
        brightness: int | None = None,
        speed: int | None = None,
        direction: int | None = None,
        context: str = "",
    ) -> None:
        """Save a scene to the account, so it becomes an effect by name.

        A scene saved here is the app's own kind: it shows up in the light's
        effect list straight away, because the cloud is where that list is read
        from. Without colours the palette the light is showing at the moment is
        used, so a scene can be captured rather than typed out.
        """
        try:
            light_id = await self._cloud.async_create_scene(
                self._device.serial,
                name,
                colors=[tuple(color) for color in colors] if colors else None,
                mode=mode,
                brightness=brightness,
                speed=speed,
                direction=direction,
                context=context,
            )
        except EufyLifeCloudError as err:
            raise HomeAssistantError(str(err)) from err
        _LOGGER.info(
            "Saved the scene %r for %s%s",
            name,
            self._device.name,
            f" as cloud id {light_id}" if light_id else "",
        )
        self.async_write_ha_state()

    async def async_create_ai_scene(self, name: str, prompt: str | None = None) -> None:
        """Generate a light effect with Eufy's AI and save it as a scene.

        The description is what the AI generates from; without one it picks at
        random, which is the app's magic dice button. The design it answers with
        is what gets saved, and it is logged so it can be read back.
        """
        try:
            design = await self._cloud.async_generate_and_save_ai_scene(
                self._device.serial, name, prompt
            )
        except EufyLifeCloudError as err:
            raise HomeAssistantError(str(err)) from err
        _LOGGER.info(
            "The AI saved the scene %r for %s: %s (mode %s, colours %s, speed %s)",
            name,
            self._device.name,
            design.get("context"),
            design.get("dynamic"),
            design.get("colors"),
            design.get("speed"),
        )
        self.async_write_ha_state()

    async def async_preview_ai_effect(self, prompt: str | None = None) -> None:
        """Generate a light effect with Eufy's AI and show it without saving it.

        The light is switched on at the design's own brightness, because the
        palette frame carries only the effect layer: a light that was left dim
        would show nothing recognisable of the design.
        """
        try:
            design = await self._cloud.async_apply_ai_effect(
                self._device.serial, prompt
            )
        except EufyLifeCloudError as err:
            raise HomeAssistantError(str(err)) from err
        _LOGGER.info(
            "Showing the AI effect %r for %s: %s at %s%%",
            design.get("context"),
            self._device.name,
            design.get("colors"),
            design.get("brightness"),
        )

    async def async_delete_scene(
        self,
        effect: str | None = None,
        light_id: int | None = None,
    ) -> None:
        """Remove one of the account's own scenes, by name or by cloud id."""
        if light_id is None:
            if not effect:
                raise HomeAssistantError("Provide either the scene name or its light_id")
            try:
                light_id = await self._cloud.async_find_scene(
                    self._device.serial, effect
                )
            except EufyLifeCloudError as err:
                raise HomeAssistantError(str(err)) from err
            if light_id is None:
                raise HomeAssistantError(
                    f"{effect!r} is not a scene this account saved; a catalog "
                    "preset is shared by every account and cannot be deleted"
                )
        try:
            await self._cloud.async_delete_scene(self._device.serial, light_id)
        except EufyLifeCloudError as err:
            raise HomeAssistantError(str(err)) from err
        _LOGGER.info(
            "Deleted the scene %s for %s", light_id, self._device.name
        )
        self.async_write_ha_state()

    def _set_power(self, is_on: bool, brightness: int | None = None) -> None:
        try:
            self._cloud.set_power(self._device.serial, is_on, brightness)
        except EufyLifeCloudError as err:
            raise HomeAssistantError(str(err)) from err


class EufyLifeSegmentLight(LightEntity):
    """A single segment of a Eufy light string."""

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.RGBWW
    _attr_supported_color_modes = {ColorMode.RGBWW}
    # Segments don't have their own power/brightness in Eufy API, but we simulate it.
    _attr_assumed_state = True

    def __init__(self, cloud: EufyLifeLightCloud, device: EufyLifeLightDevice, index: int) -> None:
        self._cloud = cloud
        self._device = device
        self._index = index
        self._attr_unique_id = f"{device.serial}_segment_{index}"
        self._attr_name = f"Segment {index + 1}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.serial)},
        )

    @property
    def is_on(self) -> bool | None:
        """Return the device power state."""
        return self._device.is_on

    @property
    def brightness(self) -> int | None:
        """Return the device brightness."""
        value = self._device.brightness
        return round(value * 255 / 100) if value is not None else None

    @property
    def available(self) -> bool:
        """Return whether the cloud link and device are available."""
        return self._cloud.connected and self._device.online is not False

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        if self._device.colors and len(self._device.colors) > self._index:
            return self._device.colors[self._index][:3]
        return self._device.rgb_color

    @property
    def rgbww_color(self) -> tuple[int, int, int, int, int] | None:
        if self._device.colors and len(self._device.colors) > self._index:
            color = self._device.colors[self._index]
            if len(color) == 5:
                return color
            return (*color, 0, 0)
        return self._device.rgbww_color

    async def async_added_to_hass(self) -> None:
        """Subscribe to state changes."""
        await super().async_added_to_hass()
        self._cloud.add_listener(self._device.serial, self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from state changes."""
        self._cloud.remove_listener(self._device.serial, self.async_write_ha_state)
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set this segment's color, then ensure device is on."""
        if not self._device.lamp_count:
            raise HomeAssistantError("Lamp count is unknown")

        if ATTR_RGB_COLOR not in kwargs and ATTR_RGBWW_COLOR not in kwargs:
            # Just turning on or changing brightness; handled by main entity logic or global power.
            self._cloud.set_power(self._device.serial, True, kwargs.get(ATTR_BRIGHTNESS))
            return

        # Prepare full palette
        if self._device.colors:
            current_colors = [list(c) for c in self._device.colors]
        else:
            base = self._device.rgbww_color or self._device.rgb_color or (255, 255, 255, 0, 0)
            if len(base) == 3:
                base = (*base, 0, 0)
            current_colors = [list(base)] * self._device.lamp_count

        if ATTR_RGBWW_COLOR in kwargs:
            current_colors[self._index] = list(kwargs[ATTR_RGBWW_COLOR])
        elif ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            current_colors[self._index] = [rgb[0], rgb[1], rgb[2], 0, 0]

        try:
            await self._cloud.async_set_effect(
                self._device.serial,
                colors=[tuple(c) for c in current_colors],
            )
        except EufyLifeCloudError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turning off a segment is not supported individually; turn off the device."""
        self._cloud.set_power(self._device.serial, False)
