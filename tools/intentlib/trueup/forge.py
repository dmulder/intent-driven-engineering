"""GitHub and GitLab evidence: reference extraction, contextual resolution, bounded cached reads.

Network use is an explicit operator choice. Credentials come only from environment variables
named in configuration, are never persisted or logged, and are sent only to the configured API
host of the forge they belong to (never to a link found in repository text, a pagination URL on
another host, or a redirect target on another host).
"""
import ipaddress
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .util import TrueUpError, redact, sha, strip_url_credentials

DEFAULT_LIMITS = {
    "max_depth": 1,                 # follow links found inside fetched resources this many levels
    "max_resources_per_commit": 12,
    "max_comments_per_resource": 100,
    "max_resource_chars": 60000,
    "max_comment_chars": 8000,
    "max_pages": 5,
    "per_page": 100,
    "max_refs_per_commit": 40,
    "max_requests": 0,              # 0 = unlimited
    "max_rate_wait": 30,            # seconds a single rate-limit pause may last before giving up
    "rate_retries": 2,
    "net_retries": 2,
    "timeout": 20,
    "max_response_bytes": 4 * 1024 * 1024,
    "backoff_base": 1.0,
    "association": "auto",          # auto | always | never
}


# ---------------------------------------------------------------------------------------------
# Forge description and detection


def default_api_url(kind, host):
    if kind == "github":
        return "https://api.github.com" if host in ("github.com", "www.github.com") else f"https://{host}/api/v3"
    return f"https://{host}/api/v4"


def make_forge(kind, host, repo, api_url=None, token_env=None, role="primary", source="config", scheme="https"):
    if kind not in ("github", "gitlab"):
        raise TrueUpError(f"unsupported forge kind {kind!r}; use github or gitlab")
    repo = repo.strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    default_token = {"github": "GITHUB_TOKEN", "gitlab": "GITLAB_TOKEN"}[kind]
    return {"kind": kind, "host": host.lower(), "repo": repo, "scheme": scheme,
            "api_url": (api_url or default_api_url(kind, host.lower())).rstrip("/"),
            "token_env": token_env if token_env is not None else default_token, "role": role, "source": source}


def parse_remote_url(url):
    """Return (scheme, host, path) for https/http/ssh/scp-like Git remotes; credentials are dropped."""
    url = url.strip()
    match = re.match(r"^(?P<scheme>[a-z+]+)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+)$", url)
    if match:
        return ("http" if match.group("scheme") == "http" else "https"), match.group("host").lower(), match.group("path")
    match = re.match(r"^(?:[^@/]+@)?(?P<host>[^/:]+):(?P<path>[^/].*)$", url)
    if match:
        return "https", match.group("host").lower(), match.group("path")
    return None


def classify_host(host):
    if host in ("github.com", "www.github.com") or host.startswith("github."):
        return "github"
    if "gitlab" in host:
        return "gitlab"
    return None


def detect_forges(repo):
    """Forges from remotes. First remote (origin if present) is primary; others are aliases."""
    raw = repo.run("config", "--get-regexp", r"^remote\..*\.url$", ok=(0, 1)).decode("utf-8", "replace")
    remotes = []
    for line in raw.splitlines():
        key, _, url = line.partition(" ")
        name = key[len("remote."):-len(".url")]
        remotes.append((name, url))
    remotes.sort(key=lambda r: (r[0] != "origin", r[0]))
    forges, notes, seen = [], [], set()
    for name, url in remotes:
        parsed = parse_remote_url(url)
        if not parsed:
            notes.append(f"remote {name}: URL form not recognised (local path or unsupported scheme)")
            continue
        scheme, host, path = parsed
        kind = classify_host(host)
        if not kind:
            notes.append(f"remote {name}: host {host} is not recognised as GitHub or GitLab; configure it with --forge")
            continue
        forge = make_forge(kind, host, path, role="primary" if not forges else "alias", source=f"remote:{name}")
        key = (forge["kind"], forge["host"], forge["repo"])
        if key not in seen:
            seen.add(key)
            forges.append(forge)
    return forges, notes


