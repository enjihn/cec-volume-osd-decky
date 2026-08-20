from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import cec_monitor
from cec_monitor import (
    AUDIO_ADDRESS_PROPERTY,
    CECD_DEVICE_INTERFACE,
    CECD_SERVICE,
    DBUS_INTERFACE,
    DBUS_PATH,
    DBUS_PROPERTIES_INTERFACE,
    DBUS_SERVICE,
    REPORT_AUDIO_STATUS,
    BaselineGate,
    CecProtocolError,
    CecdVolumeMonitor,
    ConfirmedSnapshot,
    DbusNextCecdTransport,
    MonitorTiming,
    Observation,
    _ConnectionInfo,
    _PropertyReply,
    _QueryReply,
    _TransportEvent,
    decode_audio_status,
    decode_report_audio_status,
    parse_busctl_json_line as _parse_busctl_json_line,
    parse_busctl_message as _parse_busctl_message,
)

CECD_DEVICE_PATH = "/com/steampowered/CecDaemon1/Devices/Cec7"


def _managed_objects_payload(
    *,
    active: bool = False,
    audio: object = 5,
    path: str = CECD_DEVICE_PATH,
) -> bytes:
    return json.dumps(
        {
            "type": "a{oa{sa{sv}}}",
            "data": [
                {
                    path: {
                        CECD_DEVICE_INTERFACE: {
                            "Active": {"type": "b", "data": active},
                            "PhysicalAddress": {
                                "type": "q",
                                "data": 0x1000,
                            },
                            "LogicalAddresses": {
                                "type": "ay",
                                "data": [8],
                            },
                            "AudioLogicalAddress": {
                                "type": "y",
                                "data": audio,
                            },
                        }
                    }
                }
            ],
        }
    ).encode()


def parse_busctl_message(message: dict[str, object]):
    return _parse_busctl_message(message, CECD_DEVICE_PATH)


def parse_busctl_json_line(line: bytes):
    return _parse_busctl_json_line(line, CECD_DEVICE_PATH)


def _record(
    message_type: str,
    signature: str,
    data: list[object],
    **headers: object,
) -> dict[str, object]:
    return {
        "type": message_type,
        **headers,
        "payload": {"type": signature, "data": data},
    }


class DecodeTests(unittest.TestCase):
    def test_decodes_known_and_unknown_audio_status(self) -> None:
        self.assertEqual(decode_audio_status(0, False), (0.0, False))
        self.assertEqual(decode_audio_status(100, True), (100.0, True))
        self.assertEqual(decode_audio_status(0x7F, False), (None, False))

    def test_rejects_untrusted_audio_status(self) -> None:
        invalid = [
            (True, False),
            ("17", False),
            (-1, False),
            (101, False),
            (128, False),
            (17, 0),
        ]
        for volume, mute in invalid:
            with self.subTest(volume=volume, mute=mute):
                with self.assertRaises(CecProtocolError):
                    decode_audio_status(volume, mute)

    def test_accepts_only_audio_system_report_audio_status(self) -> None:
        self.assertEqual(
            decode_report_audio_status(
                5, bytes([REPORT_AUDIO_STATUS, 0x80 | 42])
            ),
            Observation("cec-audio-system", 42.0, True),
        )
        self.assertEqual(
            decode_report_audio_status(
                5, bytes([REPORT_AUDIO_STATUS, 0x7F])
            ),
            Observation("cec-audio-system", None, False),
        )
        self.assertIsNone(
            decode_report_audio_status(
                0, bytes([REPORT_AUDIO_STATUS, 42])
            )
        )
        self.assertIsNone(decode_report_audio_status(5, b"\x7a"))
        self.assertIsNone(decode_report_audio_status(5, b"\x71\x2a"))
        self.assertIsNone(
            decode_report_audio_status(5, [REPORT_AUDIO_STATUS, 256])
        )


class TimingDefaultsTests(unittest.TestCase):
    def test_adaptive_available_poll_defaults(self) -> None:
        timing = MonitorTiming()
        self.assertEqual(timing.available_poll, 0.5)
        self.assertEqual(timing.rapid_poll, 0.2)
        self.assertEqual(timing.rapid_window, 2.0)


class BaselineGateTests(unittest.TestCase):
    def test_baselines_then_publishes_only_real_changes(self) -> None:
        gate = BaselineGate()
        baseline, changed, publish = gate.observe(
            Observation("cec-audio-system", 14.0, False)
        )
        self.assertEqual(
            baseline,
            ConfirmedSnapshot("cec-audio-system", 14.0, False),
        )
        self.assertFalse(changed)
        self.assertTrue(publish)

        duplicate, changed, publish = gate.observe(
            Observation("cec-audio-system", 14.0, False)
        )
        self.assertEqual(duplicate, baseline)
        self.assertFalse(changed)
        self.assertFalse(publish)

        update, changed, publish = gate.observe(
            Observation("cec-audio-system", 16.0, False)
        )
        self.assertEqual(
            update,
            ConfirmedSnapshot("cec-audio-system", 16.0, False),
        )
        self.assertTrue(changed)
        self.assertTrue(publish)

    def test_unknown_never_merges_with_old_number(self) -> None:
        gate = BaselineGate()
        gate.observe(Observation("cec-audio-system", 14.0, False))
        snapshot, changed, publish = gate.observe(
            Observation("cec-audio-system", None, True)
        )
        self.assertIsNone(snapshot.volume)
        self.assertTrue(snapshot.muted)
        self.assertTrue(changed)
        self.assertTrue(publish)


