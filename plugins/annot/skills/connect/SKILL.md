---
name: connect
description: Pair this machine with your Annot account so Claude Code sessions auto-save to your second brain. Use when the user runs /annot:connect or asks to connect, pair, or set up Annot session saving.
---

Run the Annot pairing flow:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/annot_ingest.py" connect
```

The script prints a short pairing code and an approval URL (and tries to open it
in the browser). Tell the user to approve the code in their logged-in Annot
account; the script waits up to 10 minutes and stores the device token in
`~/.config/annot/ingest.json` once approved.

After a successful pairing, tell the user: from now on every Claude Code session
saves itself to Annot when it ends — no action needed. Mention that setting
`ANNOT_NO_INGEST=1` in a project's environment keeps that project's sessions out,
and that devices can be revoked any time from Annot → Connections → Claude Code.

If the script reports that ingest isn't enabled or the pairing expired, relay the
message as-is; don't retry in a loop.
