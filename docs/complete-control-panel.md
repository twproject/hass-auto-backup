# Complete service coverage (1.12)

The interface covers every public Auto Backup service and creation parameter. It remains admin-only, follows the HA theme and account language (English/Italian), and stores private installation details in HA integration options. Existing 1.11 settings are backward compatible. Validate each rollout in a separate test installation before upgrading production.

| Original service | GUI |
|---|---|
| `backup` | Automatic mode with includes and exclusions |
| `backup_full` | Full mode, optional exclusions |
| `backup_partial` | Partial mode, selected folders and apps |
| `purge` | Cleanup preview, confirmation, then recheck before removal |
| `check_nas` | Check NAS / refresh verified inventory |
| `verify_nas` | Full integrity check; optional legacy import with separate preview and confirmation |

All common fields are available: name (including the original default), encryption key/custom password, decimal `keep_days`, primary Supervisor storage location, additional download paths, compression, and database exclusion. Folders support the original aliases; apps accept names, slugs and wildcards. The service-schema parity test compares the complete field sets against the integration's actual Voluptuous schemas.

**Backup settings** configures the job used by the built-in schedule and the main backup button. **Run once** uses an independent form: running it changes neither the stored job nor its schedule. Custom passwords can be saved or supplied once; blank preserves a saved password and an explicit remove control clears it. Saved passwords and one-off secrets never appear in state, operation history, or form defaults.

**Backups** lists HA-local archives, protection, size, and tracked expiry separately from the NAS catalog and checksum evidence. Restoration and direct downloads use the native Home Assistant backup page, which is not a service implemented by Auto Backup itself.

## Backend capabilities and inherited limits

- Standard mode works without a verified NAS. It uses `keep_days`, optional download directories, and automatic/manual expiry cleanup.
- Verified NAS mode uses calendar tiers and byte verification. Its backend does not support simultaneous `download_path` or `keep_days`; the UI/API rejects these combinations before a job starts, rather than ignoring them.
- NAS calendar retention keeps the latest archive for a date, regardless of its contents. The one-off form asks for confirmation before a partial NAS backup which could replace a full archive for that date.
- Supervisor installations support partial selections, compression controls and named Supervisor backup mounts. Core installations support the capabilities of the existing native handler; unsupported combinations are rejected before creation. Native inventory reads the local agent's size/protection metadata.
- Additional download directories must exist on the HA server. The interface does not create arbitrary directories.
- A default encryption key must already be configured in native HA backup settings. A custom password needs no default key.
- Legacy import requires the schedule off and legacy handling enabled. A validated legacy baseline is labelled separately from a source-and-NAS match. Import creates checksum evidence; it is not an independent restore drill.
- Cleanup is limited to the existing Auto Backup expiry/verified-retention scope. A changed preview invalidates confirmation. Unmanaged files are not included.

Schedules, restart claims, operation locking, integrity failure protection and the existing service API remain compatible. No YAML generation is needed.