class BusctlParserTests(unittest.TestCase):
    def _device_call(
        self, member: str, target: int, cookie: int = 7
    ) -> dict[str, object]:
        return _record(
            "method_call",
            "y",
            [target],
            sender=":1.20",
            destination=CECD_SERVICE,
            path=CECD_DEVICE_PATH,
            interface=CECD_DEVICE_INTERFACE,
            member=member,
            cookie=cookie,
        )

    def test_parses_only_valid_control_and_status_calls(self) -> None:
        for member in ("VolumeUp", "VolumeDown", "Mute"):
            event = parse_busctl_message(self._device_call(member, 255))
            self.assertEqual(event.kind, "command")
            self.assertEqual(event.body[-1], 255)

        event = parse_busctl_message(
            self._device_call("GetAudioStatus", 5)
        )
        self.assertEqual(
            event,
            _TransportEvent(
                "status_call", (":1.20", 7, CECD_SERVICE, 5)
            ),
        )

        invalid = [
            self._device_call("VolumeUp", 4),
            self._device_call("Poll", 5),
            {
                **self._device_call("VolumeUp", 5),
                "payload": {"type": "u", "data": [5]},
            },
            {
                **self._device_call("VolumeUp", 5),
                "destination": "other.service",
            },
        ]
        for record in invalid:
            self.assertIsNone(parse_busctl_message(record))

    def test_parses_correlatable_method_returns(self) -> None:
        event = parse_busctl_message(
            _record(
                "method_return",
                "yb",
                [17, False],
                sender=":1.9",
                destination=":1.20",
                cookie=90,
                reply_cookie=7,
            )
        )
        self.assertEqual(
            event,
            _TransportEvent(
                "method_return",
                (":1.9", ":1.20", 7, "yb", (17, False)),
            ),
        )
        self.assertIsNone(
            parse_busctl_message(
                _record(
                    "method_return",
                    "yb",
                    [17, False],
                    sender="not-unique",
                    destination=":1.20",
                    reply_cookie=7,
                )
            )
        )
        error = parse_busctl_message(
            _record(
                "error",
                "s",
                ["CEC transaction failed"],
                sender=":1.9",
                destination=":1.20",
                reply_cookie=7,
                error_name="com.steampowered.CecDaemon1.Error.Failed",
            )
        )
        self.assertEqual(
            error,
            _TransportEvent(
                "method_error",
                (
                    ":1.9",
                    ":1.20",
                    7,
                    "com.steampowered.CecDaemon1.Error.Failed",
                ),
            ),
        )

    def test_parses_authenticated_property_traffic(self) -> None:
        call = _record(
            "method_call",
            "ss",
            [CECD_DEVICE_INTERFACE, AUDIO_ADDRESS_PROPERTY],
            sender=":1.20",
            destination=CECD_SERVICE,
            path=CECD_DEVICE_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="Get",
            cookie=12,
        )
        self.assertEqual(
            parse_busctl_message(call),
            _TransportEvent(
                "property_call", (":1.20", 12, CECD_SERVICE)
            ),
        )
        signal = _record(
            "signal",
            "sa{sv}as",
            [
                CECD_DEVICE_INTERFACE,
                {AUDIO_ADDRESS_PROPERTY: {"type": "y", "data": 5}},
                [],
            ],
            sender=":1.9",
            path=CECD_DEVICE_PATH,
            interface=DBUS_PROPERTIES_INTERFACE,
            member="PropertiesChanged",
        )
        self.assertEqual(
            parse_busctl_message(signal),
            _TransportEvent("property", (":1.9", True, 5)),
        )

    def test_only_adapter_identity_changes_force_rediscovery(self) -> None:
        for property_name, signature, value in (
            ("PhysicalAddress", "q", 0x2000),
            ("LogicalAddresses", "ay", [4]),
        ):
            with self.subTest(property_name=property_name):
                signal = _record(
                    "signal",
                    "sa{sv}as",
                    [
                        CECD_DEVICE_INTERFACE,
                        {property_name: {"type": signature, "data": value}},
                        [],
                    ],
                    sender=":1.9",
                    path=CECD_DEVICE_PATH,
                    interface=DBUS_PROPERTIES_INTERFACE,
                    member="PropertiesChanged",
                )
                self.assertEqual(
                    parse_busctl_message(signal),
                    _TransportEvent("devices_changed", (":1.9",)),
                )

        other_path_audio_hint = _record(
            "signal",
            "sa{sv}as",
            [
                CECD_DEVICE_INTERFACE,
                {AUDIO_ADDRESS_PROPERTY: {"type": "y", "data": 0}},
                [],
            ],
            sender=":1.9",
            path="/com/steampowered/CecDaemon1/Devices/Cec99",
            interface=DBUS_PROPERTIES_INTERFACE,
            member="PropertiesChanged",
        )
        self.assertIsNone(parse_busctl_message(other_path_audio_hint))

    def test_advisory_active_changes_do_not_force_rediscovery(self) -> None:
        for changed, invalidated in (
            ({"Active": {"type": "b", "data": False}}, []),
            ({"Active": {"type": "b", "data": True}}, []),
            ({}, ["Active"]),
        ):
            with self.subTest(changed=changed, invalidated=invalidated):
                signal = _record(
                    "signal",
                    "sa{sv}as",
                    [CECD_DEVICE_INTERFACE, changed, invalidated],
                    sender=":1.9",
                    path=CECD_DEVICE_PATH,
                    interface=DBUS_PROPERTIES_INTERFACE,
                    member="PropertiesChanged",
                )
                self.assertIsNone(parse_busctl_message(signal))

    def test_parses_report_and_owner_transition(self) -> None:
        report = _record(
            "signal",
            "yytay",
            [5, 0, 1234, [REPORT_AUDIO_STATUS, 31]],
            sender=":1.9",
            path=CECD_DEVICE_PATH,
            interface=CECD_DEVICE_INTERFACE,
            member="ReceivedMessage",
        )
        self.assertEqual(
            parse_busctl_message(report),
            _TransportEvent(
                "report", (":1.9", 5, 0, 1234, (0x7A, 31))
            ),
        )

        owner = _record(
            "signal",
            "sss",
            [CECD_SERVICE, ":1.8", ":1.9"],
            sender=DBUS_SERVICE,
            path=DBUS_PATH,
            interface=DBUS_INTERFACE,
            member="NameOwnerChanged",
        )
        self.assertEqual(
            parse_busctl_message(owner),
            _TransportEvent("owner", (":1.8", ":1.9")),
        )

    def test_json_transport_is_line_and_size_bounded(self) -> None:
        record = self._device_call("VolumeUp", 5)
        encoded = json.dumps(record).encode() + b"\n"
        self.assertEqual(parse_busctl_json_line(encoded).kind, "command")
        with self.assertRaises(CecProtocolError):
            parse_busctl_json_line(encoded[:-1])
        with self.assertRaises(CecProtocolError):
            parse_busctl_json_line(b"{" + b"x" * (64 * 1024) + b"}\n")


