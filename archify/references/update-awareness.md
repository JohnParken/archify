# Update awareness

The native Python runtime performs no update check and emits no update notice.
It has no `check-update.mjs`, snooze or ignore command. Do not run an obsolete
notifier command or infer update state from a legacy receipt.

If the user asks about an update, distinguish verified release information from
the local Skill version in [release identity](../skill-release.json). An update
notice is information, never permission to download, execute, install or change
a live installation. Obtain explicit authorization for the source, targets and
scope of any live installation action.
