"""Read-only HDMI-CEC state observer for the Decky volume OSD.

Valve's stock ``cecd`` and ``cec-audio-control`` processes remain untouched.
The observer passively watches their session-bus traffic and accepts state only
when a ``GetAudioStatus`` request can be correlated with a reply from the
current unique owner of ``cecd``, or when that owner emits a valid
``ReportAudioStatus`` message.

The sole CEC-device call this module can originate is a bounded, non-activating
``GetAudioStatus(5)`` fallback.  It never opens ``/dev/cec*`` and never sends a
volume or mute command.  The usable cecd object is rediscovered from strict
ObjectManager state on every connection epoch.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Union

from cec_device import (
    CECD_DEVICE_INTERFACE,
    CECD_DEVICES_PATH,
    CECD_ROOT_PATH,
    CECD_SERVICE,
    CEC_AUDIO_SYSTEM_LOGICAL_ADDRESS,
    DBUS_OBJECT_MANAGER_INTERFACE,
    CecDeviceDiscoveryError,
    CecDeviceSelection,
    is_cec_device_path,
    select_unique_cec_device,
)

DBUS_SERVICE = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS_INTERFACE = "org.freedesktop.DBus"
DBUS_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
CEC_CURRENT_AUDIO_ADDRESS = 0xFF
REPORT_AUDIO_STATUS = 0x7A
ROUTE_NAME = "cec-audio-system"
AUDIO_ADDRESS_PROPERTY = "AudioLogicalAddress"

INITIAL_BACKOFF_SECONDS = 0.25
MAX_BACKOFF_SECONDS = 10.0
AVAILABLE_POLL_SECONDS = 0.5
UNAVAILABLE_POLL_SECONDS = 15.0
RAPID_POLL_SECONDS = 0.2
RAPID_POLL_WINDOW_SECONDS = 2.0
COMMAND_INITIAL_DELAY_SECONDS = 0.08
COMMAND_PROBE_SECONDS = 0.14
COMMAND_SETTLE_SECONDS = 0.40
QUERY_TIMEOUT_SECONDS = 1.5
FAILURES_BEFORE_UNAVAILABLE = 2

MAX_JSON_LINE_BYTES = 64 * 1024
MAX_CALL_STDOUT_BYTES = 256 * 1024
MAX_CALL_STDERR_BYTES = 16 * 1024
PROCESS_SHUTDOWN_SECONDS = 0.25
EVENT_QUEUE_MAX = 128
MAX_PENDING_CALLS = 128

_OBSERVED_CONTROL_METHODS = frozenset({"VolumeUp", "VolumeDown", "Mute"})
_OBSERVED_TARGETS = frozenset(
    {CEC_AUDIO_SYSTEM_LOGICAL_ADDRESS, CEC_CURRENT_AUDIO_ADDRESS}
)


class CecProtocolError(RuntimeError):
    """A D-Bus or CEC value was outside the confirmed-state contract."""


@dataclass(frozen=True)
class Observation:
    route: str
    volume: Optional[float]
    mute: Optional[bool]


@dataclass(frozen=True)
class ConfirmedSnapshot:
    route: str
    volume: Optional[float]
    muted: Optional[bool]

    def payload(self) -> dict[str, object]:
        return {
            "route": self.route,
            "volume": self.volume,
            "muted": self.muted,
        }


@dataclass(frozen=True)
class MonitorTiming:
    available_poll: float = AVAILABLE_POLL_SECONDS
    unavailable_poll: float = UNAVAILABLE_POLL_SECONDS
    rapid_poll: float = RAPID_POLL_SECONDS
    rapid_window: float = RAPID_POLL_WINDOW_SECONDS
    command_initial_delay: float = COMMAND_INITIAL_DELAY_SECONDS
    command_probe: float = COMMAND_PROBE_SECONDS
    command_settle: float = COMMAND_SETTLE_SECONDS
    query_timeout: float = QUERY_TIMEOUT_SECONDS
    initial_backoff: float = INITIAL_BACKOFF_SECONDS
    max_backoff: float = MAX_BACKOFF_SECONDS


class BaselineGate:
    """Baselines a route, then identifies only fully confirmed changes."""

    def __init__(self) -> None:
        self._snapshot: Optional[ConfirmedSnapshot] = None

    def reset(self) -> None:
        self._snapshot = None

    def observe(
        self, observation: Observation
    ) -> tuple[ConfirmedSnapshot, bool, bool]:
        # Do not merge unknown fields with an older snapshot.  In particular,
        # CEC volume 0x7f must never retain or republish an old number.
        snapshot = ConfirmedSnapshot(
            route=observation.route,
            volume=observation.volume,
            muted=observation.mute,
        )
        previous = self._snapshot
        self._snapshot = snapshot
        if previous is None or previous.route != snapshot.route:
            return snapshot, False, True
        changed = snapshot != previous
        return snapshot, changed, changed


def decode_audio_status(
    volume: object, mute: object
) -> tuple[Optional[float], bool]:
    if isinstance(volume, bool) or not isinstance(volume, int):
        raise CecProtocolError("CEC volume is not an integer")
    if not isinstance(mute, bool):
        raise CecProtocolError("CEC mute state is not boolean")
    if 0 <= volume <= 100:
        return float(volume), mute
    if volume == 0x7F:
        return None, mute
    raise CecProtocolError(
        "CEC volume must be in 0..=100 or 0x7f (unknown)"
    )


def decode_report_audio_status(
    initiator: object,
    message: object,
) -> Optional[Observation]:
    if initiator != CEC_AUDIO_SYSTEM_LOGICAL_ADDRESS:
        return None
    if not isinstance(message, (bytes, bytearray, list, tuple)):
        return None
    try:
        octets = bytes(message)
    except (TypeError, ValueError):
        return None
    if len(octets) != 2 or octets[0] != REPORT_AUDIO_STATUS:
        return None
    raw = octets[1]
    try:
        volume, mute = decode_audio_status(raw & 0x7F, bool(raw & 0x80))
    except CecProtocolError:
        return None
    return Observation(ROUTE_NAME, volume, mute)


SnapshotCallback = Callable[
    [ConfirmedSnapshot], Union[Awaitable[None], None]
]
UnavailableCallback = Callable[[], Union[Awaitable[None], None]]
ActivityCallback = Callable[[], Union[Awaitable[None], None]]


async def _invoke(callback: Callable[..., object], *args: object) -> None:
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


@dataclass(frozen=True)
class _TransportEvent:
    kind: str
    body: tuple[object, ...] = ()


@dataclass(frozen=True)
class _QueuedEvent:
    epoch: int
    event: _TransportEvent


@dataclass(frozen=True)
class _ConnectionInfo:
    owner: Optional[str]
    local_unique_name: Optional[str]
    reported_audio_address: Optional[int]
    device_path: Optional[str] = None


@dataclass(frozen=True)
class _QueryReply:
    sender: str
    volume: object
    mute: object


@dataclass(frozen=True)
class _PropertyReply:
    sender: str
    address: int


@dataclass(frozen=True)
class _PendingCall:
    kind: str
    target: Optional[int]
    observed_at: float
    epoch: int


TransportEventCallback = Callable[[_TransportEvent], None]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_unique_name(value: object) -> bool:
    return isinstance(value, str) and value.startswith(":") and len(value) > 1


def _payload(
    message: dict[str, object], signature: str
) -> Optional[list[object]]:
    payload = message.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != signature:
        return None
    data = payload.get("data")
    return data if isinstance(data, list) else None


def _byte_variant(value: object) -> Optional[int]:
    if not isinstance(value, dict) or value.get("type") != "y":
        return None
    data = value.get("data")
    if not _is_int(data) or not 0 <= data <= 0xFF:
        return None
    return int(data)


def parse_busctl_json_line(
    line: bytes, device_path: Optional[str]
) -> Optional[_TransportEvent]:
    """Parse one bounded ``busctl --json=short monitor`` record.

    Unrelated, well-formed bus traffic is ignored.  Malformed JSON or a line
    that exceeds the explicit bound tears down the connection so the reducer
    can fail closed and rebaseline on a fresh epoch.
    """

    if not isinstance(line, bytes):
        raise CecProtocolError("busctl monitor line is not bytes")
    if not line or len(line) > MAX_JSON_LINE_BYTES:
        raise CecProtocolError("busctl monitor JSON line is empty or oversized")
    if not line.endswith(b"\n"):
        raise CecProtocolError("busctl monitor JSON line is unterminated")
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CecProtocolError(
            "busctl monitor returned malformed JSON"
        ) from error
    if not isinstance(message, dict):
        raise CecProtocolError("busctl monitor record is not an object")
    return parse_busctl_message(message, device_path)


def parse_busctl_message(
    message: dict[str, object],
    device_path: Optional[str],
) -> Optional[_TransportEvent]:
    message_type = message.get("type")

    if message_type == "method_call":
        sender = message.get("sender")
        destination = message.get("destination")
        cookie = message.get("cookie")
        if (
            not _is_unique_name(sender)
            or not isinstance(destination, str)
            or not (_is_unique_name(destination) or destination == CECD_SERVICE)
            or not _is_int(cookie)
            or int(cookie) <= 0
            or message.get("path") != device_path
        ):
            return None

        interface = message.get("interface")
        member = message.get("member")
        if interface == CECD_DEVICE_INTERFACE:
            data = _payload(message, "y")
            if (
                member in _OBSERVED_CONTROL_METHODS
                and data is not None
                and len(data) == 1
                and _is_int(data[0])
                and data[0] in _OBSERVED_TARGETS
            ):
                return _TransportEvent(
                    "command",
                    (
                        sender,
                        int(cookie),
                        destination,
                        str(member),
                        int(data[0]),
                    ),
                )
            if (
                member == "GetAudioStatus"
                and data is not None
                and len(data) == 1
                and _is_int(data[0])
                and data[0] in _OBSERVED_TARGETS
            ):
                return _TransportEvent(
                    "status_call",
                    (
                        sender,
                        int(cookie),
                        destination,
                        int(data[0]),
                    ),
                )
            return None

        if (
            interface == DBUS_PROPERTIES_INTERFACE
            and member == "Get"
        ):
            data = _payload(message, "ss")
            if data == [CECD_DEVICE_INTERFACE, AUDIO_ADDRESS_PROPERTY]:
                return _TransportEvent(
                    "property_call",
                    (sender, int(cookie), destination),
                )
        return None

    if message_type == "method_return":
        sender = message.get("sender")
        destination = message.get("destination")
        reply_cookie = message.get("reply_cookie")
        if (
            not _is_unique_name(sender)
            or not _is_unique_name(destination)
            or not _is_int(reply_cookie)
            or int(reply_cookie) <= 0
        ):
            return None
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return None
        signature = payload.get("type")
        data = payload.get("data")
        if signature not in {"yb", "v"} or not isinstance(data, list):
            return None
        return _TransportEvent(
            "method_return",
            (
                sender,
                destination,
                int(reply_cookie),
                signature,
                tuple(data),
            ),
        )

    if message_type == "error":
        sender = message.get("sender")
        destination = message.get("destination")
        reply_cookie = message.get("reply_cookie")
        error_name = message.get("error_name")
        if (
            _is_unique_name(sender)
            and _is_unique_name(destination)
            and _is_int(reply_cookie)
            and int(reply_cookie) > 0
            and isinstance(error_name, str)
            and bool(error_name)
        ):
            return _TransportEvent(
                "method_error",
                (
                    sender,
                    destination,
                    int(reply_cookie),
                    error_name,
                ),
            )
        return None

    if message_type != "signal":
        return None

    if (
        message.get("sender") == DBUS_SERVICE
        and message.get("path") == DBUS_PATH
        and message.get("interface") == DBUS_INTERFACE
        and message.get("member") == "NameOwnerChanged"
    ):
        data = _payload(message, "sss")
        if (
            data is not None
            and len(data) == 3
            and data[0] == CECD_SERVICE
            and isinstance(data[1], str)
            and isinstance(data[2], str)
            and (not data[1] or _is_unique_name(data[1]))
            and (not data[2] or _is_unique_name(data[2]))
        ):
            return _TransportEvent("owner", (data[1], data[2]))
        return None

    sender = message.get("sender")
    if (
        _is_unique_name(sender)
        and message.get("interface") == DBUS_OBJECT_MANAGER_INTERFACE
        and message.get("member") in {"InterfacesAdded", "InterfacesRemoved"}
    ):
        signature = (
            "oa{sa{sv}}"
            if message.get("member") == "InterfacesAdded"
            else "oas"
        )
        data = _payload(message, signature)
        if (
            data is not None
            and len(data) == 2
            and is_cec_device_path(data[0])
        ):
            return _TransportEvent("devices_changed", (sender,))
        return None

    if (
        not _is_unique_name(sender)
        or not is_cec_device_path(message.get("path"))
    ):
        return None

    message_path = str(message.get("path"))

    if (
        message_path == device_path
        and message.get("interface") == CECD_DEVICE_INTERFACE
        and message.get("member") == "ReceivedMessage"
    ):
        data = _payload(message, "yytay")
        if (
            data is None
            or len(data) != 4
            or not all(_is_int(item) for item in data[:3])
            or not 0 <= data[0] <= 0x0F
            or not 0 <= data[1] <= 0x0F
            or data[2] < 0
            or not isinstance(data[3], list)
            or not all(
                _is_int(octet) and 0 <= octet <= 0xFF
                for octet in data[3]
            )
        ):
            return None
        return _TransportEvent(
            "report",
            (
                sender,
                int(data[0]),
                int(data[1]),
                int(data[2]),
                tuple(int(octet) for octet in data[3]),
            ),
        )

    if (
        message.get("interface") == DBUS_PROPERTIES_INTERFACE
        and message.get("member") == "PropertiesChanged"
    ):
        data = _payload(message, "sa{sv}as")
        if (
            data is None
            or len(data) != 3
            or data[0] != CECD_DEVICE_INTERFACE
            or not isinstance(data[1], dict)
            or not isinstance(data[2], list)
            or not all(isinstance(item, str) for item in data[2])
        ):
            return None
        changed = data[1]
        invalidated = data[2]
        topology_properties = {
            "PhysicalAddress",
            "LogicalAddresses",
        }
        if topology_properties.intersection(changed) or (
            topology_properties.intersection(invalidated)
        ):
            return _TransportEvent("devices_changed", (sender,))
        if message_path != device_path:
            return None
        if AUDIO_ADDRESS_PROPERTY in changed:
            address = _byte_variant(changed[AUDIO_ADDRESS_PROPERTY])
            return _TransportEvent(
                "property", (sender, True, address)
            )
        if AUDIO_ADDRESS_PROPERTY in invalidated:
            return _TransportEvent("property", (sender, False, None))
    return None


class DbusNextCecdTransport:
    """Read-only cecd transport implemented exclusively with ``busctl``.

    The historical class name is retained for compatibility.  There is no
    Python D-Bus dependency: one long-lived busctl process observes traffic,
    while bounded, non-activating busctl calls perform the two allowed reads.
    """

    def __init__(self) -> None:
        self._monitor_process = None
        self._monitor_task: Optional[asyncio.Task[None]] = None
        self._call_processes: set[object] = set()
        self._current_owner: Optional[str] = None
        self._device_path: Optional[str] = None
        self._owner_revision = 0

    @staticmethod
    def _native_subprocess_environment() -> dict[str, str]:
        """Undo PyInstaller's private library path for system binaries."""
        environment = dict(os.environ)
        original_library_path = environment.pop(
            "LD_LIBRARY_PATH_ORIG", None
        )
        if original_library_path:
            environment["LD_LIBRARY_PATH"] = original_library_path
        else:
            environment.pop("LD_LIBRARY_PATH", None)
        environment.pop("LD_PRELOAD", None)
        runtime_directory = f"/run/user/{os.getuid()}"
        environment.setdefault("XDG_RUNTIME_DIR", runtime_directory)
        environment.setdefault(
            "DBUS_SESSION_BUS_ADDRESS",
            f"unix:path={runtime_directory}/bus",
        )
        return environment

    async def connect(
        self, on_event: TransportEventCallback
    ) -> _ConnectionInfo:
        if self._monitor_process is not None:
            raise RuntimeError("D-Bus transport is already connected")
        self._current_owner = None
        self._device_path = None
        self._owner_revision = 0

        rules = (
            (
                "type='method_call',"
                f"destination='{CECD_SERVICE}',"
                f"path_namespace='{CECD_DEVICES_PATH}'"
            ),
            f"type='method_return',sender='{CECD_SERVICE}'",
            f"type='error',sender='{CECD_SERVICE}'",
            (
                "type='signal',"
                f"sender='{CECD_SERVICE}',"
                f"path_namespace='{CECD_ROOT_PATH}'"
            ),
            (
                "type='signal',"
                f"sender='{DBUS_SERVICE}',"
                f"path='{DBUS_PATH}',"
                f"interface='{DBUS_INTERFACE}',"
                "member='NameOwnerChanged',"
                f"arg0='{CECD_SERVICE}'"
            ),
        )
        command = [
            "/usr/bin/busctl",
            "--user",
            "--json=short",
            "--quiet",
        ]
        command.extend(f"--match={rule}" for rule in rules)
        command.append("monitor")
        self._monitor_process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_JSON_LINE_BYTES + 1,
            env=self._native_subprocess_environment(),
        )
        if self._monitor_process.stdout is None:
            await self._terminate_process(self._monitor_process)
            raise RuntimeError("busctl monitor has no output stream")
        self._monitor_task = asyncio.create_task(
            self._read_monitor(on_event),
            name="cec-osd-busctl-monitor",
        )

        # Resolve only after monitoring starts. If a NameOwnerChanged signal
        # races GetNameOwner, the signal-updated owner wins.
        revision = self._owner_revision
        resolved_owner = await self._get_current_owner()
        if self._owner_revision == revision:
            self._current_owner = resolved_owner
        owner = self._current_owner

        reported_audio_address = None
        device_path = None
        if owner is not None:
            selection = await self._get_cec_device(owner)
            device_path = selection.path
            reported_audio_address = selection.reported_audio_address
            self._device_path = device_path
        if self._monitor_task.done():
            await self._monitor_task
        return _ConnectionInfo(
            owner, None, reported_audio_address, device_path
        )

    async def _get_cec_device(
        self, owner: str
    ) -> CecDeviceSelection:
        if not _is_unique_name(owner) or self._current_owner != owner:
            raise CecProtocolError(
                "CEC endpoint discovery requires the current unique owner"
            )
        status, stdout, stderr = await self._execute_busctl(
            self._call_command(
                owner,
                CECD_ROOT_PATH,
                DBUS_OBJECT_MANAGER_INTERFACE,
                "GetManagedObjects",
                None,
            ),
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        if status != 0:
            raise RuntimeError(
                "cecd GetManagedObjects failed "
                f"({status}): {self._diagnostic(stderr)}"
            )
        try:
            selection = select_unique_cec_device(stdout)
        except CecDeviceDiscoveryError as error:
            raise CecProtocolError(str(error)) from error
        if self._current_owner != owner:
            raise CecProtocolError(
                "cecd owner changed during CEC endpoint discovery"
            )
        return selection

    async def _get_initial_audio_address(
        self, owner: str
    ) -> _PropertyReply:
        return await asyncio.wait_for(
            self.get_audio_logical_address(owner),
            timeout=QUERY_TIMEOUT_SECONDS,
        )

    async def _get_current_owner(self) -> Optional[str]:
        status, stdout, stderr = await self._execute_busctl(
            self._call_command(
                DBUS_SERVICE,
                DBUS_PATH,
                DBUS_INTERFACE,
                "GetNameOwner",
                "s",
                CECD_SERVICE,
            ),
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        if status != 0:
            diagnostic = self._diagnostic(stderr)
            if self._is_name_has_no_owner(diagnostic):
                return None
            raise RuntimeError(
                f"D-Bus GetNameOwner failed ({status}): {diagnostic}"
            )
        reply = self._decode_call_json(stdout)
        data = reply.get("data")
        if (
            reply.get("type") != "s"
            or not isinstance(data, list)
            or len(data) != 1
            or not _is_unique_name(data[0])
        ):
            raise CecProtocolError("D-Bus GetNameOwner reply is malformed")
        return str(data[0])

    async def _read_monitor(
        self, on_event: TransportEventCallback
    ) -> None:
        process = self._monitor_process
        if process is None or process.stdout is None:
            raise RuntimeError("busctl monitor has no output stream")
        while True:
            try:
                line = await process.stdout.readline()
            except ValueError as error:
                raise CecProtocolError(
                    "busctl monitor JSON line exceeded the read bound"
                ) from error
            if not line:
                status = await process.wait()
                raise RuntimeError(
                    f"busctl monitor exited with status {status}"
                )
            event = parse_busctl_json_line(line, self._device_path)
            if event is None:
                continue
            if event.kind == "owner" and len(event.body) == 2:
                new_owner = event.body[1]
                self._current_owner = (
                    str(new_owner)
                    if _is_unique_name(new_owner)
                    else None
                )
                self._owner_revision += 1
            on_event(event)
            if event.kind == "owner":
                raise RuntimeError(
                    "cecd owner changed; rediscovering usable endpoint"
                )

    async def query_audio_status(self, address: int) -> _QueryReply:
        # Keep this allowlist adjacent to process creation. This transport can
        # never originate a CEC write.
        if address != CEC_AUDIO_SYSTEM_LOGICAL_ADDRESS:
            raise ValueError("only GetAudioStatus(5) is permitted")
        expected_owner = self._current_owner
        if not _is_unique_name(expected_owner):
            raise RuntimeError("cecd has no confirmed unique owner")
        status, stdout, stderr = await self._execute_busctl(
            self._call_command(
                expected_owner,
                self._require_device_path(),
                CECD_DEVICE_INTERFACE,
                "GetAudioStatus",
                "y",
                str(CEC_AUDIO_SYSTEM_LOGICAL_ADDRESS),
            ),
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        if status != 0:
            raise RuntimeError(
                "cecd GetAudioStatus failed "
                f"({status}): {self._diagnostic(stderr)}"
            )
        reply = self._decode_call_json(stdout)
        data = reply.get("data")
        if (
            reply.get("type") != "yb"
            or not isinstance(data, list)
            or len(data) != 2
            or not _is_int(data[0])
            or not 0 <= int(data[0]) <= 0xFF
            or not isinstance(data[1], bool)
        ):
            raise CecProtocolError("cecd GetAudioStatus reply is malformed")
        if self._current_owner != expected_owner:
            raise CecProtocolError(
                "cecd owner changed during GetAudioStatus"
            )
        return _QueryReply(expected_owner, int(data[0]), data[1])

    async def get_audio_logical_address(
        self, owner: str
    ) -> _PropertyReply:
        if not _is_unique_name(owner):
            raise ValueError(
                "AudioLogicalAddress requires a unique cecd owner"
            )
        if self._current_owner != owner:
            raise CecProtocolError(
                "AudioLogicalAddress owner is no longer current"
            )
        status, stdout, stderr = await self._execute_busctl(
            self._call_command(
                owner,
                self._require_device_path(),
                DBUS_PROPERTIES_INTERFACE,
                "Get",
                "ss",
                CECD_DEVICE_INTERFACE,
                AUDIO_ADDRESS_PROPERTY,
            ),
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        if status != 0:
            raise RuntimeError(
                "cecd AudioLogicalAddress read failed "
                f"({status}): {self._diagnostic(stderr)}"
            )
        reply = self._decode_call_json(stdout)
        data = reply.get("data")
        variant = (
            data[0]
            if isinstance(data, list) and len(data) == 1
            else None
        )
        address = (
            variant.get("data")
            if isinstance(variant, dict)
            and set(variant) == {"type", "data"}
            and variant.get("type") == "y"
            else None
        )
        if (
            reply.get("type") != "v"
            or not _is_int(address)
            or not 0 <= int(address) <= 0xFF
        ):
            raise CecProtocolError(
                "cecd AudioLogicalAddress reply is malformed"
            )
        if self._current_owner != owner:
            raise CecProtocolError(
                "cecd owner changed during AudioLogicalAddress read"
            )
        return _PropertyReply(owner, int(address))

    @staticmethod
    def _call_command(
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: Optional[str],
        *arguments: str,
    ) -> list[str]:
        timeout = max(0.001, float(QUERY_TIMEOUT_SECONDS))
        command = [
            "/usr/bin/busctl",
            "--user",
            "--json=short",
            "--auto-start=no",
            f"--timeout={timeout:.3f}s",
            "call",
            destination,
            path,
            interface,
            member,
        ]
        if signature is not None:
            command.extend((signature, *arguments))
        elif arguments:
            raise ValueError("D-Bus arguments require a signature")
        return command

    def _require_device_path(self) -> str:
        if not is_cec_device_path(self._device_path):
            raise RuntimeError("cecd has no selected usable CEC endpoint")
        return str(self._device_path)

    @staticmethod
    def _decode_call_json(stdout: bytes) -> dict[str, object]:
        if (
            not isinstance(stdout, bytes)
            or not stdout
            or len(stdout) > MAX_CALL_STDOUT_BYTES
        ):
            raise CecProtocolError(
                "busctl call returned empty or oversized output"
            )
        try:
            reply = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CecProtocolError(
                "busctl call returned malformed JSON"
            ) from error
        if (
            not isinstance(reply, dict)
            or set(reply) != {"type", "data"}
            or not isinstance(reply.get("type"), str)
        ):
            raise CecProtocolError(
                "busctl call JSON shape is malformed"
            )
        return reply

    @staticmethod
    def _diagnostic(stderr: bytes) -> str:
        if not isinstance(stderr, bytes):
            return "invalid diagnostic"
        diagnostic = stderr.decode("utf-8", errors="replace").strip()
        return diagnostic[:512] if diagnostic else "no diagnostic"

    @staticmethod
    def _is_name_has_no_owner(diagnostic: str) -> bool:
        lowered = diagnostic.lower()
        return (
            "namehasnoowner" in lowered
            or "name has no owner" in lowered
            or (
                CECD_SERVICE.lower() in lowered
                and "does not exist" in lowered
            )
        )

    @staticmethod
    async def _read_bounded(
        stream: object, maximum: int, label: str
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(min(4096, maximum - total + 1))
            if not chunk:
                return b"".join(chunks)
            if not isinstance(chunk, bytes):
                raise CecProtocolError(
                    f"busctl {label} stream returned non-bytes"
                )
            total += len(chunk)
            if total > maximum:
                raise CecProtocolError(
                    f"busctl {label} exceeded its output bound"
                )
            chunks.append(chunk)

    @staticmethod
    async def _terminate_process(process: object) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(
                process.wait(), timeout=PROCESS_SHUTDOWN_SECONDS
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    async def _execute_busctl(
        self, command: list[str], *, timeout: float
    ) -> tuple[int, bytes, bytes]:
        if (
            not isinstance(command, list)
            or not command
            or command[0] != "/usr/bin/busctl"
            or any(not isinstance(item, str) for item in command)
        ):
            raise ValueError("busctl command is not allowlisted")
        if timeout <= 0:
            raise ValueError("busctl timeout must be positive")

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=max(MAX_CALL_STDOUT_BYTES, MAX_CALL_STDERR_BYTES) + 1,
            env=self._native_subprocess_environment(),
        )
        if process.stdout is None or process.stderr is None:
            await self._terminate_process(process)
            raise RuntimeError("busctl call has no output pipes")
        self._call_processes.add(process)
        stdout_task = asyncio.create_task(
            self._read_bounded(
                process.stdout, MAX_CALL_STDOUT_BYTES, "stdout"
            )
        )
        stderr_task = asyncio.create_task(
            self._read_bounded(
                process.stderr, MAX_CALL_STDERR_BYTES, "stderr"
            )
        )
        wait_task = asyncio.create_task(process.wait())
        tasks = (stdout_task, stderr_task, wait_task)
        combined = asyncio.gather(*tasks)
        try:
            try:
                stdout, stderr, status = await asyncio.wait_for(
                    combined, timeout=timeout
                )
            except asyncio.TimeoutError as error:
                await self._terminate_process(process)
                raise asyncio.TimeoutError(
                    "busctl call exceeded its deadline"
                ) from error
            except asyncio.CancelledError:
                await self._terminate_process(process)
                raise
            except Exception:
                await self._terminate_process(process)
                raise
            return int(status), stdout, stderr
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not combined.done():
                combined.cancel()
            await asyncio.gather(combined, return_exceptions=True)
            self._call_processes.discard(process)

    async def wait_disconnected(self) -> None:
        if self._monitor_task is not None:
            await self._monitor_task

    async def close(self) -> None:
        call_processes = list(self._call_processes)
        if call_processes:
            await asyncio.gather(
                *(
                    self._terminate_process(process)
                    for process in call_processes
                ),
                return_exceptions=True,
            )
        if (
            self._monitor_process is not None
            and self._monitor_process.returncode is None
        ):
            await self._terminate_process(self._monitor_process)
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(
                self._monitor_task, return_exceptions=True
            )
        self._monitor_process = None
        self._monitor_task = None
        self._current_owner = None
        self._device_path = None
        self._call_processes.clear()


TransportFactory = Callable[[], object]


class CecdVolumeMonitor:
    """Serialize passive observations into confirmed Decky snapshots."""

    def __init__(
        self,
        on_snapshot: SnapshotCallback,
        on_change: SnapshotCallback,
        on_unavailable: UnavailableCallback,
        logger: object,
        transport_factory: Optional[TransportFactory] = None,
        timing: MonitorTiming = MonitorTiming(),
        on_activity: Optional[ActivityCallback] = None,
    ) -> None:
        self._on_snapshot = on_snapshot
        self._on_change = on_change
        self._on_unavailable = on_unavailable
        self._on_activity = on_activity
        self._logger = logger
        self._transport_factory = (
            transport_factory
            if transport_factory is not None
            else DbusNextCecdTransport
        )
        self._timing = timing
        self._stopping = asyncio.Event()
        self._gate = BaselineGate()
        self._available = False
        self._query_failures = 0
        self._transport = None
        self._connection_epoch = 0
        self._device_path: Optional[str] = None

        # The following fields are mutated only by the connected reducer.
        self._owner: Optional[str] = None
        self._endpoint_ready = False
        self._audio_address: Optional[int] = None
        self._property_authenticated = False
        self._bootstrap_pending = True
        self._local_unique_name: Optional[str] = None
        self._pending: dict[tuple[str, int], _PendingCall] = {}
        self._revision = 0
        self._property_revision = 0
        self._query_task: Optional[asyncio.Task[object]] = None
        self._query_context: Optional[
            tuple[int, str, int]
        ] = None
        self._property_task: Optional[asyncio.Task[object]] = None
        self._property_context: Optional[
            tuple[int, str, int]
        ] = None
        self._next_idle = float("inf")
        self._rapid_until: Optional[float] = None

    def stop(self) -> None:
        self._stopping.set()

    @property
    def device_path(self) -> Optional[str]:
        return self._device_path

    async def run(self) -> None:
        backoff = self._timing.initial_backoff
        while not self._stopping.is_set():
            self._connection_epoch += 1
            epoch = self._connection_epoch
            queue: asyncio.Queue[_QueuedEvent] = asyncio.Queue(
                maxsize=EVENT_QUEUE_MAX
            )

            def enqueue(event: _TransportEvent) -> None:
                if self._stopping.is_set():
                    return
                queued = _QueuedEvent(epoch, event)
                try:
                    queue.put_nowait(queued)
                except asyncio.QueueFull:
                    self._log_warning(
                        "cecd observer event queue overflowed; "
                        "discarding the epoch backlog"
                    )
                    while True:
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    queue.put_nowait(
                        _QueuedEvent(
                            epoch, _TransportEvent("overflow")
                        )
                    )

            transport = self._transport_factory()
            self._transport = transport
            self._device_path = None
            try:
                info = await transport.connect(enqueue)
                if not isinstance(info, _ConnectionInfo):
                    info = _ConnectionInfo(None, None, None, None)
                self._device_path = info.device_path
                backoff = self._timing.initial_backoff
                await self._run_connected(
                    transport, queue, epoch, info
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._log_warning(
                    "read-only cecd observer reconnecting after "
                    f"{type(error).__name__}: {error!r}"
                )
            finally:
                await self._cancel_query()
                await self._cancel_property_read()
                try:
                    await transport.close()
                except Exception as error:
                    self._log_warning(
                        f"read-only cecd observer close failed: {error}"
                    )
                self._transport = None
                self._device_path = None

            if self._stopping.is_set():
                break
            await self._fail_closed(reset_baseline=True)
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=backoff
                )
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self._timing.max_backoff)

    async def _run_connected(
        self,
        transport: object,
        queue: asyncio.Queue[_QueuedEvent],
        epoch: int,
        info: _ConnectionInfo,
    ) -> None:
        now = asyncio.get_running_loop().time()
        self._owner = None
        self._endpoint_ready = False
        self._audio_address = None
        self._property_authenticated = False
        self._local_unique_name = info.local_unique_name
        self._device_path = info.device_path
        self._pending.clear()
        self._query_failures = 0
        self._next_idle = float("inf")
        self._rapid_until = None
        await self._set_owner(
            info.owner,
            now,
            transport,
            epoch,
            initial_address=info.reported_audio_address,
        )

        stop_task = asyncio.create_task(
            self._stopping.wait(), name="cec-osd-stop-wait"
        )
        disconnect_task = asyncio.create_task(
            transport.wait_disconnected(),
            name="cec-osd-dbus-disconnect",
        )
        event_task = asyncio.create_task(
            queue.get(), name="cec-osd-event-reducer-input"
        )
        try:
            while not self._stopping.is_set():
                # Drain an already-delivered passive event before evaluating
                # an idle deadline. Continuous stock activity must postpone,
                # not race with, the observer's fallback read.
                if event_task.done():
                    queued = event_task.result()
                    event_task = asyncio.create_task(
                        queue.get(),
                        name="cec-osd-event-reducer-input",
                    )
                    if queued.epoch == epoch:
                        await self._reduce_event(
                            queued.event, transport, epoch
                        )
                    continue
                now = asyncio.get_running_loop().time()
                await self._drive_timers(transport, epoch, now)
                deadline = self._next_deadline(now)
                timer_task = asyncio.create_task(
                    asyncio.sleep(max(0.0, deadline - now)),
                    name="cec-osd-reducer-timer",
                )
                waiters = {
                    stop_task,
                    disconnect_task,
                    event_task,
                    timer_task,
                }
                if self._query_task is not None:
                    waiters.add(self._query_task)
                if self._property_task is not None:
                    waiters.add(self._property_task)
                done, _pending = await asyncio.wait(
                    waiters, return_when=asyncio.FIRST_COMPLETED
                )

                if not timer_task.done():
                    timer_task.cancel()
                    await asyncio.gather(
                        timer_task, return_exceptions=True
                    )

                if stop_task in done:
                    return

                # Bus events are ordered ahead of a simultaneously completed
                # fallback.  An owner-loss event therefore invalidates the old
                # reply before it can reach the state gate.
                if event_task in done:
                    queued = event_task.result()
                    event_task = asyncio.create_task(
                        queue.get(),
                        name="cec-osd-event-reducer-input",
                    )
                    if queued.epoch == epoch:
                        await self._reduce_event(
                            queued.event, transport, epoch
                        )
                    continue

                if disconnect_task in done:
                    await disconnect_task
                    if not self._stopping.is_set():
                        raise RuntimeError(
                            "session D-Bus monitor disconnected"
                        )
                    return

                if (
                    self._query_task is not None
                    and self._query_task in done
                ):
                    await self._reduce_query_result(
                        self._query_task, epoch
                    )
                    continue
                if (
                    self._property_task is not None
                    and self._property_task in done
                ):
                    await self._reduce_property_result(
                        self._property_task, epoch
                    )
                    continue
                # Otherwise only the reducer timer fired; the next iteration
                # drives the due transition.
        finally:
            for task in (stop_task, disconnect_task, event_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                stop_task,
                disconnect_task,
                event_task,
                return_exceptions=True,
            )

    async def _reduce_event(
        self,
        event: _TransportEvent,
        transport: object,
        epoch: int,
    ) -> None:
        now = asyncio.get_running_loop().time()
        self._prune_pending(now)

        if event.kind == "owner" and len(event.body) == 2:
            old_owner, new_owner = event.body
            owner = (
                str(new_owner)
                if _is_unique_name(new_owner)
                else None
            )
            if owner == self._owner:
                return
            if (
                (self._owner is None and old_owner)
                or (
                    self._owner is not None
                    and old_owner != self._owner
                )
            ):
                # NameOwnerChanged is consumed on both the direct safety
                # match and the busctl monitor.  A delayed duplicate from one
                # connection must not roll the reducer back to an old owner.
                return
            raise RuntimeError(
                "cecd owner changed; rediscovering usable endpoint"
            )

        if event.kind == "devices_changed" and len(event.body) == 1:
            if event.body[0] != self._owner:
                return
            raise RuntimeError(
                "cecd endpoint topology changed; rediscovering"
            )

        if event.kind == "overflow":
            self._note_passive_activity(now)
            self._property_revision += 1
            self._pending.clear()
            self._audio_address = None
            self._property_authenticated = False
            await self._cancel_query()
            await self._cancel_property_read()
            await self._fail_closed(reset_baseline=True, now=now)
            if self._owner is not None:
                self._launch_property_read(transport, epoch)
            return

        if event.kind == "property" and len(event.body) == 3:
            sender, present, address = event.body
            if sender != self._owner:
                return
            self._note_passive_activity(now)
            self._property_revision += 1
            if not present or address is None:
                await self._set_audio_address(
                    None, authenticated=False
                )
                self._launch_property_read(transport, epoch)
            elif _is_int(address) and 0 <= int(address) <= 0xFF:
                await self._set_audio_address(
                    int(address), authenticated=True
                )
            return

        if event.kind == "command" and len(event.body) == 5:
            sender, _cookie, destination, _member, target = event.body
            if (
                not _is_unique_name(sender)
                or not self._valid_service_destination(destination)
                or target not in _OBSERVED_TARGETS
            ):
                return
            self._note_passive_activity(now)
            if self._on_activity is not None:
                await self._safe_callback(
                    "activity", self._on_activity
                )
            return

        if event.kind == "status_call" and len(event.body) == 4:
            sender, cookie, destination, target = event.body
            if (
                sender == self._local_unique_name
                or not _is_unique_name(sender)
                or not _is_int(cookie)
                or not self._valid_service_destination(destination)
                or target not in _OBSERVED_TARGETS
            ):
                return
            self._note_passive_activity(now)
            await self._remember_call(
                (str(sender), int(cookie)),
                _PendingCall(
                    "status",
                    int(target),
                    now,
                    epoch,
                ),
                now,
            )
            return

        if event.kind == "property_call" and len(event.body) == 3:
            sender, cookie, destination = event.body
            if (
                sender == self._local_unique_name
                or not _is_unique_name(sender)
                or not _is_int(cookie)
                or not self._valid_service_destination(destination)
            ):
                return
            self._note_passive_activity(now)
            await self._remember_call(
                (str(sender), int(cookie)),
                _PendingCall("property", None, now, epoch),
                now,
            )
            return

        if event.kind == "method_error" and len(event.body) == 4:
            sender, destination, reply_cookie, error_name = event.body
            if (
                sender != self._owner
                or not _is_unique_name(destination)
                or not _is_int(reply_cookie)
            ):
                return
            pending = self._pending.pop(
                (str(destination), int(reply_cookie)), None
            )
            if pending is None or pending.epoch != epoch:
                return
            self._note_passive_activity(now)
            if pending.kind == "property":
                self._property_revision += 1
                self._log_warning(
                    "advisory cecd AudioLogicalAddress read failed: "
                    f"{error_name}; continuing direct GetAudioStatus(5)"
                )
                await self._set_audio_address(
                    None, authenticated=False
                )
                self._launch_property_read(transport, epoch)
                return
            self._log_warning(
                "authenticated cecd read failed: "
                f"{error_name}"
            )
            await self._fail_closed(reset_baseline=True, now=now)
            return

        if event.kind == "method_return" and len(event.body) == 5:
            sender, destination, reply_cookie, signature, data = event.body
            if (
                sender != self._owner
                or not _is_unique_name(destination)
                or not _is_int(reply_cookie)
            ):
                return
            pending = self._pending.pop(
                (str(destination), int(reply_cookie)), None
            )
            if pending is None or pending.epoch != epoch:
                return
            if pending.kind == "property":
                if signature != "v" or len(data) != 1:
                    return
                self._note_passive_activity(now)
                self._property_revision += 1
                address = _byte_variant(data[0])
                await self._set_audio_address(
                    address,
                    authenticated=address is not None,
                )
                if address is None:
                    self._launch_property_read(transport, epoch)
                return
            if pending.kind != "status":
                return
            self._note_passive_activity(now)
            if signature != "yb" or len(data) != 2:
                self._log_warning(
                    "authenticated cecd status reply had an invalid shape"
                )
                await self._fail_closed(reset_baseline=True, now=now)
                return
            if (
                pending.target == CEC_CURRENT_AUDIO_ADDRESS
                and (
                    not self._property_authenticated
                    or self._audio_address
                    != CEC_AUDIO_SYSTEM_LOGICAL_ADDRESS
                )
            ):
                return
            try:
                volume, mute = decode_audio_status(data[0], data[1])
            except CecProtocolError as error:
                self._log_warning(
                    f"authenticated cecd status was malformed: {error}"
                )
                await self._fail_closed(reset_baseline=True, now=now)
                return
            await self._accept_observation(
                Observation(ROUTE_NAME, volume, mute),
                now,
            )
            return

        if event.kind == "report" and len(event.body) == 5:
            sender, initiator, _destination, _timestamp, message = event.body
            if sender != self._owner:
                return
            observation = decode_report_audio_status(
                initiator, message
            )
            if observation is not None:
                self._note_passive_activity(now)
                await self._accept_observation(observation, now)
                if self._on_activity is not None:
                    await self._safe_callback(
                        "activity", self._on_activity
                    )
                return

    def _valid_service_destination(self, destination: object) -> bool:
        return destination == CECD_SERVICE or (
            self._owner is not None and destination == self._owner
        )

    async def _remember_call(
        self,
        key: tuple[str, int],
        pending: _PendingCall,
        now: float,
    ) -> None:
        if len(self._pending) >= MAX_PENDING_CALLS and key not in self._pending:
            self._pending.clear()
            self._log_warning(
                "cecd observer pending-call table overflowed; "
                "discarding correlation state"
            )
            self._note_passive_activity(now)
            await self._cancel_query()
            await self._fail_closed(reset_baseline=True)
            self._next_idle = (
                now + self._timing.available_poll
                if (
                    self._owner is not None
                    and self._endpoint_ready
                )
                else float("inf")
            )
            return
        self._pending[key] = pending

    async def _set_owner(
        self,
        owner: Optional[str],
        now: float,
        transport: object,
        epoch: int,
        initial_address: Optional[int] = None,
    ) -> None:
        if owner == self._owner:
            if (
                owner is not None
                and initial_address is not None
                and not self._property_authenticated
            ):
                await self._set_audio_address(
                    initial_address, authenticated=True
                )
            return
        self._revision += 1
        self._property_revision += 1
        self._owner = owner
        self._endpoint_ready = (
            owner is not None and is_cec_device_path(self._device_path)
        )
        self._audio_address = None
        self._property_authenticated = False
        self._bootstrap_pending = True
        self._pending.clear()
        self._query_failures = 0
        await self._cancel_query()
        await self._cancel_property_read()
        await self._fail_closed(reset_baseline=True)
        self._next_idle = float("inf")
        if owner is None:
            return
        if not self._endpoint_ready:
            raise CecProtocolError(
                "cecd owner has no selected usable playback endpoint"
            )
        # The endpoint selection and unique owner authenticate a direct
        # protocol-defined GetAudioStatus(5). AudioLogicalAddress remains an
        # advisory hint only and must not block the confirmed read path.
        self._next_idle = now + self._timing.command_initial_delay
        if initial_address is not None:
            await self._set_audio_address(
                initial_address, authenticated=True
            )
            return
        self._launch_property_read(transport, epoch)

    async def _set_audio_address(
        self,
        address: Optional[int],
        authenticated: bool,
    ) -> None:
        previous = self._audio_address
        was_authenticated = self._property_authenticated
        self._audio_address = address
        self._property_authenticated = authenticated
        property_changed = (
            previous != address
            or was_authenticated != authenticated
        )
        if property_changed:
            # The hint only authenticates correlated calls made to cecd's
            # special "current audio address" target (255). A changed or
            # missing hint invalidates those correlations, but never revokes
            # the independently authenticated direct target-5 read path.
            self._pending.clear()
        if authenticated and not was_authenticated:
            self._bootstrap_pending = True
            if self._endpoint_ready:
                self._next_idle = min(
                    self._next_idle,
                    asyncio.get_running_loop().time()
                    + self._timing.command_initial_delay,
                )

    def _note_passive_activity(self, now: float) -> None:
        self._revision += 1
        if self._bootstrap_pending and self._next_idle != float("inf"):
            return
        self._next_idle = (
            now + self._idle_interval(now)
            if (
                self._owner is not None
                and self._endpoint_ready
            )
            else float("inf")
        )

    def _launch_property_read(
        self, transport: object, epoch: int
    ) -> None:
        if self._property_task is not None or self._owner is None:
            return
        owner = self._owner
        self._property_context = (
            epoch,
            owner,
            self._property_revision,
        )
        self._property_task = asyncio.create_task(
            asyncio.wait_for(
                transport.get_audio_logical_address(owner),
                timeout=self._timing.query_timeout,
            ),
            name="cec-osd-audio-address-read",
        )

    async def _reduce_property_result(
        self, task: asyncio.Task[object], epoch: int
    ) -> None:
        context = self._property_context
        self._property_task = None
        self._property_context = None
        if context is None:
            return
        property_epoch, expected_owner, property_revision = context
        try:
            reply = task.result()
        except asyncio.CancelledError:
            return
        except Exception as error:
            self._log_warning(
                "advisory AudioLogicalAddress read failed; "
                f"continuing direct GetAudioStatus(5): {error}"
            )
            await self._set_audio_address(None, authenticated=False)
            return
        if (
            property_epoch != epoch
            or expected_owner != self._owner
            or property_revision != self._property_revision
        ):
            return
        if (
            not isinstance(reply, _PropertyReply)
            or reply.sender != self._owner
        ):
            self._log_warning(
                "advisory AudioLogicalAddress reply was not from the "
                "current cecd owner; continuing direct GetAudioStatus(5)"
            )
            await self._set_audio_address(None, authenticated=False)
            return
        self._revision += 1
        await self._set_audio_address(
            reply.address, authenticated=True
        )

    async def _drive_timers(
        self, transport: object, epoch: int, now: float
    ) -> None:
        self._prune_pending(now)
        if (
            self._owner is not None
            and self._endpoint_ready
            and self._query_task is None
            and now >= self._next_idle
        ):
            self._launch_query(transport, epoch)
            self._next_idle = float("inf")

    def _launch_query(
        self,
        transport: object,
        epoch: int,
    ) -> None:
        if (
            self._query_task is not None
            or self._owner is None
            or not self._endpoint_ready
        ):
            return
        owner = self._owner
        self._query_context = (epoch, owner, self._revision)
        self._query_task = asyncio.create_task(
            asyncio.wait_for(
                transport.query_audio_status(
                    CEC_AUDIO_SYSTEM_LOGICAL_ADDRESS
                ),
                timeout=self._timing.query_timeout,
            ),
            name="cec-osd-idle-status-fallback",
        )

    async def _reduce_query_result(
        self, task: asyncio.Task[object], epoch: int
    ) -> None:
        context = self._query_context
        self._query_task = None
        self._query_context = None
        if context is None:
            return
        query_epoch, expected_owner, launch_revision = context
        try:
            reply = task.result()
            if (
                query_epoch != epoch
                or expected_owner != self._owner
                or launch_revision != self._revision
                or not self._endpoint_ready
            ):
                return
            if not isinstance(reply, _QueryReply):
                raise CecProtocolError(
                    "fallback query did not return an authenticated reply"
                )
            if reply.sender != self._owner:
                raise CecProtocolError(
                    "fallback query reply was not sent by current cecd owner"
                )
            volume, mute = decode_audio_status(
                reply.volume, reply.mute
            )
            await self._accept_observation(
                Observation(ROUTE_NAME, volume, mute),
                asyncio.get_running_loop().time(),
            )
        except asyncio.CancelledError:
            return
        except Exception as error:
            if (
                query_epoch == epoch
                and expected_owner == self._owner
                and launch_revision == self._revision
            ):
                await self._record_query_failure(error)
        now = asyncio.get_running_loop().time()
        self._next_idle = now + self._idle_interval(now)

    async def _record_query_failure(self, error: Exception) -> None:
        self._query_failures += 1
        self._log_warning(f"confirmed CEC status query failed: {error}")
        if self._query_failures >= FAILURES_BEFORE_UNAVAILABLE:
            await self._fail_closed(reset_baseline=True)
            raise RuntimeError(
                "confirmed CEC endpoint became unavailable; rediscovering"
            )

    async def _accept_observation(
        self,
        observation: Observation,
        observed_at: float,
    ) -> None:
        if observation.volume is None:
            self._log_warning(
                "CEC reported explicit unknown volume; hiding stale state"
            )
            await self._fail_closed(
                reset_baseline=True, now=observed_at
            )
            return
        if not isinstance(observation.mute, bool):
            await self._fail_closed(
                reset_baseline=True, now=observed_at
            )
            return

        snapshot, changed, publish = self._gate.observe(observation)
        was_available = self._available
        was_bootstrap = self._bootstrap_pending
        self._bootstrap_pending = False
        self._available = True
        self._query_failures = 0
        if was_bootstrap or not was_available:
            self._next_idle = observed_at + self._timing.available_poll
        if publish or not was_available:
            await self._safe_callback(
                "snapshot", self._on_snapshot, snapshot
            )
        if changed:
            self._enter_rapid_polling(observed_at)
            await self._safe_callback(
                "change", self._on_change, snapshot
            )

    async def _fail_closed(
        self,
        reset_baseline: bool,
        now: Optional[float] = None,
    ) -> None:
        observed_at = (
            asyncio.get_running_loop().time()
            if now is None
            else now
        )
        self._rapid_until = None
        if reset_baseline:
            self._gate.reset()
        was_available = self._available
        self._available = False
        self._next_idle = (
            observed_at + self._timing.unavailable_poll
            if (
                self._owner is not None
                and self._endpoint_ready
            )
            else float("inf")
        )
        if not was_available:
            return
        await self._safe_callback(
            "unavailable", self._on_unavailable
        )

    async def _safe_callback(
        self,
        name: str,
        callback: Callable[..., object],
        *args: object,
    ) -> None:
        try:
            await _invoke(callback, *args)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._log_warning(
                f"CEC observer {name} callback failed: "
                f"{type(error).__name__}: {error}"
            )

    async def _cancel_query(self) -> None:
        task = self._query_task
        self._query_task = None
        self._query_context = None
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _cancel_property_read(self) -> None:
        task = self._property_task
        self._property_task = None
        self._property_context = None
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def _prune_pending(self, now: float) -> None:
        cutoff = now - self._timing.query_timeout
        stale = [
            key
            for key, pending in self._pending.items()
            if pending.observed_at < cutoff
        ]
        for key in stale:
            self._pending.pop(key, None)

    def _enter_rapid_polling(self, now: float) -> None:
        self._rapid_until = now + self._timing.rapid_window
        if (
            self._owner is None
            or not self._endpoint_ready
        ):
            return
        next_rapid_poll = now + min(
            self._timing.rapid_poll,
            self._timing.rapid_window,
        )
        self._next_idle = min(self._next_idle, next_rapid_poll)

    def _idle_interval(self, now: float) -> float:
        if not self._available:
            return self._timing.unavailable_poll
        if self._rapid_until is None:
            return self._timing.available_poll
        remaining = self._rapid_until - now
        if remaining <= 0:
            self._rapid_until = None
            return self._timing.available_poll
        return min(self._timing.rapid_poll, remaining)

    def _next_deadline(self, now: float) -> float:
        deadlines = []
        if (
            self._owner is not None
            and self._endpoint_ready
            and self._query_task is None
        ):
            deadlines.append(self._next_idle)
        if self._pending:
            deadlines.append(
                min(
                    pending.observed_at + self._timing.query_timeout
                    for pending in self._pending.values()
                )
            )
        finite = [
            deadline
            for deadline in deadlines
            if deadline != float("inf")
        ]
        if not finite:
            return now + max(0.05, self._timing.available_poll)
        return min(finite)

    def _log_warning(self, message: str) -> None:
        warning = getattr(self._logger, "warning", None)
        if callable(warning):
            warning(message)
