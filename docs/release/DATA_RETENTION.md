# What the desktop app keeps, and what uninstalling leaves

Uninstalling the app removes the application. **It keeps your data**: captured notes, connected agents and their
authority records, approvals and their history, installed applications and their records, dashboard views and the replay
store. This is deliberate, so reinstalling or upgrading keeps your work. The install acceptance checks it after every
uninstall.

| System | Where the data is |
|---|---|
| Windows | `%LOCALAPPDATA%\io.redevops.agentic-apps\` |
| macOS | `~/Library/Application Support/io.redevops.agentic-apps/` (the launcher's cache: `~/Library/Caches/io.redevops.agentic-apps.r0prime/`) |
| Linux | `${XDG_STATE_HOME:-~/.local/state}/io.redevops.agentic-apps/` |

To remove everything, uninstall the app and then delete that folder. Before deleting it, revoke connected agents on the
app's Agents screen, or delete the folder while no agent is running: their token files are inside it. Export any
application you want to keep first (Applications, then Export).
