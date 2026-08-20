# v1.0.0-rc.1 maintainer-host acceptance

Date: 2026-08-20

The deterministic release candidate was installed on the maintainer SteamOS
Gaming Mode host from the exact public artifact:

`bcd7337934717a73a851bc7b168ad5aa0af83efe3d41db3333829e0bc1172d66`

## Verified automatically

- Decky loaded frontend and backend version `1.0.0-rc.1` through the official
  `@decky/api` package.
- The installed 10-file runtime tree byte-matched the release ZIP.
- The existing Valve `cecd` service dynamically selected the usable `Cec1`
  playback endpoint and confirmed volume/mute state through
  `GetAudioStatus(5)`.
- The hidden lifecycle canary reused one DOM host, extended its five-second
  deadline, revived during fade, removed the host, and fully released Steam's
  Notification composition request.
- A later real physical CEC change advanced the frontend's bound generation
  cursor with no error.
- Over the focused game, the fail-closed focus lease acquired Steam's proven
  singleton native game window once and compare-and-swap restored the prior
  zero sentinel once after Notification release.
- Decky Loader, Steam, Gamescope, PipeWire, WirePlumber, `cecd`,
  `cec-audio-control`, and the installed MangoApp supervisor/child retained
  their process identities throughout deployment and initial acceptance.
- The immutable prior OSD checkpoint remained available for rollback.

## User-visible acceptance

The user enabled Steam's performance overlay, changed HDMI-CEC volume over a
focused game, and confirmed that the Steam performance overlay and CEC Volume
OSD appeared simultaneously.

This accepts the maintainer-host coexistence path for the GitHub release
candidate. It does not claim independent SteamOS Stable/Beta or Bazzite
portability, every performance-overlay level, suspend/resume, or reboot
persistence. Those remain stable-release gates.
