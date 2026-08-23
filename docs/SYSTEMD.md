# Optional systemd Service

The safest first run is interactive. Use systemd only after `lightclaw demo`, onboarding, and `lightclaw doctor` succeed for the dedicated unprivileged account.

Assume LightClaw was installed with pipx for user `lightclaw` and the command is `/home/lightclaw/.local/bin/lightclaw`.

```ini
[Unit]
Description=LightClaw Telegram mission control
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=lightclaw
Group=lightclaw
WorkingDirectory=/home/lightclaw
ExecStart=/home/lightclaw/.local/bin/lightclaw run
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadOnlyPaths=/home/lightclaw/.config/lightclaw
ReadWritePaths=/home/lightclaw/.lightclaw
RestrictSUIDSGID=true
LockPersonality=true
UMask=0077

[Install]
WantedBy=multi-user.target
```

Install it as `/etc/systemd/system/lightclaw.service`, then:

```bash
sudo systemd-analyze verify /etc/systemd/system/lightclaw.service
sudo systemctl daemon-reload
sudo systemctl enable --now lightclaw
sudo journalctl -u lightclaw -f
```

The service intentionally has no write access outside `~/.lightclaw`. A `trusted-command` run may fail under these controls; weakening the unit broadens the host boundary and must be a deliberate administrator decision.

Never put provider or Telegram credentials directly in the unit file. LightClaw reads its mode-`0600` app config from the dedicated account.
