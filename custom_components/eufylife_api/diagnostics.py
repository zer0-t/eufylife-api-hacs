"""Diagnostics support for the EufyLife API integration."""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .cloud import EufyLifeLightDevice
from .const import CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL, DOMAIN
from .models import EufyLifeConfigEntry, entry_country

# Everything that identifies the account or grants access to it. Device serials
# are deliberately kept: a report about a light is filed under them, they are
# printed on the light itself, and the discovery tooling prints them too.
TO_REDACT = {
    "access_token",
    "customer_ids",
    "device_id",
    "email",
    "openudid",
    "password",
    "user_center_id",
    "user_center_token",
    "user_id",
}


def _light_diagnostics(device: EufyLifeLightDevice) -> dict[str, Any]:
    """Return one light as its own report describes it.

    A light that misbehaves is diagnosed from what it reported, not from what was
    asked of it: the effect name next to the cloud id and mode id behind that
    name, the values the effect was applied with, the palette it acknowledged and
    the scenes its account can render are all here.
    """
    return {
        "name": device.name,
        "model": device.model,
        "model_name": device.model_name,
        "known_model": device.known_model,
        "protocol": "modern" if device.animation_protocol else "generic",
        "session_handshake": device.session_handshake,
        "silent_effect": device.silent_effect,
        "online": device.online,
        "cloud_status": device.cloud_status,
        "power": device.is_on,
        "brightness_pct": device.brightness,
        "segments": device.lamp_count,
        "mode": device.light_id,
        "effect": device.effect,
        "effect_id": device.effect_id,
        "speed": device.speed,
        "direction": device.direction,
        "colors": [list(color) for color in device.colors or []],
        "scenes": sorted(device.effects),
        "last_report": (
            datetime.fromtimestamp(device.last_report, tz=timezone.utc).isoformat(
                timespec="seconds"
            )
            if device.last_report is not None
            else None
        ),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: EufyLifeConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    What the scale side needs from a report is the entry's own shape - where the
    account resolves to, how long its token lives, how many customers were read
    and how the polling behaves - so that is summarised. The light side is where
    the state is, because a light answers over MQTT rather than by request: the
    link state, every discovered light with the ids its report carries, and the
    scenes each one can render.
    """
    data = entry.runtime_data
    diagnostics: dict[str, Any] = {
        "entry": async_redact_data(
            {
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            TO_REDACT,
        ),
        "account": {
            "country": entry_country(entry.data),
            "update_interval": entry.data.get(
                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
            ),
            "customers": len(data.customer_ids),
            "token_valid_for_seconds": round(data.expires_at - time.time()),
        },
        "light_cloud": None,
        "scale_polling": None,
    }

    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        diagnostics["scale_polling"] = {
            "update_count": getattr(coordinator, "_update_count", None),
            "consecutive_failures": getattr(coordinator, "_consecutive_failures", None),
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "last_update_time": (
                coordinator._last_update_time.isoformat()
                if getattr(coordinator, "_last_update_time", None)
                else None
            ),
        }

    cloud = data.light_cloud
    if cloud is None:
        diagnostics["light_cloud"] = {
            "connected": False,
            "note": "The light cloud is not set up for this account",
        }
        return diagnostics

    diagnostics["light_cloud"] = {
        "connected": cloud.connected,
        "lights": {
            serial: _light_diagnostics(device)
            for serial, device in cloud.devices.items()
        },
    }
    return diagnostics
