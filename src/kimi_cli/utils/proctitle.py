from __future__ import annotations

import contextlib
import os
import re
import sys
import threading
from typing import IO

# Maximum length of the topic snippet rendered in the terminal title. Tabs
# are typically narrow; 40 characters fits while still being descriptive.
_TITLE_TOPIC_MAX = 40

# Strip C0 controls (0x00-0x1f), DEL (0x7f), and C1 controls (0x80-0x9f)
# from the OSC payload. Without this, a topic or cwd basename containing
# BEL / ESC / CR / LF could terminate the title sequence early and let an
# attacker inject arbitrary terminal escape codes. All other Unicode
# (Chinese, Japanese, emoji, accented Latin, etc.) is preserved verbatim.
_OSC_CONTROL_BYTES_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize_osc_payload(payload: str) -> str:
    """Strip control bytes that could break out of an OSC title sequence."""
    return _OSC_CONTROL_BYTES_RE.sub("", payload)


# Cached binary handle to the pre-redirect stderr fd. Opened lazily on
# first successful use and held for the process lifetime so we avoid an
# os.dup + fdopen round-trip on every title refresh. The redirector is
# installed after CLI parsing, so early callers (init_process_name) fall
# back to sys.stderr until then.
_original_stderr_lock = threading.Lock()
_original_stderr_handle: IO[bytes] | None = None


def _get_original_stderr_handle() -> IO[bytes] | None:
    """Return a cached, TTY-bound handle to the pre-redirect stderr, or None.

    Returns ``None`` if the stderr redirector has not been installed yet
    (callers should fall back to ``sys.stderr``) or if the original stderr
    fd is not a TTY (callers should no-op rather than emit OSC bytes into
    a log file or pipe). Negative results from "redirector not installed"
    are intentionally not cached so a later call after the redirector is
    installed can still succeed.
    """
    global _original_stderr_handle
    handle = _original_stderr_handle
    if handle is not None:
        return handle
    with _original_stderr_lock:
        if _original_stderr_handle is not None:
            return _original_stderr_handle
        # Lazy import: utils.logging pulls in `kimi_cli.logger`, which
        # touches several utils modules during startup.
        from kimi_cli.utils.logging import get_original_stderr_handle

        try:
            candidate = get_original_stderr_handle()
        except OSError:
            return None
        if candidate is None:
            return None
        try:
            is_tty = os.isatty(candidate.fileno())
        except OSError:
            is_tty = False
        if not is_tty:
            with contextlib.suppress(OSError):
                candidate.close()
            return None
        # Mark the cached fd non-inheritable so subprocesses spawned later
        # (shell commands, MCP servers) do not accidentally inherit a TTY
        # handle they should not own. The redirector returns an
        # inheritable fd by default; flipping the flag here is local to
        # our cached copy.
        with contextlib.suppress(OSError, AttributeError):
            os.set_inheritable(candidate.fileno(), False)
        _original_stderr_handle = candidate
        return candidate


# Maximum length of the short session id rendered in titles. Eight hex chars
# is plenty for visual disambiguation while keeping process titles compact
# (Windows Task Manager and macOS `ps` both truncate aggressively).
_SHORT_SESSION_ID_LEN = 8


def set_process_title(title: str) -> None:
    """Set the OS-level process title visible in ps/top/terminal panels."""
    try:
        import setproctitle

        setproctitle.setproctitle(title)
    except ImportError:
        pass


def set_terminal_title(title: str) -> None:
    """Set the terminal tab/window title via an ANSI OSC escape sequence.

    Prefers the pre-redirect stderr fd (via the logging redirector) so
    that title refreshes keep working after ``redirect_stderr_to_logger``
    swaps fd 2 for a pipe. Falls back to ``sys.stderr`` only when no
    redirector is installed yet (early startup, before CLI fully boots).
    No-ops when no TTY is reachable, so piped output and ``kimi --print``
    never see stray OSC bytes. The composed payload is sanitized at this
    chokepoint to prevent control-byte injection from user-influenced
    topic strings or filesystem-derived cwd basenames.
    """
    payload = _sanitize_osc_payload(title)
    osc = f"\033]0;{payload}\007"

    handle = _get_original_stderr_handle()
    if handle is not None:
        try:
            handle.write(osc.encode("utf-8", errors="replace"))
            handle.flush()
        except OSError:
            pass
        return

    stderr = sys.stderr
    if not stderr.isatty():
        return
    # On legacy Windows code pages or ASCII/C locale, stderr cannot encode
    # arbitrary Unicode (Chinese topic, emoji, etc.) and would otherwise
    # raise UnicodeEncodeError mid-startup. Title updates are best-effort,
    # so degrade gracefully: substitute unencodable characters with "?".
    try:
        encoding = getattr(stderr, "encoding", None) or "utf-8"
        safe = osc.encode(encoding, errors="replace").decode(encoding, errors="replace")
        stderr.write(safe)
        stderr.flush()
    except (OSError, UnicodeError, LookupError):
        pass


