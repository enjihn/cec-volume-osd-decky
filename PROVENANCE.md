# Provenance

This public repository is a clean release tree derived from a privately
developed SteamOS HDMI-CEC volume OSD. The private development history also
contains machine-specific deployment, rollback, input switching, audio
experiments, and MangoApp recovery work; those unrelated components are not
part of this repository.

The public release preserves the independently tested read-only CEC observer,
OSD presentation state machine, Steam `Notification` composition integration,
and fail-closed focused-window lease. Machine-specific Guide-button CEC input
switching was deliberately separated before publication.

The frontend uses official Decky Loader packages. The backend is original
Python code using documented D-Bus contracts and the system `busctl` client.
No Valve, Steam, Gamescope, MangoHud, or linux-cec source code is bundled.

Release artifacts are deterministically built from the tagged source and
include `SHA256SUMS` plus a generated provenance record.