class _FakePipe:
    def __init__(self, data: bytes = b"") -> None:
        self.data = data

    async def read(self, size: int = -1) -> bytes:
        if not self.data:
            return b""
        if size < 0:
            size = len(self.data)
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class _HangingProcess:
    def __init__(
        self, stdout: bytes = b"", stderr: bytes = b""
    ) -> None:
        self.stdout = _FakePipe(stdout)
        self.stderr = _FakePipe(stderr)
        self.returncode = None
        self.terminated = False
        self.killed = False
        self._done = asyncio.Event()

    async def wait(self) -> int:
        await self._done.wait()
        return int(self.returncode)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()


class _MonitorPipe:
    def __init__(self, *lines: bytes) -> None:
        self.lines = list(lines)

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""


class _ExitedMonitorProcess:
    def __init__(self, *lines: bytes) -> None:
        self.stdout = _MonitorPipe(*lines)
        self.returncode = 0

    async def wait(self) -> int:
        return 0


class BusctlTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_native_subprocess_environment_removes_pyinstaller_libraries(
        self,
    ) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                "LD_LIBRARY_PATH": "/tmp/_MEIfixture",
                "LD_LIBRARY_PATH_ORIG": "/usr/local/lib",
                "LD_PRELOAD": "/tmp/injected.so",
            },
            clear=True,
        ):
            environment = (
                DbusNextCecdTransport._native_subprocess_environment()
            )
        self.assertEqual(
            environment["DBUS_SESSION_BUS_ADDRESS"],
            "unix:path=/run/user/1000/bus",
        )
        self.assertEqual(
            environment["LD_LIBRARY_PATH"], "/usr/local/lib"
        )
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", environment)
        self.assertNotIn("LD_PRELOAD", environment)

        with mock.patch.dict(
            "os.environ",
            {"LD_LIBRARY_PATH": "/tmp/_MEIfixture"},
            clear=True,
        ), mock.patch("os.getuid", return_value=1000):
            environment = DbusNextCecdTransport._native_subprocess_environment()
        self.assertNotIn("LD_LIBRARY_PATH", environment)
        self.assertEqual(environment["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertEqual(
            environment["DBUS_SESSION_BUS_ADDRESS"],
            "unix:path=/run/user/1000/bus",
        )

    async def test_reads_use_unique_owner_and_disable_activation(self) -> None:
        transport = DbusNextCecdTransport()
        transport._current_owner = ":1.9"
        transport._device_path = CECD_DEVICE_PATH
        transport._execute_busctl = mock.AsyncMock(
            side_effect=[
                (0, b'{"type":"yb","data":[17,false]}\n', b""),
                (
                    0,
                    (
                        b'{"type":"v","data":['
                        b'{"type":"y","data":5}]}\n'
                    ),
                    b"",
                ),
            ]
        )

        self.assertEqual(
            await transport.query_audio_status(5),
            _QueryReply(":1.9", 17, False),
        )
        self.assertEqual(
            await transport.get_audio_logical_address(":1.9"),
            _PropertyReply(":1.9", 5),
        )
        calls = transport._execute_busctl.await_args_list
        status_command = calls[0].args[0]
        property_command = calls[1].args[0]
        for command in (status_command, property_command):
            self.assertEqual(command[0], "/usr/bin/busctl")
            self.assertIn("--user", command)
            self.assertIn("--json=short", command)
            self.assertIn("--auto-start=no", command)
            self.assertTrue(
                any(item.startswith("--timeout=") for item in command)
            )
        self.assertEqual(status_command[6], ":1.9")
        self.assertEqual(status_command[-3:], ["GetAudioStatus", "y", "5"])
        self.assertEqual(property_command[6], ":1.9")
        self.assertEqual(
            property_command[-4:],
            [
                "Get",
                "ss",
                CECD_DEVICE_INTERFACE,
                AUDIO_ADDRESS_PROPERTY,
            ],
        )
        for target in (0, 4, 255):
            with self.assertRaises(ValueError):
                await transport.query_audio_status(target)
        self.assertEqual(transport._execute_busctl.await_count, 2)

    async def test_usable_device_discovery_accepts_inactive_adapter_and_stale_audio_hint(
        self,
    ) -> None:
        for reported in (0, 5, 12):
            with self.subTest(reported=reported):
                transport = DbusNextCecdTransport()
                transport._current_owner = ":1.9"
                transport._execute_busctl = mock.AsyncMock(
                    return_value=(
                        0,
                        _managed_objects_payload(audio=reported),
                        b"",
                    )
                )
                selection = await transport._get_cec_device(":1.9")
                self.assertEqual(selection.path, CECD_DEVICE_PATH)
                self.assertFalse(selection.reported_active)
                self.assertEqual(
                    selection.reported_audio_address, reported
                )

    async def test_owner_race_during_device_discovery_fails_closed(
        self,
    ) -> None:
        transport = DbusNextCecdTransport()
        transport._current_owner = ":1.9"

        async def owner_changes(*_args, **_kwargs):
            transport._current_owner = ":1.10"
            return (0, _managed_objects_payload(audio=0), b"")

        transport._execute_busctl = owner_changes
        with self.assertRaisesRegex(CecProtocolError, "owner changed"):
            await transport._get_cec_device(":1.9")

    async def test_get_name_owner_uses_strict_live_schema(self) -> None:
        transport = DbusNextCecdTransport()
        transport._execute_busctl = mock.AsyncMock(
            return_value=(
                0,
                b'{"type":"s","data":[":1.9"]}\n',
                b"",
            )
        )
        self.assertEqual(await transport._get_current_owner(), ":1.9")
        command = transport._execute_busctl.await_args.args[0]
        self.assertEqual(command[6], DBUS_SERVICE)
        self.assertEqual(
            command[-3:], ["GetNameOwner", "s", CECD_SERVICE]
        )

        malformed = [
            b'{"type":"s","data":":1.9"}\n',
            b'{"type":"s","data":["not-unique"]}\n',
            b'{"type":"s","data":[":1.9"],"extra":true}\n',
        ]
        for stdout in malformed:
            transport._execute_busctl = mock.AsyncMock(
                return_value=(0, stdout, b"")
            )
            with self.assertRaises(CecProtocolError):
                await transport._get_current_owner()

    async def test_nonzero_exit_and_no_owner_are_distinct(self) -> None:
        transport = DbusNextCecdTransport()
        transport._execute_busctl = mock.AsyncMock(
            return_value=(
                1,
                b"",
                (
                    b'Call failed: Name "'
                    + CECD_SERVICE.encode()
                    + b'" does not exist'
                ),
            )
        )
        self.assertIsNone(await transport._get_current_owner())

        transport._execute_busctl = mock.AsyncMock(
            return_value=(1, b"", b"Access denied")
        )
        with self.assertRaises(RuntimeError):
            await transport._get_current_owner()

    async def test_rejects_owner_change_during_each_read(self) -> None:
        transport = DbusNextCecdTransport()
        transport._current_owner = ":1.9"
        transport._device_path = CECD_DEVICE_PATH

        async def changed_status(*_args, **_kwargs):
            transport._current_owner = ":1.10"
            return (0, b'{"type":"yb","data":[17,false]}\n', b"")

        transport._execute_busctl = changed_status
        with self.assertRaisesRegex(CecProtocolError, "owner changed"):
            await transport.query_audio_status(5)

        transport._current_owner = ":1.9"

        async def changed_property(*_args, **_kwargs):
            transport._current_owner = ":1.10"
            return (
                0,
                (
                    b'{"type":"v","data":['
                    b'{"type":"y","data":5}]}\n'
                ),
                b"",
            )

        transport._execute_busctl = changed_property
        with self.assertRaisesRegex(CecProtocolError, "owner changed"):
            await transport.get_audio_logical_address(":1.9")

    async def test_property_requires_exact_properties_get_variant(
        self,
    ) -> None:
        malformed = [
            b'{"type":"y","data":5}\n',
            b'{"type":"v","data":[{"type":"y","data":5,"extra":0}]}\n',
            b'{"type":"v","data":[{"type":"u","data":5}]}\n',
        ]
        for stdout in malformed:
            with self.subTest(stdout=stdout):
                transport = DbusNextCecdTransport()
                transport._current_owner = ":1.9"
                transport._device_path = CECD_DEVICE_PATH
                transport._execute_busctl = mock.AsyncMock(
                    return_value=(0, stdout, b"")
                )
                with self.assertRaises(CecProtocolError):
                    await transport.get_audio_logical_address(":1.9")

    async def test_initial_property_read_is_timeout_bounded(self) -> None:
        transport = DbusNextCecdTransport()

        async def never_returns(_owner: str) -> _PropertyReply:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        transport.get_audio_logical_address = never_returns
        with mock.patch.object(
            cec_monitor, "QUERY_TIMEOUT_SECONDS", 0.005
        ):
            with self.assertRaises(asyncio.TimeoutError):
                await transport._get_initial_audio_address(":1.9")

    async def test_subprocess_timeout_and_cancellation_terminate_child(
        self,
    ) -> None:
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                transport = DbusNextCecdTransport()
                process = _HangingProcess()
                with mock.patch(
                    "asyncio.create_subprocess_exec",
                    new=mock.AsyncMock(return_value=process),
                ):
                    task = asyncio.create_task(
                        transport._execute_busctl(
                            ["/usr/bin/busctl", "--user"],
                            timeout=0.01 if not cancel else 1.0,
                        )
                    )
                    if cancel:
                        await asyncio.sleep(0)
                        task.cancel()
                    with self.assertRaises(
                        asyncio.CancelledError
                        if cancel
                        else asyncio.TimeoutError
                    ):
                        await task
                self.assertTrue(process.terminated)
                self.assertFalse(transport._call_processes)

    async def test_subprocess_output_is_hard_bounded(self) -> None:
        transport = DbusNextCecdTransport()
        process = _HangingProcess(
            stdout=b"x" * (cec_monitor.MAX_CALL_STDOUT_BYTES + 1)
        )
        with mock.patch(
            "asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=process),
        ):
            with self.assertRaisesRegex(
                CecProtocolError, "stdout exceeded"
            ):
                await transport._execute_busctl(
                    ["/usr/bin/busctl", "--user"], timeout=1.0
                )
        self.assertTrue(process.terminated)

    async def test_owner_is_updated_before_event_is_forwarded(self) -> None:
        owner_record = _record(
            "signal",
            "sss",
            [CECD_SERVICE, ":1.8", ":1.9"],
            sender=DBUS_SERVICE,
            path=DBUS_PATH,
            interface=DBUS_INTERFACE,
            member="NameOwnerChanged",
        )
        transport = DbusNextCecdTransport()
        transport._monitor_process = _ExitedMonitorProcess(
            json.dumps(owner_record).encode() + b"\n"
        )
        seen = []

        def callback(event: _TransportEvent) -> None:
            seen.append((transport._current_owner, event))

        with self.assertRaisesRegex(RuntimeError, "rediscovering"):
            await transport._read_monitor(callback)
        self.assertEqual(seen[0][0], ":1.9")
        self.assertEqual(seen[0][1].kind, "owner")


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str) -> None:
        self.messages.append(message)