def parse_forge_spec(spec):
    """KIND:REPO_URL[,api=URL][,token-env=NAME][,alias]  e.g. gitlab:https://git.example/g/p,api=https://git.example/api/v4"""
    head, *options = spec.split(",")
    kind, sep, url = head.partition(":")
    if not sep:
        raise TrueUpError(f"invalid --forge {spec!r}; expected KIND:REPO_URL[,api=URL][,token-env=NAME][,alias]")
    parsed = parse_remote_url(url)
    if not parsed:
        raise TrueUpError(f"invalid forge repository URL {url!r}")
    scheme = "http" if url.startswith("http://") else "https"
    api, token_env, role = None, None, "primary"
    for option in options:
        if option == "alias":
            role = "alias"
        elif option.startswith("api="):
            api = option[4:]
        elif option.startswith("token-env="):
            token_env = option[len("token-env="):]
        elif option:
            raise TrueUpError(f"unknown --forge option {option!r}")
    return make_forge(kind, parsed[1], parsed[2], api_url=api, token_env=token_env, role=role, source="config", scheme=scheme)


# ---------------------------------------------------------------------------------------------
# Reference extraction (pure text, no network)

_URL = re.compile(r"https?://[^\s<>\"'`]+")
_CROSS = re.compile(r"(?<![\w/.:@-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)([#!])(\d{1,9})\b")
_HASH = re.compile(r"(?<![\w&/#.@-])#(\d{1,9})\b")
_BANG = re.compile(r"(?<![\w!/.@-])!(\d{1,9})\b")
_GH = re.compile(r"(?<![\w-])GH-(\d{1,9})\b")
_CLOSING = re.compile(r"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\b[:\s]+$")


def _clean_url(url):
    return url.rstrip(".,;:!?)]}>")


def extract_references(text, source, forges=(), max_refs=40, allow_bare=True):
    """Find issue/PR/MR references. Bare `#N` is deliberately not assumed to be a PR or local."""
    refs, seen = [], set()

    def add(ref):
        key = (ref["form"], ref.get("host"), ref.get("repo"), ref["number"], ref.get("kind_hint"))
        if key in seen or len(refs) >= max_refs:
            return
        seen.add(key)
        refs.append({**ref, "source": source})

    consumed = []
    for match in _URL.finditer(text):
        raw = _clean_url(match.group(0))
        consumed.append((match.start(), match.end()))
        safe = strip_url_credentials(raw)
        parsed = urllib.parse.urlparse(safe)
        host = (parsed.hostname or "").lower()
        path = parsed.path
        found = None
        gh = re.match(r"^/([^/]+)/([^/]+)/(issues|pull)/(\d+)(?:/|$)", path)
        gl = re.match(r"^/(.+?)/(?:-/)?(issues|merge_requests)/(\d+)(?:/|$)", path)
        known_kind = classify_host(host) or next((f["kind"] for f in forges if f["host"] == host), None)
        if gh and known_kind != "gitlab":
            found = {"repo": f"{gh.group(1)}/{gh.group(2)}", "number": int(gh.group(4)),
                     "kind_hint": "issue" if gh.group(3) == "issues" else "pull", "forge_kind": "github"}
        elif gl and known_kind != "github":
            found = {"repo": gl.group(1), "number": int(gl.group(3)),
                     "kind_hint": "issue" if gl.group(2) == "issues" else "merge_request", "forge_kind": "gitlab"}
        if found:
            add({"raw": safe, "form": "url", "host": host, **found, "closing": False})
        else:
            add({"raw": safe, "form": "external-url", "host": host, "repo": None, "number": None, "kind_hint": None,
                 "closing": False})
    masked = list(text)
    for a, z in consumed:
        masked[a:z] = " " * (z - a)
    text = "".join(masked)
    for match in _CROSS.finditer(text):
        repo, mark, number = match.groups()
        # `a/b#1` could also be a path fragment; keep it, but record the form so resolution stays honest.
        add({"raw": match.group(0), "form": "cross-repo", "host": None, "repo": repo, "number": int(number),
             "kind_hint": "merge_request" if mark == "!" else "issue_or_pull",
             "closing": bool(_CLOSING.search(text[max(0, match.start() - 24):match.start()]))})
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    text = "".join(masked)
    if allow_bare:
        for match in _HASH.finditer(text):
            add({"raw": match.group(0), "form": "hash", "host": None, "repo": None, "number": int(match.group(1)),
                 "kind_hint": "issue_or_pull",
                 "closing": bool(_CLOSING.search(text[max(0, match.start() - 24):match.start()]))})
        for match in _GH.finditer(text):
            add({"raw": match.group(0), "form": "gh-prefix", "host": None, "repo": None, "number": int(match.group(1)),
                 "kind_hint": "issue_or_pull", "closing": False})
        for match in _BANG.finditer(text):
            add({"raw": match.group(0), "form": "bang", "host": None, "repo": None, "number": int(match.group(1)),
                 "kind_hint": "merge_request", "closing": False})
    return refs


