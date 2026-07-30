<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://useannot.com/assets/logo/wordmark-dark.png">
    <img src="https://useannot.com/assets/logo/wordmark-light.png" alt="Annot" width="160">
  </picture>
</p>

# Annot for Claude Code

Auto-save every Claude Code session to your [Annot](https://useannot.com) second
brain — searchable, summarized, and connected to everything else you've saved.

## Install

```
/plugin marketplace add AnnotLab/claude-plugins
/plugin install annot@annotlab
/annot:connect
```

`/annot:connect` shows a short pairing code and opens your Annot account to
approve it. Pair once per machine — from then on every session saves itself when
it ends, with no action needed.

## What you get

- **Auto-save** — a SessionEnd hook uploads each session's transcript in the
  background. Claude Code's exit is never delayed; offline sessions queue and
  retry on the next session end.
- **Recall** — at chat start, Claude sees the *titles* of resources Annot has
  saved about the repo you're in (past sessions, notes, pages). Content is only
  loaded when you pick it yourself via `/annot:recall`.
- **Control** — pause saving per device (without unpairing), toggle recall, or
  revoke a device entirely from **Annot → Connections → Claude Code**. Set
  `ANNOT_NO_INGEST=1` in a project's environment to keep that project's sessions
  out.

## Commands

| Command | What it does |
|---|---|
| `/annot:connect` | One-time device pairing with your Annot account |
| `/annot:recall` | Pick saved resources related to this repo to load into the session |

## Privacy

Your transcripts are uploaded only to your own Annot account, over HTTPS, using
a device token you can revoke at any time. Nothing is shared with anyone else.
