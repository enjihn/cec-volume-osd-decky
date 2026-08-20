"""Strict discovery of the usable CEC endpoint exposed by Valve's cecd.

SteamOS may renumber ``/dev/cec*`` and the corresponding D-Bus objects after
kernel, firmware, dock, or display updates.  Selection therefore uses cecd's
live ObjectManager state instead of a persisted Cec0/Cec1 index.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

CECD_SERVICE = "com.steampowered.CecDaemon1"
CECD_ROOT_PATH = "/com/steampowered/CecDaemon1"
CECD_DEVICES_PATH = f"{CECD_ROOT_PATH}/Devices"
CECD_DEVICE_PATH_PREFIX = f"{CECD_DEVICES_PATH}/Cec"
CECD_DEVICE_INTERFACE = "com.steampowered.CecDaemon1.CecDevice1"
DBUS_OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
CEC_AUDIO_SYSTEM_LOGICAL_ADDRESS = 5
CEC_PLAYBACK_LOGICAL_ADDRESSES = frozenset({4, 8, 11})
CEC_INVALID_PHYSICAL_ADDRESS = 0xFFFF
MAX_MANAGED_OBJECTS_BYTES = 256 * 1024


class CecDeviceDiscoveryError(RuntimeError):
    """No unique, usable CEC endpoint could be established."""


@dataclass(frozen=True)
class CecDeviceSelection:
    path: str
    # Active is an advisory cecd state report, not adapter identity. A valid
    # local playback adapter can remain false while direct CEC traffic works.
    # Its D-Bus variant is still required to be a strictly typed boolean.
    reported_active: bool
    # cecd's AudioLogicalAddress is only a topology hint. Some valid
    # TV/soundbar combinations leave it at 0 (TV), another stale address, or
    # temporarily omit it even though a direct GetAudioStatus(5) succeeds.
    # Keep the reported value for authenticating passive target-255 replies;
    # direct status reads always use the protocol-defined address 5.
    reported_audio_address: int | None
    physical_address: int
    logical_addresses: tuple[int, ...]


def is_cec_device_path(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith(
        CECD_DEVICE_PATH_PREFIX
    ):
        return False
    suffix = value[len(CECD_DEVICE_PATH_PREFIX) :]
    return bool(suffix) and suffix.isdecimal()


def _variant(
    properties: object, name: str, signature: str
) -> object | None:
    if not isinstance(properties, dict):
        return None
    value = properties.get(name)
    if (
        not isinstance(value, dict)
        or set(value) != {"type", "data"}
        or value.get("type") != signature
    ):
        return None
    return value.get("data")


def select_unique_cec_device(payload: bytes) -> CecDeviceSelection:
    """Select exactly one usable playback endpoint from GetManagedObjects.

    The selector accepts only an endpoint with a real physical address and at
    least one allocated CEC Playback logical address. ``Active`` must be a
    strict D-Bus boolean but its value is advisory: cecd can report ``false``
    for a working local adapter. ``AudioLogicalAddress`` is likewise retained
    as an advisory value, but never selects the local adapter. Zero or multiple
    candidates fail closed.
    """

    if (
        not isinstance(payload, bytes)
        or not payload
        or len(payload) > MAX_MANAGED_OBJECTS_BYTES
    ):
        raise CecDeviceDiscoveryError(
            "cecd managed-object reply is empty or oversized"
        )
    try:
        reply = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CecDeviceDiscoveryError(
            "cecd managed-object reply is malformed JSON"
        ) from error
    if (
        not isinstance(reply, dict)
        or set(reply) != {"type", "data"}
        or reply.get("type") != "a{oa{sa{sv}}}"
    ):
        raise CecDeviceDiscoveryError(
            "cecd managed-object reply has an unexpected signature"
        )
    data = reply.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(
        data[0], dict
    ):
        raise CecDeviceDiscoveryError(
            "cecd managed-object reply has an unexpected shape"
        )

    candidates: list[CecDeviceSelection] = []
    for path, interfaces in data[0].items():
        if not is_cec_device_path(path) or not isinstance(interfaces, dict):
            continue
        properties = interfaces.get(CECD_DEVICE_INTERFACE)
        reported_active = _variant(properties, "Active", "b")
        physical = _variant(properties, "PhysicalAddress", "q")
        logical = _variant(properties, "LogicalAddresses", "ay")
        reported_audio = _variant(
            properties, "AudioLogicalAddress", "y"
        )
        if not isinstance(reported_active, bool):
            continue
        if (
            isinstance(physical, bool)
            or not isinstance(physical, int)
            or not 0 < physical < CEC_INVALID_PHYSICAL_ADDRESS
        ):
            continue
        if (
            not isinstance(logical, list)
            or not logical
            or any(
                isinstance(address, bool)
                or not isinstance(address, int)
                or not 0 <= address < 0x0F
                for address in logical
            )
            or len(set(logical)) != len(logical)
            or not CEC_PLAYBACK_LOGICAL_ADDRESSES.intersection(logical)
        ):
            continue
        audio = (
            int(reported_audio)
            if (
                isinstance(reported_audio, int)
                and not isinstance(reported_audio, bool)
                and 0 <= reported_audio <= 0xFF
            )
            else None
        )
        candidates.append(
            CecDeviceSelection(
                path=path,
                reported_active=reported_active,
                reported_audio_address=audio,
                physical_address=physical,
                logical_addresses=tuple(logical),
            )
        )

    if not candidates:
        raise CecDeviceDiscoveryError(
            "cecd has no usable playback endpoint"
        )
    if len(candidates) != 1:
        paths = ", ".join(sorted(item.path for item in candidates))
        raise CecDeviceDiscoveryError(
            f"cecd has multiple usable playback endpoints: {paths}"
        )
    return candidates[0]