def init_process_name(name: str = "Kimi Code") -> None:
    """Initialize process name: OS process title + terminal tab title."""
    set_process_title(name)
    set_terminal_title(name)


def _short_session_id(session_id: str) -> str:
    """Return a short, display-friendly version of a session id.

    Strips dashes (uuid-style) and truncates to ``_SHORT_SESSION_ID_LEN``
    characters so the resulting tag fits comfortably in process titles.
    """
    compact = session_id.replace("-", "")
    return compact[:_SHORT_SESSION_ID_LEN] or session_id


def _sanitize_proctitle_token(value: str) -> str:
    """Make ``value`` safe to embed in a ``key=value`` process-title token.

    Whitespace and ``=`` would break naive split-on-whitespace parsing by
    external observers, so both are replaced with ``_``. All other Unicode,
    including non-ASCII path components, is preserved verbatim.
    """
    chars: list[str] = []
    for ch in value:
        if ch.isspace() or ch == "=":
            chars.append("_")
        else:
            chars.append(ch)
    return "".join(chars)


def compose_session_process_title(
    session_id: str,
    work_dir: str | os.PathLike[str] | None = None,
    *,
    base_name: str = "kimi-code",
) -> str:
    """Compose an OS process title that encodes the live session identity.

    Format: ``"<base> session=<short-id>[ cwd=<basename>]"``.

    External tools (terminal multiplexers, tab managers, IDE integrations)
    can read this from ``ps``/Task Manager and reliably map a running
    process to its session — even when the session was created without an
    explicit ``--session`` flag, which is the common case.

    The ``key=value`` token form is intentional: it parses with simple
    splits and avoids ambiguity if user-facing branding changes. To keep
    that contract intact, whitespace and ``=`` inside the cwd basename are
    replaced with ``_`` via :func:`_sanitize_proctitle_token`; otherwise a
    repository under, say, ``.../John Doe/`` would yield ``cwd=John Doe``
    and break naive token parsing.
    """
    parts: list[str] = [
        base_name,
        f"session={_sanitize_proctitle_token(_short_session_id(session_id))}",
    ]
    if work_dir is not None:
        basename = os.path.basename(os.path.normpath(str(work_dir)))
        if basename:
            parts.append(f"cwd={_sanitize_proctitle_token(basename)}")
    return " ".join(parts)


def set_session_process_title(
    session_id: str,
    work_dir: str | os.PathLike[str] | None = None,
    *,
    base_name: str = "kimi-code",
) -> None:
    """Set the OS process title to encode the live session identity.

    Convenience wrapper around :func:`set_process_title` that uses
    :func:`compose_session_process_title` to build a stable, parseable
    title containing the session id.
    """
    set_process_title(compose_session_process_title(session_id, work_dir, base_name=base_name))


def _truncate_topic(topic: str, max_len: int = _TITLE_TOPIC_MAX) -> str:
    """Collapse whitespace and clamp ``topic`` to ``max_len`` chars."""
    flat = " ".join(topic.split())
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1].rstrip() + "\u2026"


def compose_session_terminal_title(
    work_dir: str | os.PathLike[str] | None = None,
    topic: str | None = None,
    *,
    base_name: str = "Kimi Code",
) -> str:
    """Compose a terminal tab title that conveys live session context.

    Format: ``"<base>[ \u00b7 <topic>][ \u00b7 <cwd-basename>]"``.

    The intent is to mirror what Copilot CLI and Claude Code do — give
    each tab a stable, human-readable identifier so users with many open
    sessions can find the right one at a glance, while still being
    low-frequency (the topic comes from the session's auto-generated /
    user-set title and only changes at well-defined moments).
    """
    parts: list[str] = [base_name]
    if topic:
        clipped = _truncate_topic(topic)
        if clipped:
            parts.append(clipped)
    if work_dir is not None:
        basename = os.path.basename(os.path.normpath(str(work_dir)))
        if basename:
            parts.append(basename)
    return " \u00b7 ".join(parts)


def update_terminal_title_for_session(
    work_dir: str | os.PathLike[str] | None = None,
    topic: str | None = None,
    *,
    base_name: str = "Kimi Code",
) -> None:
    """Refresh the terminal tab/window title for the live session.

    Safe to call repeatedly; no-ops when stderr is not a TTY.
    """
    set_terminal_title(compose_session_terminal_title(work_dir, topic, base_name=base_name))
