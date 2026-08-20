# Security policy

## Supported versions

Security fixes are provided for the latest release candidate or stable release.

## Report a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do
not include SSH keys, host addresses, logs containing account tokens, or other
machine-specific secrets in a public issue.

## Mutation boundary

CEC Volume OSD is intentionally read-only with respect to the host system and
the HDMI-CEC bus. Runtime code may:

- subscribe to Decky events and Steam's composition store;
- create and remove its own DOM node inside GamepadUI;
- make bounded D-Bus reads using `/usr/bin/busctl`;
- temporarily lease Steam's selected focused-window cache under strict
  compare-and-swap preconditions.

Runtime code must never:

- open `/dev/cec*` or use `cec-ctl`/libCEC;
- invoke CEC write/control methods, including volume, mute, standby, routing,
  active-source, or wake methods;
- run `systemctl`, replace packages, write under `/usr`, or modify audio
  configuration;
- create a separate Gamescope/X11/Wayland overlay or set the external-overlay
  property used by MangoApp;
- start, stop, patch, or reconfigure MangoApp, Steam, Gamescope, PipeWire,
  WirePlumber, `cecd`, or `cec-audio-control`.

CI checks these constraints mechanically and tests the focus-lease lifecycle.
