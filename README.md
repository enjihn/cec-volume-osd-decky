# CEC Volume OSD for Decky Loader

CEC Volume OSD adds a Steam-styled, number-free volume bar for HDMI-CEC audio
systems. It reads confirmed volume and mute state from Valve's existing `cecd`
service and displays changes over Steam Home and focused games.

The plugin is deliberately read-only. It does not open `/dev/cec*`, send CEC
volume commands, replace PipeWire or WirePlumber, patch Gamescope, or change
MangoApp. Steam's built-in performance overlay remains on Gamescope's separate
external-overlay plane and can be shown at the same time.

> This release candidate is for SteamOS and Bazzite Gaming Mode systems that
> already provide Valve's `com.steampowered.CecDaemon1` D-Bus service and a
> usable HDMI-CEC playback endpoint. It does not install or configure a CEC
> stack.

## What it does

- Discovers exactly one usable CEC playback endpoint through D-Bus ObjectManager;
  no `Cec0`/`Cec1` path is hard-coded.
- Accepts only confirmed `GetAudioStatus(5)` or `Report Audio Status` state.
- Uses a 500 ms healthy fallback poll and a temporary 200 ms activity burst,
  with one query in flight and a 15 second unavailable backoff.
- Shows one pointer-transparent right-edge bar for five seconds after the most
  recent change, then fades out and removes the DOM node.
- Requests Steam's minimum `Notification` composition state while visible.
  A fail-closed, compare-and-swap focus lease repairs current GamepadUI
  publication over games without taking input or Gamescope's MangoApp slot.
- Baselines startup, reconnects, owner changes, and recovery without flashing.
- Treats CEC volume `0x7f` as unknown rather than inventing a number.

## Requirements

- Decky Loader with the official `@decky/api` global-component API.
- SteamOS Gaming Mode or Bazzite Gaming Mode.
- `/usr/bin/busctl` and a running user-session
  `com.steampowered.CecDaemon1` service.
- Exactly one managed CEC endpoint with a real physical address and a Playback
  logical address.
- An HDMI audio system that answers `CecDevice1.GetAudioStatus(5)`.

The plugin reports a compatibility reason in its QAM page when these runtime
contracts are not available. It fails closed without changing services.

## Install the release candidate

Download `cec-volume-osd-1.0.0-rc.1.zip` and its `SHA256SUMS` from the GitHub
release. Verify the checksum, then use Decky Loader's developer plugin installer
or copy the extracted plugin directory into Decky's plugins directory.

This project is not submitted to the Decky Plugin Store yet. The release is a
GitHub release candidate pending physical testing on independent SteamOS and
Bazzite hardware.

## User interface

The QAM page shows readiness and the latest confirmed CEC state. **Preview**
renders the current confirmed state without changing volume. There is no input
switching, volume control, audio routing, or device configuration in this
plugin.

## Build and test

Requirements: Node.js 20+, pnpm 10 with lockfile format 9, and Python 3.11+
(the runtime itself uses only Python's standard library).

```sh
corepack enable
pnpm install --frozen-lockfile
pnpm test
pnpm build
./scripts/check-forbidden-mutations.sh
./scripts/package.sh
```

`scripts/package.sh` builds twice and requires the two ZIP archives to be
byte-for-byte identical. The release ZIP contains only the runtime allowlist
documented in `RELEASE_MANIFEST.txt`.

## Architecture and coexistence

The Python backend runs inside Decky's plugin process and starts one bounded
`busctl monitor` plus serialized `busctl call` reads. `cecd` remains the sole
owner of the kernel CEC device. The frontend reuses Steam's existing GamepadUI
document and registers an always-mounted global component through
`@decky/api`.

Gamescope composes Steam notification surfaces separately from MangoApp's
external-overlay surface. The OSD therefore never creates a Wayland or X11
overlay, never sets `GAMESCOPE_EXTERNAL_OVERLAY`, and never starts, stops, or
reconfigures MangoApp. Its focus lease writes only Steam's selected-window
cache from the zero sentinel to Steam's already-proven singleton focused
window, and restores zero only if the value is still owned by the lease.

See [SECURITY.md](SECURITY.md) for the enforced mutation boundary and
[PROVENANCE.md](PROVENANCE.md) for the development history.

## Release status

`v1.0.0-rc.1` is intended to validate portability. Stable `v1.0.0` requires:

- physical SteamOS Stable and Beta testing;
- physical Bazzite Gaming Mode testing;
- Steam Home and focused SDR/HDR game acceptance;
- performance-overlay levels 1–4 visible simultaneously with the OSD;
- suspend/resume and explicitly approved reboot persistence tests.

## License

MIT. See [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) for Decky interface and
build-tool notices.
