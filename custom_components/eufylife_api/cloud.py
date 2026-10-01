"""Eufy Life cloud transport used by Eufy lights (E10, E22, ...)."""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import platform
import re
import secrets
import ssl
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import paho.mqtt.client as mqtt

from .const import (
    AUTH_API_BASE_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    LIGHT_API_BASE_URL,
    USER_AGENT_VERSION,
)

_LOGGER = logging.getLogger(__name__)

_LIGHT_PRESET_KEY = "444b380e56f3ffad8aeb4c76494c15f9"
# The T8L0x family is the eufy Life smart-lighting line. Codes and wire behaviour
# are cross-checked against the independent eufy-sdk project
# (github.com/mega-yfue/eufy-sdk), which captured the family on real hardware in
# 2026-07: T8L02 is the "Permanent Outdoor Lights E22" and the whole line shares
# one secure-MQTT DP TLV wire (0x0200/0x0201/0x0204/0x0206/0x020D). See
# E10_Animation_Research.md for the frame-by-frame comparison.
_T8L02_MODEL = "T8L02"
_T8L30_MODEL = "T8L30"
_T8L40_MODEL = "T8L40"
# Retail names of the family, for logs and diagnostics only. Behaviour is decided
# by the trait sets below, never by this table.
_FAMILY_MODEL_NAMES = {
    "T8L00": "Permanent Outdoor Light E120 (30 m)",
    "T8L01": "Permanent Outdoor Light E120 (15 m)",
    "T8L02": "Permanent Outdoor Lights E22",
    "T8L04": "Permanent Outdoor Lights S4",
    "T8L10": "Outdoor String Lights E10",
    "T8L20": "Outdoor Spotlights E10",
    "T8L30": "Outdoor Pathway Lights E10",
    "T8L40": "Indoor Floor Lamp E10",
}
# Lights that render the binary multi-layer animation frame (0x020D) instead of
# the classic E10 effect frame. Confirmed on the T8L02 and the T8L40; the sibling
# SDK gates its effect writer to exactly that pair too.
_ANIMATION_PROTOCOL_MODELS = frozenset({_T8L02_MODEL, _T8L40_MODEL})
# Firmware that needs the 0x0200 session handshake before it accepts an effect.
# Observed on the T8L40 only: the T8L02 writes effects straight after connecting.
_SESSION_HANDSHAKE_MODELS = frozenset({_T8L40_MODEL})
# Firmware that answers an effect with a (0x02, 0x04) status report and never
# echoes a settings report, so the settings refresh after an effect is skipped.
_SILENT_EFFECT_MODELS = frozenset({_T8L40_MODEL})
# Model codes whose protocol has been confirmed on real hardware: T8L02 and T8L40
# from captured frames, T8L30 from this integration's own testing. The Eufy Life
# light service only lists lights, so an unlisted code is still discovered and
# driven through the generic path until it is verified and added here.
_KNOWN_MODELS = frozenset({_T8L02_MODEL, _T8L30_MODEL, _T8L40_MODEL})
_GET_SETTINGS = (0x02, 0x00)
_GET_SETTINGS_RESPONSE = (0x0A, 0x00)
_SET_POWER = (0x02, 0x01)
_SET_POWER_RESPONSE = (0x0A, 0x01)
_REPORT_DEVICE_INFO = (0x02, 0x04)
_SET_SCENE = (0x02, 0x02)
_SET_SCENE_RESPONSE = (0x0A, 0x02)
_SET_EFFECT = (0x02, 0x06)
_SET_EFFECT_RESPONSE = (0x0A, 0x06)
_SET_ANIMATION = (0x02, 0x0D)
_SET_ANIMATION_RESPONSE = (0x0A, 0x0D)
_SET_LIGHT_SHOW = (0x02, 0x10)
_SET_LIGHT_SHOW_RESPONSE = (0x0A, 0x10)
_SET_LIGHT_AI = (0x02, 0x11)
_SET_LIGHT_AI_RESPONSE = (0x0A, 0x11)
# App CmdHandler default at 0x5f0858; LightTransportNewCmd uses that default.
_EFFECT_RESPONSE_TIMEOUT = 5


class EufyLifeAuthError(Exception):
    """Raised when Eufy Life rejects account credentials."""


class EufyLifeCloudError(Exception):
    """Raised when the light cloud returns invalid or rejected data."""


@dataclass
class EufyLifeLightDevice:
    """Cloud-discovered Eufy light."""

    serial: str
    name: str
    model: str
    account_id: str = ""
    is_on: bool | None = None
    online: bool | None = None
    # The reachability the cloud inventory lists for the light (``device_status``
    # of ``/app/devicerelation/get_device_list``). It is read at discovery, before
    # any light has reported, and kept apart from ``online``, which is what the
    # light itself last said on its own status topic.
    cloud_status: bool | None = None
    brightness: int | None = None
    lamp_count: int | None = None
    light_id: int | None = None
    rgb_color: tuple[int, int, int] | None = None
    rgbww_color: tuple[int, int, int, int, int] | None = None
    effect: str | None = None
    # The cloud id the light reports for the effect it runs (the A6 of its own
    # report): a catalog preset's own light_id, or the id of a scene the account
    # built in the app. The name above is resolved from it; the id itself stays
    # on the device, so an effect this account cannot name is still visible as
    # the id the light showed.
    effect_id: int | None = None
    speed: int | None = None
    direction: int | None = None
    colors: list[tuple[int, ...]] | None = None
    effects: dict[str, dict[str, Any]] = field(default_factory=dict)
    # When the light last reported its settings, or answered a settings read.
    last_report: float | None = None

    @property
    def animation_protocol(self) -> bool:
        """Whether the model renders the binary 0x020D multi-layer animation."""
        return self.model in _ANIMATION_PROTOCOL_MODELS

    @property
    def session_handshake(self) -> bool:
        """Whether the firmware needs the 0x0200 handshake before an effect."""
        return self.model in _SESSION_HANDSHAKE_MODELS

    @property
    def silent_effect(self) -> bool:
        """Whether the firmware acks an effect without echoing its settings."""
        return self.model in _SILENT_EFFECT_MODELS

    @property
    def known_model(self) -> bool:
        """Whether this model code has been verified against real hardware."""
        return self.model in _KNOWN_MODELS

    @property
    def reachable(self) -> bool | None:
        """Return what is known about the light being reachable.

        The light's own status report is fresher than the inventory entry it was
        discovered from, so it wins while it is known; ``None`` means neither has
        said anything yet, which is not the same as "offline".
        """
        if self.online is not None:
            return self.online
        return self.cloud_status

    @property
    def model_name(self) -> str:
        """Retail name of the model, or the raw code for an unknown family member."""
        return _FAMILY_MODEL_NAMES.get(self.model, self.model)


def _adopt_effect_settings(device: EufyLifeLightDevice) -> None:
    """Fill in the speed and direction the running effect was applied with.

    A report carries the power, the brightness, the segment count and the effect
    ids, but never the speed or the direction the write sent, so for a light
    whose effect was not applied from here the values the effect itself carries
    are the only ones known to have been applied with it: exactly what
    ``async_set_effect`` sends when it is given no override. A value this
    integration wrote is never replaced, and a direction outside the two the
    firmware takes is left alone rather than shown as an unknown option.
    """
    if device.effect is None:
        return
    preset = device.effects.get(device.effect) or {}
    if device.speed is None and preset.get("speed") is not None:
        device.speed = int(preset["speed"])
    if device.direction is None and preset.get("direction") in (0, 1):
        device.direction = int(preset["direction"])


