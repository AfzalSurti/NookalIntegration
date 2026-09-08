# Recovery notes (clinic Mac)

## FileVault

After reboot or power cut, the Mac must be unlocked **physically at the clinic**
before launchd services and remote access come up. Tailscale / SSH will not
help until someone is at the keyboard. Flag this to the client in writing —
it is an operational risk for morning reminders.

## Kill switch

```bash
sudo -u automation /Users/automation/app/.venv/bin/python -m app.shared.kill_switch on --reason "..."
```

Halts Nookal writes and all outbound messaging immediately. Does not roll back
in-flight HTTP calls already on the wire.

## Audit logs

Daily files under `/Users/automation/logs/audit/audit-YYYY-MM-DD.jsonl`.
Append-only. Do not edit by hand.
