# Compatibility

Compatibility is determined from live runtime contracts, not a SteamOS build
number. The plugin is ready only when it can prove all of the following:

1. Decky Loader exposes the official callable, event, and global-component API.
2. `/usr/bin/busctl` can reach the user-session
   `com.steampowered.CecDaemon1` owner without activating a replacement.
3. ObjectManager exposes exactly one usable Playback endpoint.
4. That endpoint returns `GetAudioStatus(5) -> (yb)` with a volume in 0–100 or
   CEC's unknown sentinel, plus a boolean mute state.
5. Steam's minimum Notification composition hook is uniquely identified.

SteamOS and Bazzite are supported only when those existing contracts are
present. The plugin does not install `cecd`, repair HDMI topology, or add audio
control services.

The release candidate has been validated on the maintainer's SteamOS runtime.
Independent SteamOS Stable/Beta and Bazzite physical acceptance remains a gate
for stable 1.0.0.