class _FakeTransport:
    def __init__(
        self,
        replies: Optional[list[object]] = None,
        owner: str = ":1.9",
        local_name: str = ":1.50",
        audio_address: Optional[int] = 5,
    ) -> None:
        self.replies = list(replies or [])
        self.current: tuple[object, object] = (14, False)
        self.owner_name = owner
        self.local_name = local_name
        self.audio_address = audio_address
        self.query_count = 0
        self.query_addresses: list[int] = []
        self.query_started_at: list[float] = []
        self.query_gate: Optional[asyncio.Event] = None
        self.property_gate: Optional[asyncio.Event] = None
        self.property_read_count = 0
        self.closed = False
        self.callback = None
        self.disconnected = asyncio.Event()

    async def connect(self, callback) -> _ConnectionInfo:
        self.callback = callback
        return _ConnectionInfo(
            self.owner_name,
            self.local_name,
            self.audio_address,
            CECD_DEVICE_PATH,
        )

    async def query_audio_status(self, address: int) -> _QueryReply:
        self.query_count += 1
        self.query_addresses.append(address)
        self.query_started_at.append(asyncio.get_running_loop().time())
        gate = self.query_gate
        if gate is not None:
            await gate.wait()
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            self.current = reply
        return _QueryReply(
            self.owner_name, self.current[0], self.current[1]
        )

    async def get_audio_logical_address(
        self, owner: str
    ) -> _PropertyReply:
        self.property_read_count += 1
        gate = self.property_gate
        if gate is not None:
            await gate.wait()
        return _PropertyReply(owner, self.audio_address)

    async def wait_disconnected(self) -> None:
        await self.disconnected.wait()

    async def close(self) -> None:
        self.closed = True
        self.disconnected.set()

    def emit(self, kind: str, *body: object) -> None:
        assert self.callback is not None
        self.callback(_TransportEvent(kind, tuple(body)))

    def command(
        self,
        cookie: int,
        target: int = 5,
        member: str = "VolumeUp",
        sender: str = ":1.20",
    ) -> None:
        self.emit(
            "command",
            sender,
            cookie,
            CECD_SERVICE,
            member,
            target,
        )

    def status(
        self,
        cookie: int,
        volume: int,
        mute: bool = False,
        target: int = 5,
        caller: str = ":1.20",
        owner: Optional[str] = None,
    ) -> None:
        reply_owner = owner or self.owner_name
        if reply_owner == self.owner_name:
            self.current = (volume, mute)
        self.emit(
            "status_call", caller, cookie, CECD_SERVICE, target
        )
        self.emit(
            "method_return",
            reply_owner,
            caller,
            cookie,
            "yb",
            (volume, mute),
        )

    def property(
        self,
        address: int,
        cookie: int = 80,
        caller: str = ":1.20",
        owner: Optional[str] = None,
    ) -> None:
        self.emit(
            "property_call", caller, cookie, CECD_SERVICE
        )
        self.emit(
            "method_return",
            owner or self.owner_name,
            caller,
            cookie,
            "v",
            ({"type": "y", "data": address},),
        )

    def report(
        self,
        volume: int,
        mute: bool = False,
        owner: Optional[str] = None,
    ) -> None:
        report_owner = owner or self.owner_name
        if report_owner == self.owner_name:
            self.current = (volume, mute)
        status = volume | (0x80 if mute else 0)
        self.emit(
            "report",
            report_owner,
            5,
            0,
            1,
            (REPORT_AUDIO_STATUS, status),
        )


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise asyncio.TimeoutError("condition was not reached")
        await asyncio.sleep(0.002)


class MonitorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.timing = MonitorTiming(
            available_poll=0.05,
            unavailable_poll=0.07,
            rapid_poll=0.01,
            rapid_window=0.06,
            command_initial_delay=0.002,
            command_probe=0.012,
            command_settle=0.025,
            query_timeout=0.04,
            initial_backoff=0.003,
            max_backoff=0.01,
        )

    async def _start(
        self,
        transport: _FakeTransport,
        on_snapshot=None,
        on_change=None,
        on_unavailable=None,
        on_activity=None,
        logger: Optional[_Logger] = None,
    ):
        snapshots = []
        changes = []
        unavailable = []
        monitor = CecdVolumeMonitor(
            on_snapshot or snapshots.append,
            on_change or changes.append,
            on_unavailable or (lambda: unavailable.append(True)),
            logger or _Logger(),
            transport_factory=lambda: transport,
            timing=self.timing,
            on_activity=on_activity,
        )
        task = asyncio.create_task(monitor.run())
        await _wait_until(lambda: transport.query_count >= 1)
        await asyncio.sleep(0.004)
        return monitor, task, snapshots, changes, unavailable

    async def _stop(self, monitor, task) -> None:
        monitor.stop()
        await asyncio.wait_for(task, timeout=0.5)

    async def test_async_report_updates_immediately_from_current_owner(
        self,
    ) -> None:
        transport = _FakeTransport([(14, False)])
        monitor, task, snapshots, changes, _ = await self._start(transport)
        try:
            transport.report(16)
            await _wait_until(lambda: len(changes) == 1)
            self.assertEqual([item.volume for item in snapshots], [14.0, 16.0])
            self.assertEqual(changes[0].volume, 16.0)

            transport.report(18, owner=":1.777")
            await asyncio.sleep(0.015)
            self.assertEqual(len(changes), 1)
        finally:
            await self._stop(monitor, task)

    async def test_confirmed_change_enters_extends_and_exits_rapid_polling(
        self,
    ) -> None:
        snapshots = []
        changes = []
        monitor = CecdVolumeMonitor(
            snapshots.append,
            changes.append,
            lambda: None,
            _Logger(),
        )
        monitor._owner = ":1.9"
        monitor._endpoint_ready = True
        monitor._property_authenticated = True

        await monitor._accept_observation(
            Observation("cec-audio-system", 14.0, False),
            100.0,
        )
        self.assertEqual([item.volume for item in snapshots], [14.0])
        self.assertEqual(changes, [])
        self.assertIsNone(monitor._rapid_until)
        self.assertEqual(monitor._next_idle, 100.5)

        await monitor._accept_observation(
            Observation("cec-audio-system", 15.0, False),
            100.1,
        )
        self.assertEqual([item.volume for item in changes], [15.0])
        self.assertEqual(monitor._rapid_until, 102.1)
        self.assertAlmostEqual(monitor._next_idle, 100.3)

        await monitor._accept_observation(
            Observation("cec-audio-system", 15.0, False),
            101.0,
        )
        self.assertEqual([item.volume for item in changes], [15.0])
        self.assertEqual(monitor._rapid_until, 102.1)

        await monitor._accept_observation(
            Observation("cec-audio-system", 16.0, False),
            101.9,
        )
        self.assertEqual([item.volume for item in changes], [15.0, 16.0])
        self.assertEqual(monitor._rapid_until, 103.9)
        self.assertAlmostEqual(monitor._idle_interval(103.8), 0.1)
        self.assertEqual(monitor._idle_interval(103.9), 0.5)
        self.assertIsNone(monitor._rapid_until)

    async def test_poll_change_runs_rapid_then_returns_to_steady_cadence(
        self,
    ) -> None:
        transport = _FakeTransport([(14, False), (15, False)])
        monitor, task, snapshots, changes, _ = await self._start(transport)
        try:
            self.assertEqual(changes, [])
            await _wait_until(lambda: len(changes) == 1)
            self.assertEqual(changes[0].volume, 15.0)
            changed_query_count = transport.query_count

            await _wait_until(
                lambda: transport.query_count > changed_query_count
            )
            rapid_gap = (
                transport.query_started_at[changed_query_count]
                - transport.query_started_at[changed_query_count - 1]
            )
            self.assertLess(rapid_gap, self.timing.available_poll)

            await _wait_until(
                lambda: (
                    monitor._rapid_until is None
                    and transport.query_count > changed_query_count + 1
                )
            )
            expired_query_count = transport.query_count
            await _wait_until(
                lambda: transport.query_count > expired_query_count
            )
            steady_gap = (
                transport.query_started_at[expired_query_count]
                - transport.query_started_at[expired_query_count - 1]
            )
            self.assertGreaterEqual(
                steady_gap,
                self.timing.available_poll * 0.75,
            )
            self.assertEqual([item.volume for item in snapshots], [14.0, 15.0])
            self.assertEqual([item.volume for item in changes], [15.0])
        finally:
            await self._stop(monitor, task)

    async def test_unrelated_frames_do_not_postpone_poll(
        self,
    ) -> None:
        monitor = CecdVolumeMonitor(
            lambda _snapshot: None,
            lambda _snapshot: None,
            lambda: None,
            _Logger(),
            timing=self.timing,
        )
        monitor._owner = ":1.9"
        monitor._endpoint_ready = True
        monitor._property_authenticated = True
        monitor._available = True
        loop = asyncio.get_running_loop()
        original_deadline = loop.time() + 1.0
        monitor._next_idle = original_deadline

        await monitor._reduce_event(
            _TransportEvent(
                "report",
                (":1.9", 0, 15, 1, (0x87, 0x00)),
            ),
            _FakeTransport(),
            1,
        )
        self.assertEqual(monitor._next_idle, original_deadline)
        self.assertIsNone(monitor._rapid_until)

    async def test_post_auth_baseline_uses_short_bootstrap_delay(self) -> None:
        transport = _FakeTransport([(14, False)])
        started = asyncio.get_running_loop().time()
        monitor, task, _, changes, _ = await self._start(transport)
        try:
            elapsed = asyncio.get_running_loop().time() - started
            self.assertLess(elapsed, self.timing.available_poll / 2)
            transport.status(29, 15, target=255)
            await _wait_until(lambda: len(changes) == 1)
            self.assertEqual(changes[0].volume, 15.0)
        finally:
            await self._stop(monitor, task)

    async def test_correlated_status_requires_owner_and_matching_cookie(
        self,
    ) -> None:
        transport = _FakeTransport([(14, False)])
        monitor, task, _, changes, _ = await self._start(transport)
        try:
            transport.emit(
                "status_call", ":1.20", 31, CECD_SERVICE, 5
            )
            transport.emit(
                "method_return",
                ":1.777",
                ":1.20",
                31,
                "yb",
                (20, False),
            )
            transport.emit(
                "method_return",
                ":1.9",
                ":1.20",
                32,
                "yb",
                (21, False),
            )
            await asyncio.sleep(0.015)
            self.assertEqual(changes, [])

            transport.status(33, 22)
            await _wait_until(lambda: len(changes) == 1)
            self.assertEqual(changes[0].volume, 22.0)
        finally:
            await self._stop(monitor, task)

    async def test_correlated_status_error_fails_closed(self) -> None:
        transport = _FakeTransport([(14, False)])
        monitor, task, _, _, unavailable = await self._start(transport)
        try:
            transport.emit(
                "status_call", ":1.20", 34, CECD_SERVICE, 5
            )
            transport.emit(
                "method_error",
                ":1.9",
                ":1.20",
                34,
                "com.steampowered.CecDaemon1.Error.Failed",
            )
            await _wait_until(lambda: len(unavailable) == 1)
            self.assertFalse(monitor._available)
        finally:
            await self._stop(monitor, task)

    async def test_audio_hint_error_does_not_revoke_direct_status(
        self,
    ) -> None:
        transport = _FakeTransport([(14, False)])
        monitor, task, snapshots, changes, unavailable = await self._start(
            transport
        )
        try:
            transport.emit(
                "property_call", ":1.20", 36, CECD_SERVICE
            )
            transport.emit(
                "method_error",
                ":1.9",
                ":1.20",
                36,
                "com.steampowered.CecDaemon1.Error.Failed",
            )
            await _wait_until(lambda: transport.property_read_count == 1)
            transport.current = (19, False)
            await _wait_until(lambda: transport.query_count >= 2)
            await _wait_until(lambda: snapshots[-1].volume == 19.0)
            self.assertFalse(unavailable)
            self.assertEqual([item.volume for item in changes], [19.0])
            self.assertTrue(monitor._endpoint_ready)
            self.assertTrue(all(item == 5 for item in transport.query_addresses))
        finally:
            await self._stop(monitor, task)

    async def test_malformed_correlated_status_fails_closed(self) -> None:
        transport = _FakeTransport([(14, False)])
        monitor, task, _, _, unavailable = await self._start(transport)
        try:
            transport.emit(
                "status_call", ":1.20", 35, CECD_SERVICE, 5
            )
            transport.emit(
                "method_return",
                ":1.9",
                ":1.20",
                35,
                "yb",
                (126, False),
            )
            await _wait_until(lambda: len(unavailable) == 1)
            self.assertFalse(monitor._available)
        finally:
            await self._stop(monitor, task)

    async def test_stale_audio_hint_keeps_direct_target_5_polling(
        self,
    ) -> None:
        transport = _FakeTransport([(14, False)])
        monitor, task, _, changes, _ = await self._start(transport)
        try:
            transport.status(40, 15, target=255)
            await _wait_until(lambda: len(changes) == 1)
            self.assertEqual(changes[0].volume, 15.0)

            transport.property(0, cookie=81)
            await _wait_until(lambda: monitor._audio_address == 0)
            self.assertTrue(monitor._available)
            transport.status(42, 17, target=255)
            await _wait_until(
                lambda: bool(changes) and changes[-1].volume == 17.0
            )
            self.assertEqual(transport.query_addresses, [5, 5])
            self.assertTrue(monitor._available)
        finally:
            await self._stop(monitor, task)

    async def test_each_reported_audio_hint_bootstraps_direct_target_5(
        self,
    ) -> None:
        for reported in (0, 5, 12):
            with self.subTest(reported=reported):
                transport = _FakeTransport(
                    [(14, False)], audio_address=reported
                )
                monitor, task, snapshots, _, _ = await self._start(
                    transport
                )
                try:
                    self.assertEqual(transport.query_addresses, [5])
                    self.assertEqual(snapshots[-1].volume, 14.0)
                    self.assertEqual(monitor._audio_address, reported)
                    self.assertTrue(monitor._endpoint_ready)
                finally:
                    await self._stop(monitor, task)

    async def test_initial_audio_hint_failure_keeps_direct_target_5(
        self,
    ) -> None:
        class FailingPropertyTransport(_FakeTransport):
            async def get_audio_logical_address(
                self, owner: str
            ) -> _PropertyReply:
                self.property_read_count += 1
                raise RuntimeError(f"property unavailable from {owner}")

        transport = FailingPropertyTransport(
            [(14, False)], audio_address=None
        )
        logger = _Logger()
        monitor, task, snapshots, changes, unavailable = await self._start(
            transport, logger=logger
        )
        try:
            await _wait_until(lambda: transport.property_read_count == 1)
            self.assertEqual([item.volume for item in snapshots], [14.0])
            self.assertEqual(changes, [])
            self.assertEqual(unavailable, [])
            self.assertTrue(monitor._available)
            self.assertTrue(monitor._endpoint_ready)
            self.assertEqual(transport.query_addresses, [5])
            self.assertTrue(
                any(
                    "continuing direct GetAudioStatus(5)" in message
                    for message in logger.messages
                )
            )
        finally:
            await self._stop(monitor, task)

    async def test_owner_change_requires_full_endpoint_rediscovery(
        self,
    ) -> None:
        monitor = CecdVolumeMonitor(
            lambda _snapshot: None,
            lambda _snapshot: None,
            lambda: None,
            _Logger(),
            timing=self.timing,
        )
        monitor._owner = ":1.9"
        monitor._device_path = CECD_DEVICE_PATH
        monitor._endpoint_ready = True
        with self.assertRaisesRegex(RuntimeError, "rediscovering"):
            await monitor._reduce_event(
                _TransportEvent("owner", (":1.9", ":1.10")),
                _FakeTransport(),
                1,
            )
        self.assertEqual(monitor._owner, ":1.9")
        self.assertEqual(monitor._device_path, CECD_DEVICE_PATH)

    async def test_only_current_owner_topology_forces_rediscovery(
        self,
    ) -> None:
        monitor = CecdVolumeMonitor(
            lambda _snapshot: None,
            lambda _snapshot: None,
            lambda: None,
            _Logger(),
            timing=self.timing,
        )
        monitor._owner = ":1.9"
        monitor._device_path = CECD_DEVICE_PATH
        monitor._endpoint_ready = True
        transport = _FakeTransport()

        await monitor._reduce_event(
            _TransportEvent("devices_changed", (":1.777",)),
            transport,
            1,
        )
        with self.assertRaisesRegex(RuntimeError, "topology changed"):
            await monitor._reduce_event(
                _TransportEvent("devices_changed", (":1.9",)),
                transport,
                1,
            )

    async def test_local_property_call_cannot_stale_reauth_read(self) -> None:
        transport = _FakeTransport([(14, False)])
        monitor, task, snapshots, changes, unavailable = await self._start(
            transport
        )
        try:
            gate = asyncio.Event()
            transport.property_gate = gate
            transport.emit("property", ":1.9", False, None)
            await _wait_until(
                lambda: (
                    transport.property_read_count == 1
                    and not monitor._property_authenticated
                )
            )

            # busctl can observe the observer's own Properties.Get. It must
            # not enter the passive pending table or advance property state.
            transport.emit(
                "property_call", ":1.50", 201, ":1.9"
            )
            transport.emit(
                "method_return",
                ":1.9",
                ":1.50",
                201,
                "v",
                ({"type": "y", "data": 5},),
            )
            transport.current = (22, False)
            gate.set()

            await _wait_until(
                lambda: monitor._property_authenticated
            )
            await _wait_until(lambda: transport.query_count == 2)
            await _wait_until(lambda: snapshots[-1].volume == 22.0)
            self.assertFalse(unavailable)
            self.assertEqual([item.volume for item in changes], [22.0])
        finally:
            await self._stop(monitor, task)

    async def test_continuous_command_and_status_activity_makes_no_queries(
        self,
    ) -> None:
        transport = _FakeTransport([(14, False)])
        monitor, task, _, _, _ = await self._start(transport)
        try:
            self.assertEqual(transport.query_count, 1)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timing.available_poll * 2.5
            cookie = 50
            while loop.time() < deadline:
                transport.command(cookie)
                transport.status(cookie + 100, 14)
                cookie += 1
                await asyncio.sleep(self.timing.available_poll / 5)
            self.assertEqual(transport.query_count, 1)
            self.assertEqual(transport.query_addresses, [5])
            await _wait_until(lambda: transport.query_count == 2)
        finally:
            await self._stop(monitor, task)

    async def test_trusted_command_publishes_keepalive_activity(self) -> None:
        transport = _FakeTransport([(14, False)])
        activity = []
        monitor, task, _, _, _ = await self._start(
            transport,
            on_activity=lambda: activity.append(True),
        )
        try:
            transport.command(71)
            await _wait_until(lambda: len(activity) == 1)
            transport.emit(
                "command",
                ":1.20",
                72,
                "com.example.Untrusted",
                "VolumeUp",
                5,
            )
            await asyncio.sleep(0.01)
            self.assertEqual(activity, [True])
        finally:
            await self._stop(monitor, task)

    async def test_authenticated_duplicate_report_keeps_osd_alive(self) -> None:
        transport = _FakeTransport([(14, False)])
        activity = []
        monitor, task, _, _, _ = await self._start(
            transport,
            on_activity=lambda: activity.append(True),
        )
        try:
            transport.report(14)
            await _wait_until(lambda: len(activity) == 1)
            transport.report(14, owner=":1.777")
            await asyncio.sleep(0.01)
            self.assertEqual(activity, [True])
        finally:
            await self._stop(monitor, task)

    async def test_newer_passive_state_rejects_inflight_idle_result(
        self,
    ) -> None:
        transport = _FakeTransport([(14, False)])
        monitor, task, snapshots, changes, _ = await self._start(transport)
        try:
            gate = asyncio.Event()
            transport.query_gate = gate
            await _wait_until(lambda: transport.query_count == 2)
            transport.report(20)
            await _wait_until(
                lambda: bool(changes) and changes[-1].volume == 20.0
            )
            self.assertIsNotNone(monitor._rapid_until)
            self.assertNotEqual(monitor._next_idle, float("inf"))
            await asyncio.sleep(self.timing.rapid_poll * 2)
            self.assertEqual(transport.query_count, 2)
            transport.current = (20, False)
            gate.set()
            await _wait_until(lambda: transport.query_count >= 3)
            self.assertEqual(snapshots[-1].volume, 20.0)
            self.assertEqual([item.volume for item in changes], [20.0])
        finally:
            await self._stop(monitor, task)

    async def test_idle_fallback_is_sparse_and_unchanged_is_silent(
        self,
    ) -> None:
        transport = _FakeTransport([(14, False)])
        monitor, task, snapshots, changes, _ = await self._start(transport)
        try:
            await _wait_until(lambda: transport.query_count >= 2)
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(changes, [])
            self.assertEqual(transport.query_addresses, [5, 5])
        finally:
            await self._stop(monitor, task)

    async def test_explicit_unknown_fails_closed_and_rebaselines(
        self,
    ) -> None:
        transport = _FakeTransport([(14, False)])
        monitor, task, snapshots, changes, unavailable = await self._start(
            transport
        )
        try:
            transport.report(15)
            await _wait_until(lambda: len(changes) == 1)
            self.assertIsNotNone(monitor._rapid_until)
            transport.status(90, 0x7F)
            await _wait_until(lambda: len(unavailable) == 1)
            self.assertEqual([item.volume for item in changes], [15.0])
            self.assertFalse(monitor._available)
            self.assertIsNone(monitor._rapid_until)
            unavailable_query_count = transport.query_count
            remaining = (
                monitor._next_idle - asyncio.get_running_loop().time()
            )
            self.assertGreater(
                remaining,
                self.timing.rapid_poll * 2,
            )
            await asyncio.sleep(self.timing.rapid_poll * 2)
            self.assertEqual(transport.query_count, unavailable_query_count)

            transport.status(91, 20)
            await _wait_until(lambda: snapshots[-1].volume == 20.0)
            self.assertEqual([item.volume for item in changes], [15.0])
            self.assertNotIn(14.0, [item.volume for item in snapshots[2:]])
        finally:
            await self._stop(monitor, task)

    async def test_callback_failures_are_logged_and_isolated(self) -> None:
        transport = _FakeTransport([(14, False)])
        logger = _Logger()
        changes = []
        calls = 0

        async def broken_snapshot(_snapshot) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("frontend unavailable")

        monitor, task, _, _, _ = await self._start(
            transport,
            on_snapshot=broken_snapshot,
            on_change=changes.append,
            logger=logger,
        )
        try:
            transport.report(15)
            await _wait_until(lambda: len(changes) == 1)
            self.assertEqual(changes[0].volume, 15.0)
            self.assertTrue(
                any("snapshot callback failed" in item for item in logger.messages)
            )
        finally:
            await self._stop(monitor, task)

    async def test_snapshot_callback_precedes_change_callback(self) -> None:
        transport = _FakeTransport([(14, False)])
        order = []

        async def snapshot(value) -> None:
            order.append(("snapshot", value.volume))

        async def change(value) -> None:
            order.append(("change", value.volume))

        monitor, task, _, _, _ = await self._start(
            transport, on_snapshot=snapshot, on_change=change
        )
        try:
            transport.report(15)
            await _wait_until(lambda: len(order) >= 3)
            self.assertEqual(
                order,
                [
                    ("snapshot", 14.0),
                    ("snapshot", 15.0),
                    ("change", 15.0),
                ],
            )
        finally:
            await self._stop(monitor, task)

    async def test_queue_overflow_fails_closed_then_rebaselines(
        self,
    ) -> None:
        transport = _FakeTransport([(14, False)])
        entered = asyncio.Event()
        release = asyncio.Event()
        unavailable = []
        snapshots = []

        async def slow_snapshot(snapshot) -> None:
            snapshots.append(snapshot)
            if len(snapshots) == 1:
                entered.set()
                await release.wait()

        monitor = CecdVolumeMonitor(
            slow_snapshot,
            lambda _snapshot: None,
            lambda: unavailable.append(True),
            _Logger(),
            transport_factory=lambda: transport,
            timing=self.timing,
        )
        task = asyncio.create_task(monitor.run())
        try:
            await asyncio.wait_for(entered.wait(), timeout=0.5)
            for value in range(130):
                transport.report(value % 100)
            release.set()
            await _wait_until(lambda: bool(unavailable))
            await _wait_until(lambda: len(snapshots) >= 2)
            self.assertTrue(monitor._available)
        finally:
            await self._stop(monitor, task)

    async def test_reconnect_discards_old_epoch_events(self) -> None:
        first = _FakeTransport([(14, False)], owner=":1.9")
        second = _FakeTransport([(30, False)], owner=":1.10")
        transports = [first, second]
        snapshots = []
        changes = []
        unavailable = []
        monitor = CecdVolumeMonitor(
            snapshots.append,
            changes.append,
            lambda: unavailable.append(True),
            _Logger(),
            transport_factory=lambda: transports.pop(0),
            timing=self.timing,
        )
        task = asyncio.create_task(monitor.run())
        try:
            await _wait_until(
                lambda: snapshots and snapshots[-1].volume == 14.0
            )
            first.report(15)
            await _wait_until(lambda: len(changes) == 1)
            self.assertIsNotNone(monitor._rapid_until)
            old_callback = first.callback
            first.disconnected.set()
            await _wait_until(
                lambda: snapshots and snapshots[-1].volume == 30.0
            )
            self.assertIsNone(monitor._rapid_until)
            assert old_callback is not None
            old_callback(
                _TransportEvent(
                    "report",
                    (
                        ":1.9",
                        5,
                        0,
                        2,
                        (REPORT_AUDIO_STATUS, 99),
                    ),
                )
            )
            await asyncio.sleep(0.02)
            self.assertEqual(snapshots[-1].volume, 30.0)
            self.assertEqual([item.volume for item in changes], [15.0])
            self.assertTrue(unavailable)
        finally:
            await self._stop(monitor, task)
