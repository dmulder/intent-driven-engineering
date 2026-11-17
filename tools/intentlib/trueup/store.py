"""Durable true-up state: one directory, one writer, atomic per-record files.

Layout (all JSON, relative to the state directory):

  meta.json                   format version, store id, repository identity (informational)
  config.json                 settings recorded at init (never credentials)
  manifests/NNNNNN.json       frozen, ordered commit set per generation (immutable once written)
  commits/xx/SHA.json         per-commit outcome record (mutable, replaced atomically)
  chunks/xx/SHA/DIGEST.json   per-chunk model results, reused on resume (immutable)
  attempts/xx/SHA/N.json      composed analysis attempts (immutable; reanalysis appends)
  links/xx/SHA.json           graph-linking decisions for a commit
  resources/xx/KEY.json       cached forge resources
  graph/                      derived intent graph (rebuildable from attempts + links)
  circuits.json               provider/forge circuit-breaker state
  runs/RUNID.json             one record per `run` invocation
  events.jsonl                append-only audit log
  reviews/, promotions/       maintainer decisions about candidates (local, not canonical)
"""
import json
import os
from pathlib import Path
import threading

from . import STATE_FORMAT
from .util import FatalStateError, TrueUpError, dumps, iso, read_json, shard, sha, write_json

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

STATE_DIRNAME = "intent-trueup"


