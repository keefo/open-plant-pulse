# Deployment

Platform packaging for the hub.

```text
deploy/
├── launchd/    macOS LaunchAgent, app bundle and install/remove scripts
├── systemd/    Linux unit templates and install/remove scripts (not yet)
└── containers/ Optional headless image after Linux BLE passthrough is proven (not yet)
```

`launchd/install.sh` renders the templates for the checkout it sits in and
installs them; see "Running as a macOS service" in `docs/hub.md`. The launcher
serves the page on port 80 for this computer only.

Deployment artifacts must define a dedicated data/config location, least-privilege
LAN binding and firewall behavior, log rotation, clean shutdown, database
migration, and rollback.
Templates must never contain credentials or machine-specific paths.
