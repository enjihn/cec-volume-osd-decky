# Contributing

Contributions are welcome. Keep the plugin's read-only and coexistence
boundaries intact.

Before opening a pull request:

```sh
pnpm install --frozen-lockfile
pnpm test
pnpm build
./scripts/check-forbidden-mutations.sh
./scripts/package.sh
```

Include the operating system, Decky Loader version, whether Valve's `cecd`
service exists, the selected CEC object path, and whether the test occurred in
Steam Home or over a focused game. Never post credentials or complete system
logs without reviewing them for private data.

Changes that add CEC writes, system-service management, audio-stack changes,
or a Gamescope external-overlay surface are outside this plugin's scope.
