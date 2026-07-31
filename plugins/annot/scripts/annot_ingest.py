#!/usr/bin/env python3
"""Annot session ingest for Claude Code — hook, uploader, and pairing in one file.

Modes:
    annot_ingest.py hook          SessionEnd hook: spool a job, detach an uploader, exit.
    annot_ingest.py upload        Detached uploader: drain the job queue over HTTP.
    annot_ingest.py connect       One-time device pairing (also the /annot:connect skill).
    annot_ingest.py session-start SessionStart hook: inject a titles-only recall hint.
    annot_ingest.py recall-list   Print related-resource titles as JSON (the /annot:recall skill).
    annot_ingest.py recall-get ID Print one resource's content as JSON.
    annot_ingest.py status        Show config/queue state for debugging.

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
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_API_URL = "https://api.useannot.com"
CONFIG_DIR = os.path.expanduser(os.environ.get("ANNOT_CONFIG_DIR", "~/.config/annot"))
CONFIG_PATH = os.path.join(CONFIG_DIR, "ingest.json")
QUEUE_DIR = os.path.join(CONFIG_DIR, "queue")
LOCK_PATH = os.path.join(CONFIG_DIR, "upload.lock")

CLIENT_VERSION = "annot-plugin/0.3.0"
# Job keys forwarded verbatim as multipart form fields. Everything past `ended_at` is
# repo identity (see _git_identity); the server treats every one as optional, so an
# older plugin — or a session in a plain directory — simply omits them.
UPLOAD_FIELDS = (
    "session_id", "cwd", "reason", "ended_at",
    "repo", "repo_path", "worktree", "worktree_name", "worktree_path", "git_branch",
)
UPLOAD_TIMEOUT_SECONDS = 60
# The SessionStart hint blocks session startup, so its budget is tight: fail
# open and silent on anything slower.
RECALL_HINT_TIMEOUT_SECONDS = 2
# One `git rev-parse` at session end. Short enough that a hung git (network FS, stale
# lock) costs a blink rather than the hook's whole 10s budget.
GIT_TIMEOUT_SECONDS = 1.0
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


# -------------------------------------------------------------------- tls ----

_SSL_CONTEXT: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """A verifying context that works on Pythons shipped without root CAs.

    python.org macOS installs have zero trusted roots until the user runs
    "Install Certificates.command" — the default context then rejects every
    HTTPS host, which reads as an Annot outage. Verification stays on; we only
    widen where the roots come from: default paths, then certifi, then the CA
    bundle the OS itself ships.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats()["x509_ca"]:
        try:
            import certifi

            ctx.load_verify_locations(certifi.where())
        except (ImportError, OSError, ssl.SSLError):
            for bundle in (
                "/etc/ssl/cert.pem",  # macOS
                "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu/Alpine
                "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Fedora
            ):
                try:
                    ctx.load_verify_locations(bundle)
                    break
                except (OSError, ssl.SSLError):
                    continue
    _SSL_CONTEXT = ctx
    return ctx


def _is_cert_failure(exc: Exception) -> bool:
    return isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError)


# --------------------------------------------------------- repo identity ----

