# Backup control panel

The integration adds **Auto Backup** to Home Assistant's sidebar. Only administrators can open the panel or call its WebSocket API. It contains status and operation history, NAS inventory, scheduling, backup contents, retention, encryption and notification settings. There is no generated YAML and no required automation, script or helper.

The panel follows Home Assistant theme colours and card styling. It uses the signed-in user's Home Assistant language: English and Italian are supported, with English fallback for other languages. Dates and numbers follow the selected locale.

## First setup

1. Add Auto Backup under Settings → Devices & services.
2. Open Auto Backup → Settings. Configure an existing SMB 2/3 share and optional relative folder. The folder must already exist. Test the connection and save.
3. Choose the archive contents, retention counts, creation timeout and notification entities. Encryption uses the recovery key already configured in Home Assistant's Backup settings; the panel never displays that key.
4. Run a manual backup and check its NAS verification result.
5. Disable older automations or other applications which create the same backups, then enable the built-in schedule.

The built-in scheduler is **off by default**, including when upgrading an existing installation. Existing services remain available. Upgrading does not silently disable or rewrite the user's automations. Home Assistant's native backup schedule is separate; keep it disabled if this integration owns the schedule.

## Scheduling and notifications

Select weekdays and a backup time in Home Assistant's configured timezone. Optional weekly full archive verification and weekly reports each have their own weekday and time. Every new NAS copy is always fully reread and SHA-256 checked, independently of the optional recurring verification.

Missed schedules can be caught up within a configurable window on the same local date. A schedule slot is durably claimed before starting. Restarting or the autumn DST fold therefore cannot start the same claimed slot twice. A job interrupted by restart is recorded as interrupted rather than retried automatically. The next regular slot remains scheduled. A spring-forward gap is normalised to a valid local time.

Configure notifications for failures, old or missing backups, excessive operation duration and optionally each success. Without notify entities, messages use persistent Home Assistant notifications. The scheduler's missing/stalled monitors and automatic report run only while its master switch is enabled. Manual operations still report their failures when error notifications are enabled. A duration warning does not cancel a running transfer. Persistent NAS availability or checksum-state problems are also reported after 15 minutes (5 minutes for an empty archive list), even when the latest verified backup is recent. Recovery clears the pending warning; repeated warnings are limited per local date.

Operations are serialised; the panel rejects simultaneous requests and configuration changes during a managed operation. Long operations continue if the browser closes. The last 100 operations and schedule claims survive restart. Settings use a revision check to prevent overwriting a concurrent edit.

## Inventory, retention and restore

Inventory is refreshed by the NAS check, after backup retention, and after full verification. Presence/size are distinct from the date of the last complete byte verification. An archive with no independent receipt is not described as verified. Receipts and history are isolated per NAS server/share/folder, so changing destination cannot authorise deletion of local originals based on a copy at another destination.

Daily, Sunday and monthly tiers share files, retaining one archive per calendar date. Monthly copies select the first available Sunday in each retained month. A failed creation or integrity verification never triggers rotation. The integration's namespace prevents unrelated backups from being managed. Legacy adoption is not exposed as a routine panel action.

Use Home Assistant's native Backup page to restore a local copy. For NAS-only copies, retrieve the archive from the SMB share and upload it to Home Assistant's restore workflow. Keep encryption recovery keys separately. SHA-256 byte equality is not a complete disaster-recovery restore test.

NAS recycle-bin settings and NAS-to-offsite replication are managed on the NAS. Only the application password is saved in Home Assistant's private integration options; it is never returned in panel status or suggested form values. A blank password field preserves the stored password.
