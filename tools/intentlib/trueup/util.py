"""Durable-write, hashing, redaction, and text-safety helpers for the true-up state."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time


class TrueUpError(Exception):
    """Operator-facing failure."""


class FatalStateError(TrueUpError):
    """State cannot be written or recovered; the run must stop and keep prior durable work."""


class SimulatedCrash(BaseException):
    """Raised only by tests via FAULT_HOOK to emulate a process dying at a named point."""


FAULT_HOOK = None


def fault(point):
    if FAULT_HOOK is not None:
        FAULT_HOOK(point)


def now():
    return time.time()


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now() if ts is None else ts))


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def sha(value):
    data = value if isinstance(value, bytes) else dumps(value)
    return hashlib.sha256(data).hexdigest()


def assert_safe_path(path):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise FatalStateError(f"refusing true-up cache access through symlink: {path}")


def atomic_write(path, data):
    """Write bytes durably: temp file in the same directory, fsync, rename, fsync directory."""
    path = Path(path)
    assert_safe_path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            fault("atomic_write:before_rename:" + path.name)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    except OSError as exc:
        raise FatalStateError(f"cannot write true-up state at {path}: {exc}") from exc


def write_json(path, value):
    atomic_write(path, dumps(value))


def read_json(path, default=None):
    path = Path(path)
    assert_safe_path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        if default is not None:
            return default
        raise
    except OSError as exc:
        raise FatalStateError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise TrueUpError(f"corrupt JSON in {path}: {exc}") from exc


def safe_text(raw, limit=None):
    """Decode arbitrary bytes for JSON storage without raising; invalid UTF-8 becomes escapes."""
    if isinstance(raw, str):
        text = raw
    else:
        text = raw.decode("utf-8", errors="backslashreplace")
    if limit is not None and len(text) > limit:
        text = text[:limit]
    return text


def path_text(raw):
    """Display form of a Git path (bytes). Returns (text, lossless)."""
    try:
        return raw.decode("utf-8"), True
    except UnicodeError:
        return raw.decode("utf-8", errors="backslashreplace"), False


_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")

# Conservative credential shapes. Redaction is best effort and never a guarantee of secrecy.
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED:private-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED:github-token]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED:github-token]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"), "[REDACTED:gitlab-token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key-id]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:slack-token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[REDACTED:api-key]"),
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*(?:bearer|basic|token)\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b((?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key)\s*[:=]\s*)(['\"]?)[^\s'\"]{6,}\2"), r"\1[REDACTED]"),
]


_QUERY_SECRET = re.compile(r"(?i)([?&](?:access_token|private_token|token|api_key|apikey|key|password|secret|sig|signature|x-amz-signature|x-amz-credential)=)[^&\s#\"'<>]+")


def strip_url_credentials(text):
    """Drop userinfo from URLs and mask secret-looking query parameters."""
    return _QUERY_SECRET.sub(r"\1REDACTED", _USERINFO.sub(r"\g<scheme>", text))


def redact(text):
    """Return (text, count). Removes URL credentials and common secret shapes."""
    count = 0
    text, n = _USERINFO.subn(r"\g<scheme>[REDACTED]@", text)
    count += n
    text, n = _QUERY_SECRET.subn(r"\1REDACTED", text)
    count += n
    for pattern, replacement in _SECRET_PATTERNS:
        text, n = pattern.subn(replacement, text)
        count += n
    return text, count


def shard(name):
    return name[:2] if len(name) >= 2 else "00"


def normalize_ws(text):
    return " ".join(text.split())
