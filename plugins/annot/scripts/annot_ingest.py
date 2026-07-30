#!/usr/bin/env python3
"""Annot session ingest for Claude Code — hook, uploader, and pairing in one file.

Modes:
    annot_ingest.py hook      SessionEnd hook: spool a job, detach an uploader, exit.
    annot_ingest.py upload    Detached uploader: drain the job queue over HTTP.
    annot_ingest.py connect   One-time device pairing (also the /annot:connect skill).
    annot_ingest.py status    Show config/queue state for debugging.

Design constraints, in order:
1.  NEVER delay Claude Code. The hook mode does only local, sub-50ms work (read
    stdin, write one small file, spawn a detached process) and always exits 0.
2.  NEVER nag. With no token configured the hook prints one pairing hint ever,
    then stays silent. A revoked token (401) silences uploads until re-connect.
3.  Zero dependencies. Python 3 stdlib only — python3 is present on macOS and
    effectively all Linux dev machines; bash would need jq, node isn't a given.

State lives in ~/.config/annot/:
    ingest.json     {"api_url", "token", "deny_paths": [], "hinted", "token_invalid"}
    queue/<id>.json one pending upload per session (the offline retry queue)
    upload.lock     flock guard so two uploaders never race
"""

from __future__ import annotations

import gzip
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_API_URL = "https://api.useannot.com"
CONFIG_DIR = os.path.expanduser(os.environ.get("ANNOT_CONFIG_DIR", "~/.config/annot"))
CONFIG_PATH = os.path.join(CONFIG_DIR, "ingest.json")
QUEUE_DIR = os.path.join(CONFIG_DIR, "queue")
LOCK_PATH = os.path.join(CONFIG_DIR, "upload.lock")

CLIENT_VERSION = "annot-plugin/0.1.0"
UPLOAD_TIMEOUT_SECONDS = 60
MAX_STALE_JOBS_PER_RUN = 3
PAIRING_POLL_SECONDS = 3
PAIRING_MAX_WAIT_SECONDS = 600


# ----------------------------------------------------------------- config ----

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
            return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def api_url(cfg: dict) -> str:
    return (cfg.get("api_url") or DEFAULT_API_URL).rstrip("/")


# ------------------------------------------------------------------- hook ----

