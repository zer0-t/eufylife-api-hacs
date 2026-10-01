"""EufyLife API binary sensors."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud import EufyLifeLightCloud, EufyLifeLightDevice
from .const import DOMAIN
from .models import EufyLifeConfigEntry

DOCUMENTATION_URL = "https://github.com/zer0-t/eufylife-api-hacs"


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: EufyLifeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the reachability sensors of the Eufy light cloud."""
    cloud = entry.runtime_data.light_cloud
    if cloud is None:
        return
    entities: list[BinarySensorEntity] = [
        EufyLifeLightConnectivity(cloud, device) for device in cloud.devices.values()
    ]
    entities.append(EufyLifeCloudLink(cloud, entry))
    async_add_entities(entities)


class EufyLifeLightConnectivity(BinarySensorEntity):
    """Whether a light is reachable.

    Two sources answer this and they are not the same: the cloud inventory lists
    the reachability it knows (``device_status``, read once at discovery) and the
    light announces itself on its own status topic while it is connected. The
    light's own word is the fresher one and wins while it is known; nothing said
    yet leaves the sensor unknown rather than claiming the light is gone.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    # The state arrives on the light cloud's listeners; there is nothing to poll.
    _attr_should_poll = False

    def __init__(self, cloud: EufyLifeLightCloud, device: EufyLifeLightDevice) -> None:
        """Initialize the connectivity sensor of one light."""
        self._cloud = cloud
        self._device = device
        self._attr_unique_id = f"{device.serial}_connectivity"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.serial)},
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether the light is reachable, or None while it is unknown."""
        return self._device.reachable

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Say which of the two sources reported what.

        A light that is reachable can still be silent for hours, so the moment of
        its last report is part of what this sensor says.
        """
        attributes: dict[str, Any] = {
            "reported_by_device": self._device.online,
            "reported_by_cloud": self._device.cloud_status,
        }
        if self._device.last_report is not None:
            attributes["last_report"] = datetime.fromtimestamp(
                self._device.last_report, tz=timezone.utc
            ).isoformat(timespec="seconds")
        return attributes

    async def async_added_to_hass(self) -> None:
        """Subscribe to the light's own reports."""
        self._cloud.add_listener(self._device.serial, self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from the light's own reports."""
        self._cloud.remove_listener(self._device.serial, self.async_write_ha_state)


class EufyLifeCloudLink(BinarySensorEntity):
    """Whether the MQTT link with the light cloud is up.

    Everything the lights report arrives over this one link, which belongs to the
    account rather than to a single light, so the entity sits on a service device
    of its own. The link going down is visible here immediately, which is what
    tells a silent light apart from a light that has nothing to say; the entity
    stays available while the link is down, because "the link is down" is its
    state rather than a reason to be unknown.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "cloud_link"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, cloud: EufyLifeLightCloud, entry: EufyLifeConfigEntry) -> None:
        """Initialize the link sensor of one account."""
        self._cloud = cloud
        self._attr_unique_id = f"{entry.entry_id}_cloud_link"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EufyLife API",
            manufacturer="Eufy",
            model="Eufy Life light cloud",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=DOCUMENTATION_URL,
        )

    @property
    def is_on(self) -> bool:
        """Return whether the link the lights report over is connected."""
        return self._cloud.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return how many lights the link carries."""
        return {"lights": len(self._cloud.devices)}

    async def async_added_to_hass(self) -> None:
        """Subscribe to the link coming up or going down."""
        self._cloud.add_link_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from the link coming up or going down."""
        self._cloud.remove_link_listener(self.async_write_ha_state)
