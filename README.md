# Annot Claude Code plugin (marketplace source)

This folder is the SOURCE OF TRUTH for the public plugin marketplace repo
`AnnotLab/claude-plugins` (GitHub). The monorepo is private; this folder's
contents are mirrored verbatim as the ROOT of that public repo by
`scripts/publish-claude-plugin.sh`. Never edit the mirror directly.

## What the plugin does

`annot` ships a SessionEnd hook that auto-saves every Claude Code session to the
user's Annot second brain:

- `hooks/hooks.json` — SessionEnd → `scripts/annot_ingest.py hook`, which spools
  a job and detaches an uploader (Claude Code's exit is never delayed).
- `scripts/annot_ingest.py` — single-file Python 3 stdlib client: hook, queued
  gzip upload with retry, and the device-pairing `connect` flow.
- `skills/connect` — `/annot:connect`, the one-time pairing command.

Server side: `POST /v1/ingest/code-session` (services/api), authenticated by a
personal ingest token from the pairing flow, gated by the `ingest.code_sessions`
feature flag.

## User install

```
/plugin marketplace add AnnotLab/claude-plugins
/plugin install annot@annotlab
/annot:connect
```

## Publishing a release

1. Bump `version` in `plugins/annot/.claude-plugin/plugin.json` (users don't get
   updates without a bump).
2. Run `scripts/publish-claude-plugin.sh` from the monorepo root.

## Local testing

`/plugin marketplace add /path/to/monorepo/apps/claude-plugin` then
`/plugin install annot@annotlab`. Point the client at a local API with
`ANNOT_CONFIG_DIR=/tmp/annot-test` and an `api_url` of `http://localhost:8787`
in the config file (or pair against it — the connect flow stores whatever
`api_url` it was run with).