def cmd_hook() -> int:
    """SessionEnd. Reads the hook JSON on stdin; must be fast and always exit 0."""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    transcript_path = event.get("transcript_path") or ""
    session_id = event.get("session_id") or ""
    cwd = event.get("cwd") or ""

    if not session_id or not transcript_path or not os.path.isfile(transcript_path):
        return 0
    if os.environ.get("ANNOT_NO_INGEST"):
        return 0

    cfg = load_config()
    for deny in cfg.get("deny_paths") or []:
        if isinstance(deny, str) and deny and cwd.startswith(os.path.expanduser(deny)):
            return 0

    if not cfg.get("token"):
        if not cfg.get("hinted"):
            print(
                "Annot: pair this machine once with `/annot:connect` to auto-save "
                "your Claude Code sessions.",
                file=sys.stderr,
            )
            cfg["hinted"] = True
            save_config(cfg)
        return 0
    if cfg.get("token_invalid"):
        return 0

    os.makedirs(QUEUE_DIR, mode=0o700, exist_ok=True)
    job = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "reason": event.get("reason") or "",
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    job_path = os.path.join(QUEUE_DIR, f"{session_id}.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)

    # Hand off to a detached uploader and get out of Claude Code's way. The
    # uploader re-execs this same file, so the plugin stays one script.
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "upload"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0


# ----------------------------------------------------------------- upload ----

def _acquire_lock():
    """One uploader at a time. Returns the held file object, or None when another
    uploader is live (it will drain the queue, including our job)."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    lock_file = open(LOCK_PATH, "w")  # noqa: SIM115 - held for process lifetime
    try:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except (ImportError, OSError):
        lock_file.close()
        return None


def cmd_upload() -> int:
    cfg = load_config()
    token = cfg.get("token")
    if not token or cfg.get("token_invalid"):
        return 0

    lock = _acquire_lock()
    if lock is None:
        return 0

    try:
        jobs = sorted(
            (os.path.join(QUEUE_DIR, name) for name in os.listdir(QUEUE_DIR)),
            key=os.path.getmtime,
            reverse=True,  # newest (the session that just ended) first
        )
    except OSError:
        return 0

    for job_path in jobs[: 1 + MAX_STALE_JOBS_PER_RUN]:
        try:
            with open(job_path, encoding="utf-8") as fh:
                job = json.load(fh)
        except (OSError, ValueError):
            _remove(job_path)
            continue

        outcome = _upload_job(cfg, token, job)
        if outcome in ("done", "poison"):
            _remove(job_path)
        elif outcome == "unauthorized":
            cfg["token_invalid"] = True
            save_config(cfg)
            return 0
        # "retry": keep the job file; the next session end tries again.
    return 0


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _upload_job(cfg: dict, token: str, job: dict) -> str:
    transcript_path = job.get("transcript_path") or ""
    try:
        with open(transcript_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return "poison"  # transcript is gone; nothing to upload, ever
    if not raw.strip():
        return "poison"

    body, content_type = _multipart(
        fields={
            "session_id": job.get("session_id") or "",
            "cwd": job.get("cwd") or "",
            "reason": job.get("reason") or "",
            "ended_at": job.get("ended_at") or "",
            "client_version": CLIENT_VERSION,
        },
        file_field="transcript",
        filename="transcript.jsonl.gz",
        file_bytes=gzip.compress(raw),
        file_type="application/gzip",
    )
    request = urllib.request.Request(
        f"{api_url(cfg)}/v1/ingest/code-session",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_SECONDS):
            return "done"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "unauthorized"
        if exc.code == 429 or exc.code >= 500:
            return "retry"
        return "poison"  # 404 flag-off, 413 too large, 422 malformed: don't loop
    except (urllib.error.URLError, socket.timeout, OSError):
        return "retry"  # offline; the next session end retries


def _multipart(
    fields: dict, file_field: str, filename: str, file_bytes: bytes, file_type: str
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        if not value:
            continue
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{name}"\r\n\r\n{value}\r\n'
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {file_type}\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- connect ----

def cmd_connect() -> int:
    cfg = load_config()
    base = api_url(cfg)
    label = socket.gethostname().split(".")[0] or "Claude Code"

    try:
        pairing = _post_json(f"{base}/v1/ingest-tokens/pairings", {"label": label})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(
                "Annot: session ingest isn't enabled for this server yet.",
                file=sys.stderr,
            )
            return 1
        print(f"Annot: pairing failed ({exc.code}).", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"Annot: could not reach {base} ({exc}).", file=sys.stderr)
        return 1

    url = pairing.get("verification_url") or ""
    print()
    print(f"  Pairing code:  {pairing.get('user_code', '?')}")
    print(f"  Approve here:  {url}")
    print()
    print("Waiting for approval (Ctrl-C to cancel)…")
    _try_open_browser(url)

    poll_interval = int(pairing.get("poll_interval") or PAIRING_POLL_SECONDS)
    deadline = time.time() + PAIRING_MAX_WAIT_SECONDS
    poll_url = f"{base}/v1/ingest-tokens/pairings/{pairing.get('device_code', '')}"
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            state = _get_json(poll_url)
        except urllib.error.HTTPError as exc:
            if exc.code == 410:
                print("Annot: the pairing expired — run connect again.", file=sys.stderr)
                return 1
            continue
        except (urllib.error.URLError, OSError):
            continue
        if state.get("status") == "approved" and state.get("token"):
            cfg.update(
                {
                    "api_url": base,
                    "token": state["token"],
                    "token_invalid": False,
                    "hinted": True,
                }
            )
            save_config(cfg)
            print("Annot connected — your Claude Code sessions will now save themselves.")
            return 0
    print("Annot: pairing timed out — run connect again.", file=sys.stderr)
    return 1


def _try_open_browser(url: str) -> None:
    if not url:
        return
    opener = {"darwin": "open"}.get(sys.platform, "xdg-open")
    try:
        subprocess.Popen(
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


# ----------------------------------------------------------------- status ----

def cmd_status() -> int:
    cfg = load_config()
    queued = []
    try:
        queued = os.listdir(QUEUE_DIR)
    except OSError:
        pass
    print(f"api_url:       {api_url(cfg)}")
    print(f"paired:        {'yes' if cfg.get('token') else 'no'}")
    print(f"token_invalid: {bool(cfg.get('token_invalid'))}")
    print(f"queued jobs:   {len(queued)}")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "hook":
        try:
            return cmd_hook()
        except Exception:
            return 0  # a broken hook must never surface into Claude Code
    if mode == "upload":
        return cmd_upload()
    if mode == "connect":
        return cmd_connect()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