def resource_key(forge, number, kind_hint):
    if forge["kind"] == "gitlab" and kind_hint == "merge_request":
        return f"gitlab:{forge['host']}/{forge['repo']}!{number}"
    return f"{forge['kind']}:{forge['host']}/{forge['repo']}#{number}"


def web_url(forge, number, type_):
    base = f"{forge['scheme']}://{forge['host']}/{forge['repo']}"
    if forge["kind"] == "github":
        return f"{base}/{'pull' if type_ == 'pull_request' else 'issues'}/{number}"
    return f"{base}/-/{'merge_requests' if type_ == 'merge_request' else 'issues'}/{number}"


def resolve_reference(ref, forges):
    """Map a reference to candidate (forge, number, kind_hint) lookups using its actual context.

    Returns {'status': 'candidates'|'unresolvable', 'candidates': [...], 'reason': str}.
    A bare `#N` is never assumed to be a PR, and with several configured repositories every
    candidate is tried in order (primary first) so migrated repositories can still resolve.
    """
    if ref["form"] == "external-url":
        return {"status": "unresolvable", "candidates": [], "reason": f"host {ref['host']} is not a recognised forge link"}
    if ref["form"] == "url":
        forge = next((f for f in forges if f["host"] == ref["host"] and f["kind"] == ref["forge_kind"]), None)
        if forge is None:
            if classify_host(ref["host"]) == ref["forge_kind"] or ref["forge_kind"] == "github" and ref["host"] == "github.com":
                forge = make_forge(ref["forge_kind"], ref["host"], ref["repo"], token_env="", role="alias", source="link")  # unconfigured host: anonymous only
            else:
                return {"status": "unresolvable", "candidates": [],
                        "reason": f"host {ref['host']} is not configured as a {ref['forge_kind']} forge (add --forge)"}
        forge = {**forge, "repo": ref["repo"]}
        return {"status": "candidates", "candidates": [{"forge": forge, "number": ref["number"], "kind_hint": ref["kind_hint"]}], "reason": ""}
    if not forges:
        return {"status": "unresolvable", "candidates": [], "reason": "no forge configured for this repository; reference left unresolved"}
    if ref["form"] == "cross-repo":
        out = []
        for forge in forges:
            if ref["kind_hint"] == "merge_request" and forge["kind"] != "gitlab":
                continue
            out.append({"forge": {**forge, "repo": ref["repo"]}, "number": ref["number"], "kind_hint": ref["kind_hint"]})
        # Deduplicate by host/kind; the repository was named explicitly.
        uniq, seen = [], set()
        for c in out:
            k = (c["forge"]["kind"], c["forge"]["host"])
            if k not in seen:
                seen.add(k)
                uniq.append(c)
        return {"status": "candidates" if uniq else "unresolvable", "candidates": uniq,
                "reason": "" if uniq else "reference syntax does not match any configured forge"}
    out = []
    for forge in forges:
        if ref["form"] == "bang" and forge["kind"] != "gitlab":
            continue
        if ref["form"] == "gh-prefix" and forge["kind"] != "github":
            continue
        out.append({"forge": forge, "number": ref["number"], "kind_hint": ref["kind_hint"]})
    return {"status": "candidates" if out else "unresolvable", "candidates": out,
            "reason": "" if out else f"{ref['raw']} syntax does not match the configured forge type"}


# ---------------------------------------------------------------------------------------------
# HTTP


class ForgeError(Exception):
    retryable = False
    status_name = "error"


class NotFound(ForgeError):
    status_name = "missing"


class Inaccessible(ForgeError):
    status_name = "inaccessible"


class RateLimited(ForgeError):
    status_name = "rate_limited"
    retryable = True

    def __init__(self, message, until=None):
        super().__init__(message)
        self.until = until


