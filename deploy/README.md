# Deployment

This directory will contain platform packaging after the hub can scan, persist,
and report health reliably.

```text
deploy/
├── launchd/    macOS plist templates and install/remove scripts
├── systemd/    Linux unit templates and install/remove scripts
└── containers/ Optional headless image after Linux BLE passthrough is proven
```

Deployment artifacts must define a dedicated data/config location, least-privilege
LAN binding and firewall behavior, log rotation, clean shutdown, database
migration, and rollback.
Templates must never contain credentials or machine-specific paths.