def default_state_dir(repo):
    return Path(repo.common_dir) / STATE_DIRNAME


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self._lock_fd = None
        self._events_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------------------------
    @classmethod
    def exists(cls, path):
        return (Path(path) / "meta.json").exists()

    @classmethod
    def create(cls, path, repo, config):
        store = cls(path)
        if cls.exists(path):
            raise TrueUpError(f"a true-up store already exists at {path}; use `trueup extend` or choose --state-dir")
        try:
            store.path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FatalStateError(f"cannot create state directory {path}: {exc}") from exc
        store.acquire()
        if cls.exists(path):
            store.release()
            raise TrueUpError(f"a true-up store already exists at {path}; use `trueup extend` or choose --state-dir")
        write_json(store.path / "config.json", config)
        write_json(store.path / "meta.json", {
            "format": STATE_FORMAT, "kind": "intent-trueup-store", "store_id": sha({"path": str(path), "at": iso(), "pid": os.getpid()})[:16],
            "created_at": iso(), "repository": {"toplevel": str(repo.toplevel) if repo.toplevel else None,
                                                 "git_dir": str(repo.common_dir)},
            "authority": "non-authoritative; never read by canonical validation"})
        return store

    @classmethod
    def open(cls, path):
        store = cls(path)
        if not cls.exists(path):
            raise TrueUpError(f"no true-up store at {path}; run `intentctl trueup init` first")
        meta = read_json(store.path / "meta.json")
        if meta.get("format") != STATE_FORMAT:
            raise FatalStateError(f"true-up state format {meta.get('format')} is not supported by this tool (expects {STATE_FORMAT})")
        return store

    def acquire(self):
        """Exclusive writer lock. Held until release(); a second writer fails fast."""
        if self._lock_fd is not None:
            return
        self.path.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"pid={os.getpid()}\n".encode())
        except OSError:
            holder = ""
            try:
                holder = (self.path / "lock").read_text().strip()
            except OSError:
                pass
            os.close(descriptor)
            raise TrueUpError(f"another true-up process holds the writer lock on {self.path} ({holder or 'unknown holder'})")
        self._lock_fd = descriptor

    def release(self):
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            finally:
                self._lock_fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()

    # -- generic sharded records -----------------------------------------------------------
    def get(self, kind, key, default=None):
        path = self.path / kind / shard(key) / (key + ".json")
        return read_json(path) if path.exists() else default

    def put(self, kind, key, value):
        write_json(self.path / kind / shard(key) / (key + ".json"), value)

    def put_nested(self, kind, key, name, value):
        write_json(self.path / kind / shard(key) / key / (name + ".json"), value)

    def get_nested(self, kind, key, name):
        path = self.path / kind / shard(key) / key / (name + ".json")
        return read_json(path) if path.exists() else None

    def list_nested(self, kind, key):
        directory = self.path / kind / shard(key) / key
        if not directory.exists():
            return []
        return sorted(p.stem for p in directory.glob("*.json"))

    # -- configuration and manifests -------------------------------------------------------
    def config(self):
        return read_json(self.path / "config.json")

    def write_config(self, config):
        write_json(self.path / "config.json", config)

    def meta(self):
        return read_json(self.path / "meta.json")

    def generations(self):
        directory = self.path / "manifests"
        return sorted(directory.glob("*.json")) if directory.exists() else []

    def write_manifest(self, manifest):
        generation = len(self.generations()) + 1
        manifest = {**manifest, "generation": generation, "created_at": iso()}
        manifest["digest"] = sha([c["sha"] for c in manifest["commits"]] + [manifest.get("previous_digest")])
        path = self.path / "manifests" / f"{generation:06d}.json"
        if path.exists():
            raise FatalStateError(f"manifest generation {generation} already exists")
        write_json(path, manifest)
        return manifest

    def manifest(self):
        """Merged view across generations: ordered commits, latest scope/coverage."""
        files = self.generations()
        if not files:
            raise FatalStateError("true-up store has no manifest; the state is incomplete (re-run `trueup init` in a new --state-dir)")
        commits, generations, last = [], [], None
        for file in files:
            doc = read_json(file)
            last = doc
            for item in doc["commits"]:
                item = {**item, "generation": doc["generation"]}
                commits.append(item)
            generations.append({"generation": doc["generation"], "created_at": doc["created_at"], "digest": doc["digest"],
                                "count": len(doc["commits"]), "scope": doc["scope"]})
        for index, item in enumerate(commits):
            item["index"] = index
        return {"commits": commits, "generations": generations, "scope": last["scope"], "coverage": last["coverage"],
                "ordering": last["ordering"], "digest": last["digest"]}

    # -- per-commit records ----------------------------------------------------------------
    def commit(self, sha_):
        return self.get("commits", sha_)

    def put_commit(self, record):
        record = {**record, "updated_at": iso()}
        self.put("commits", record["sha"], record)

    def iter_commits(self, manifest=None):
        manifest = manifest or self.manifest()
        for item in manifest["commits"]:
            yield item, self.commit(item["sha"])

    # -- audit log -------------------------------------------------------------------------
    def event(self, kind, **fields):
        line = dumps({"at": iso(), "kind": kind, **fields})
        with self._events_lock:
            try:
                self.path.mkdir(parents=True, exist_ok=True)
                with open(self.path / "events.jsonl", "a+b") as stream:
                    if stream.tell():
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) != b"\n":
                            stream.write(b"\n")
                    stream.write(line)
                    stream.flush()
            except OSError as exc:
                raise FatalStateError(f"cannot append audit log: {exc}") from exc

    def events(self, limit=None):
        path = self.path / "events.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(errors="replace").splitlines()
        out = []
        for line in lines[-limit:] if limit else lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"kind": "corrupt-log-line"})  # a torn final line after a crash is tolerated
        return out

    # -- circuits, runs ----------------------------------------------------------------------
    def circuits(self):
        return read_json(self.path / "circuits.json", default={"providers": {}, "forges": {}})

    def write_circuits(self, value):
        write_json(self.path / "circuits.json", value)

    def write_run(self, run_id, value):
        write_json(self.path / "runs" / (run_id + ".json"), value)

    def runs(self):
        directory = self.path / "runs"
        return [read_json(p) for p in sorted(directory.glob("*.json"))] if directory.exists() else []

    def cleanup_temp(self):
        """Remove orphaned temp files left by a crash during an atomic write."""
        removed = 0
        for path in self.path.rglob(".tmp-*"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed
