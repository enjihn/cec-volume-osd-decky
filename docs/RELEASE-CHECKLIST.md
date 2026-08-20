# Release checklist

- [x] Official `@decky/api` migration.
- [x] OSD-only runtime; machine-specific Guide-button CEC writes removed.
- [x] Backend/frontend/typecheck/build tests.
- [x] Read-only mutation-boundary audit.
- [x] Deterministic double package and SHA-256 verification.
- [x] Guarded install and hidden lifecycle test on the maintainer SteamOS host.
- [ ] User-visible Steam Home test with a physical CEC volume change.
- [x] User-visible focused-game test with a physical CEC volume change.
- [x] User-confirmed simultaneous OSD and nonzero Steam performance overlay.
- [ ] Performance-overlay levels 1–4 coexistence acceptance.
- [ ] Independent SteamOS Stable and Beta hardware acceptance.
- [ ] Independent Bazzite Gaming Mode hardware acceptance.
- [ ] Suspend/resume and explicitly approved reboot persistence.

Unchecked items block stable 1.0.0. Hardware portability items do not block the
GitHub release candidate.
