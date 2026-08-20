"""Decky backend for the Steam-native CEC volume display."""

from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from typing import Optional

import decky

from cec_monitor import CecdVolumeMonitor, ConfirmedSnapshot
EVENT_CHANGED = "cec_volume_changed"
EVENT_ACTIVITY = "cec_volume_activity"
EVENT_STATUS = "cec_volume_status"
EVENT_UNAVAILABLE = "cec_volume_unavailable"


def _parse_os_release(contents: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _runtime_identity(decky_version: str) -> dict[str, str]:
    identity = {
        "compatibility_mode": "runtime-contract",
        "decky_loader": decky_version,
        "os_id": "unknown",
        "os_variant": "unknown",
        "os_build": "unknown",
    }
    try:
        with open("/etc/os-release", encoding="utf-8") as release_file:
            os_release = _parse_os_release(release_file.read())
        current_build = os_release.get("BUILD_ID") or os_release.get("VERSION_ID")
        identity["os_id"] = os_release.get("ID", "unknown")
        identity["os_variant"] = os_release.get("VARIANT_ID", "unknown")
        if current_build:
            identity["os_build"] = current_build
    except OSError:
        pass
    return identity


class Plugin:
    async def _main(self) -> None:
        # Generation numbers are scoped to this backend lifecycle. The Decky
        # frontend can survive a backend restart, so give every reset a fresh,
        # opaque identity rather than treating generation as process-global.
        self._instance = uuid.uuid4().hex
        self._generation = 0
        self._snapshot: Optional[ConfirmedSnapshot] = None
        self._monitor: Optional[CecdVolumeMonitor] = None
        self._monitor_task: Optional[asyncio.Task[None]] = None
        self._status: dict[str, object] = {
            "instance": self._instance,
            "available": False,
            "route": None,
            "volume": None,
            "muted": None,
            "generation": 0,
            "last_confirmed_at": None,
            "last_error": None,
            "compatibility": {
                "compatible": False,
                "reason_code": "starting",
                "message": "Waiting for Valve cecd and confirmed HDMI-CEC audio state",
            },
        }
        decky_version = str(
            getattr(
                decky,
                "DECKY_VERSION",
                os.environ.get("DECKY_VERSION", "unknown"),
            )
        )
        self._runtime = _runtime_identity(decky_version)

        self._monitor = CecdVolumeMonitor(
            on_snapshot=self._on_snapshot,
            on_change=self._on_change,
            on_unavailable=self._on_unavailable,
            logger=decky.logger,
            on_activity=self._on_activity,
        )
        self._monitor_task = asyncio.create_task(
            self._monitor.run(), name="cec-read-only-state-observer"
        )
        decky.logger.info(
            "CEC Volume OSD read-only cecd observer started; "
            "runtime contracts active; Valve ExternalVolume control "
            "remains untouched"
        )

    async def _unload(self) -> None:
        if self._monitor is not None:
            self._monitor.stop()
        if self._monitor_task is not None:
            done, pending = await asyncio.wait(
                {self._monitor_task}, timeout=1.5
            )
            if pending:
                self._monitor_task.cancel()
                done_after_cancel, pending = await asyncio.wait(
                    pending, timeout=0.5
                )
                done |= done_after_cancel
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            if pending:
                decky.logger.warning(
                    "CEC observer task did not stop within the unload bound"
                )
        decky.logger.info("CEC Volume OSD read-only cecd observer stopped")

    async def _on_snapshot(self, snapshot: ConfirmedSnapshot) -> None:
        self._generation += 1
        self._snapshot = snapshot
        self._status = {
            "instance": self._instance,
            "available": True,
            **snapshot.payload(),
            "generation": self._generation,
            "last_confirmed_at": time.time(),
            "last_error": None,
            "compatibility": {
                "compatible": True,
                "reason_code": "ready",
                "message": "Confirmed HDMI-CEC audio state is available",
            },
        }
        await decky.emit(EVENT_STATUS, dict(self._status))

    async def _on_change(self, snapshot: ConfirmedSnapshot) -> None:
        decky.logger.info(
            "CEC confirmed change generation=%s volume=%s muted=%s monotonic_ns=%s",
            self._generation,
            snapshot.volume,
            snapshot.muted,
            time.monotonic_ns(),
        )
        await decky.emit(
            EVENT_CHANGED,
            {
                **snapshot.payload(),
                "instance": self._instance,
                "generation": self._generation,
                "source": "cec",
                "diagnostic": False,
            },
        )

    async def _on_activity(self) -> None:
        if self._snapshot is not None:
            await decky.emit(EVENT_ACTIVITY)

    async def _on_unavailable(self) -> None:
        self._snapshot = None
        self._status = {
            "instance": self._instance,
            "available": False,
            "route": None,
            "volume": None,
            "muted": None,
            "generation": self._generation,
            "last_confirmed_at": None,
            "last_error": "confirmed cecd audio status unavailable",
            "compatibility": {
                "compatible": False,
                "reason_code": "cec_status_unavailable",
                "message": "Valve cecd is unavailable or did not confirm an HDMI-CEC audio status",
            },
        }
        await decky.emit(EVENT_UNAVAILABLE)

    async def get_status(self) -> dict[str, object]:
        status = dict(self._status)
        status["runtime"] = dict(
            getattr(
                self,
                "_runtime",
                _runtime_identity("unknown"),
            )
        )
        status["cec_device_path"] = (
            getattr(self, "_monitor", None).device_path
            if getattr(self, "_monitor", None) is not None
            else None
        )
        return status

    async def preview(self) -> bool:
        if self._snapshot is None:
            return False
        await decky.emit(
            EVENT_CHANGED,
            {
                **self._snapshot.payload(),
                "generation": self._generation,
                "source": "preview",
                "diagnostic": False,
            },
        )
        return True

    async def hidden_self_test(self, volume: object = 73.0) -> bool:
        """Exercise the real event/DOM path while CSS forces it invisible."""
        if isinstance(volume, bool) or not isinstance(volume, (int, float)):
            return False
        diagnostic_volume = float(volume)
        if not math.isfinite(diagnostic_volume) or not 0 <= diagnostic_volume <= 100:
            return False

        await decky.emit(
            EVENT_CHANGED,
            {
                "route": "diagnostic",
                "volume": diagnostic_volume,
                "muted": False,
                "generation": self._generation,
                "source": "diagnostic",
                "diagnostic": True,
            },
        )
        return True