async def async_login(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    country: str,
) -> dict[str, Any]:
    """Log in using the request emitted by Eufy Life 3.3.12."""
    headers = {
        "Accept": "*/*",
        "User-Agent": f"EufyLife-Android-{USER_AGENT_VERSION}",
        "Category": "Health",
        "Language": "en",
        "Timezone": "UTC",
        "Country": country.upper(),
        "Content-Type": "application/json",
    }
    body = {
        "client_id": CLIENT_ID,
        "client_Secret": CLIENT_SECRET,
        "email": email,
        "password": password,
        "ab": country.lower(),
        "un_subscribe_flag": True,
    }

    async with session.post(
        f"{AUTH_API_BASE_URL}/user/v2/email/login/",
        headers=headers,
        json=body,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        response.raise_for_status()
        data = await response.json(content_type=None)

    if not isinstance(data, dict):
        raise EufyLifeCloudError("Login returned an invalid response")
    if data.get("res_code") != 1:
        raise EufyLifeAuthError(str(data.get("res_code", "invalid response")))

    access_token = data.get("access_token")
    user_id = data.get("user_id")
    if not isinstance(access_token, str) or not isinstance(user_id, str):
        raise EufyLifeCloudError("Login response is missing account tokens")

    expires_in = data.get("expires_in", 2592000)
    return {
        "access_token": access_token,
        "user_id": user_id,
        "user_center_id": data.get("user_center_id"),
        "user_center_token": data.get("user_center_token"),
        "expires_at": time.time() + int(expires_in),
        "device_id": data.get("device_id"),
        "customer_ids": [
            customer["id"]
            for customer in data.get("customers", [])
            if isinstance(customer, dict) and customer.get("id")
        ],
    }


def _nonce() -> str:
    """Match LightCryptoTools::generateNonce()."""
    return hashlib.md5(secrets.token_hex(16).encode()).hexdigest()


def _signature(message: str, key: str) -> str:
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def _aes_encrypt(value: str, key: bytes) -> str:
    iv = secrets.token_bytes(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(value.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(
        iv + encryptor.update(padded) + encryptor.finalize()
    ).decode()


def _aes_decrypt(value: str, key: bytes) -> str:
    raw = base64.b64decode(value, validate=True)
    if len(raw) < 32 or len(raw) % 16:
        raise EufyLifeCloudError("Invalid encrypted response length")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode()


class _LightCrypto:
    """Small Python equivalent of the app's native LightCryptoUtil."""

    def __init__(self) -> None:
        self._private_key = ec.generate_private_key(ec.SECP256R1())
        numbers = self._private_key.public_key().public_numbers()
        self._public_key = f"04{numbers.x:064X}{numbers.y:064X}"
        ident_material = (
            f"{_LIGHT_PRESET_KEY}ident_salt{int(time.time() * 1000)}{_nonce()}"
        )
        self._key_ident = hashlib.md5(ident_material.encode()).hexdigest()
        self._key: bytes | None = None
        self._security_key: str | None = None

    async def async_exchange(
        self, session: aiohttp.ClientSession, base_url: str
    ) -> None:
        """Exchange P-256 public keys with the production light service."""
        preset = bytes.fromhex(_LIGHT_PRESET_KEY)
        encrypted_key = _aes_encrypt(self._public_key, preset)
        timestamp = str(int(time.time()))
        nonce = _nonce()
        headers = {
            "App-name": "eufy_life",
            "Content-Type": "application/json",
            "X-Encryption-Info": "algo_ecdh",
            "X-Request-Ts": timestamp,
            "X-Request-Once": nonce,
            "X-Key-Ident": self._key_ident,
            "X-Signature": _signature(
                f"{timestamp}+{nonce}+{encrypted_key}", _LIGHT_PRESET_KEY
            ),
        }
        async with session.post(
            f"{base_url}/openapi/oauth/key/exchange",
            headers=headers,
            json={"client_public_key": encrypted_key},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            response.raise_for_status()
            result = await response.json(content_type=None)

        if not isinstance(result, dict) or result.get("code") != 0:
            raise EufyLifeCloudError("Light public-key exchange was rejected")
        data = result.get("data")
        if not isinstance(data, dict):
            raise EufyLifeCloudError("Light public-key response is missing data")
        server_key = data.get("server_public_key")
        server_signature = data.get("signature")
        if not isinstance(server_key, str) or not isinstance(server_signature, str):
            raise EufyLifeCloudError("Light public-key response is incomplete")
        expected = _signature(f"{timestamp}+{nonce}+{server_key}", _LIGHT_PRESET_KEY)
        if not hmac.compare_digest(server_signature.lower(), expected):
            raise EufyLifeCloudError("Invalid light public-key signature")

        public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), bytes.fromhex(_aes_decrypt(server_key, preset))
        )
        shared_key = self._private_key.exchange(ec.ECDH(), public_key)[:16]
        self._key = shared_key
        self._security_key = shared_key.hex()

    def encrypt(self, value: str) -> tuple[str, str, str, dict[str, str]]:
        """Encrypt and sign one AIoT request body."""
        if self._key is None or self._security_key is None:
            raise EufyLifeCloudError("Light public key has not been exchanged")
        timestamp = str(int(time.time()))
        nonce = _nonce()
        encrypted = _aes_encrypt(value, self._key)
        return (
            encrypted,
            timestamp,
            nonce,
            {
                "X-Replay-Info": "replay",
                "X-Request-Ts": timestamp,
                "X-Request-Once": nonce,
                "X-Encryption-Info": "algo_ecdh",
                "X-Key-Ident": self._key_ident,
                "X-Signature": _signature(
                    f"{timestamp}+{nonce}+{encrypted}", self._security_key
                ),
            },
        )

    def decrypt(
        self,
        value: str,
        request_timestamp: str,
        request_nonce: str,
    ) -> str:
        """Authenticate and decrypt one AIoT response body."""
        if self._key is None or self._security_key is None:
            raise EufyLifeCloudError("Light public key has not been exchanged")
        response = json.loads(value)
        if not isinstance(response, dict) or response.get("code") != 0:
            raise EufyLifeCloudError("Invalid encrypted light response")
        encrypted = response.get("data")
        signature = response.get("signature")
        if not isinstance(encrypted, str):
            raise EufyLifeCloudError("Encrypted light response has no data")
        if not isinstance(signature, str) or not signature:
            raise EufyLifeCloudError("Encrypted light response has no signature")
        expected = _signature(
            f"{request_timestamp}+{request_nonce}+{encrypted}", self._security_key
        )
        if not hmac.compare_digest(signature.lower(), expected):
            raise EufyLifeCloudError("Invalid encrypted light response signature")
        return _aes_decrypt(encrypted, self._key)


# A single-byte length prefix caps one TLV value at 255 bytes.
_TLV_MAX_VALUE = 255
# The palette is [colour count] + 5 bytes (R, G, B, W, C) per colour, so one frame
# addresses at most (255 - 1) // 5 = 50 segments carrying distinct colours.
_MAX_PALETTE_COLORS = (_TLV_MAX_VALUE - 1) // 5


def _tlv(tag: int, value: bytes) -> bytes:
    if len(value) > _TLV_MAX_VALUE:
        raise ValueError("E10 TLV value is too long")
    return bytes((tag, len(value))) + value


def _tlv_long(tag: int, value: bytes) -> bytes:
    """TLV with 2-byte little-endian length."""
    return bytes([tag]) + len(value).to_bytes(2, "little") + value


def _frame(opcode: tuple[int, int], payload: bytes, version: int = 0) -> bytes:
    frame = bytearray((0xFF, 0x09))
    frame.extend((len(payload) + 10).to_bytes(2, "little"))
    frame.extend((0x03, version, 0x02, *opcode))
    frame.extend(payload)
    frame.append(0)
    frame[-1] = _xor(frame[:-1])
    return bytes(frame)


def _xor(value: bytes | bytearray) -> int:
    result = 0
    for byte in value:
        result ^= byte
    return result


def _command_payload(user_id: str, value: bytes) -> bytes:
    user = user_id.encode()
    return _tlv(0xA1, int(time.time()).to_bytes(4, "little")) + _tlv(0xA2, user) + value


def _effect_payload(
    light_id: int,
    colors: list[tuple[int, ...]],
    direction: int,
    speed: int,
    cloud_id: int | None,
    update_method: int = 0,
) -> bytes:
    """LightTransportNewCmd (0xa7cd78), using native RGBWC channels.

    This 0x0206 write matches the app's own "set effect params" frame byte for
    byte, as captured off a T8L02 (E22 permanent outdoor lights) by the eufy-sdk
    project: 0xA3 carries the colour id (20006 for a plain DIY colour), 0xA6 is
    the colour count followed by 5-byte R/G/B/W/C blocks, 0xA7 lists the
    addressed segments grouped per colour, and 0xA8/0xA9/0xAA close the frame
    with 100 %, five zero channels and a status byte.
    """
    if not 0 <= light_id <= 65535 or not 0 <= direction <= 65535:
        raise EufyLifeCloudError("Invalid light effect ID or direction")
    if not 1 <= speed <= 10 or not colors:
        raise EufyLifeCloudError("Invalid light effect speed or empty palette")
    for color in colors:
        if len(color) not in (3, 5) or any(
            type(c) is not int or not 0 <= c <= 255 for c in color
        ):
            raise EufyLifeCloudError("Color channels must be integers between 0 and 255 (3 or 5 channels)")
    indices = b""
    if 20000 <= light_id < 30000:
        grouped: dict[tuple[int, ...], list[int]] = {}
        for index, color in enumerate(colors):
            grouped.setdefault(color, []).append(index)
        indices = b"".join(bytes([len(slots), *slots]) for slots in grouped.values())
        colors = list(grouped)
    # palette: direct RGBWC entries; port model/firmware calibration for app-identical whites.
    palette_data = []
    for color in colors:
        if len(color) == 3:
            palette_data.extend((*color, 0, 0))
        else:
            palette_data.extend(color)
    palette = bytes([len(colors)]) + bytes(palette_data)
    if len(palette) > _TLV_MAX_VALUE:
        raise EufyLifeCloudError(
            "Too many light segments to address in one frame "
            f"({len(colors)} colors, maximum {_MAX_PALETTE_COLORS})"
        )
    value = (
        _tlv(0xA3, light_id.to_bytes(2, "little"))
        + _tlv(0xA4, direction.to_bytes(2, "little"))
        + _tlv(0xA5, bytes([speed]))
        + _tlv(0xA6, palette)
    )
    if 20000 <= light_id < 30000:
        value += _tlv(0xA7, indices)
    # Global brightness is controlled separately by SetUpDeviceCmd, not multiplied twice.
    value += _tlv(0xA8, b"\x64") + _tlv(0xA9, bytes(5)) + _tlv(0xAA, b"\x00")
    if cloud_id is not None:
        if not 0 <= cloud_id <= 0xFFFFFFFF:
            raise EufyLifeCloudError("Invalid cloud preset ID")
        value += _tlv(0xAC, cloud_id.to_bytes(4, "little"))
    return value + _tlv(0xAE, b"\x00") + _tlv(0xB0, bytes([update_method]))


_KNOWN_COLOR_MAP = {
    "ff4d6a": bytes.fromhex("ff01130500"),
    "ffa36a": bytes.fromhex("ff00146200"),
    "ff6a8a": bytes.fromhex("ff01280c00"),
    "a36aff": bytes.fromhex("8701ff0011"),
    "6affff": bytes.fromhex("03ff740017"),
    "a8ff59": bytes.fromhex("02ff011701"),
    "ff5157": bytes.fromhex("ff010b0600"),
    "ff6c5b": bytes.fromhex("ff000b0d00"),
    "ffce3d": bytes.fromhex("ffa1000b00"),
    "0ff3ff": bytes.fromhex("01ff85000e"),
    "99e2ff": bytes.fromhex("04ffc30038"),
    "a35bff": bytes.fromhex("9600ff000b"),
    "fc5bb2": bytes.fromhex("fc01470800"),
    "ff9a03": bytes.fromhex("ff56000000"),
    "ff9f85": bytes.fromhex("ff01404e00"),
    "ffde93": bytes.fromhex("13ff059d34"),
}

_T8L40_VERIFIED_ANIMATION_LAYERS: dict[int, dict[str, Any]] = {
    10854: {  # Guy Fawkes Night
        "speed": 54,
        "exec_mode": 3,
        "layers": [
            bytes.fromhex("0164640001010082010005ff01130500ff00146200ff01280c008701ff001103ff740017000000000000000300000900046410020c"),
            bytes.fromhex("0014644b0101000103010503ff7400178701ff0011ff01280c0002ff011701ff0113050061010202000000010002"),
        ],
    },
    10599: {  # Celebrating
        "speed": 30,
        "exec_mode": 0,
        "layers": [
            bytes.fromhex("0014640001010080000105ff010b0600ff000b0d00ffa1000b0001ff85000e04ffc3003864010502000000000001"),
            bytes.fromhex("0019640001010080000205ff010b0600ff000b0d00ffa1000b0001ff85000e04ffc3003803640901010502ff01020001"),
        ],
    },
    10589: {  # Party
        "speed": 28,
        "exec_mode": 0,
        "layers": [
            bytes.fromhex("00246400010100800001059600ff000bfc01470800ff56000000ff01404e0013ff059d3464010502000000000100"),
        ],
    },
}


def _encode_animation_layer(layer: dict[str, Any]) -> bytes:
    pri = int(layer.get("layer_priority", 0)) & 0xFF
    spd = int(layer.get("layer_speed", 100)) & 0xFF
    rng = layer.get("layer_range", [0, 100])
    r_hi = int(rng[1]) & 0xFF if len(rng) > 1 else 100
    r_lo = int(rng[0]) & 0xFF if len(rng) > 0 else 0
    i_type = int(layer.get("interval_type", 1)) & 0xFF
    i_val = int(layer.get("interval_value", 1)) & 0xFF
    exec_param = int(layer.get("layer_execution_parameter", 1)) & 0xFFFF
    post_status = int(layer.get("light_effect_post_cycle_status", 0)) & 0xFF
    layer_type = int(layer.get("current_layer_type", 1)) & 0xFF

    header = (
        bytes([pri, spd, r_hi, r_lo, i_type, i_val])
        + exec_param.to_bytes(2, "big")
        + bytes([post_status, layer_type])
    )

    raw_colors = layer.get("colors", "")
    color_list = raw_colors.split("|") if isinstance(raw_colors, str) else []
    color_bytes = bytearray([len(color_list)])
    for c_str in color_list:
        c_clean = c_str.strip().lower()
        if c_clean in _KNOWN_COLOR_MAP:
            color_bytes.extend(_KNOWN_COLOR_MAP[c_clean])
        elif len(c_clean) == 6:
            r = int(c_clean[0:2], 16)
            g = int(c_clean[2:4], 16)
            b = int(c_clean[4:6], 16)
            color_bytes.extend([r, g, b, 0, 0])
        else:
            color_bytes.extend([255, 255, 255, 0, 0])

    trailing = bytearray()
    if layer_type == 0:
        grad = int(layer.get("gradient_value", 0)) & 0xFFFF
        trailing.extend([
            int(layer.get("color_fill_mode", 0)) & 0xFF,
            int(layer.get("color_pick_mode", 0)) & 0xFF,
            int(layer.get("flow_direction", 0)) & 0xFF,
            int(layer.get("direction_change_mode", 0)) & 0xFF,
        ])
        trailing.extend(grad.to_bytes(2, "big"))
        i_blk = layer.get("insert_block_range", 0)
        i_blk_val = int(i_blk[0] if isinstance(i_blk, list) else i_blk) & 0xFF
        blk_blk = layer.get("insert_black_block_range", 0)
        blk_blk_val = int(blk_blk[0] if isinstance(blk_blk, list) else blk_blk) & 0xFF
        trailing.extend([
            int(layer.get("insert_block_mode", 0)) & 0xFF,
            i_blk_val,
            int(layer.get("insert_black_block_mode", 0)) & 0xFF,
            0,
            blk_blk_val,
            int(layer.get("insert_black_block_position_mode", 0)) & 0xFF,
        ])
        b_var = int(layer.get("brightness_variation_type", 0)) & 0xFF
        b_rng = layer.get("brightness_range", [0, 100])
        if isinstance(b_rng, list):
            b_hi = int(b_rng[1]) & 0xFF if len(b_rng) > 1 else 100
            b_lo = int(b_rng[0]) & 0xFF if len(b_rng) > 0 else 0
        else:
            b_hi = int(b_rng) & 0xFF
            b_lo = 0
        cycle = int(layer.get("light_effect_cycle_method", 0)) & 0xFF
        param = int(layer.get("execution_parameter", 0)) & 0xFF
        trailing.extend([b_var, b_hi, b_lo, cycle, param])
    elif layer_type == 1:
        b_val = int(layer.get("brightness_value", 100)) & 0xFF
        disp = int(layer.get("display_mode", 1)) & 0xFF
        q_rng = int(layer.get("color_quantity_range", len(color_list))) & 0xFF
        trans = int(layer.get("transition_mode", 2)) & 0xFF
        switch = int(layer.get("color_switch_mode", 0)) & 0xFF
        seq = int(layer.get("color_pick_sequence", 0)) & 0xFF
        cycle = int(layer.get("light_effect_cycle_method", 0)) & 0xFF
        param = int(layer.get("execution_parameter", 0)) & 0xFF
        trailing.extend([b_val, disp, q_rng, trans, switch, 0, 0, seq, cycle, param])
    elif layer_type == 2:
        b_var = int(layer.get("brightness_variation_type", 0)) & 0xFF
        b_rng = layer.get("brightness_range", [0, 100])
        if isinstance(b_rng, list):
            b_hi = int(b_rng[1]) & 0xFF if len(b_rng) > 1 else 100
            b_lo = int(b_rng[0]) & 0xFF if len(b_rng) > 0 else 0
        else:
            b_hi = int(b_rng) & 0xFF
            b_lo = 0
        c_count = int(layer.get("blink_cycle_count", 1)) & 0xFF
        pos = int(layer.get("blink_position_mode", 1)) & 0xFF
        b_int = layer.get("blink_interval", [2, 5])
        if isinstance(b_int, list):
            i_hi = int(b_int[1]) & 0xFF if len(b_int) > 1 else 5
            i_lo = int(b_int[0]) & 0xFF if len(b_int) > 0 else 2
        else:
            i_hi = int(b_int) & 0xFF
            i_lo = 0
        qty = int(layer.get("blink_quantity", 255)) & 0xFF
        async_flag = int(layer.get("blink_asynchrony", 1)) & 0xFF
        c_switch = int(layer.get("blink_color_switch_mode", 2)) & 0xFF
        cycle = int(layer.get("light_effect_cycle_method", 0)) & 0xFF
        param = int(layer.get("execution_parameter", 0)) & 0xFF
        trailing.extend([b_var, b_hi, b_lo, c_count, pos, i_hi, i_lo, qty, async_flag, c_switch, cycle, param])

    return header + bytes(color_bytes) + bytes(trailing)


def _build_animation_payload(
    cloud_id: int,
    params: str | dict[str, Any] | None = None,
    speed: int | None = None,
    verified_layers: bool = False,
) -> bytes:
    """Build the binary multi-layer animation payload for the T8L0x family (0x020D).

    The layout is the family's shared one — 0xA3 cloud id, 0xA4 speed, 0xA5 layer
    count, 0xA6 execution mode, 0xA8 mode flag, one 0xA9+ layer blob per layer —
    and matches both the T8L40 captures and the sibling eufy-sdk's 0x020D
    serializer, which it validated on a T8L02 (E22). The verified table below
    holds layer blobs calibrated for the T8L40's 12-lamp strip, so it is only
    replayed when the caller passes ``verified_layers=True`` for that model;
    every other light renders from the catalog ``params`` JSON the app itself
    sends for the model in front of it.
    """
    if verified_layers and cloud_id in _T8L40_VERIFIED_ANIMATION_LAYERS:
        data = _T8L40_VERIFIED_ANIMATION_LAYERS[cloud_id]
        eff_speed = speed if speed is not None else data["speed"]
        body = (
            _tlv(0xA3, cloud_id.to_bytes(4, "little"))
            + _tlv(0xA4, bytes([eff_speed & 0xFF]))
            + _tlv(0xA5, bytes([len(data["layers"]) & 0xFF]))
            + _tlv(0xA6, bytes([data["exec_mode"] & 0xFF]))
            + _tlv(0xA8, b"\x00")
        )
        for i, l_bytes in enumerate(data["layers"]):
            tag = 0xA9 + i
            body += bytes([tag, len(l_bytes)]) + l_bytes
        return body

    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    if not isinstance(params, dict):
        params = {}

    eff_speed = speed if speed is not None else int(params.get("light_effect_speed") or 50)
    layers = params.get("layer", [])
    exec_mode = int(params.get("layer_execution_mode") or 0)
    body = (
        _tlv(0xA3, cloud_id.to_bytes(4, "little"))
        + _tlv(0xA4, bytes([eff_speed & 0xFF]))
        + _tlv(0xA5, bytes([len(layers) & 0xFF]))
        + _tlv(0xA6, bytes([exec_mode & 0xFF]))
        + _tlv(0xA8, b"\x00")
    )
    for i, layer in enumerate(layers):
        l_bytes = _encode_animation_layer(layer)
        tag = 0xA9 + i
        body += bytes([tag, len(l_bytes)]) + l_bytes
    return body


def _parse_effects(data: Any) -> dict[str, dict[str, Any]]:
    """Expose only catalog entries supported by the recovered classic serializer."""
    result = {}
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise EufyLifeCloudError("Invalid light preset catalog")
    for category in data["list"]:
        if not isinstance(category, dict) or not isinstance(
            category.get("scene_info"), list
        ):
            raise EufyLifeCloudError("Invalid preset category")
        for scene in category.get("scene_info", []):
            if not isinstance(scene, dict) or not isinstance(scene.get("light"), list):
                raise EufyLifeCloudError("Invalid preset scene")
            for preset in scene.get("light", []):
                try:
                    if not isinstance(preset, dict):
                        continue
                    name = preset.get("name", "Unknown")
                    palette = preset.get("rgb_hex")
                    if (
                        isinstance(name, str)
                        and name
                        and isinstance(palette, str)
                        and re.fullmatch(
                            r"[0-9a-fA-F]{6}(?:\|[0-9a-fA-F]{6})*", palette
                        )
                        and "dynamic" in preset
                    ):
                        colors = [tuple(bytes.fromhex(c)) for c in palette.split("|")]
                        params = {
                            "light_id": int(preset["light_id"]),
                            "dynamic": int(preset["dynamic"]),
                            "direction": int(preset.get("dynamic_direct", 0)),
                            "speed": int(preset.get("speed", 1)),
                            "colors": colors,
                        }
                        if "scene_id" in scene:
                            params["scene_id"] = int(scene["scene_id"])
                        if preset.get("params"):
                            params["params"] = preset["params"]
                        if preset.get("params_version"):
                            params["params_version"] = int(preset["params_version"])
                        _effect_payload(
                            params["dynamic"],
                            colors,
                            params["direction"],
                            params["speed"],
                            params["light_id"],
                        )
                        result[name] = params
                        continue

                    params_version = int(preset.get("params_version") or 0)
                    if params_version > 0 and preset.get("params"):
                        params = {
                            "params_version": params_version,
                            "params": preset.get("params"),
                            "light_id": int(preset.get("light_id") or 0),
                            "scene_id": int(scene.get("scene_id") or 0),
                        }
                        result[name] = params
                except (KeyError, TypeError, ValueError, EufyLifeCloudError):
                    _LOGGER.debug("Skipping unsupported light preset")
    return result


# The scenes a user builds in the app are not in the catalog the cloud serves
# every account: they live on this route, whose entries carry - in ``light_id`` -
# the id the light reports back in A6 once one of them is applied.
_CATALOG_PATH = "/app/light/lightmode/list"
_PERSONAL_SCENES_PATH = "/app/light/diy/list"
# The routes behind the app's own scene editor and its AI tab. Read off the app's
# Flutter module (``libapp.so``) and confirmed against the live service, where an
# incomplete body is answered with the field it still wants — the required shapes
# below were read off the service that way:
#
#   /app/light/diy/create   {"sn", "name", "is_group", "light_effect": [...]}
#   /app/light/diy/update   the same plus the scene's own "light_id"
#   /app/light/diy/delete   {"sn", "light_id"}
#   /app/light/aigc/get     {"sn", "keywords"} -> a design, newly generated
#   /app/light/aigc/magic   {"sn"}             -> a design, picked at random
#   /app/light/aigc/create   {"sn", "keywords", "name", "rgb_hex", "brightness",
#                             "speed", "dynamic"} -> {"light_id": N}, and the
#                           scene is saved to the account straight away
#   /app/light/aigc/update  {"sn", "keywords", "light_id", ...}
#   /app/light/aigc/recommend/list {"sn", "region"} -> the suggestion chips
#
# A light is addressed by ``sn`` (singular) here, unlike the paged list route
# above which takes ``sns``, and the recommendation route additionally wants the
# region, which the app sends empty.
_DIY_CREATE_PATH = "/app/light/diy/create"
_DIY_UPDATE_PATH = "/app/light/diy/update"
_DIY_DELETE_PATH = "/app/light/diy/delete"
_AIGC_CREATE_PATH = "/app/light/aigc/create"
_AIGC_GET_PATH = "/app/light/aigc/get"
_AIGC_MAGIC_PATH = "/app/light/aigc/magic"
_AIGC_RECOMMEND_PATH = "/app/light/aigc/recommend/list"
_AIGC_REGION = ""
# A generation is answered with the service's own error (190002 was seen live)
# every now and then, for a description that answers fine a moment later, so a
# design is asked for again before that failure is passed on. Every attempt is a
# new generation — the same description does not answer the same design twice —
# so a retry can never hand back a stale one.
_AI_ATTEMPTS = 3
_AI_RETRY_DELAY = 0.5
# The mode a scene that was built segment by segment is saved with: the same
# custom palette mode a plain colour write uses, so a scene built here renders the
# palette it was saved with.
_SCENE_CUSTOM_MODE = 20006
# The app groups every scene it saves under this name.
_SCENE_GROUP_NAME = "default_group"
# The route also answers "is_more" on the page past the end, so the fetch stops on
# an empty page and this bounds the number of requests regardless.
_PERSONAL_SCENE_PAGE_LIMIT = 5


def _scene_palette(effect: dict[str, Any]) -> list[tuple[int, ...]]:
    """The colours an app-built scene renders from.

    A scene stores the palette it was built with (``rgb_hex``). When the cloud
    sends only the per-segment map instead, the colours of the segments the scene
    lights are used in order and without repeats, which is that same palette.
    """
    palette = effect.get("rgb_hex")
    if isinstance(palette, str) and re.fullmatch(
        r"[0-9a-fA-F]{6}(?:\|[0-9a-fA-F]{6})*", palette
    ):
        return [tuple(bytes.fromhex(c)) for c in palette.split("|")]
    colors: list[tuple[int, ...]] = []
    segments = effect.get("light_detail_info")
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict) or not segment.get("state"):
            continue
        hex_value = segment.get("rgb_hex")
        if not isinstance(hex_value, str) or not re.fullmatch(
            r"[0-9a-fA-F]{6}", hex_value
        ):
            continue
        color = tuple(bytes.fromhex(hex_value))
        if color not in colors:
            colors.append(color)
    if not colors:
        raise EufyLifeCloudError("App-built scene has no colours to render")
    return colors


def _parse_personal_scenes(data: Any) -> dict[str, dict[str, Any]]:
    """Expose the scenes the account built in the app, next to the catalog.

    The catalog behind ``/app/light/lightmode/list`` is shared by every account;
    the scenes a user creates in the app's own (Persoonlijk) tab come from
    ``/app/light/diy/list`` instead, which is why a catalog listing can never
    show them. Each scene is reduced to the preset shape the catalog parser
    produces, so the effect list and ``async_set_effect`` handle it unchanged,
    with the app's own type kept as ``light_type`` (1 for a scene built on a
    catalog mode's palette, 4 for one the app renders segment by segment).
    """
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise EufyLifeCloudError("Invalid app-built scene list")
    for scene in data["list"]:
        if not isinstance(scene, dict):
            raise EufyLifeCloudError("Invalid app-built scene")
        name = scene.get("name")
        effects = scene.get("light_effect")
        if not isinstance(name, str) or not name:
            raise EufyLifeCloudError("App-built scene is missing its name")
        if not isinstance(effects, list) or not effects:
            raise EufyLifeCloudError("App-built scene is missing its effect")
        effect = effects[0]
        if not isinstance(effect, dict):
            raise EufyLifeCloudError("Invalid app-built scene effect")
        try:
            colors = _scene_palette(effect)
            direction = int(effect.get("dynamic_direct") or 0)
            speed = int(effect.get("speed") or 1)
            params = {
                # The scene's own cloud id, which the light echoes back in A6.
                "light_id": int(scene["light_id"]),
                "dynamic": int(effect["dynamic"]),
                "direction": max(0, min(65535, direction)),
                # The frame carries 1-10; a scene saved below that renders at 1.
                "speed": max(1, min(10, speed)),
                "colors": colors,
                "light_type": int(scene.get("light_type") or 0),
            }
            _effect_payload(
                params["dynamic"],
                colors,
                params["direction"],
                params["speed"],
                params["light_id"],
            )
            result[name] = params
        except (KeyError, TypeError, ValueError, EufyLifeCloudError):
            _LOGGER.debug("Skipping unsupported app-built scene %s", name)
    return result


def _scene_palette_hex(colors: list[tuple[int, ...]]) -> str:
    """A palette in the pipe-separated form the service stores it in."""
    return "|".join(bytes(color[:3]).hex() for color in colors)


def _scene_detail(
    colors: list[tuple[int, ...]], lamp_count: int | None
) -> list[dict[str, Any]]:
    """The per-segment map of a saved scene, when it addresses segments.

    A scene saved from a palette (a handful of colours that the mode repeats
    along the strip) carries an empty map; one built segment by segment carries an
    entry per segment, with ``state`` 2 for a lit segment and 0 for one that is
    switched off. The service refuses a scene whose map is nil, so the empty list
    is what a palette scene sends.
    """
    if lamp_count is None or len(colors) != lamp_count:
        return []
    detail: list[dict[str, Any]] = []
    for pos, color in enumerate(colors):
        rgb = tuple(color[:3])
        if not any(rgb):
            detail.append({"pos": pos, "rgb_hex": "000000", "state": 0})
            continue
        detail.append({"pos": pos, "rgb_hex": bytes(rgb).hex(), "state": 2})
    return detail


def _scene_effect(
    colors: list[tuple[int, ...]],
    *,
    mode: int,
    brightness: int,
    speed: int,
    direction: int,
    context: str,
    lamp_count: int | None,
) -> dict[str, Any]:
    """One ``light_effect`` entry in the shape the scene routes validate.

    The palette is written twice, as the app does: ``rgb_hex`` is what the editor
    reads back and ``light_detail_info`` is what a per-segment scene renders.
    """
    if not colors:
        raise EufyLifeCloudError("A scene needs at least one colour")
    return {
        "brightness": brightness,
        "context": context,
        "cover": "",
        "dynamic": str(mode),
        "dynamic_direct": str(direction),
        "group_name": _SCENE_GROUP_NAME,
        "light_detail_info": _scene_detail(colors, lamp_count),
        "params": "",
        "params_version": 0,
        "rgb_hex": _scene_palette_hex(colors),
        "speed": speed,
    }


_AI_PALETTE = re.compile(r"[0-9a-fA-F]{6}(?:\|[0-9a-fA-F]{6})*")


def _parse_ai_effect(data: Any) -> dict[str, Any]:
    """The design the AI answers with, in the shape a scene effect uses.

    ``/app/light/aigc/get`` and ``/app/light/aigc/magic`` answer one effect: the
    description the AI wrote for it (``context``), the palette it chose
    (``rgb_hex``), the mode it renders with (``dynamic``), and the brightness and
    speed it picked. Two calls with the same keywords do not answer the same
    design, so this is an idea rather than a stored lookup.
    """
    if not isinstance(data, dict):
        raise EufyLifeCloudError("Invalid AI light effect")
    palette = data.get("rgb_hex")
    if not isinstance(palette, str) or not _AI_PALETTE.fullmatch(palette):
        raise EufyLifeCloudError("AI light effect has no palette")
    dynamic = data.get("dynamic")
    if not isinstance(dynamic, str) or not dynamic.isdigit():
        raise EufyLifeCloudError("AI light effect has no mode")
    try:
        return {
            "colors": [tuple(bytes.fromhex(color)) for color in palette.split("|")],
            "dynamic": int(dynamic),
            "direction": int(data.get("dynamic_direct") or 0),
            "speed": max(1, min(10, int(data.get("speed") or 1))),
            "brightness": max(0, min(100, int(data.get("brightness") or 100))),
            "context": str(data.get("context") or ""),
            "cover": str(data.get("cover") or ""),
        }
    except (TypeError, ValueError) as err:
        raise EufyLifeCloudError(f"Invalid AI light effect: {err}") from err


def _parse_frame(frame: bytes) -> tuple[tuple[int, int], bytes]:
    if len(frame) < 10 or frame[:2] != b"\xff\x09":
        raise ValueError("Invalid E10 frame header")
    if frame[4] != 3 or frame[5] not in (0, 1) or frame[6] != 2:
        raise ValueError("Invalid E10 frame protocol")
    if int.from_bytes(frame[2:4], "little") != len(frame):
        raise ValueError("Invalid E10 frame length")
    if _xor(frame[:-1]) != frame[-1]:
        raise ValueError("Invalid E10 frame checksum")
    return (frame[7], frame[8]), frame[9:-1]


def _parse_tlvs(payload: bytes) -> dict[int, bytes]:
    result: dict[int, bytes] = {}
    offset = 0
    while offset < len(payload):
        if offset + 2 > len(payload):
            raise ValueError("Truncated E10 TLV header")
        tag, length = payload[offset : offset + 2]
        offset += 2
        if offset + length > len(payload):
            raise ValueError("Truncated E10 TLV value")
        result[tag] = payload[offset : offset + length]
        offset += length
    return result


def _unwrap_frame(data: Any) -> bytes | None:
    """Pull the raw E10 frame out of whatever nest of encodings carries it.

    The broker passes the frame as hex, as base64 of a ``{"data": ...}`` JSON
    document or as either of those nested in the other, so every encoding is
    tried until the ``ff 09`` frame header shows up. Used for the light's
    replies and for the frames the phone app publishes, which are wrapped
    exactly the same way.
    """
    if isinstance(data, bytes):
        if data.startswith(b"\xff\x09"):
            return data
        try:
            return _unwrap_frame(data.decode())
        except Exception:
            return None
    if not isinstance(data, str):
        return None
    if not data:
        return None

    # Try Hex
    if len(data) >= 20 and all(c in "0123456789abcdefABCDEF" for c in data[:20]):
        try:
            b = bytes.fromhex(data)
            if b.startswith(b"\xff\x09"):
                return b
        except Exception:
            pass

    # Try JSON
    if data.startswith("{"):
        try:
            j = json.loads(data)
            if isinstance(j, dict) and "data" in j:
                return _unwrap_frame(j["data"])
        except Exception:
            pass

    # Try Base64
    try:
        b = base64.b64decode(data, validate=True)
        return _unwrap_frame(b)
    except Exception:
        pass

    return None


def _paho_client_arguments(client_id: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Return the ``mqtt.Client`` arguments for the installed paho-mqtt.

    paho-mqtt 2.0 turned the callback API into an explicit constructor argument
    and paho-mqtt 1.6.1 (which Home Assistant pinned up to 2024.10, and therefore
    what the scripts find in a dev environment) does not have
    ``CallbackAPIVersion`` at all. Both are supported: with 1.x the callback API
    is implicit and ``clean_session`` is passed the classic way, with 2.x
    ``VERSION2`` is requested and ``clean_session`` defaults to true for MQTTv311.
    """
    callback_api_version = getattr(mqtt, "CallbackAPIVersion", None)
    if callback_api_version is None:
        return (), {
            "client_id": client_id,
            "clean_session": True,
            "protocol": mqtt.MQTTv311,
        }
    return (callback_api_version.VERSION2,), {
        "client_id": client_id,
        "protocol": mqtt.MQTTv311,
    }


def _create_mqtt_client(client_id: str) -> mqtt.Client:
    """Build the MQTT client with whichever paho-mqtt API is installed."""
    args, kwargs = _paho_client_arguments(client_id)
    return mqtt.Client(*args, **kwargs)


def _mqtt_reason_code(value: Any) -> int:
    """Return the numeric result code of a paho 1.x return code or 2.x ReasonCode."""
    value = getattr(value, "value", value)
    return value if isinstance(value, int) else 0


class EufyLifeLightCloud:
    """Discover and control Eufy lights through Eufy's app cloud.

    Every light the account exposes is discovered. Model codes verified against
    real hardware get their specific protocol; anything else (for example the
    E22 permanent outdoor lights) is driven through the generic E10 path.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        user_id: str,
        user_center_id: str,
        user_center_token: str,
        openudid: str,
        country: str,
        language: str,
        timezone: str,
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._user_center_id = user_center_id
        self._user_center_token = user_center_token
        self._openudid = openudid
        self._country = country.upper()
        self._language = language
        self._timezone = timezone
        self._base_url = LIGHT_API_BASE_URL
        self._crypto = _LightCrypto()
        self._loop = asyncio.get_running_loop()
        self._mqtt: mqtt.Client | None = None
        self._mqtt_files: tempfile.TemporaryDirectory[str] | None = None
        self._listeners: dict[str, set[Callable[[], None]]] = defaultdict(set)
        # Link-level listeners follow the MQTT link itself (up or down) rather
        # than one light, so an entity can say whether the link the whole account
        # shares is up while every light is silent.
        self._link_listeners: set[Callable[[], None]] = set()
        self.devices: dict[str, EufyLifeLightDevice] = {}
        self.connected = False
        self._effect_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._effect_replies: dict[str, asyncio.Future[None]] = {}
        self._msg_seq = 0
        # The broker delivers our own QoS 1 publish back to us because we
        # subscribe to the topic we command on. The phone app writes the same
        # head ``client_id`` as this integration (both are
        # ``android-eufy_life-<user id>``), so an echo is recognised by the
        # payload that was published here instead of by that head — otherwise
        # the app's own frames would be dropped as ours and never logged.
        self._own_payloads: deque[bytes] = deque(maxlen=64)

    async def async_start(self) -> None:
        """Discover lights and start the app's certificate-authenticated MQTT link."""
        async with self._session.post(
            f"{LIGHT_API_BASE_URL}/passport/estimate_domain",
            headers={"App-Name": "eufy_life"},
            json={"ab": self._country, "mode": 1},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            response.raise_for_status()
            region = await response.json(content_type=None)
        if not isinstance(region, dict) or region.get("code") != 0:
            raise EufyLifeCloudError("Light region lookup was rejected")
        data = region.get("data")
        domain = data.get("domain") if isinstance(data, dict) else None
        if not isinstance(domain, str):
            raise EufyLifeCloudError("Light region response is missing its domain")
        domain = domain.removeprefix("https://")
        if (
            not domain.endswith(".eufylife.com")
            or urlsplit(f"https://{domain}").hostname != domain
        ):
            raise EufyLifeCloudError("Invalid light region domain")
        self._base_url = f"https://{domain}"
        await self._crypto.async_exchange(self._session, self._base_url)
        discovered = await self._async_post("/app/devicerelation/get_device_list", {})
        relations = (
            discovered.get("devices", []) if isinstance(discovered, dict) else []
        )
        for relation in relations or []:
            if not isinstance(relation, dict):
                continue
            raw = relation.get("device", relation)
            if not isinstance(raw, dict):
                continue
            model = raw.get("device_model")
            if not isinstance(model, str) or not model.strip():
                _LOGGER.debug("Skipping Eufy light without a model code: %s", raw)
                continue
            model = model.strip()
            serial = raw.get("device_sn")
            if not isinstance(serial, str) or not serial:
                continue
            member = raw.get("member")
            account_id = self._user_id
            if isinstance(member, dict) and member.get("member_type") != 2:
                account_id = member.get("admin_user_id")
                if not isinstance(account_id, str) or not account_id:
                    raise EufyLifeCloudError("Shared light is missing its owner ID")
            name = str(raw.get("device_name") or "Eufy Light")
            # A light that is switched off at the mains is still listed with its
            # last known state, so the inventory's own ``device_status`` is the
            # only reachability such a light has: it reads 0 where a reachable
            # light reports 1. The value is kept next to what the light says
            # itself instead of overwriting it, because this one is read once and
            # the light's own status topic keeps talking.
            status = raw.get("device_status")
            cloud_status: bool | None = None
            if isinstance(status, bool):
                cloud_status = status
            elif isinstance(status, int):
                cloud_status = bool(status)
            if model not in _KNOWN_MODELS:
                _LOGGER.info(
                    "Discovered %s (%s, model %s) which is not in the verified "
                    "model list %s; controlling it through the generic E10 path",
                    name,
                    serial,
                    model,
                    sorted(_KNOWN_MODELS),
                )
            self.devices[serial] = EufyLifeLightDevice(
                serial=serial,
                name=name,
                model=model,
                account_id=account_id,
                cloud_status=cloud_status,
            )

        if not self.devices:
            return
        for device in self.devices.values():
            try:
                device.effects = _parse_effects(
                    await self._async_post(
                        _CATALOG_PATH,
                        {"sns": [device.serial], "light_type": None, "scene_id": None},
                    )
                )
            except (
                aiohttp.ClientError,
                TimeoutError,
                ValueError,
                TypeError,
                EufyLifeCloudError,
            ):
                _LOGGER.warning(
                    "Light presets unavailable; power and RGB remain supported"
                )
            try:
                # The app-built scenes come last: a name the user created wins
                # over a catalog entry that happens to be called the same.
                device.effects.update(
                    await self.async_fetch_personal_scenes(device.serial)
                )
            except (
                aiohttp.ClientError,
                TimeoutError,
                ValueError,
                TypeError,
                EufyLifeCloudError,
            ):
                _LOGGER.debug(
                    "App-built scenes unavailable for %s; catalog presets remain",
                    device.serial,
                )
        mqtt_info = await self._async_post("/app/devicemanage/get_user_mqtt_info", {})
        if not isinstance(mqtt_info, dict):
            raise EufyLifeCloudError("MQTT response is missing credentials")
        await asyncio.to_thread(self._start_mqtt, mqtt_info)

    async def async_fetch_personal_scenes(
        self, serial: str
    ) -> dict[str, dict[str, Any]]:
        """The scenes the account built in the app, paged from the light cloud.

        The catalog is shared with every account, so a scene a user created for
        this light exists only here. The route wants a ``start_index`` and keeps
        answering ``is_more`` even on the page past the end, so the loop stops on
        the first empty page and stays bounded regardless.
        """
        scenes: dict[str, dict[str, Any]] = {}
        start = 0
        for _ in range(_PERSONAL_SCENE_PAGE_LIMIT):
            data = await self._async_post(
                _PERSONAL_SCENES_PATH, {"sns": [serial], "start_index": start}
            )
            scenes.update(_parse_personal_scenes(data))
            page = data.get("list") if isinstance(data, dict) else None
            if not page or not data.get("is_more"):
                break
            start += len(page)
        return scenes

    async def _async_post(self, path: str, body: dict[str, Any]) -> Any:
        raw_body = json.dumps(body, separators=(",", ":"))
        encrypted, request_timestamp, request_nonce, crypto_headers = (
            self._crypto.encrypt(raw_body)
        )
        timestamp = str(int(time.time()))
        headers = {
            "X-Request_Ts": timestamp,
            "X-Request_Once": self._openudid,
            "Unique-Sign": self._openudid,
            "Gtoken": hashlib.md5(self._user_center_id.encode()).hexdigest(),
            "X-Auth-Token": self._user_center_token,
            "X-Custom": '{"light_effect":"new"}',
            "App-Name": "eufy_life",
            "Model-Type": "PHONE",
            "Openudid": self._openudid,
            "Content-Type": "application/json",
            "Timezone": self._timezone,
            "Os-Type": "android",
            "Os-Version": platform.release(),
            "Phone-Model": platform.machine(),
            "App-Version": USER_AGENT_VERSION,
            "Country": self._country,
            "Language": self._language,
            "ab_code": self._country,
            "Encrypt-Algorithm": "algorithm_ecdh",
            **crypto_headers,
        }
        async with self._session.post(
            f"{self._base_url}{path}",
            headers=headers,
            data=encrypted,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            response.raise_for_status()
            response_body = await response.text()
            response_headers = response.headers

        if response_headers.get("encrypt-algorithm") == "algorithm_ecdh":
            response_body = self._crypto.decrypt(
                response_body,
                request_timestamp,
                request_nonce,
            )
        result = json.loads(response_body)
        if not isinstance(result, dict) or result.get("code") != 0:
            code = (
                result.get("code") if isinstance(result, dict) else "invalid response"
            )
            raise EufyLifeCloudError(f"Light API {path} failed: {code}")
        return result.get("data")

    def _start_mqtt(self, info: dict[str, Any]) -> None:
        endpoint = _required_string(info, "endpoint_addr")
        certificate = _required_string(info, "certificate_pem")
        private_key = _required_string(info, "private_key")

        files = tempfile.TemporaryDirectory(prefix="eufylife-mqtt-")
        certificate_path = Path(files.name, "client.pem")
        key_path = Path(files.name, "client.key")
        certificate_path.write_text(certificate)
        key_path.write_text(private_key)
        os.chmod(key_path, 0o600)
        # Eufy's supplied legacy CA fails strict X.509; the broker uses a public CA.
        context = ssl.create_default_context()
        context.load_cert_chain(certificate_path, key_path)

        client_id = f"android-eufy_life-{self._user_id}-{self._openudid}"
        client = _create_mqtt_client(client_id)
        client.tls_set_context(context)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(endpoint, 8883, 60)
        self._mqtt_files = files
        self._mqtt = client
        client.loop_start()

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        """Subscribe and refresh every light once the broker accepts the link.

        paho 1.x stops its callback arguments at the result code and paho 2.x
        passes the acknowledgement properties as a fifth argument, so the
        trailing parameter is optional. Both pass the result code in fourth
        place, as an ``int`` (1.x) or as a ``ReasonCode`` (2.x).
        """
        if _mqtt_reason_code(reason_code) != 0:
            _LOGGER.error("Eufy Life MQTT connection rejected: %s", reason_code)
            return
        self.connected = True
        for device in self.devices.values():
            prefix = f"eufy_life/{device.model}/{device.serial}"
            for topic in (
                f"cmd/{prefix}/req",
                f"cmd/{prefix}/res",
                f"cmd/{prefix}/app/req",
                f"cmd/{prefix}/app/res",
                f"cmd/{prefix}/app/ota/res",
                f"synq/{prefix}/state_info",
            ):
                client.subscribe(topic, qos=1)
            self.request_settings(device.serial)
        self._notify()

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        *args: Any,
    ) -> None:
        """Handle a dropped link on paho 1.x and 2.x alike.

        paho 1.x calls ``on_disconnect(client, userdata, rc)`` and paho 2.x calls
        ``(client, userdata, disconnect_flags, reason_code, properties)``.
        """
        reason_code = args[-2] if len(args) >= 3 else (args[0] if args else 0)
        self.connected = False
        if _mqtt_reason_code(reason_code) != 0:
            _LOGGER.warning("Eufy Life MQTT disconnected: %s", reason_code)
        self._notify()

    def _on_message(
        self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage
    ) -> None:
        # A request frame is logged decoded below: the phone app polls a light
        # with a settings request every few seconds while it is open, so dumping
        # each of those raw frames would bury the one frame a scene capture is
        # after. Reply topics keep the raw dump.
        log = _LOGGER.debug if message.topic.endswith("/req") else _LOGGER.info
        log("MQTT message received on %s: %s", message.topic, message.payload[:200])
        try:
            parts = message.topic.split("/")
            if len(parts) < 5 or (serial := parts[3]) not in self.devices:
                return
            outer = json.loads(message.payload.decode())
            inner = outer.get("payload")
            if isinstance(inner, str):
                inner = json.loads(inner)
            if message.topic.endswith("/req"):
                # Only the phone app and this integration command a light, and
                # the broker delivers our own QoS 1 publish back to us because we
                # subscribe to the topic we publish on. The app writes the same
                # head ``client_id`` as we do, so the echo is told apart by the
                # payload this integration published: keying off that head would
                # silently drop the app's own frames, which are the reference for
                # everything the integration cannot write yet (the catalog's
                # layered presets and the app's personal scenes are all built by
                # the phone itself).
                if message.payload in self._own_payloads:
                    _LOGGER.debug("Ignoring our own command echoed on %s", message.topic)
                    return
                self._log_app_command(serial, inner)
                return
            if not isinstance(inner, dict):
                _LOGGER.info("MQTT payload is not a dict")
                return
            if message.topic.endswith("/state_info"):
                status = inner.get("status")
                if isinstance(status, bool):
                    self.devices[serial].online = status
                    self._notify(serial)
                return

            frame_bytes = _unwrap_frame(inner.get("data"))
            if not frame_bytes:
                _LOGGER.info("MQTT message on %s could not be unwrapped to an E10 frame: %s", message.topic, inner)
                return

            _LOGGER.info("Unwrapped frame bytes for %s (len=%s): %s", serial, len(frame_bytes), frame_bytes.hex())
            opcode, payload = _parse_frame(frame_bytes)
            self._handle_frame(serial, opcode, payload)
        except Exception:  # MQTT input is an external trust boundary.
            _LOGGER.exception("Discarding an invalid Eufy Life MQTT message")

    def _log_app_command(self, serial: str, inner: Any) -> None:
        """Log a frame the phone app published for one of its lights.

        The app's frames are the reference for everything the integration does
        not write itself yet, so they are decoded and printed instead of being
        parsed: a command from the phone says nothing about the light's current
        state, and an app-side frame that happens to carry the report opcode
        must not resolve a pending write or replace the cached state.

        The app polls the light with a settings request every few seconds while
        it is open, so those stay at debug level and only a frame that actually
        commands the light is logged as traffic.
        """
        data = inner.get("data") if isinstance(inner, dict) else inner
        frame_bytes = _unwrap_frame(data)
        if not frame_bytes:
            _LOGGER.debug("Eufy app frame on %s could not be unwrapped: %s", serial, inner)
            return
        try:
            opcode, payload = _parse_frame(frame_bytes)
        except ValueError:
            _LOGGER.debug("Eufy app frame for %s is not an E10 frame: %s", serial, frame_bytes.hex())
            return
        log = _LOGGER.debug if opcode == _GET_SETTINGS else _LOGGER.info
        log("Eufy app frame for %s: opcode=%s, payload=%s", serial, opcode, payload.hex())

    def _handle_frame(
        self, serial: str, opcode: tuple[int, int], payload: bytes
    ) -> None:
        _LOGGER.info("Frame received for %s: opcode=%s, payload=%s", serial, opcode, payload.hex())

        # Animation-protocol lights report the applied state on (0x02, 0x04)
        # instead of echoing the command opcode, so a report also acks it.
        if self.devices[serial].animation_protocol and opcode == (0x02, 0x04):
            self._loop.call_soon_threadsafe(self._complete_effect, serial, 0)

        if opcode == (0x02, 0x00):
            self._loop.call_soon_threadsafe(self._complete_effect, serial, 0)
            return

        if opcode in (
            _SET_EFFECT_RESPONSE,
            _SET_ANIMATION_RESPONSE,
            _SET_LIGHT_SHOW_RESPONSE,
            _SET_LIGHT_AI_RESPONSE,
            _SET_SCENE_RESPONSE,
        ):
            # Captured 00 a1 01 00: envelope success plus command result TLV A1.
            # Some responses (like SET_SCENE) are just a single byte 00.
            if not payload:
                raise ValueError("Effect response is missing status")
            status = payload[0]
            if status == 0 and len(payload) > 1:
                tlvs = _parse_tlvs(payload[1:])
                result = tlvs.get(0xA1)
                if result is not None and len(result) == 1:
                    status = result[0]
            # result: 00 = Success, anything else = Failure
            self._loop.call_soon_threadsafe(self._complete_effect, serial, status)
            return
        if opcode in (_GET_SETTINGS_RESPONSE, _SET_POWER_RESPONSE):
            if not payload:
                raise ValueError("E10 response is missing its status")
            if payload[0] != 0:
                _LOGGER.warning("Eufy Life command %s rejected: %s", opcode, payload[0])
                return
            payload = payload[1:]
        if opcode in (_GET_SETTINGS_RESPONSE, _REPORT_DEVICE_INFO):
            values = _parse_tlvs(payload)
            power = values.get(0xA1)
            brightness = values.get(0xA2)
            if brightness is not None and (len(brightness) != 1 or brightness[0] > 100):
                raise ValueError("Invalid E10 brightness percentage")
            if power is not None:
                self.devices[serial].is_on = int.from_bytes(power, "little") == 1
            if brightness is not None:
                self.devices[serial].brightness = brightness[0]
            device = self.devices[serial]
            device.last_report = time.time()
            if (count := values.get(0xA3)) is not None:
                if len(count) not in (1, 2):
                    raise ValueError("Invalid light count")
                device.lamp_count = int.from_bytes(count, "little")
            if (mode := values.get(0xA4)) is not None:
                # Settings use uint16; unsolicited reports use uint32 (captured).
                if len(mode) not in (2, 4):
                    raise ValueError("Invalid light effect ID")
                light_id = int.from_bytes(mode, "little")
                if light_id != device.light_id:
                    device.rgb_color = None
                    device.effect = None
                    device.effect_id = None
                    device.colors = []
                device.light_id = light_id
            cloud_id = values.get(0xA6)
            if cloud_id and len(cloud_id) == 4:
                # The cloud id is the effect's identity, so the name is looked up
                # on every report, and one the account cannot name leaves no name
                # at all: two app-built scenes can share the mode in A4, so the
                # name that ran before would be a lie. The id the light showed is
                # kept either way.
                cid = int.from_bytes(cloud_id, "little")
                if cid:
                    device.effect_id = cid
                    device.effect = next(
                        (
                            name
                            for name, preset in device.effects.items()
                            if preset.get("light_id") == cid
                        ),
                        None,
                    )
                    _adopt_effect_settings(device)
            self._notify(serial)
            return
        if opcode == _SET_POWER_RESPONSE:
            self.request_settings(serial)

    def request_settings(self, serial: str) -> None:
        payload = _command_payload(
            self._user_id, _tlv(0xA3, (0x1FF).to_bytes(4, "little"))
        )
        self._publish(serial, _GET_SETTINGS, payload)

    def _complete_effect(self, serial: str, status: int) -> None:
        future = self._effect_replies.get(serial)
        if future is not None and not future.done():
            if status:
                future.set_exception(
                    EufyLifeCloudError(f"Light rejected effect: {status}")
                )
            else:
                future.set_result(None)

    async def async_handshake(self, serial: str) -> None:
        """Perform the iOS-style session handshake (Opcode 0200)."""
        # A1: Timestamp, A2: UserID, A3: ff010000 (Session Mask)
        value = _tlv(0xA3, bytes.fromhex("ff010000"))
        payload = _command_payload(self._user_id, value)

        async with self._effect_locks[serial]:
            future = self._loop.create_future()
            self._effect_replies[serial] = future
            try:
                _LOGGER.info("Performing Session Handshake for %s", serial)
                # Handshake always uses Version 0
                self._publish(serial, (0x02, 0x00), payload, version=0)
                await asyncio.wait_for(future, timeout=_EFFECT_RESPONSE_TIMEOUT)
                _LOGGER.info("Handshake Success for %s", serial)
            except asyncio.TimeoutError:
                _LOGGER.warning("Handshake timed out for %s, continuing anyway", serial)
            finally:
                self._effect_replies.pop(serial, None)

    async def async_set_effect(
        self,
        serial: str,
        rgb_color: tuple[int, int, int] | None = None,
        rgbww_color: tuple[int, int, int, int, int] | None = None,
        effect: str | None = None,
        colors: list[tuple[int, ...]] | None = None,
        speed: int | None = None,
        direction: int | None = None,
        params: str | None = None,
        use_ai_opcode: bool = False,
        refresh: bool = True,
    ) -> None:
        """Wait for the device result before remembering the selected palette."""
        device = self.devices[serial]

        # Firmware that needs the 0x0200 handshake before it accepts an effect
        # (T8L40) gets one here; the E22 answers effects without it.
        if device.session_handshake and effect is not None:
            await self.async_handshake(serial)
            await asyncio.sleep(0.5)

        if (rgb_color is not None or rgbww_color is not None) and effect is not None:
            raise EufyLifeCloudError("Choose either a color or a preset")

        # Fallback to current device state when adjusting speed or direction alone
        if (
            effect is None
            and params is None
            and colors is None
            and rgb_color is None
            and rgbww_color is None
            and (speed is not None or direction is not None)
        ):
            if device.effect is not None and device.effect in device.effects:
                effect = device.effect
            elif device.colors:
                colors = device.colors
            elif device.rgbww_color is not None:
                rgbww_color = device.rgbww_color
            elif device.rgb_color is not None:
                rgb_color = device.rgb_color
            else:
                rgbww_color = (255, 255, 255, 0, 0)

        # Determine parameters
        target_speed = speed if speed is not None else (device.speed if device.speed is not None else 1)
        target_direction = direction if direction is not None else (device.direction if device.direction is not None else 0)
        target_cloud_id = None
        light_id = None
        target_colors = None
        target_params = params

        use_version_1 = False
        if effect is not None:
            if effect not in device.effects:
                raise EufyLifeCloudError(f"Unsupported light preset: {effect}")
            p = device.effects[effect]
            target_cloud_id = p.get("light_id")

            # The captured T8L40 layer blobs are calibrated for that strip, so they
            # are only replayed on the code they came from.
            if (
                device.model == _T8L40_MODEL
                and target_cloud_id in _T8L40_VERIFIED_ANIMATION_LAYERS
            ):
                opcode = _SET_ANIMATION
                value = _build_animation_payload(
                    target_cloud_id or 0, p.get("params"), speed, verified_layers=True
                )
                payload = _command_payload(self._user_id, value)
            # Every animation-protocol light (the E22/T8L02 included) renders the
            # catalog's own params JSON: that is the frame the app sends to the
            # model in front of it, with the model's own segment geometry.
            elif device.animation_protocol and "params" in p:
                opcode = _SET_ANIMATION
                value = _build_animation_payload(
                    target_cloud_id or 0, p.get("params"), speed
                )
                payload = _command_payload(self._user_id, value)
            elif "dynamic" in p:
                opcode = _SET_EFFECT
                light_id = p["dynamic"]
                target_colors = p["colors"]
                if speed is None:
                    target_speed = p["speed"]
                if direction is None:
                    target_direction = p["direction"]
                value = _effect_payload(
                    light_id, target_colors, target_direction, target_speed, target_cloud_id
                )
                payload = _command_payload(self._user_id, value)
            elif "params" in p:
                opcode = _SET_LIGHT_AI if use_ai_opcode else _SET_LIGHT_SHOW
                light_id = 0
                target_params = p["params"]
                value = (
                    _tlv_long(0xA3, target_cloud_id.to_bytes(2, "little"))
                    + _tlv_long(0xA4, target_params.encode())
                    + _tlv(0xA8, b"\x64")
                    + _tlv(0xA9, bytes(5))
                    + _tlv(0xAA, b"\x00")
                    + _tlv(0xAE, b"\x00")
                    + _tlv(0xB0, b"\x00")
                )
                payload = _command_payload(self._user_id, value)
        elif target_params is not None:
            # Custom JSON animation
            if device.animation_protocol:
                opcode = _SET_ANIMATION
                value = _build_animation_payload(0, target_params, speed)
                payload = _command_payload(self._user_id, value)
            else:
                opcode = _SET_LIGHT_AI if use_ai_opcode else _SET_LIGHT_SHOW
                light_id = 0
                value = (
                    _tlv_long(0xA4, target_params.encode())
                    + _tlv(0xA8, b"\x64")
                    + _tlv(0xA9, bytes(5))
                    + _tlv(0xAA, b"\x00")
                    + _tlv(0xAE, b"\x00")
                    + _tlv(0xB0, b"\x00")
                )
                payload = _command_payload(self._user_id, value)
        elif colors is not None:
            opcode = _SET_EFFECT
            if not device.lamp_count:
                 raise EufyLifeCloudError("Waiting for the light's lamp count")
            if len(colors) != device.lamp_count:
                raise EufyLifeCloudError(f"Effect requires exactly {device.lamp_count} colors")
            light_id = 20006
            target_colors = colors
            try:
                value = _effect_payload(
                    light_id, target_colors, target_direction, target_speed, None, 7
                )
            except (TypeError, ValueError) as err:
                raise EufyLifeCloudError(f"Invalid color palette: {err}") from err
            payload = _command_payload(self._user_id, value)
        else:
            # Single color for all segments
            if rgb_color is None and rgbww_color is None:
                 raise EufyLifeCloudError("Provide a color, effect, or color list")
            if not device.lamp_count:
                raise EufyLifeCloudError("Waiting for the light's lamp count")
            opcode = _SET_EFFECT
            light_id = 20006
            color = rgbww_color if rgbww_color is not None else rgb_color
            try:
                target_colors = [tuple(color)] * device.lamp_count
                value = _effect_payload(
                    light_id, target_colors, target_direction, target_speed, None, 7
                )
                payload = _command_payload(self._user_id, value)
            except (TypeError, ValueError) as err:
                raise EufyLifeCloudError("Invalid color or lamp count") from err

        async with self._effect_locks[serial]:
            future = self._loop.create_future()
            self._effect_replies[serial] = future
            try:
                self._publish(
                    serial, opcode, payload, version=1 if use_version_1 else 0
                )
                await asyncio.wait_for(future, timeout=_EFFECT_RESPONSE_TIMEOUT)
            except asyncio.TimeoutError as err:
                raise EufyLifeCloudError(
                    "Light did not acknowledge the color/preset"
                ) from err
            finally:
                self._effect_replies.pop(serial, None)

            # ponytail: ACK-backed selection, not palette readback
            device.light_id = light_id if light_id else target_cloud_id
            device.rgb_color = tuple(rgb_color) if rgb_color is not None else None
            device.rgbww_color = tuple(rgbww_color) if rgbww_color is not None else None
            device.effect = effect
            # The id of the effect that was asked for, kept until the light
            # reports which one it actually runs (A6).
            device.effect_id = target_cloud_id if effect is not None else None
            device.speed = target_speed
            device.direction = target_direction
            device.colors = target_colors
            if refresh:
                self.request_settings(serial)
            self._notify(serial)

    async def async_set_scene(
        self,
        serial: str,
        scene_id: int,
        refresh: bool = True,
    ) -> None:
        """Set a specific cloud scene by ID (Opcode 0202)."""
        device = self.devices[serial]

        # Firmware that needs the session handshake before a scene write.
        if device.session_handshake:
            await self.async_handshake(serial)
            await asyncio.sleep(0.5)

        value = _tlv(0xA3, scene_id.to_bytes(4, "little"))
        payload = _command_payload(self._user_id, value)

        async with self._effect_locks[serial]:
            future = self._loop.create_future()
            self._effect_replies[serial] = future
            try:
                self._publish(
                    serial, _SET_SCENE, payload
                )
                await asyncio.wait_for(future, timeout=_EFFECT_RESPONSE_TIMEOUT)
            except asyncio.TimeoutError as err:
                raise EufyLifeCloudError(
                    "Light did not acknowledge the scene change"
                ) from err
            finally:
                self._effect_replies.pop(serial, None)

            if refresh:
                self.request_settings(serial)
            self._notify(serial)

    def set_power(
        self, serial: str, is_on: bool, brightness: int | None = None
    ) -> None:
        """Publish power; only device settings/reports change the entity state."""
        value = _tlv(0xA3, bytes((int(is_on),)))
        if brightness is not None:
            if not 0 <= brightness <= 100:
                raise EufyLifeCloudError("Brightness must be between 0 and 100 percent")
            value += _tlv(0xA4, bytes((brightness,)))
        self._publish(
            serial,
            _SET_POWER,
            _command_payload(self._user_id, value),
        )

    @property
    def _command_client_id(self) -> str:
        """The app's MQTT client id, as used in the head of every command.

        The broker also delivers our own publishes back to us, so the echoed
        head is how a message is recognised as ours instead of the light's.
        """
        return f"android-eufy_life-{self._user_id}"

    def _publish(self, serial: str, opcode: tuple[int, int], payload: bytes, version: int = 0) -> None:
        if not self.connected or self._mqtt is None:
            raise EufyLifeCloudError("Eufy Life MQTT is not connected")
        device = self.devices[serial]

        self._msg_seq = (self._msg_seq + 1) % 1000
        timestamp = int(time.time())

        command = {
            "head": {
                "version": "1.0.0.1",
                "client_id": self._command_client_id,
                "sess_id": "1",
                "msg_seq": self._msg_seq,
                "cmd": 17,
                "cmd_status": 1,
                "sign_code": 0,
                "seed": "",
                "timestamp": timestamp,
            },
            "payload": json.dumps(
                {
                    "account_id": device.account_id,
                    "device_sn": serial,
                    "data": base64.b64encode(_frame(opcode, payload, version)).decode(),
                },
                separators=(",", ":"),
            ),
        }
        body = json.dumps(command, separators=(",", ":"))
        # Remember what went out so the broker's echo of it is recognisable.
        self._own_payloads.append(body.encode())
        result = self._mqtt.publish(
            f"cmd/eufy_life/{device.model}/{serial}/req",
            body,
            qos=1,
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise EufyLifeCloudError(f"Eufy Life MQTT publish failed: {result.rc}")

    async def _async_refresh_effects(self, serial: str) -> None:
        """Rebuild a light's effect list from the catalog and the account's scenes.

        Saving or deleting a scene changes what a light can be asked for, so the
        list that ``effect_list`` and ``async_set_effect`` read is read again the
        way discovery builds it. A read that fails leaves the entries that are
        already known in place instead of emptying the list.
        """
        device = self.devices[serial]
        effects = device.effects
        try:
            effects = _parse_effects(
                await self._async_post(
                    _CATALOG_PATH,
                    {"sns": [serial], "light_type": None, "scene_id": None},
                )
            )
        except (
            aiohttp.ClientError,
            TimeoutError,
            ValueError,
            TypeError,
            EufyLifeCloudError,
        ):
            _LOGGER.debug("Light presets unavailable for %s", serial)
        try:
            effects.update(await self.async_fetch_personal_scenes(serial))
        except (
            aiohttp.ClientError,
            TimeoutError,
            ValueError,
            TypeError,
            EufyLifeCloudError,
        ):
            _LOGGER.debug("App-built scenes unavailable for %s", serial)
        device.effects = effects
        if device.effect is not None and device.effect not in effects:
            # A scene that was just deleted cannot stay selected.
            device.effect = None
        self._notify(serial)

    async def async_recommend_keywords(self, serial: str) -> list[str]:
        """The suggestions the app offers under "Laat u inspireren!".

        They come from the cloud and are localised by the app, so this is the list
        of descriptions a scene can be generated from.
        """
        data = await self._async_post(
            _AIGC_RECOMMEND_PATH, {"sn": serial, "region": _AIGC_REGION}
        )
        entries = data.get("list") if isinstance(data, dict) else None
        keywords = [
            entry["keywords"]
            for entry in entries or []
            if isinstance(entry, dict) and isinstance(entry.get("keywords"), str)
        ]
        if not keywords:
            raise EufyLifeCloudError("The light service suggested no descriptions")
        return keywords

    async def async_generate_ai_effect(
        self, serial: str, keywords: str | None = None
    ) -> dict[str, Any]:
        """Ask the cloud's AI for a light effect.

        A description is generated from; without one the cloud picks at random,
        which is what the app's magic dice button does. Nothing is saved: the
        design is an answer that still has to be given a name.

        The service fails a generation on its own side every now and then (code
        190002 was seen live, for a description that answered fine a moment
        later), so the same request is tried again before its failure is passed
        on. Only the transport failure is retried: a malformed answer is not
        something a second attempt fixes.
        """
        if keywords is None:
            path, body = _AIGC_MAGIC_PATH, {"sn": serial}
        else:
            if not keywords.strip():
                raise EufyLifeCloudError("An AI light effect needs a description")
            path, body = _AIGC_GET_PATH, {"sn": serial, "keywords": keywords}
        for attempt in range(1, _AI_ATTEMPTS + 1):
            try:
                data = await self._async_post(path, body)
            except EufyLifeCloudError:
                if attempt == _AI_ATTEMPTS:
                    raise
                _LOGGER.debug(
                    "The AI light effect was not generated, asking again (%s/%s)",
                    attempt,
                    _AI_ATTEMPTS,
                )
                await asyncio.sleep(_AI_RETRY_DELAY)
                continue
            return _parse_ai_effect(data)
        raise EufyLifeCloudError("The AI light effect was not generated")

    async def _async_wait_for_scene(
        self, serial: str, name: str, *, present: bool, timeout: float = 10.0
    ) -> bool:
        """Wait until a scene name is, or is no longer, in the account's list.

        The service answers a create with the new id straight away, but its own
        list can take a few seconds to include the scene, and the effect list is
        read from that list. Trusting the answer alone would make a save look like
        it did nothing, and a delete look like it left the scene behind, so both
        wait for the list to agree. A list that never agrees is logged instead of
        raising: the write itself succeeded either way.
        """
        deadline = time.monotonic() + timeout
        while True:
            scenes = await self.async_fetch_personal_scenes(serial)
            if (name in scenes) is present:
                return True
            if time.monotonic() >= deadline:
                _LOGGER.debug(
                    "The scene %s is still %s in the list after %.0f s",
                    name,
                    "missing" if present else "present",
                    timeout,
                )
                return False
            await asyncio.sleep(0.5)

    async def async_create_scene(
        self,
        serial: str,
        name: str,
        *,
        colors: list[tuple[int, ...]] | None = None,
        mode: int | None = None,
        brightness: int | None = None,
        speed: int | None = None,
        direction: int | None = None,
        context: str = "",
        keywords: str | None = None,
    ) -> int | None:
        """Save a scene to the account's own (Persoonlijk) list.

        Which of the two routes saves it follows the shape of the palette, because
        they store different things: a palette that addresses every segment goes to
        the editor's own route, which keeps that per-segment map, while a palette a
        mode repeats goes to the route the AI's saves use, which keeps the colours
        and the mode as they were given. Either way the effect list is read again
        afterwards, so the scene is selectable by name.
        """
        if not name.strip():
            raise EufyLifeCloudError("A scene needs a name")
        device = self.devices[serial]
        target_colors = colors or device.colors or []
        target_brightness = brightness if brightness is not None else (device.brightness or 100)
        if not 0 <= target_brightness <= 100:
            raise EufyLifeCloudError("Brightness must be between 0 and 100 percent")
        target_speed = speed if speed is not None else (device.speed or 1)
        if not 1 <= target_speed <= 10:
            raise EufyLifeCloudError("Speed must be between 1 and 10")
        target_direction = direction if direction is not None else (device.direction or 0)
        effect = _scene_effect(
            target_colors,
            mode=mode or _SCENE_CUSTOM_MODE,
            brightness=target_brightness,
            speed=target_speed,
            direction=target_direction,
            context=context,
            lamp_count=device.lamp_count,
        )
        if len(target_colors) == device.lamp_count:
            # A scene that addresses every segment, which is what the editor's grid
            # builds. That route stores the per-segment map: its palette, its mode
            # and its speed survive, and the scene is listed — the map is what the
            # list's palette is derived from when the effect's own palette comes
            # back empty, which is what the service does with this route.
            body: dict[str, Any] = {
                "sn": serial,
                "name": name,
                "is_group": 0,
                "light_effect": [effect],
            }
            path = _DIY_CREATE_PATH
        else:
            # A palette a mode repeats. The editor's route drops it (it keeps only
            # the segment map, so its colours come back empty and the scene never
            # reaches the effect list), while the route the AI's saves use keeps the
            # palette, the mode, the brightness and the speed exactly as given. So a
            # palette scene is saved there, filed under the description — or under
            # its own name when there is none — which is the keyword the app shows.
            body = {
                "sn": serial,
                "keywords": keywords or context or name,
                "name": name,
                "rgb_hex": effect["rgb_hex"],
                "brightness": effect["brightness"],
                "speed": effect["speed"],
                "dynamic": effect["dynamic"],
            }
            path = _AIGC_CREATE_PATH
        data = await self._async_post(path, body)
        light_id = data.get("light_id") if isinstance(data, dict) else None
        await self._async_wait_for_scene(serial, name, present=True)
        await self._async_refresh_effects(serial)
        return int(light_id) if isinstance(light_id, int) else None

    async def async_generate_and_save_ai_scene(
        self, serial: str, name: str, keywords: str | None = None
    ) -> dict[str, Any]:
        """Generate a light effect with the cloud's AI and save it as a scene.

        This is the app's own flow: the AI answers a design, the user keeps it,
        and the result is a scene in the account — which the integration then
        offers as an effect by name. The design that was saved is returned.
        """
        design = await self.async_generate_ai_effect(serial, keywords)
        light_id = await self.async_create_scene(
            serial,
            name,
            colors=design["colors"],
            mode=design["dynamic"],
            brightness=design["brightness"],
            speed=design["speed"],
            direction=design["direction"],
            context=design["context"],
            keywords=keywords if keywords is not None else name,
        )
        return {**design, "light_id": light_id}

    async def async_apply_ai_effect(
        self, serial: str, keywords: str | None = None
    ) -> dict[str, Any]:
        """Generate a light effect with the AI and show it, without saving it.

        The palette is repeated along the strip, which is what a saved scene's own
        mode does when it renders, so what shows on the light is what saving the
        same design would produce. The design's own speed and direction go with
        it, and then its brightness and power: the palette frame carries only the
        effect layer's level, so a light that was left dim or off shows nothing of
        the design until the design's brightness is applied as well. That is the
        order the light entity uses too — the acknowledged effect first, then the
        requested power and brightness.
        """
        design = await self.async_generate_ai_effect(serial, keywords)
        device = self.devices[serial]
        colors = design["colors"]
        if not device.lamp_count:
            raise EufyLifeCloudError("Waiting for the light's lamp count")
        palette = [
            colors[index % len(colors)] for index in range(device.lamp_count)
        ]
        await self.async_set_effect(
            serial,
            colors=palette,
            speed=design["speed"],
            direction=design["direction"],
            refresh=not device.silent_effect,
        )
        self.set_power(serial, True, brightness=design["brightness"])
        return design

    async def async_delete_scene(self, serial: str, light_id: int) -> None:
        """Remove one of the account's own scenes by the id it was saved with."""
        # The name is what the list can be checked against, so it is looked up
        # first; the list is read from the same place either way.
        scenes = await self.async_fetch_personal_scenes(serial)
        name = next(
            (
                scene_name
                for scene_name, preset in scenes.items()
                if preset.get("light_id") == light_id
            ),
            None,
        )
        await self._async_post(_DIY_DELETE_PATH, {"sn": serial, "light_id": light_id})
        if name is not None:
            await self._async_wait_for_scene(serial, name, present=False)
        await self._async_refresh_effects(serial)

    async def async_find_scene(self, serial: str, name: str) -> int | None:
        """The cloud id of an app-built scene, or None when the name is a preset.

        Only the account's own scenes can be changed: a catalog preset is shared
        by every account and has no id of its own to delete.
        """
        return (await self.async_fetch_personal_scenes(serial)).get(name, {}).get(
            "light_id"
        )

    def add_listener(self, serial: str, listener: Callable[[], None]) -> None:
        self._listeners[serial].add(listener)

    def remove_listener(self, serial: str, listener: Callable[[], None]) -> None:
        self._listeners[serial].discard(listener)

    def add_link_listener(self, listener: Callable[[], None]) -> None:
        """Subscribe to the MQTT link coming up or going down."""
        self._link_listeners.add(listener)

    def remove_link_listener(self, listener: Callable[[], None]) -> None:
        """Unsubscribe from the MQTT link coming up or going down."""
        self._link_listeners.discard(listener)

    def _notify(self, serial: str | None = None) -> None:
        if serial is None:
            listeners = [
                listener
                for group in self._listeners.values()
                for listener in group
            ]
            listeners.extend(self._link_listeners)
        else:
            listeners = self._listeners.get(serial, ())
        for listener in tuple(listeners):
            self._loop.call_soon_threadsafe(listener)

    async def async_close(self) -> None:
        """Stop the MQTT client and remove its temporary certificate files."""
        self.connected = False
        if self._mqtt is not None:
            self._mqtt.disconnect()
            await asyncio.to_thread(self._mqtt.loop_stop)
            self._mqtt = None
        if self._mqtt_files is not None:
            self._mqtt_files.cleanup()
            self._mqtt_files = None


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise EufyLifeCloudError(f"MQTT response is missing {key}")
    return value


def _self_check() -> None:
    from unittest.mock import AsyncMock, MagicMock, call, patch

    payload = _tlv(0xA1, b"\x01") + _tlv(0xA2, b"owner")
    frame = _frame(_GET_SETTINGS_RESPONSE, payload)
    assert frame.hex() == "ff0914000300020a00a10101a2056f776e65728e"
    assert len(frame) == int.from_bytes(frame[2:4], "little")
    assert _parse_frame(frame) == (_GET_SETTINGS_RESPONSE, payload)
    assert _parse_tlvs(payload) == {0xA1: b"\x01", 0xA2: b"owner"}
    report = bytearray(_frame(_REPORT_DEVICE_INFO, _tlv(0xA1, b"\x01")))
    report[5] = 1  # Observed unsolicited report header: 03 01 02.
    report[-1] = _xor(report[:-1])
    assert _parse_frame(report) == (_REPORT_DEVICE_INFO, _tlv(0xA1, b"\x01"))
    key = bytes.fromhex(_LIGHT_PRESET_KEY)
    assert _aes_decrypt(_aes_encrypt("eufy", key), key) == "eufy"
    crypto = _LightCrypto()
    crypto._key = key
    crypto._security_key = key.hex()
    plaintext = '{"code":0,"data":{"devices":[]}}'
    encrypted = _aes_encrypt(plaintext, key)
    response = {
        "code": 0,
        "data": encrypted,
        "signature": _signature(f"123+nonce+{encrypted}", key.hex()),
    }
    assert crypto.decrypt(json.dumps(response), "123", "nonce") == plaintext
    response["signature"] = "0" * 64
    try:
        crypto.decrypt(json.dumps(response), "123", "nonce")
    except EufyLifeCloudError as err:
        assert str(err) == "Invalid encrypted light response signature"
    else:
        raise AssertionError("Tampered light response was accepted")

    async def check_empty_inventory() -> None:
        async with aiohttp.ClientSession() as session:
            cloud = EufyLifeLightCloud(session, "", "", "", "", "DE", "en", "UTC")
            response = MagicMock()
            response.json = AsyncMock(
                return_value={
                    "code": 0,
                    "data": {"domain": "aiot-light-api-eu.eufylife.com"},
                }
            )
            with (
                patch.object(session, "post") as post,
                patch.object(
                    cloud._crypto, "async_exchange", new=AsyncMock()
                ) as exchange,
                patch.object(
                    cloud, "_async_post", new=AsyncMock(return_value={"devices": None})
                ),
            ):
                post.return_value.__aenter__.return_value = response
                await cloud.async_start()
                assert cloud.devices == {}
                exchange.assert_awaited_once_with(
                    session, "https://aiot-light-api-eu.eufylife.com"
                )
            inventory = {
                "devices": [
                    {
                        "device": {
                            "device_sn": "shared",
                            "device_model": "T8L30",
                            "member": {"member_type": 1, "admin_user_id": "owner"},
                        }
                    }
                ]
            }
            with (
                patch.object(session, "post") as post,
                patch.object(cloud._crypto, "async_exchange", new=AsyncMock()),
                patch.object(
                    cloud,
                    "_async_post",
                    # The second empty list is the app-built scene route, which
                    # async_start reads for every discovered light.
                    new=AsyncMock(
                        side_effect=[inventory, {"list": []}, {"list": []}, {}]
                    ),
                ),
                patch.object(cloud, "_start_mqtt"),
            ):
                post.return_value.__aenter__.return_value = response
                await cloud.async_start()
            cloud._user_id = "member"
            cloud.connected = True
            cloud._mqtt = MagicMock()
            cloud._mqtt.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
            cloud.request_settings("shared")
            sent = json.loads(cloud._mqtt.publish.call_args.args[1])
            inner = json.loads(sent["payload"])
            assert inner["account_id"] == "owner"
            _, body = _parse_frame(base64.b64decode(inner["data"]))
            assert _parse_tlvs(body)[0xA2] == b"member"
            cloud._handle_frame("shared", _GET_SETTINGS_RESPONSE, b"\x00\xa1\x01\x01")
            assert cloud.devices["shared"].is_on is True
            cloud._handle_frame("shared", _GET_SETTINGS_RESPONSE, b"\x01\xa1\x01\x00")
            assert cloud.devices["shared"].is_on is True
            cloud.set_power("shared", False)
            cloud._handle_frame("shared", _SET_POWER_RESPONSE, b"\x00")
            assert cloud.devices["shared"].is_on is True  # ACK is not a state report.
            cloud._handle_frame("shared", _REPORT_DEVICE_INFO, b"\xa1\x01\x00")
            assert cloud.devices["shared"].is_on is False
            from .light import EufyLifeLight
            from homeassistant.components.light import ColorMode

            light = EufyLifeLight(cloud, cloud.devices["shared"])
            assert light.supported_color_modes == {ColorMode.RGBWW}
            cloud._handle_frame("shared", _GET_SETTINGS_RESPONSE, b"\x00\xa2\x01\x32")
            assert light.brightness == 128
            await light.async_turn_on(brightness=255)
            sent = json.loads(cloud._mqtt.publish.call_args.args[1])
            inner = json.loads(sent["payload"])
            op, body = _parse_frame(base64.b64decode(inner["data"]))
            assert op == _SET_POWER
            assert _parse_tlvs(body)[0xA3] == b"\x01"
            assert _parse_tlvs(body)[0xA4] == b"\x64"
            assert light.brightness == 128  # Wait for device state, not PUBACK.

            # Captured DIY command: one RGBWC palette entry, four lamp indices.
            values = _parse_tlvs(
                _effect_payload(20006, [(255, 0, 0)] * 4, 0, 1, None, 7)
            )
            assert values[0xA3] == bytes.fromhex("264e")
            assert values[0xA6] == bytes.fromhex("01ff00000000")
            assert values[0xA7] == bytes.fromhex("0400010203")
            assert values[0xA8] == b"\x64"
            assert values[0xA9] == bytes(5)
            assert values[0xB0] == b"\x07"
            assert 0xAC not in values
            animation_payload = _build_animation_payload(10854, "{}", 54, verified_layers=True)
            assert animation_payload.startswith(
                bytes.fromhex("a304662a0000a40136a50102a60103a80100a935")
            )
            assert tuple(_frame(_SET_ANIMATION, animation_payload, version=0)[7:9]) == _SET_ANIMATION
            preset = {
                "name": "White",
                "dynamic": "44",
                "dynamic_direct": "0",
                "rgb_hex": "FFDCB3",
                "speed": 1,
                "light_id": 30024,
            }
            catalog = {
                "list": [
                    {
                        "scene_info": [
                            {
                                "light": [
                                    preset,
                                    dict(
                                        preset,
                                        name="New format",
                                        params_version=1,
                                        params="{}",
                                    ),
                                    dict(preset, name="Bad palette", rgb_hex="xyz"),
                                ]
                            }
                        ]
                    }
                ]
            }
            effects = _parse_effects(catalog)
            assert list(effects) == ["White", "New format"]
            assert effects["White"]["light_id"] == 30024
            device = cloud.devices["shared"]
            cloud._handle_frame(
                "shared", _GET_SETTINGS_RESPONSE, bytes.fromhex("00a3020400a402264e")
            )
            assert device.lamp_count == 4 and device.light_id == 20006
            device.effects = effects
            assert light.effect_list == ["White", "New format"]
            task = asyncio.create_task(light.async_turn_on(rgb_color=(255, 0, 0)))
            await asyncio.sleep(0)
            assert light.rgb_color is None
            cloud._handle_frame("shared", (10, 6), bytes.fromhex("00a10100"))
            await task
            assert light.rgb_color == (255, 0, 0)
            task = asyncio.create_task(light.async_turn_on(effect="White"))
            await asyncio.sleep(0)
            cloud._handle_frame("shared", (10, 6), bytes.fromhex("00a10101"))
            from homeassistant.exceptions import HomeAssistantError

            try:
                await task
            except HomeAssistantError:
                pass
            else:
                raise AssertionError("Rejected preset was accepted")
            assert light.effect is None and light.rgb_color == (255, 0, 0)
            task = asyncio.create_task(light.async_turn_on(effect="White"))
            await asyncio.sleep(0)
            sent = json.loads(cloud._mqtt.publish.call_args.args[1])
            _, body = _parse_frame(
                base64.b64decode(json.loads(sent["payload"])["data"])
            )
            assert _parse_tlvs(body)[0xA3] == b"\x2c\x00"
            assert _parse_tlvs(body)[0xAC] == (30024).to_bytes(4, "little")
            cloud._handle_frame("shared", (10, 6), bytes.fromhex("00a10100"))
            await task
            assert light.effect == "White" and light.rgb_color is None
            cloud._handle_frame(
                "shared", _REPORT_DEVICE_INFO, bytes.fromhex("a4042d000000")
            )
            assert (
                light.rgb_color is None
            )  # External animation change invalidates cache.

    async def check_generic_model_discovery() -> None:
        async with aiohttp.ClientSession() as session:
            cloud = EufyLifeLightCloud(session, "user", "", "", "", "DE", "en", "UTC")
            response = MagicMock()
            response.json = AsyncMock(
                return_value={
                    "code": 0,
                    "data": {"domain": "aiot-light-api-eu.eufylife.com"},
                }
            )
            # A model code the integration has never seen is still discovered and
            # driven through the generic path instead of being dropped.
            inventory = {
                "devices": [
                    {
                        "device": {
                            "device_sn": "unknown-serial",
                            "device_model": "T8L99",
                            "device_name": "Mystery Light",
                        }
                    }
                ]
            }
            with (
                patch.object(session, "post") as post,
                patch.object(cloud._crypto, "async_exchange", new=AsyncMock()),
                patch.object(
                    cloud,
                    "_async_post",
                    # Catalog, then the app-built scene route, then the MQTT info.
                    new=AsyncMock(
                        side_effect=[inventory, {"list": []}, {"list": []}, {}]
                    ),
                ),
                patch.object(cloud, "_start_mqtt"),
            ):
                post.return_value.__aenter__.return_value = response
                await cloud.async_start()
            device = cloud.devices["unknown-serial"]
            assert device.model == "T8L99"
            assert device.name == "Mystery Light"
            assert device.account_id == "user"
            # An inventory entry without a device_status says nothing at all,
            # which is not the same as an unreachable light.
            assert device.cloud_status is None and device.reachable is None
            assert not device.known_model
            assert not device.animation_protocol
            assert not device.session_handshake and not device.silent_effect
            assert device.model_name == "T8L99"  # No retail name for an unlisted code.

            # The generic path controls it without a session handshake.
            cloud._user_id = "user"
            cloud.connected = True
            cloud._mqtt = MagicMock()
            cloud._mqtt.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
            cloud._handle_frame(
                "unknown-serial", _GET_SETTINGS_RESPONSE, bytes.fromhex("00a3020600")
            )
            assert device.lamp_count == 6
            task = asyncio.create_task(
                cloud.async_set_effect("unknown-serial", rgbww_color=(255, 0, 0, 0, 0))
            )
            await asyncio.sleep(0)
            assert cloud._mqtt.publish.call_count == 1  # Effect only, no handshake.
            sent = json.loads(cloud._mqtt.publish.call_args.args[1])
            opcode, body = _parse_frame(
                base64.b64decode(json.loads(sent["payload"])["data"])
            )
            assert opcode == _SET_EFFECT
            # This is the frame captured off a T8L02: the plain-colour id, then
            # one RGBCW block (the count byte plus five channels).
            assert _parse_tlvs(body)[0xA3] == (20006).to_bytes(2, "little")
            assert _parse_tlvs(body)[0xA6] == bytes.fromhex("01ff00000000")
            cloud._handle_frame(
                "unknown-serial", _SET_EFFECT_RESPONSE, bytes.fromhex("00a10100")
            )
            await task
            assert device.light_id == 20006
            assert device.rgbww_color == (255, 0, 0, 0, 0)

            # Entries without a model code are ignored instead of creating a
            # broken entity.
            cloud.devices.clear()
            with (
                patch.object(session, "post") as post,
                patch.object(cloud._crypto, "async_exchange", new=AsyncMock()),
                patch.object(
                    cloud,
                    "_async_post",
                    new=AsyncMock(
                        side_effect=[
                            {
                                "devices": [
                                    {
                                        "device": {
                                            "device_sn": "",
                                            "device_model": None,
                                        }
                                    },
                                    "invalid relation",
                                ]
                            },
                        ]
                    ),
                ),
                patch.object(cloud, "_start_mqtt"),
            ):
                post.return_value.__aenter__.return_value = response
                await cloud.async_start()
            assert cloud.devices == {}

            # Palettes are length-prefixed with one byte, so one frame carries at
            # most _MAX_PALETTE_COLORS five-channel blocks; a longer one fails
            # readably instead of corrupting the payload.
            assert _MAX_PALETTE_COLORS == 50
            assert _effect_payload(
                20006, [(i, 0, 0) for i in range(_MAX_PALETTE_COLORS)], 0, 1, None, 7
            )
            try:
                _effect_payload(
                    20006,
                    [(i, 0, 0) for i in range(_MAX_PALETTE_COLORS + 1)],
                    0,
                    1,
                    None,
                    7,
                )
            except EufyLifeCloudError as err:
                assert "Too many light segments" in str(err)
            else:
                raise AssertionError("Oversized light palette was accepted")

    async def check_e22_model_support() -> None:
        """The E22 is the T8L02: family animation protocol, no T8L40 handshake."""
        e22 = EufyLifeLightDevice(
            serial="t8l02", name="Permanent Outdoor Lights", model=_T8L02_MODEL
        )
        assert e22.known_model and e22.animation_protocol
        assert not e22.session_handshake and not e22.silent_effect
        assert e22.model_name == "Permanent Outdoor Lights E22"

        # The handshake and the post-effect silence are T8L40 firmware quirks and
        # stay on the T8L40; the pathway light keeps the classic E10 path.
        t8l40 = EufyLifeLightDevice(
            serial="t8l40", name="Floor Lamp", model=_T8L40_MODEL
        )
        assert t8l40.known_model and t8l40.animation_protocol
        assert t8l40.session_handshake and t8l40.silent_effect
        assert t8l40.model_name == "Indoor Floor Lamp E10"
        t8l30 = EufyLifeLightDevice(
            serial="t8l30", name="Pathway", model=_T8L30_MODEL
        )
        assert t8l30.known_model and not t8l30.animation_protocol
        assert not t8l30.session_handshake and not t8l30.silent_effect
        assert t8l30.model_name == "Outdoor Pathway Lights E10"

        async with aiohttp.ClientSession() as session:
            cloud = EufyLifeLightCloud(session, "user", "", "", "", "DE", "en", "UTC")
        e22.lamp_count = 12
        # The cloud id is one the T8L40 table holds, so this also proves the
        # E22 renders the catalog's own params instead of the T8L40 calibration.
        e22.effects = {
            "New format": {
                "params_version": 1,
                "light_id": 10854,
                "scene_id": 3,
                "params": json.dumps(
                    {
                        "light_effect_speed": 40,
                        "layer_execution_mode": 2,
                        "layer": [
                            {
                                "current_layer_type": 1,
                                "layer_range": [0, 11],
                                "colors": "ff0000|00ff00",
                            }
                        ],
                    }
                ),
            }
        }
        cloud.devices["t8l02"] = e22
        cloud._user_id = "user"
        cloud.connected = True
        cloud._mqtt = MagicMock()
        cloud._mqtt.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS

        task = asyncio.create_task(cloud.async_set_effect("t8l02", effect="New format"))
        await asyncio.sleep(0)
        assert cloud._mqtt.publish.call_count == 1  # No handshake in front of it.
        sent = json.loads(cloud._mqtt.publish.call_args.args[1])
        opcode, body = _parse_frame(
            base64.b64decode(json.loads(sent["payload"])["data"])
        )
        assert opcode == _SET_ANIMATION
        values = _parse_tlvs(body)
        # The family 0x020D layout the sibling eufy-sdk validated on a T8L02.
        assert values[0xA3] == (10854).to_bytes(4, "little")
        assert values[0xA4] == b"\x28"  # 40, from the catalog params.
        assert values[0xA5] == b"\x01"  # One layer.
        assert values[0xA6] == b"\x02"  # Catalog layer execution mode.
        assert values[0xA8] == b"\x00"
        layer = values[0xA9]
        assert len(layer) == 31  # Header, one 5-channel colour, type-1 trailer.
        assert layer[0] == 0  # Catalog priority, not the T8L40 blob's 1.
        assert layer[9] == 1 and layer[10] == 2  # Layer type and colour count.
        # A (0x02, 0x04) status report acks an animation-protocol write.
        cloud._handle_frame(
            "t8l02", _REPORT_DEVICE_INFO, bytes.fromhex("a10101a20140a3010ca404662a0000")
        )
        await task
        assert e22.effect == "New format"
        assert e22.light_id == 10854

    async def check_own_command_echo() -> None:
        """Only a device report may change state, whichever client sent what."""
        async with aiohttp.ClientSession() as session:
            cloud = EufyLifeLightCloud(session, "user", "", "", "", "DE", "en", "UTC")
        device = EufyLifeLightDevice(serial="e22", name="E22", model=_T8L02_MODEL)
        cloud.devices["e22"] = device
        client = MagicMock()
        client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
        cloud._mqtt = client
        cloud.connected = True

        class _Message:
            def __init__(self, topic: str, body: dict[str, Any] | str) -> None:
                self.topic = topic
                self.payload = (
                    body if isinstance(body, str) else json.dumps(body)
                ).encode()

        def _echo(opcode: tuple[int, int], value: bytes) -> _Message:
            """Publish a command and return what the broker delivers back."""
            cloud._publish("e22", opcode, _command_payload("user", value))
            published = client.publish.call_args.args[1]
            assert isinstance(published, str)
            return _Message("cmd/eufy_life/T8L02/e22/req", published)

        # Captured on real hardware: the broker returns our own request on /req.
        cloud._on_message(None, None, _echo(_GET_SETTINGS, b""))
        assert device.is_on is None and device.brightness is None

        # The filter keys off the payload we published, not the opcode: a report
        # shaped frame that we sent ourselves must not be applied either.
        cloud._on_message(
            None, None, _echo(_REPORT_DEVICE_INFO, _tlv(0xA1, b"\x01"))
        )
        assert device.is_on is None

        # The app writes the same head client_id as this integration, so its
        # frames have to survive the echo filter — but as app traffic: a command
        # from the phone says nothing about the light, and an app-side frame with
        # the report opcode must not resolve a write or replace the state.
        app_frame = _frame(_REPORT_DEVICE_INFO, _tlv(0xA1, b"\x01"))
        app_command = _Message(
            "cmd/eufy_life/T8L02/e22/req",
            {
                "head": {"client_id": cloud._command_client_id},
                "payload": json.dumps(
                    {"device_sn": "e22", "data": base64.b64encode(app_frame).decode()}
                ),
            },
        )
        with patch.object(cloud, "_handle_frame") as handled:
            with patch("custom_components.eufylife_api.cloud._LOGGER") as logger:
                cloud._on_message(None, None, app_command)
            assert handled.call_count == 0
            logged = [str(call) for call in logger.info.call_args_list]
        assert any("Eufy app frame" in line for line in logged)
        # The raw body of a request frame is not dumped at info level either: the
        # app polls while a capture runs, and that dump would bury the captured
        # frame under the app's own traffic.
        assert not any("MQTT message received" in line for line in logged)
        assert any(
            "MQTT message received" in str(call) for call in logger.debug.call_args_list
        )
        assert device.is_on is None

        # The light's own reply on the response topic still updates the state.
        reply = {
            "head": {"client_id": "android-eufy_life-device", "msg_seq": 1},
            "payload": json.dumps(
                {
                    "device_sn": "e22",
                    "data": base64.b64encode(
                        _frame(
                            _GET_SETTINGS_RESPONSE,
                            b"\x00" + _tlv(0xA1, b"\x01") + _tlv(0xA2, b"\x32"),
                        )
                    ).decode(),
                }
            ),
        }
        cloud._on_message(None, None, _Message("cmd/eufy_life/T8L02/e22/res", reply))
        assert device.is_on is True and device.brightness == 50

    async def check_captured_e22_state_frame() -> None:
        """Byte-exact 0x0A00 replies captured from a live E22 (T8L02)."""
        cloud = EufyLifeLightCloud(MagicMock(), "user", "", "", "", "NL", "en", "UTC")
        device = EufyLifeLightDevice(serial="e22", name="E22", model=_T8L02_MODEL)
        cloud.devices["e22"] = device
        frame = bytes.fromhex(
            "ff093a000300020a0000"  # Length 58, version 0, opcode (0x0A, 0x00).
            "a10100"  # A1: power, off.
            "a20164"  # A2: brightness, 100%.
            "a3023200"  # A3: lamp count, 50.
            "a4022d00"  # A4: light id, 45.
            "a50100a60400000000a71000000000000000000000000000000000a80100a90100"
            "08"  # Frame checksum, verified by _parse_frame.
        )
        opcode, payload = _parse_frame(frame)
        assert opcode == _GET_SETTINGS_RESPONSE and payload[0] == 0
        # The classic settings path, so the E22 needs no session handshake.
        cloud._handle_frame("e22", opcode, payload)
        assert device.is_on is False
        assert device.brightness == 100
        assert device.lamp_count == 50
        assert device.light_id == 45

    async def check_captured_e22_lit_state_frame() -> None:
        """The same E22 report after it was switched on at 1 % in the app."""
        cloud = EufyLifeLightCloud(MagicMock(), "user", "", "", "", "NL", "en", "UTC")
        device = EufyLifeLightDevice(serial="e22", name="E22", model=_T8L02_MODEL)
        cloud.devices["e22"] = device
        frame = bytes.fromhex(
            "ff093a000300020a0000"
            "a10101"  # A1: power, on.
            "a20101"  # A2: brightness, 1% — so A2 really is a 0-100 percentage.
            "a3023200"  # A3: lamp count, 50.
            "a4022d00"  # A4: light id, 45.
            "a50100"
            "a6043b750000"  # A6: preset cloud id 30011, as the app selected.
            "a71000000000000000000000000000000000"  # A7: still no colour data.
            "a80102a90100"
            "20"  # Frame checksum, verified by _parse_frame.
        )
        opcode, payload = _parse_frame(frame)
        assert opcode == _GET_SETTINGS_RESPONSE and payload[0] == 0
        cloud._handle_frame("e22", opcode, payload)
        assert device.is_on is True
        assert device.brightness == 1
        assert device.lamp_count == 50
        assert device.light_id == 45
        # The reported cloud id names the running preset instead of clearing it.
        device.effects["Eave"] = {"light_id": 30011}
        cloud._handle_frame("e22", opcode, payload)
        assert device.effect == "Eave"
        assert device.colors == []

    async def check_captured_personal_scene_report() -> None:
        """The (0x02, 0x04) reports two app-built personal scenes produced.

        Applying "kerst" and "disco 1" from the app's own (Persoonlijk) tab made
        the light report these two frames. Both name a mode id in A4 and a cloud
        id in A6 that the light-mode catalog does not list — A7=3 is the number
        of colours the scene was built from and there is no A9 layer blob at all,
        so the app writes those scenes as a palette it renders itself rather than
        as a catalog preset. That cloud id is the id the app's own scene list
        carries, so the catalog cannot resolve it while ``_parse_personal_scenes``
        (see ``check_personal_scene_list``) can.
        """
        cloud = EufyLifeLightCloud(MagicMock(), "user", "", "", "", "NL", "en", "UTC")
        device = EufyLifeLightDevice(serial="e22", name="E22", model=_T8L02_MODEL)
        cloud.devices["e22"] = device
        # "disco 1" reports the mode id of the catalog preset it was derived from,
        # so a known light id would otherwise resolve: only the personal cloud id
        # in A6 makes the difference.
        device.effects = {"Rainbow Bridge": {"light_id": 30011}}
        for frame_hex, mode_id, personal_cloud_id in (
            (
                # "kerst": A4=20002, A6=171200, A7=3.
                "ff0925000301020204a10101a20101a30132a404224e0000a50100"
                "a604c09c0200a70103"
                "77",
                20002,
                171200,
            ),
            (
                # "disco 1": A4=30011, A6=168622, A7=3.
                "ff0925000301020204a10101a20101a30132a4043b750000a50100"
                "a604ae920200a70103"
                "35",
                30011,
                168622,
            ),
        ):
            device.effect = None
            opcode, payload = _parse_frame(bytes.fromhex(frame_hex))
            assert opcode == _REPORT_DEVICE_INFO
            cloud._handle_frame("e22", opcode, payload)
            assert device.is_on is True and device.brightness == 1
            assert device.lamp_count == 50
            assert device.light_id == mode_id
            assert device.effect is None
            assert personal_cloud_id not in {
                preset.get("light_id") for preset in device.effects.values()
            }

    async def check_reported_effect_status() -> None:
        """A report names the running effect, keeps its ids and its own settings.

        The captured (0x02, 0x04) frame of "kerst", with the account's own scene
        list read back: the cloud id in A6 is what names the scene, that id stays
        on the device so a scene the account cannot name is still visible as the
        id the light showed, and the mode in A4 is kept next to it. A report
        carries no speed and no direction — the write's own fields are not echoed
        — so the scene's own values are adopted, and a value this integration
        wrote is never replaced by them.
        """
        cloud = EufyLifeLightCloud(MagicMock(), "user", "", "", "", "NL", "en", "UTC")
        device = EufyLifeLightDevice(serial="e22", name="E22", model=_T8L02_MODEL)
        cloud.devices["e22"] = device
        device.effects = {
            # The app's own scene as ``_parse_personal_scenes`` lists it.
            "kerst": {
                "light_id": 171200,
                "dynamic": 20002,
                "direction": 0,
                "speed": 10,
                "colors": [(255, 149, 13), (255, 7, 75)],
                "light_type": 4,
            },
        }
        opcode, payload = _parse_frame(
            bytes.fromhex(
                # "kerst": A1=1, A2=1, A3=50, A4=20002, A6=171200, A7=3.
                "ff0925000301020204a10101a20101a30132a404224e0000a50100"
                "a604c09c0200a70103"
                "77"
            )
        )
        assert opcode == _REPORT_DEVICE_INFO
        cloud._handle_frame("e22", opcode, payload)
        assert device.effect == "kerst" and device.effect_id == 171200
        assert device.light_id == 20002  # The mode the light renders.
        assert device.speed == 10 and device.direction == 0
        assert device.last_report is not None

        # The same scene while a speed was written from here: the value Home
        # Assistant sent is the one the light was told to render with.
        device.speed = 4
        cloud._handle_frame("e22", opcode, payload)
        assert device.effect == "kerst" and device.speed == 4

        # An id the account cannot name leaves no name at all: two app-built
        # scenes can share the mode in A4, so the name that ran before would name
        # the wrong scene. The id the light showed is kept either way.
        opcode, payload = _parse_frame(
            _frame(
                _REPORT_DEVICE_INFO,
                _tlv(0xA4, (20002).to_bytes(4, "little"))
                + _tlv(0xA6, (999999).to_bytes(4, "little")),
                version=1,
            )
        )
        cloud._handle_frame("e22", opcode, payload)
        assert device.effect is None and device.effect_id == 999999
        assert device.light_id == 20002 and device.speed == 4

        # A report of a plain colour — the mode the colour writes use, with no
        # cloud id — leaves neither an effect nor an id behind.
        opcode, payload = _parse_frame(
            _frame(
                _REPORT_DEVICE_INFO,
                _tlv(0xA4, (20006).to_bytes(4, "little"))
                + _tlv(0xA6, (0).to_bytes(4, "little")),
                version=1,
            )
        )
        cloud._handle_frame("e22", opcode, payload)
        assert device.effect is None and device.effect_id is None

    async def check_personal_scene_list() -> None:
        """The app's own scenes become named effects that write their cloud id.

        Captured from the account: ``/app/light/diy/list`` with
        ``{"sns": [sn], "start_index": N}`` answers the scenes the user built, and
        "kerst" and "disco 1" carry exactly the A6 ids the light reported for
        them. Reading that list is what makes those ids resolvable, and applying
        one sends the id back out in 0xAC, which is where the light reads it from.
        """
        cloud = EufyLifeLightCloud(MagicMock(), "user", "", "", "", "NL", "en", "UTC")

        def scene(
            name: str,
            light_id: int,
            light_type: int,
            effect: dict[str, Any],
        ) -> dict[str, Any]:
            return {
                "name": name,
                "light_id": light_id,
                "light_type": light_type,
                "light_effect": [effect],
            }

        personal = _parse_personal_scenes(
            {
                "is_more": 1,
                "list": [
                    scene(
                        "kerst",
                        171200,
                        4,
                        {
                            "dynamic": "20002",
                            "dynamic_direct": "",
                            "speed": 100,
                            "rgb_hex": "ff950d|ff074b",
                        },
                    ),
                    scene(
                        "disco 1",
                        168622,
                        1,
                        {
                            "dynamic": "30011",
                            "dynamic_direct": "0",
                            "speed": 5,
                            "rgb_hex": "ff0217|ff7211|12ff3b|19ff19|ff132a",
                        },
                    ),
                    scene(
                        # Only the per-segment map is sent, so its lit colours are
                        # the palette; a scene saved at speed 0 renders at 1.
                        "esdoorn",
                        167575,
                        1,
                        {
                            "dynamic": "18",
                            "speed": 0,
                            "light_detail_info": [
                                {"pos": 0, "rgb_hex": "ffa500", "state": 2},
                                {"pos": 1, "rgb_hex": "ffff00", "state": 2},
                                {"pos": 2, "rgb_hex": "ffa500", "state": 2},
                                {"pos": 3, "rgb_hex": "000000", "state": 0},
                            ],
                        },
                    ),
                ],
            }
        )
        assert set(personal) == {"kerst", "disco 1", "esdoorn"}
        assert personal["kerst"]["light_id"] == 171200
        assert personal["kerst"]["dynamic"] == 20002
        assert personal["kerst"]["speed"] == 10  # The frame carries 1-10.
        assert personal["kerst"]["direction"] == 0
        assert personal["kerst"]["light_type"] == 4
        assert personal["kerst"]["colors"] == [(255, 149, 13), (255, 7, 75)]
        assert personal["disco 1"]["colors"] == [
            (255, 2, 23),
            (255, 114, 17),
            (18, 255, 59),
            (25, 255, 25),
            (255, 19, 42),
        ]
        assert personal["esdoorn"]["colors"] == [(255, 165, 0), (255, 255, 0)]
        assert personal["esdoorn"]["speed"] == 1
        # A structural gap is not a scene to guess at.
        for broken in ({"list": [{"light_id": 1}]}, {"list": "nope"}):
            try:
                _parse_personal_scenes(broken)
            except EufyLifeCloudError:
                continue
            raise AssertionError(f"Malformed app-built scene list accepted: {broken}")

        # The route is paged: reading stops on the first empty page, and each page
        # asks for the scenes after the ones already read.
        pages = [
            {
                "is_more": 1,
                "list": [
                    scene(
                        "kerst",
                        171200,
                        4,
                        {"dynamic": "20002", "rgb_hex": "ff950d|ff074b"},
                    )
                ],
            },
            {"is_more": 1, "list": []},
        ]
        with patch.object(cloud, "_async_post", new=AsyncMock(side_effect=pages)) as post:
            fetched = await cloud.async_fetch_personal_scenes("e22")
        assert set(fetched) == {"kerst"}
        assert [call.args[1] for call in post.call_args_list] == [
            {"sns": ["e22"], "start_index": 0},
            {"sns": ["e22"], "start_index": 1},
        ]

        # Applying one writes the scene's own cloud id, which is the id the light
        # reports back in A6.
        device = EufyLifeLightDevice(serial="e22", name="E22", model=_T8L02_MODEL)
        device.lamp_count = 50
        device.effects = personal
        cloud.devices["e22"] = device
        client = MagicMock()
        client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
        cloud._mqtt = client
        cloud.connected = True
        cloud._loop.call_soon(cloud._complete_effect, "e22", 0)
        await cloud.async_set_effect("e22", effect="kerst", refresh=False)
        published = json.loads(client.publish.call_args.args[1])
        opcode, payload = _parse_frame(
            base64.b64decode(json.loads(published["payload"])["data"])
        )
        assert opcode == _SET_EFFECT
        values = _parse_tlvs(payload)
        assert values[0xA3] == (20002).to_bytes(2, "little")
        assert values[0xAC] == (171200).to_bytes(4, "little")
        assert values[0xA6][0] == 2  # One palette block per colour.
        assert device.effect == "kerst"
        assert device.light_id == 20002  # The mode id the report shows in A4.

    async def check_captured_e22_animation_ack_frame() -> None:
        """The T8L02's answer to a 0x020D animation write, captured live.

        Selecting "Rainbow Bridge" from HA produced this frame, so a preset write
        on the E22 is acknowledged on the 0x0A 0x0D opcode the write already
        waits for, not by a 0x02 0x04 status report.
        """
        cloud = EufyLifeLightCloud(MagicMock(), "user", "", "", "", "NL", "en", "UTC")
        device = EufyLifeLightDevice(serial="e22", name="E22", model=_T8L02_MODEL)
        cloud.devices["e22"] = device
        frame = bytes.fromhex(
            "ff090e00030002"
            "0a0d"  # The answer to the 0x020D animation write.
            "00"  # Envelope success.
            "a10100"  # A1: command result 0, accepted.
            "5e"  # Frame checksum, verified by _parse_frame.
        )
        opcode, payload = _parse_frame(frame)
        assert opcode == _SET_ANIMATION_RESPONSE and len(payload) == 4

        # The pending write completes with success...
        future = asyncio.get_running_loop().create_future()
        cloud._effect_replies["e22"] = future
        cloud._handle_frame("e22", opcode, payload)
        await asyncio.sleep(0)
        assert future.result() is None
        # ...and an ack reports nothing, so the state stays unknown until the
        # light sends its own 0x0A00 report.
        assert device.is_on is None and device.brightness is None

    async def check_paho_compatibility() -> None:
        """The MQTT client must build and call back on paho 1.x and 2.x alike."""
        client_id = "android-eufy_life-paho-check"
        args, kwargs = _paho_client_arguments(client_id)
        assert kwargs["client_id"] == client_id
        assert kwargs["protocol"] == mqtt.MQTTv311
        callback_api_version = getattr(mqtt, "CallbackAPIVersion", None)
        if callback_api_version is None:
            # paho 1.x, as pinned by the dev requirements: implicit callback API.
            assert args == () and kwargs["clean_session"] is True
        else:
            assert args == (callback_api_version.VERSION2,)
            assert "clean_session" not in kwargs

        # paho 2.x adds the callback API version argument; simulate it on 1.x.
        class _CallbackAPI:
            VERSION2 = "2"

        with patch.object(mqtt, "CallbackAPIVersion", _CallbackAPI, create=True):
            assert _paho_client_arguments(client_id)[0] == ("2",)
        client = _create_mqtt_client(client_id)
        assert isinstance(client, mqtt.Client)
        assert client._protocol == mqtt.MQTTv311

        cloud = EufyLifeLightCloud(
            MagicMock(), "user", "", "", "", "DE", "en", "UTC"
        )

        class _ReasonCode:
            value = 0

        # The paho 1.x argument lists, then the paho 2.x ones.
        cloud._on_connect(client, None, None, 0)
        assert cloud.connected is True
        cloud._on_disconnect(client, None, 0)
        assert cloud.connected is False
        cloud._on_connect(client, None, None, _ReasonCode(), None)
        assert cloud.connected is True
        cloud._on_disconnect(client, None, _ReasonCode(), _ReasonCode(), None)
        assert cloud.connected is False
        assert _mqtt_reason_code(0) == 0
        assert _mqtt_reason_code(_ReasonCode()) == 0
        assert _mqtt_reason_code(5) == 5

    async def check_scene_and_ai_routes() -> None:
        """Saving scenes and the AI routes, pinned to the service's own answers.

        The captured answers are the reference: the magic dice design, the field
        list each route demands, the record a saved scene has to look like (the
        palette twice, a per-segment map that is never nil, mode and direction as
        strings), and the effect list that has to carry the new scene right away.
        """
        magic = {
            "brightness": 70,
            "context": "A romantic candlelight dinner walk",
            "cover": "",
            "dynamic": "30007",
            "dynamic_direct": "0",
            "rgb_hex": "ff8200|ffb600|ff7f00|ffbf00",
            "speed": 3,
        }
        design = _parse_ai_effect(magic)
        assert design["dynamic"] == 30007
        assert design["direction"] == 0
        assert design["speed"] == 3 and design["brightness"] == 70
        assert design["context"] == "A romantic candlelight dinner walk"
        assert design["colors"] == [
            (255, 130, 0),
            (255, 182, 0),
            (255, 127, 0),
            (255, 191, 0),
        ]
        # Brightness, speed, direction and the description have defaults, so a
        # one-colour answer is still a usable design; a missing palette, or a mode
        # that is not a number, is not one to guess at.
        one_colour = _parse_ai_effect({"rgb_hex": "ff8200", "dynamic": "1"})
        assert one_colour["colors"] == [(255, 130, 0)]
        assert one_colour["speed"] == 1 and one_colour["brightness"] == 100
        for broken in (
            {"rgb_hex": "ff8200"},  # No mode field.
            {"rgb_hex": "not-a-colour", "dynamic": "1"},
            {"rgb_hex": "ff8200", "dynamic": "spin"},
            {},
            None,
        ):
            try:
                _parse_ai_effect(broken)
            except EufyLifeCloudError:
                continue
            raise AssertionError(f"Malformed AI light effect accepted: {broken}")

        # A palette scene repeats its colours along the strip, so its map is
        # empty; one built segment by segment carries an entry per segment and
        # marks a black segment as switched off.
        assert _scene_detail([(255, 0, 0)] * 3, 3) == [
            {"pos": 0, "rgb_hex": "ff0000", "state": 2},
            {"pos": 1, "rgb_hex": "ff0000", "state": 2},
            {"pos": 2, "rgb_hex": "ff0000", "state": 2},
        ]
        assert _scene_detail([(255, 0, 0), (0, 0, 0)], 2)[1] == {
            "pos": 1,
            "rgb_hex": "000000",
            "state": 0,
        }
        assert _scene_detail([(255, 0, 0)], 50) == []
        assert _scene_detail([(255, 0, 0)], None) == []
        record = _scene_effect(
            [(255, 0, 0), (0, 255, 0)],
            mode=18,
            brightness=60,
            speed=4,
            direction=1,
            context="probe",
            lamp_count=50,
        )
        assert record["dynamic"] == "18" and record["dynamic_direct"] == "1"
        assert record["rgb_hex"] == "ff0000|00ff00"
        assert record["group_name"] == _SCENE_GROUP_NAME
        assert record["light_detail_info"] == []  # Two colours on fifty segments.
        assert record["params"] == "" and record["params_version"] == 0
        try:
            _scene_effect(
                [],
                mode=18,
                brightness=1,
                speed=1,
                direction=0,
                context="",
                lamp_count=1,
            )
        except EufyLifeCloudError:
            pass
        else:
            raise AssertionError("A scene without colours was accepted")

        saved: dict[str, dict[str, Any]] = {}
        calls: list[tuple[str, dict[str, Any]]] = []

        def scene(name: str, light_id: int) -> dict[str, Any]:
            return {
                "name": name,
                "light_id": light_id,
                "light_type": 1,
                "light_effect": [{"dynamic": "18", "speed": 4, "rgb_hex": "ff0000"}],
            }

        def answers(path: str, body: dict[str, Any]) -> Any:
            """A stand-in service, so the routes are checked without a network."""
            calls.append((path, body))
            if path == _CATALOG_PATH:
                return {
                    "list": [
                        {
                            "scene_info": [
                                {
                                    "scene_id": 10081,
                                    "light": [
                                        {
                                            "name": "Warm White",
                                            "light_id": 30011,
                                            "dynamic": "45",
                                            "rgb_hex": "FFB769",
                                            "speed": 1,
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                }
            if path == _PERSONAL_SCENES_PATH:
                return {"is_more": 0, "list": list(saved.values())}
            if path == _AIGC_CREATE_PATH:
                # The service saves the scene and answers its id, and its own list
                # carries it from then on.
                saved[str(body["name"])] = scene(str(body["name"]), 2189268)
                return {"light_id": 2189268}
            if path == _DIY_DELETE_PATH:
                saved.clear()
                return {}
            if path in (_AIGC_MAGIC_PATH, _AIGC_GET_PATH):
                return magic
            return {}

        cloud = EufyLifeLightCloud(MagicMock(), "user", "", "", "", "NL", "en", "UTC")
        cloud.devices["sn"] = EufyLifeLightDevice(
            serial="sn", name="Probe", model=_T8L02_MODEL, lamp_count=2, brightness=50
        )
        with patch.object(cloud, "_async_post", new=AsyncMock(side_effect=answers)):
            assert await cloud.async_generate_ai_effect("sn") == design
            assert calls[-1] == (_AIGC_MAGIC_PATH, {"sn": "sn"})
            assert await cloud.async_generate_ai_effect("sn", "sunset") == design
            assert calls[-1] == (_AIGC_GET_PATH, {"sn": "sn", "keywords": "sunset"})

            # A generation the service fails is asked for again; only the last
            # attempt's failure is passed on, so a momentary service error does
            # not look like a bad description.
            flaky = AsyncMock(
                side_effect=[EufyLifeCloudError("Light API failed: 190002"), magic]
            )
            with patch.object(cloud, "_async_post", new=flaky):
                assert await cloud.async_generate_ai_effect("sn", "sunset") == design
            assert flaky.await_count == 2
            broken = AsyncMock(
                side_effect=EufyLifeCloudError("Light API failed: 190002")
            )
            with patch.object(cloud, "_async_post", new=broken):
                try:
                    await cloud.async_generate_ai_effect("sn", "sunset")
                except EufyLifeCloudError as err:
                    assert "190002" in str(err)
                else:
                    raise AssertionError("A failed generation was not reported")
            assert broken.await_count == _AI_ATTEMPTS

            # Showing a design writes the palette with the design's own speed and
            # direction, and then applies the design's brightness: the palette
            # frame only carries the effect layer, so a dim light would otherwise
            # show nothing of the design at all.
            shown: list[tuple[str, dict[str, Any]]] = []

            async def fake_set_effect(serial: str, **kwargs: Any) -> None:
                shown.append((serial, kwargs))

            with (
                patch.object(cloud, "async_set_effect", new=fake_set_effect),
                patch.object(cloud, "set_power") as power,
            ):
                assert await cloud.async_apply_ai_effect("sn", "sunset") == design
            assert shown == [
                (
                    "sn",
                    {
                        "colors": [(255, 130, 0), (255, 182, 0)],
                        "speed": 3,
                        "direction": 0,
                        "refresh": True,
                    },
                )
            ]
            assert power.call_args == call("sn", True, brightness=70)

            # A hand-built scene goes to the editor's own route, with the record
            # the service validated: palette, mode, speed, brightness and a map
            # that addresses both segments of this light.
            expected = _scene_effect(
                [(255, 0, 0), (0, 255, 0)],
                mode=18,
                brightness=60,
                speed=4,
                direction=1,
                context="probe",
                lamp_count=2,
            )
            assert len(expected["light_detail_info"]) == 2
            saved["probe scene"] = scene("probe scene", 2189267)
            assert (
                await cloud.async_create_scene(
                    "sn",
                    "probe scene",
                    colors=[(255, 0, 0), (0, 255, 0)],
                    mode=18,
                    brightness=60,
                    speed=4,
                    direction=1,
                    context="probe",
                )
                is None
            )
            # The create writes, then the list is waited on and the effect list is
            # rebuilt from it, so the call is found by route rather than by index.
            path, body = next(
                call for call in calls if call[0] == _DIY_CREATE_PATH
            )
            assert body["sn"] == "sn" and body["name"] == "probe scene"
            assert body["is_group"] == 0
            assert body["light_effect"][0] == expected
            assert "probe scene" in cloud.devices["sn"].effects
            assert "Warm White" in cloud.devices["sn"].effects
            assert await cloud.async_find_scene("sn", "probe scene") == 2189267
            assert await cloud.async_find_scene("sn", "Warm White") is None

            # An AI scene goes to the AI route, which stores the description and
            # answers the id of the scene it saved.
            assert (
                await cloud.async_create_scene(
                    "sn",
                    "ai scene",
                    colors=design["colors"],
                    mode=design["dynamic"],
                    speed=design["speed"],
                    brightness=design["brightness"],
                    context=design["context"],
                    keywords="sunset",
                )
                == 2189268
            )
            path, body = next(call for call in calls if call[0] == _AIGC_CREATE_PATH)
            assert body["keywords"] == "sunset" and body["name"] == "ai scene"
            assert body["dynamic"] == "30007" and body["speed"] == 3
            assert body["rgb_hex"] == "ff8200|ffb600|ff7f00|ffbf00"
            # The scene the service saved is in the effect list immediately after.
            assert "ai scene" in cloud.devices["sn"].effects

            # A palette a mode repeats (fewer colours than segments) cannot be
            # stored by the editor's route — that route keeps only the per-segment
            # map — so it is saved where the colours and the mode are kept, filed
            # under the description or, without one, under its own name.
            cloud.devices["sn"].lamp_count = 50
            await cloud.async_create_scene(
                "sn", "palette scene", colors=[(255, 0, 0), (0, 255, 0)], mode=18
            )
            path, body = next(
                call
                for call in calls
                if call[0] == _AIGC_CREATE_PATH and call[1]["name"] == "palette scene"
            )
            assert body["rgb_hex"] == "ff0000|00ff00" and body["dynamic"] == "18"
            assert body["keywords"] == "palette scene"
            cloud.devices["sn"].lamp_count = 2

            # Deleting clears the selection as well, so a removed scene cannot
            # stay selected in Home Assistant.
            cloud.devices["sn"].effect = "probe scene"
            await cloud.async_delete_scene("sn", 2189267)
            assert (
                next(call for call in calls if call[0] == _DIY_DELETE_PATH)
                == (_DIY_DELETE_PATH, {"sn": "sn", "light_id": 2189267})
            )
            assert "probe scene" not in cloud.devices["sn"].effects
            assert cloud.devices["sn"].effect is None

        # The suggestion chips are what a scene can be generated from.
        other = EufyLifeLightCloud(MagicMock(), "u", "", "", "", "NL", "en", "UTC")
        with patch.object(
            other,
            "_async_post",
            new=AsyncMock(
                return_value={
                    "list": [
                        {"keywords": "Oceanic Opulence Oasis"},
                        {"keywords": "Tiki Torch Twilight"},
                        {"not-a-keyword": 1},
                    ]
                }
            ),
        ):
            assert await other.async_recommend_keywords("sn") == [
                "Oceanic Opulence Oasis",
                "Tiki Torch Twilight",
            ]

    async def check_inventory_status_and_link_listeners() -> None:
        """The inventory's own reachability, and who hears the shared link.

        A light that is switched off at the mains is still listed with its last
        known state, so the inventory's ``device_status`` is the only reachability
        a report-less light has. It stays readable next to the light's own status
        topic, and an entity that follows the link rather than one light hears the
        link going up and down without being redrawn by a single light's updates.
        """
        async with aiohttp.ClientSession() as session:
            cloud = EufyLifeLightCloud(session, "user", "", "", "", "NL", "en", "UTC")
            response = MagicMock()
            response.json = AsyncMock(
                return_value={
                    "code": 0,
                    "data": {"domain": "aiot-light-api-eu.eufylife.com"},
                }
            )
            inventory = {
                "devices": [
                    {
                        "device": {
                            "device_sn": "t8l02",
                            "device_model": _T8L02_MODEL,
                            "device_name": "permanent",
                            "device_status": 0,
                        }
                    }
                ]
            }
            with (
                patch.object(session, "post") as post,
                patch.object(cloud._crypto, "async_exchange", new=AsyncMock()),
                patch.object(
                    cloud,
                    "_async_post",
                    # Inventory, catalog, app-built scenes, then the MQTT info.
                    new=AsyncMock(
                        side_effect=[inventory, {"list": []}, {"list": []}, {}]
                    ),
                ),
                patch.object(cloud, "_start_mqtt"),
            ):
                post.return_value.__aenter__.return_value = response
                await cloud.async_start()

            device = cloud.devices["t8l02"]
            assert device.cloud_status is False
            assert device.reachable is False  # Nothing has reported from it yet.
            assert device.online is None

            class _Message:
                def __init__(self, topic: str, body: dict[str, Any]) -> None:
                    self.topic = topic
                    self.payload = json.dumps(body).encode()

            link_events: list[str] = []
            device_events: list[str] = []

            def on_link() -> None:
                link_events.append("link")

            def on_device() -> None:
                device_events.append("device")

            cloud.add_link_listener(on_link)
            cloud.add_listener("t8l02", on_device)

            # The link coming up wakes both; one light's own update wakes only
            # that light's entities, so a link entity is not redrawn per report.
            cloud.connected = True
            cloud._notify()
            await asyncio.sleep(0)
            assert link_events == ["link"]
            assert device_events == ["device"]

            cloud._notify("t8l02")
            await asyncio.sleep(0)
            assert link_events == ["link"]
            assert device_events == ["device", "device"]

            # The light's own status topic is fresher than the inventory entry,
            # so it wins while the inventory keeps saying 0.
            cloud._on_message(
                None,
                None,
                _Message(
                    "synq/eufy_life/T8L02/t8l02/state_info",
                    {"payload": json.dumps({"status": True})},
                ),
            )
            await asyncio.sleep(0)
            assert device.online is True
            assert device.reachable is True
            assert device.cloud_status is False
            assert device_events == ["device", "device", "device"]

            cloud.remove_link_listener(on_link)
            cloud.remove_listener("t8l02", on_device)
            assert not cloud._link_listeners

    asyncio.run(check_empty_inventory())
    asyncio.run(check_generic_model_discovery())
    asyncio.run(check_e22_model_support())
    asyncio.run(check_own_command_echo())
    asyncio.run(check_captured_e22_state_frame())
    asyncio.run(check_captured_e22_lit_state_frame())
    asyncio.run(check_captured_personal_scene_report())
    asyncio.run(check_reported_effect_status())
    asyncio.run(check_personal_scene_list())
    asyncio.run(check_captured_e22_animation_ack_frame())
    asyncio.run(check_scene_and_ai_routes())
    asyncio.run(check_inventory_status_and_link_listeners())
    asyncio.run(check_paho_compatibility())


if __name__ == "__main__":
    # Use the same module/class identity as light.py when invoked with python -m.
    from . import cloud

    cloud._self_check()
