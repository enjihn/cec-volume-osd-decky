from __future__ import annotations

import json
import unittest

from cec_device import (
    CECD_DEVICE_INTERFACE,
    MAX_MANAGED_OBJECTS_BYTES,
    CecDeviceDiscoveryError,
    CecDeviceSelection,
    is_cec_device_path,
    select_unique_cec_device,
)


def _device(
    *,
    active: object,
    physical: int,
    logical: list[int],
    audio: object = 5,
) -> dict[str, object]:
    return {
        CECD_DEVICE_INTERFACE: {
            "Active": {"type": "b", "data": active},
            "PhysicalAddress": {"type": "q", "data": physical},
            "LogicalAddresses": {"type": "ay", "data": logical},
            "AudioLogicalAddress": {"type": "y", "data": audio},
        }
    }


def _reply(objects: dict[str, object]) -> bytes:
    return json.dumps(
        {"type": "a{oa{sa{sv}}}", "data": [objects]},
        separators=(",", ":"),
    ).encode()


class CecDeviceDiscoveryTests(unittest.TestCase):
    def test_selects_unique_usable_inactive_endpoint_without_pin(
        self,
    ) -> None:
        payload = _reply(
            {
                "/com/steampowered/CecDaemon1/Devices/Cec0": _device(
                    active=False,
                    physical=0xFFFF,
                    logical=[],
                    audio=5,
                ),
                "/com/steampowered/CecDaemon1/Devices/Cec27": _device(
                    active=False,
                    physical=0x2100,
                    logical=[4],
                    audio=5,
                ),
            }
        )
        self.assertEqual(
            select_unique_cec_device(payload),
            CecDeviceSelection(
                path="/com/steampowered/CecDaemon1/Devices/Cec27",
                reported_active=False,
                reported_audio_address=5,
                physical_address=0x2100,
                logical_addresses=(4,),
            ),
        )

    def test_audio_property_is_advisory_for_a_valid_playback_endpoint(
        self,
    ) -> None:
        for reported in (0, 5, 12):
            with self.subTest(reported=reported):
                path = "/com/steampowered/CecDaemon1/Devices/Cec1"
                selection = select_unique_cec_device(
                    _reply(
                        {
                            path: _device(
                                active=False,
                                physical=0x1000,
                                logical=[8],
                                audio=reported,
                            )
                        }
                    )
                )
                self.assertEqual(selection.path, path)
                self.assertFalse(selection.reported_active)
                self.assertEqual(selection.reported_audio_address, reported)

    def test_active_is_strictly_typed_but_its_value_is_advisory(
        self,
    ) -> None:
        path = "/com/steampowered/CecDaemon1/Devices/Cec1"
        for active in (False, True):
            with self.subTest(active=active):
                selection = select_unique_cec_device(
                    _reply(
                        {
                            path: _device(
                                active=active,
                                physical=0x1000,
                                logical=[8],
                            )
                        }
                    )
                )
                self.assertIs(selection.reported_active, active)

        malformed_variants: tuple[object | None, ...] = (
            None,
            {"type": "b", "data": 0},
            {"type": "u", "data": False},
            {"type": "b", "data": False, "extra": None},
        )
        for variant in malformed_variants:
            with self.subTest(variant=variant):
                device = _device(
                    active=False,
                    physical=0x1000,
                    logical=[8],
                )
                properties = device[CECD_DEVICE_INTERFACE]
                if variant is None:
                    properties.pop("Active")
                else:
                    properties["Active"] = variant
                with self.assertRaisesRegex(
                    CecDeviceDiscoveryError, "no usable"
                ):
                    select_unique_cec_device(_reply({path: device}))

    def test_missing_or_malformed_audio_hint_does_not_hide_adapter(
        self,
    ) -> None:
        path = "/com/steampowered/CecDaemon1/Devices/Cec1"
        for hint in (
            None,
            {"type": "u", "data": 5},
            {"type": "y", "data": True},
            {"type": "y", "data": 256},
        ):
            with self.subTest(hint=hint):
                device = _device(
                    active=False,
                    physical=0x1000,
                    logical=[8],
                )
                properties = device[CECD_DEVICE_INTERFACE]
                if hint is None:
                    properties.pop("AudioLogicalAddress")
                else:
                    properties["AudioLogicalAddress"] = hint
                selection = select_unique_cec_device(
                    _reply({path: device})
                )
                self.assertEqual(selection.path, path)
                self.assertIsNone(selection.reported_audio_address)

    def test_accepts_only_allocated_playback_logical_addresses(self) -> None:
        for logical in ([4], [8], [11], [1, 8]):
            with self.subTest(logical=logical):
                selection = select_unique_cec_device(
                    _reply(
                        {
                            "/com/steampowered/CecDaemon1/Devices/Cec1": _device(
                                active=False,
                                physical=0x1000,
                                logical=logical,
                                audio=0,
                            )
                        }
                    )
                )
                self.assertEqual(selection.logical_addresses, tuple(logical))

    def test_path_validation_accepts_only_numbered_device_children(self) -> None:
        self.assertTrue(
            is_cec_device_path(
                "/com/steampowered/CecDaemon1/Devices/Cec12"
            )
        )
        for value in (
            None,
            "/com/steampowered/CecDaemon1/Devices/Cec",
            "/com/steampowered/CecDaemon1/Devices/Cec1/child",
            "/com/steampowered/CecDaemon1/Devices/Hdmi1",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_cec_device_path(value))

    def test_zero_or_multiple_usable_endpoints_fail_closed(self) -> None:
        no_candidate = _reply(
            {
                "/com/steampowered/CecDaemon1/Devices/Cec0": _device(
                    active=False,
                    physical=0xFFFF,
                    logical=[4],
                    audio=5,
                )
            }
        )
        with self.assertRaisesRegex(CecDeviceDiscoveryError, "no usable"):
            select_unique_cec_device(no_candidate)

        multiple = _reply(
            {
                f"/com/steampowered/CecDaemon1/Devices/Cec{number}": _device(
                    active=number == 1,
                    physical=0x1000 + number,
                    logical=[4],
                    audio=audio,
                )
                for number, audio in ((1, 0), (9, 5))
            }
        )
        with self.assertRaisesRegex(
            CecDeviceDiscoveryError, "multiple usable"
        ):
            select_unique_cec_device(multiple)

    def test_invalid_endpoint_properties_are_not_candidates(self) -> None:
        invalid_properties = (
            {"active": False, "physical": 0, "logical": [4], "audio": 5},
            {"active": False, "physical": 0xFFFF, "logical": [4], "audio": 5},
            {"active": False, "physical": 0x1000, "logical": [], "audio": 5},
            {"active": False, "physical": 0x1000, "logical": [5], "audio": 5},
            {"active": False, "physical": 0x1000, "logical": [8, 8], "audio": 5},
            {"active": False, "physical": 0x1000, "logical": [8, True], "audio": 5},
            {"active": False, "physical": 0x1000, "logical": [8, 15], "audio": 5},
        )
        for properties in invalid_properties:
            with self.subTest(properties=properties):
                payload = _reply(
                    {
                        "/com/steampowered/CecDaemon1/Devices/Cec4": _device(
                            **properties
                        )
                    }
                )
                with self.assertRaisesRegex(
                    CecDeviceDiscoveryError, "no usable"
                ):
                    select_unique_cec_device(payload)

    def test_malformed_wrong_signature_and_oversized_replies_fail_closed(self) -> None:
        for payload in (
            b"",
            b"not-json",
            b'{"type":"a{sv}","data":[{}]}',
            b'{"type":"a{oa{sa{sv}}}","data":{}}',
            b"x" * (MAX_MANAGED_OBJECTS_BYTES + 1),
        ):
            with self.subTest(payload=payload[:40]):
                with self.assertRaises(CecDeviceDiscoveryError):
                    select_unique_cec_device(payload)


if __name__ == "__main__":
    unittest.main()