def _git(cwd: str, args: list, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    """`git -C cwd <args>` stdout, or "" on ANY failure — including git not being
    installed. Never raises, never blocks longer than the timeout."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd] + args, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _git_identity(cwd: str, timeout: float = GIT_TIMEOUT_SECONDS) -> dict:
    """Where this session ran: repository, branch, and worktree, or {} when unknown.

    Resolved HERE rather than in the detached uploader because a queued job may not
    upload for minutes (offline retry), by which time a `.claude/worktrees/<slug>`
    directory can already be gone.

    The one thing worth knowing: for a linked worktree `--git-common-dir` points at the
    MAIN repo's .git, while `--show-toplevel` is the worktree's own root. So the real
    repository is dirname(common-dir) — without that, every worktree session files
    itself under a throwaway slug instead of the repo it belongs to.

    Deliberately reads no remote. A git remote is a network identity (often private,
    sometimes carrying a username) and nothing about filing a session in the user's own
    library needs one.

    An empty return is a normal outcome, not an error: git may be absent, and plenty of
    sessions run in directories that are not repositories at all.
    """
    if not cwd or not os.path.isdir(cwd):
        return {}
    # --path-format=absolute (git >= 2.31) is required: without it --git-common-dir can
    # come back relative, and dirname() of a relative path is not the repo root. On an
    # older git the flag errors out, _git returns "", and the server's path heuristic
    # takes over — which is exactly the pre-0.3.0 behavior.
    out = _git(cwd, [
        "rev-parse", "--path-format=absolute",
        "--show-toplevel", "--git-common-dir", "--abbrev-ref", "HEAD",
    ], timeout)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if len(lines) < 3:
        return {}
    toplevel, common_dir, branch = lines[0], lines[1], lines[2]
    root = os.path.dirname(common_dir.rstrip("/")) or toplevel
    ident = {
        "repo": os.path.basename(root.rstrip("/")),
        "repo_path": root,
    }
    if branch and branch != "HEAD":  # "HEAD" means detached: no branch to report
        ident["git_branch"] = branch
    try:
        linked = os.path.realpath(toplevel) != os.path.realpath(root)
    except OSError:
        linked = toplevel.rstrip("/") != root.rstrip("/")
    if linked:
        ident["worktree"] = "1"
        ident["worktree_name"] = os.path.basename(toplevel.rstrip("/"))
        ident["worktree_path"] = toplevel
    return ident


def _recall_query(cwd: str, git_timeout: float) -> str:
    """The code-recall query string for `cwd`, carrying repo identity when git resolves.

    Sending `repo`/`repo_path` is what lets a session started inside a worktree find the
    parent repository's sessions. The server can already infer that for the conventional
    `.claude/worktrees/<slug>` layout from `cwd` alone, so this is an upgrade for
    unconventional layouts rather than a requirement — hence the caller-chosen timeout,
    tight enough for the SessionStart path to stay within its own budget.
    """
    params = {"cwd": cwd}
    ident = _git_identity(cwd, git_timeout)
    for key in ("repo", "repo_path"):
        if ident.get(key):
            params[key] = ident[key]
    return urllib.parse.urlencode(params)


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
    # The one place this file spends more than a few ms (constraint 1 above): up to
    # GIT_TIMEOUT_SECONDS for a single `git rev-parse`. A deliberate exception, kept
    # bounded, fully guarded, and placed AFTER the opt-out checks so an ANNOT_NO_INGEST
    # or deny-path session never forks git at all. It rides in the job file so an
    # offline-queued upload still carries it.
    job.update(_git_identity(cwd))
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

    fields = {name: str(job.get(name) or "") for name in UPLOAD_FIELDS}
    fields["client_version"] = CLIENT_VERSION
    body, content_type = _multipart(
        fields=fields,
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
        with urllib.request.urlopen(
            request, timeout=UPLOAD_TIMEOUT_SECONDS, context=_ssl_context()
        ):
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
        if _is_cert_failure(exc):
            print(
                "Annot: your Python installation has no trusted CA certificates, so "
                "it can't verify any HTTPS server (Annot itself is fine). On macOS "
                "python.org installs, run \"Install Certificates.command\" from the "
                "Python folder in /Applications — or `pip install certifi` — then "
                "run connect again.",
                file=sys.stderr,
            )
            return 1
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
    with urllib.request.urlopen(request, timeout=15, context=_ssl_context()) as response:
        return json.load(response)


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15, context=_ssl_context()) as response:
        return json.load(response)


# ----------------------------------------------------------------- recall ----

def _recall_eligible(cfg: dict, cwd: str) -> bool:
    """Same opt-outs as ingest: unpaired, invalidated, ANNOT_NO_INGEST, deny_paths."""
    if os.environ.get("ANNOT_NO_INGEST"):
        return False
    if not cfg.get("token") or cfg.get("token_invalid"):
        return False
    for deny in cfg.get("deny_paths") or []:
        if isinstance(deny, str) and deny and cwd.startswith(os.path.expanduser(deny)):
            return False
    return True


def _recall_request(cfg: dict, path: str, timeout: float) -> dict:
    request = urllib.request.Request(
        f"{api_url(cfg)}{path}",
        headers={"Authorization": f"Bearer {cfg.get('token')}"},
    )
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
        return json.load(response)


def cmd_session_start() -> int:
    """SessionStart hook. Injects a titles-ONLY hint — content never enters the
    session here; the user picks what to load via /annot:recall. Anything slow or
    broken fails open and silent: a hint is never worth a wait or an error."""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    cwd = event.get("cwd") or os.getcwd()
    cfg = load_config()
    if not _recall_eligible(cfg, cwd):
        return 0
    try:
        data = _recall_request(
            cfg,
            # A third of the hint's own budget for git: startup latency is the whole
            # point of this path's timeouts, and the server's path heuristic covers
            # the conventional worktree layout without it.
            "/v1/ingest/code-recall?" + _recall_query(cwd, RECALL_HINT_TIMEOUT_SECONDS / 3),
            RECALL_HINT_TIMEOUT_SECONDS,
        )
    except Exception:
        return 0
    resources = [r for r in (data.get("resources") or []) if r.get("title")]
    if not resources:
        return 0
    titles = "; ".join(f'"{r["title"]}"' for r in resources)
    context = (
        f"Annot (the user's second brain) has {len(resources)} saved resource(s) "
        f"related to this repo: {titles}. Their content is NOT loaded. If any look "
        "relevant to what the user asks, suggest /annot:recall so the user can pick "
        "which to load — never load them unasked."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )
    return 0


def cmd_recall_list() -> int:
    """The /annot:recall skill's first leg: related resources for cwd, as JSON."""
    cfg = load_config()
    cwd = os.getcwd()
    if not _recall_eligible(cfg, cwd):
        print(json.dumps({"error": "not paired — run /annot:connect first", "resources": []}))
        return 0
    try:
        data = _recall_request(
            cfg, "/v1/ingest/code-recall?" + _recall_query(cwd, GIT_TIMEOUT_SECONDS), 15
        )
    except Exception as exc:
        print(json.dumps({"error": f"could not reach Annot ({exc})", "resources": []}))
        return 0
    if data.get("enabled") is False:
        data = {
            "error": "recall is turned off in Annot → Connections → Claude Code",
            "resources": [],
        }
    print(json.dumps(data))
    return 0


def cmd_recall_get(source_id: str) -> int:
    """The /annot:recall skill's second leg: one picked resource's content."""
    cfg = load_config()
    try:
        data = _recall_request(
            cfg,
            "/v1/ingest/code-recall/" + urllib.parse.quote(source_id.strip()),
            30,
        )
    except urllib.error.HTTPError as exc:
        print(json.dumps({"error": f"resource not available ({exc.code})"}))
        return 0
    except Exception as exc:
        print(json.dumps({"error": f"could not reach Annot ({exc})"}))
        return 0
    print(json.dumps(data))
    return 0


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
    if mode == "session-start":
        try:
            return cmd_session_start()
        except Exception:
            return 0  # a broken hook must never surface into Claude Code
    if mode == "recall-list":
        return cmd_recall_list()
    if mode == "recall-get":
        return cmd_recall_get(sys.argv[2] if len(sys.argv) > 2 else "")
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