class NetworkError(ForgeError):
    retryable = True


class ForgeUnavailable(ForgeError):
    retryable = True
    status_name = "rate_limited"


class BadResponse(ForgeError):
    status_name = "error"


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, {k.lower(): v for k, v in headers.items()}, body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class UrllibTransport:
    """urllib transport that never follows redirects itself (the client decides, per host)."""

    def __init__(self):
        self.opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method, url, headers, timeout, max_bytes):
        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return Response(response.status, dict(response.headers), response.read(max_bytes + 1)[:max_bytes])
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(max_bytes)
            except Exception:
                body = b""
            return Response(exc.code, dict(exc.headers or {}), body)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            raise NetworkError(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from exc


def is_loopback(host):
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Budget:
    def __init__(self, limit=0):
        self.limit, self.used, self._lock = limit, 0, threading.Lock()

    def take(self):
        with self._lock:
            if self.limit and self.used >= self.limit:
                raise ForgeUnavailable("forge request budget exhausted")
            self.used += 1


class ForgeClient:
    def __init__(self, forge, transport=None, limits=None, env=None, sleep=time.sleep, clock=time.time,
                 budget=None, allow_insecure=False, circuit=None):
        self.forge = forge
        self.transport = transport or UrllibTransport()
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.env = os.environ if env is None else env
        self.sleep, self.clock = sleep, clock
        self.budget = budget or Budget(self.limits["max_requests"])
        self.allow_insecure = allow_insecure
        parsed = urllib.parse.urlparse(forge["api_url"])
        self.api_host, self.api_scheme, self.api_port = parsed.hostname, parsed.scheme, parsed.port
        self.circuit = circuit if circuit is not None else {}
        self._lock = threading.Lock()
        self.requests = 0

    def _token(self):
        name = self.forge.get("token_env")
        token = self.env.get(name) if name else None
        return token.strip() if token else None

    def _same_origin(self, url):
        p = urllib.parse.urlparse(url)
        return p.hostname == self.api_host and p.scheme == self.api_scheme and p.port == self.api_port

    def _headers(self, url):
        headers = {"Accept": "application/vnd.github+json" if self.forge["kind"] == "github" else "application/json",
                   "User-Agent": "intentctl-trueup/1"}
        token = self._token()
        if token and self._same_origin(url) and (self.api_scheme == "https" or is_loopback(self.api_host) or self.allow_insecure):
            if self.forge["kind"] == "github":
                headers["Authorization"] = f"Bearer {token}"
            else:
                headers["PRIVATE-TOKEN"] = token
        return headers

    def _check_circuit(self):
        until = self.circuit.get("until")
        if until and until > self.clock():
            raise ForgeUnavailable(f"{self.forge['host']} unavailable until {int(until)} ({self.circuit.get('reason')})")

    def _trip(self, until, reason):
        with self._lock:
            self.circuit.update({"until": until, "reason": reason})

    def request(self, url):
        """GET one URL with bounded retries; returns Response with a 2xx status."""
        if not self._same_origin(url):
            raise BadResponse(f"refusing to send request to unexpected origin {urllib.parse.urlparse(url).hostname}")
        redirects = 0
        net_attempts = rate_attempts = 0
        while True:
            self._check_circuit()
            self.budget.take()
            self.requests += 1
            try:
                response = self.transport.request("GET", url, self._headers(url), self.limits["timeout"], self.limits["max_response_bytes"])
                self.circuit["failures"] = 0
            except NetworkError as exc:
                net_attempts += 1
                self.circuit["failures"] = self.circuit.get("failures", 0) + 1
                if self.circuit["failures"] >= 3:
                    self._trip(self.clock() + 120, "repeated network failures")
                if net_attempts > self.limits["net_retries"]:
                    raise
                self.sleep(self.limits["backoff_base"] * (2 ** (net_attempts - 1)))
                continue
            status = response.status
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                target = urllib.parse.urljoin(url, location)
                redirects += 1
                if redirects > 3 or not self._same_origin(target):
                    raise BadResponse("redirect to another origin refused" if not self._same_origin(target) else "too many redirects")
                url = target
                continue
            if 200 <= status < 300:
                return response
            body_text = response.body[:2000].decode("utf-8", "replace").lower()
            remaining = response.headers.get("x-ratelimit-remaining") or response.headers.get("ratelimit-remaining")
            if status == 429 or (status == 403 and (remaining == "0" or "rate limit" in body_text or "retry-after" in response.headers)):
                wait = self._rate_wait(response)
                rate_attempts += 1
                if wait <= self.limits["max_rate_wait"] and rate_attempts <= self.limits["rate_retries"]:
                    self.sleep(wait)
                    continue
                until = self.clock() + max(wait, 1)
                self._trip(until, "rate limited")
                raise RateLimited(f"rate limited by {self.forge['host']} (retry in ~{int(wait)}s)", until=until)
            if status == 404 or status == 410:
                raise NotFound(f"{status} for {urllib.parse.urlparse(url).path}")
            if status in (401, 403, 451):
                raise Inaccessible(f"{status}: access denied or private")
            if 500 <= status < 600:
                net_attempts += 1
                if net_attempts > self.limits["net_retries"]:
                    raise NetworkError(f"server error {status}")
                self.sleep(self.limits["backoff_base"] * (2 ** (net_attempts - 1)))
                continue
            raise BadResponse(f"unexpected status {status}")

    def _rate_wait(self, response):
        retry = response.headers.get("retry-after")
        if retry and retry.strip().isdigit():
            return float(retry)
        reset = response.headers.get("x-ratelimit-reset") or response.headers.get("ratelimit-reset")
        if reset and reset.strip().isdigit():
            return max(0.0, float(reset) - self.clock())
        return 60.0

    def get_json(self, path, params=None):
        url = self.forge["api_url"] + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        response = self.request(url)
        try:
            return json.loads(response.body.decode("utf-8")), response
        except (ValueError, UnicodeError) as exc:
            raise BadResponse(f"invalid JSON from {self.forge['host']}") from exc

    def paginate(self, path, params=None, max_items=100):
        """Collect a list endpoint following Link/X-Next-Page; flags truncation instead of looping forever."""
        params = {**(params or {}), "per_page": self.limits["per_page"]}
        url = self.forge["api_url"] + path + "?" + urllib.parse.urlencode(params)
        items, pages = [], 0
        while url:
            data, response = self.get_json_url(url)
            if not isinstance(data, list):
                raise BadResponse("expected a JSON list")
            items.extend(data)
            pages += 1
            nxt = _next_link(response)
            if nxt is None and response.headers.get("x-next-page", "").strip().isdigit():
                nxt = self.forge["api_url"] + path + "?" + urllib.parse.urlencode({**params, "page": response.headers["x-next-page"].strip()})
            if nxt:
                nxt = urllib.parse.urljoin(url, nxt)
                if not self._same_origin(nxt):
                    return items[:max_items], True, "pagination link pointed to another origin and was not followed"
            if len(items) >= max_items:
                return items[:max_items], bool(nxt) or len(items) > max_items, None
            if nxt and pages >= self.limits["max_pages"]:
                return items, True, f"stopped after {pages} pages"
            url = nxt
        return items, False, None

    def get_json_url(self, url):
        response = self.request(url)
        try:
            return json.loads(response.body.decode("utf-8")), response
        except (ValueError, UnicodeError) as exc:
            raise BadResponse(f"invalid JSON from {self.forge['host']}") from exc


def _next_link(response):
    header = response.headers.get("link")
    if not header:
        return None
    for part in header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="?next"?', part)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------------------------
# Resource fetchers


def _bounded(text, limit):
    text = text or ""
    text, _ = redact(strip_url_credentials(text))
    return (text[:limit], len(text) > limit)


def _comment(kind, cid, author, created, body, url, limits, path=None, line=None):
    text, cut = _bounded(body, limits["max_comment_chars"])
    return {"id": str(cid), "kind": kind, "author": author, "created_at": created, "body": text, "url": url,
            "path": path, "line": line, "truncated": cut}


def fetch_resource(client, candidate, limits):
    """Fetch one issue/PR/MR plus discussion. Raises ForgeError only when the primary object fails."""
    forge, number, hint = candidate["forge"], candidate["number"], candidate["kind_hint"]
    repo = forge["repo"]
    out = {"key": resource_key(forge, number, hint), "forge": forge["kind"], "host": forge["host"], "repo": repo,
           "number": number, "comments": [], "limits": {"incomplete": []}}
    if forge["kind"] == "github":
        main, _ = client.get_json(f"/repos/{repo}/issues/{number}")
        is_pr = "pull_request" in main
        out["type"] = "pull_request" if is_pr else "issue"
        out.update(title=main.get("title"), state=main.get("state"), author=(main.get("user") or {}).get("login"),
                   created_at=main.get("created_at"), url=main.get("html_url") or web_url(forge, number, out["type"]))
        out["body"], cut = _bounded(main.get("body"), limits["max_resource_chars"])
        if cut:
            out["limits"]["incomplete"].append("description truncated")
        endpoints = [("issue-comment", f"/repos/{repo}/issues/{number}/comments")]
        if is_pr:
            try:
                pr, _ = client.get_json(f"/repos/{repo}/pulls/{number}")
                out["merged"] = bool(pr.get("merged"))
                out["merge_commit_sha"] = pr.get("merge_commit_sha")
            except ForgeError as exc:
                out["limits"]["incomplete"].append(f"pull request details unavailable: {exc}")
                out["limits"]["retryable"] = out["limits"].get("retryable", False) or exc.retryable
            endpoints += [("review", f"/repos/{repo}/pulls/{number}/reviews"),
                          ("review-comment", f"/repos/{repo}/pulls/{number}/comments")]
        for kind, path in endpoints:
            try:
                items, truncated, note = client.paginate(path, max_items=limits["max_comments_per_resource"])
            except ForgeError as exc:
                out["limits"]["incomplete"].append(f"{kind}s not retrieved: {exc}")
                out["limits"]["retryable"] = out["limits"].get("retryable", False) or exc.retryable
                continue
            if truncated:
                out["limits"]["incomplete"].append(f"{kind}s truncated" + (f" ({note})" if note else ""))
            for item in items:
                body = item.get("body")
                if kind == "review" and not body:
                    continue
                out["comments"].append(_comment(kind, item.get("id"), (item.get("user") or {}).get("login"),
                                                item.get("created_at") or item.get("submitted_at"), body,
                                                item.get("html_url"), limits, path=item.get("path"),
                                                line=item.get("line") or item.get("original_line")))
    else:
        project = urllib.parse.quote(repo, safe="")
        is_mr = hint == "merge_request"
        base = f"/projects/{project}/{'merge_requests' if is_mr else 'issues'}/{number}"
        main, _ = client.get_json(base)
        out["type"] = "merge_request" if is_mr else "issue"
        out.update(title=main.get("title"), state=main.get("state"), author=(main.get("author") or {}).get("username"),
                   created_at=main.get("created_at"), url=main.get("web_url") or web_url(forge, number, out["type"]))
        out["body"], cut = _bounded(main.get("description"), limits["max_resource_chars"])
        if cut:
            out["limits"]["incomplete"].append("description truncated")
        if is_mr:
            out["merge_commit_sha"] = main.get("merge_commit_sha") or main.get("squash_commit_sha")
        try:
            items, truncated, note = client.paginate(base + "/notes", {"sort": "asc"}, max_items=limits["max_comments_per_resource"])
            if truncated:
                out["limits"]["incomplete"].append("notes truncated" + (f" ({note})" if note else ""))
            for item in items:
                if item.get("system"):
                    continue
                out["comments"].append(_comment("note", item.get("id"), (item.get("author") or {}).get("username"),
                                                item.get("created_at"), item.get("body"), None, limits,
                                                path=((item.get("position") or {}).get("new_path")),
                                                line=((item.get("position") or {}).get("new_line"))))
        except ForgeError as exc:
            out["limits"]["incomplete"].append(f"notes not retrieved: {exc}")
            out["limits"]["retryable"] = out["limits"].get("retryable", False) or exc.retryable
    total = len(out["body"] or "")
    kept = []
    for comment in out["comments"]:
        total += len(comment["body"])
        if total > limits["max_resource_chars"]:
            out["limits"]["incomplete"].append("discussion truncated at the per-resource character limit")
            break
        if comment.get("truncated"):
            out["limits"]["incomplete"].append("comment truncated at the per-comment character limit")
        kept.append(comment)
    out["comments"] = kept
    out["status"] = "partial" if out["limits"]["incomplete"] else "ok"
    return out


def associated_resources(client, forge, sha_):
    """Forge-side associations for a commit that the commit message does not state."""
    repo = forge["repo"]
    if forge["kind"] == "github":
        items, truncated, _ = client.paginate(f"/repos/{repo}/commits/{sha_}/pulls", max_items=20)
        return [{"forge": forge, "number": item["number"], "kind_hint": "pull"} for item in items if "number" in item], truncated
    items, truncated, _ = client.paginate(f"/projects/{urllib.parse.quote(repo, safe='')}/repository/commits/{sha_}/merge_requests", max_items=20)
    return [{"forge": forge, "number": item["iid"], "kind_hint": "merge_request"} for item in items if "iid" in item], truncated


class ForgeEvidence:
    """Per-store cache and resolver. Thread safe; each resource is fetched at most once per store."""

    def __init__(self, store, forges, limits=None, transport=None, env=None, sleep=time.sleep, clock=time.time,
                 allow_insecure=False, enabled=True, circuits=None):
        self.store, self.forges = store, list(forges)
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.transport, self.env, self.sleep, self.clock = transport, env, sleep, clock
        self.allow_insecure, self.enabled = allow_insecure, enabled
        self.budget = Budget(self.limits["max_requests"])
        self.circuits = circuits if circuits is not None else {}
        self._clients, self._locks, self._memo = {}, {}, {}
        self._guard = threading.Lock()
        self.stats = {"requests": 0, "cache_hits": 0, "fetched": 0}

    def client(self, forge):
        key = (forge["kind"], forge["host"], forge["api_url"])
        with self._guard:
            if key not in self._clients:
                circuit = self.circuits.setdefault(f"{forge['kind']}:{forge['host']}", {})
                self._clients[key] = ForgeClient({**forge}, self.transport, self.limits, self.env, self.sleep, self.clock,
                                                 self.budget, self.allow_insecure, circuit)
            return self._clients[key]

    def _lock_for(self, key):
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _cache_path(self, key):
        return sha(key.encode())[:40]

    def lookup(self, candidates):
        """Resolve one reference's candidates in order. Returns (resource or None, attempts, resolving forge or None)."""
        attempts = []
        for candidate in candidates:
            key = resource_key(candidate["forge"], candidate["number"], candidate["kind_hint"])
            resource = self._get(key, candidate)
            attempts.append({"key": key, "status": resource["status"], "error": resource.get("error")})
            if resource["status"] in ("ok", "partial"):
                resource = {**resource, "resolved_via_alias": candidate["forge"].get("role") == "alias" or candidate is not candidates[0]}
                return resource, attempts, candidate["forge"]
            if resource["status"] in ("rate_limited", "error"):
                return resource, attempts, candidate["forge"]  # do not mask a retryable failure by trying aliases
        return (resource if candidates else None), attempts, None

    def _get(self, key, candidate):
        with self._lock_for(key):
            if key in self._memo:
                self.stats["cache_hits"] += 1
                return self._memo[key]
            cached = self.store.get("resources", self._cache_path(key))
            if (cached and cached["status"] in ("ok", "partial", "missing", "inaccessible")
                    and not cached.get("limits", {}).get("retryable")):
                self.stats["cache_hits"] += 1
                self._memo[key] = cached
                return cached
            resource = self._fetch(key, candidate)
            self._memo[key] = resource
            self.store.put("resources", self._cache_path(key), resource)
            return resource

    def _fetch(self, key, candidate):
        forge = candidate["forge"]
        base = {"key": key, "forge": forge["kind"], "host": forge["host"], "repo": forge["repo"], "number": candidate["number"],
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())), "comments": [], "limits": {"incomplete": []}}
        if not self.enabled:
            return {**base, "status": "error", "error": "network access disabled"}
        client = self.client(forge)
        try:
            resource = fetch_resource(client, candidate, self.limits)
            resource["fetched_at"] = base["fetched_at"]
            self.stats["fetched"] += 1
            return resource
        except ForgeError as exc:
            return {**base, "status": exc.status_name, "error": str(exc)}
        except Exception as exc:  # a malformed payload must not abort the run
            return {**base, "status": "error", "error": f"unexpected {type(exc).__name__}: {exc}"}

    def collect(self, sha_, message, diff_text, depth=None):
        """Evidence for one commit: references, optional forge association, and fetched resources."""
        out = {"references": [], "resources": [], "limitations": [], "status": "not_requested"}
        refs = extract_references(message, "commit-message", self.forges, self.limits["max_refs_per_commit"])
        refs += extract_references(diff_text or "", "diff", self.forges, self.limits["max_refs_per_commit"], allow_bare=False)
        if not self.enabled:
            out["references"] = [{**r, "resolution": "not attempted (network off)"} for r in refs]
            return out
        out["status"] = "none_found"
        fetched = {}
        failures = ok = 0
        order = [(r, 0) for r in refs]
        has_explicit = any(r["form"] != "external-url" and r.get("number") for r in refs)
        mode = self.limits["association"]
        if self.forges and (mode == "always" or (mode == "auto" and not has_explicit)):
            primary = self.forges[0]
            try:
                assoc, truncated = associated_resources(self.client(primary), primary, sha_)
                if truncated:
                    out["limitations"].append("forge association list truncated")
                for c in assoc:
                    order.append(({"raw": f"association:{c['kind_hint']}#{c['number']}", "form": "association", "number": c["number"],
                                   "kind_hint": c["kind_hint"], "repo": primary["repo"], "host": primary["host"], "source": "forge-association",
                                   "closing": False, "_candidates": [c]}, 0))
            except NotFound:
                out["limitations"].append("forge association: commit unknown to forge (not pushed, rewritten, or migrated)")
            except ForgeError as exc:
                out["limitations"].append(f"forge association unavailable: {exc}")
                failures += 1
        max_depth = self.limits["max_depth"] if depth is None else depth
        while order and len(fetched) < self.limits["max_resources_per_commit"]:
            ref, level = order.pop(0)
            record = {k: v for k, v in ref.items() if k != "_candidates"}
            if "_candidates" in ref:
                candidates, reason = ref["_candidates"], ""
            else:
                resolution = resolve_reference(ref, self.forges)
                candidates, reason = resolution["candidates"], resolution["reason"]
            if not candidates:
                record.update(resolution="unresolved", reason=reason)
                out["references"].append(record)
                if ref["form"] != "external-url":
                    failures += 1
                continue
            resource, attempts, resolved_forge = self.lookup(candidates)
            record.update(attempted=attempts)
            if resource is None or resource["status"] not in ("ok", "partial"):
                record.update(resolution=resource["status"] if resource else "unresolved", reason=(resource or {}).get("error"))
                out["references"].append(record)
                failures += 1
                continue
            record.update(resolution="resolved", resource=resource["key"], resource_type=resource.get("type"),
                          resolved_via_alias=resource.get("resolved_via_alias", False))
            out["references"].append(record)
            if resource["key"] in fetched:
                record["duplicate"] = True
                continue
            fetched[resource["key"]] = resource
            ok += 1
            if resource["status"] == "partial":
                out["limitations"].append(f"{resource['key']}: " + "; ".join(resource["limits"]["incomplete"]))
            if level < max_depth:
                text = (resource.get("body") or "") + "\n" + "\n".join(c["body"] for c in resource["comments"])
                forge_context = {**resolved_forge, "role": "primary", "source": "resource"}
                for linked in extract_references(text, f"resource:{resource['key']}", [forge_context] + self.forges,
                                                 self.limits["max_refs_per_commit"]):
                    if linked["form"] in ("hash", "gh-prefix", "bang") and linked.get("number"):
                        resolution = resolve_reference(linked, [forge_context])
                        if resolution["candidates"]:
                            order.append(({**linked, "_candidates": resolution["candidates"]}, level + 1))
                    elif linked["form"] in ("url", "cross-repo"):
                        order.append((linked, level + 1))
        if order:
            out["limitations"].append(f"stopped at {self.limits['max_resources_per_commit']} resources for this commit; "
                                      f"{len(order)} reference(s) not followed")
        out["resources"] = sorted(fetched)
        if not ok and not failures:
            out["status"] = "none_found"
        elif failures or out["limitations"]:
            out["status"] = "partial" if ok else "failed"
        else:
            out["status"] = "complete"
        return out

    def load(self, key):
        return self.store.get("resources", self._cache_path(key))
