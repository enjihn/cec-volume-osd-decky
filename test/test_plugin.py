from __future__ import annotations

import asyncio
import math
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, mock_open, patch

decky_stub = types.ModuleType("decky")
decky_stub.DECKY_VERSION = "v3.2.8-pre1"
decky_stub.emit = AsyncMock()
decky_stub.logger = Mock()
sys.modules["decky"] = decky_stub

from cec_monitor import ConfirmedSnapshot  # noqa: E402
from main import (  # noqa: E402
    EVENT_ACTIVITY,
    EVENT_CHANGED,
    EVENT_STATUS,
    Plugin,
    _parse_os_release,
    _runtime_identity,
)


class CompatibilityTests(unittest.TestCase):
    def test_parses_runtime_identity_without_pinning_the_build(self) -> None:
        self.assertEqual(
            _parse_os_release(
                'NAME="SteamOS"\nBUILD_ID=20260721.1001\n'
                "VARIANT_ID=steamdeck\n"
            ),
            {
                "NAME": "SteamOS",
                "BUILD_ID": "20260721.1001",
                "VARIANT_ID": "steamdeck",
            },
        )
        with patch(
            "builtins.open",
            mock_open(
                read_data='NAME="SteamOS"\nID=steamos\nBUILD_ID=20260806.1000\n'
            ),
        ):
            self.assertEqual(
                _runtime_identity("v3.2.8-pre1"),
                {
                    "compatibility_mode": "runtime-contract",
                    "decky_loader": "v3.2.8-pre1",
                    "os_id": "steamos",
                    "os_variant": "unknown",
                    "os_build": "20260806.1000",
                },
            )


class BackendInstanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_reset_gets_a_new_opaque_instance(self) -> None:
        plugin = Plugin()

        with patch("main.decky.DECKY_VERSION", "unsupported"):
            await plugin._main()
            first = plugin._status["instance"]
            await plugin._unload()
            await plugin._main()
            second = plugin._status["instance"]
            await plugin._unload()

        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertRegex(second, r"^[0-9a-f]{32}$")
        self.assertNotEqual(first, second)
        self.assertEqual(plugin._generation, 0)
        self.assertEqual(plugin._runtime["decky_loader"], "unsupported")

    async def test_status_and_real_change_share_backend_instance(self) -> None:
        plugin = Plugin()
        plugin._instance = "backend-instance-a"
        plugin._generation = 0
        snapshot = ConfirmedSnapshot(
            route="hdmi-output-0",
            volume=16.0,
            muted=False,
        )

        with patch("main.decky.emit", new=AsyncMock()) as emit:
            await plugin._on_snapshot(snapshot)
            await plugin._on_change(snapshot)

        self.assertEqual(plugin._status["instance"], "backend-instance-a")
        self.assertEqual(plugin._status["generation"], 1)
        self.assertEqual(
            plugin._status["compatibility"]["reason_code"], "ready"
        )
        self.assertEqual(emit.await_count, 2)
        self.assertEqual(emit.await_args_list[0].args[0], EVENT_STATUS)
        self.assertEqual(
            emit.await_args_list[0].args[1]["instance"],
            "backend-instance-a",
        )
        self.assertEqual(emit.await_args_list[1].args[0], EVENT_CHANGED)
        self.assertEqual(
            emit.await_args_list[1].args[1],
            {
                "route": "hdmi-output-0",
                "volume": 16.0,
                "muted": False,
                "instance": "backend-instance-a",
                "generation": 1,
                "source": "cec",
                "diagnostic": False,
            },
        )

    async def test_unavailable_status_keeps_backend_instance(self) -> None:
        plugin = Plugin()
        plugin._instance = "backend-instance-a"
        plugin._generation = 7
        plugin._snapshot = ConfirmedSnapshot(
            route="hdmi-output-0",
            volume=16.0,
            muted=False,
        )

        with patch("main.decky.emit", new=AsyncMock()):
            await plugin._on_unavailable()

        self.assertEqual(plugin._status["instance"], "backend-instance-a")
        self.assertEqual(plugin._status["generation"], 7)
        self.assertIsNone(plugin._snapshot)
        self.assertEqual(
            plugin._status["compatibility"]["reason_code"],
            "cec_status_unavailable",
        )

    async def test_unload_allows_the_monitor_to_stop_cleanly(self) -> None:
        stopped = asyncio.Event()

        class Monitor:
            def stop(self) -> None:
                stopped.set()

        plugin = Plugin()
        plugin._monitor = Monitor()
        plugin._monitor_task = asyncio.create_task(stopped.wait())

        await plugin._unload()

        self.assertTrue(plugin._monitor_task.done())
        self.assertFalse(plugin._monitor_task.cancelled())

    async def test_activity_emits_only_with_a_confirmed_snapshot(self) -> None:
        plugin = Plugin()
        plugin._snapshot = None
        with patch("main.decky.emit", new=AsyncMock()) as emit:
            await plugin._on_activity()
            emit.assert_not_awaited()
            plugin._snapshot = ConfirmedSnapshot(
                route="cec-audio-system",
                volume=20.0,
                muted=False,
            )
            await plugin._on_activity()
            emit.assert_awaited_once_with(EVENT_ACTIVITY)


class HiddenSelfTestTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_requested_safe_diagnostic_volume(self) -> None:
        plugin = Plugin()
        plugin._generation = 9

        with patch("main.decky.emit", new=AsyncMock()) as emit:
            self.assertIs(await plugin.hidden_self_test(81.5), True)

        emit.assert_awaited_once_with(
            EVENT_CHANGED,
            {
                "route": "diagnostic",
                "volume": 81.5,
                "muted": False,
                "generation": 9,
                "source": "diagnostic",
                "diagnostic": True,
            },
        )

    async def test_uses_existing_default_diagnostic_volume(self) -> None:
        plugin = Plugin()
        plugin._generation = 3

        with patch("main.decky.emit", new=AsyncMock()) as emit:
            self.assertIs(await plugin.hidden_self_test(), True)

        self.assertEqual(emit.await_args.args[1]["volume"], 73.0)

    async def test_rejects_untrusted_diagnostic_volumes_without_emitting(
        self,
    ) -> None:
        invalid = [
            None,
            True,
            "73",
            -1,
            101,
            math.nan,
            math.inf,
            -math.inf,
        ]
        plugin = Plugin()
        plugin._generation = 1

        with patch("main.decky.emit", new=AsyncMock()) as emit:
            for volume in invalid:
                with self.subTest(volume=volume):
                    self.assertIs(await plugin.hidden_self_test(volume), False)

        emit.assert_not_awaited()
