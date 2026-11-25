"""
File-backed model store: one YAML file per model in a directory, with
optional git auto-commit on every save. Host supplies locking and durability;
the browser uses the same atomic replacement on its virtual filesystem.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from .. import io
from ..editing import check_lifecycle
from ..schema import SCHEMA_VERSION, ThreatModel

MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass
class ModelInfo:
    id: str
    system: str
    revision: int
    threats: int
    error: str | None = None


@dataclass
class Commit:
    sha: str
    date: str
    message: str


def content_hash(text: str) -> str:
    """Identity of exact YAML bytes, independent of the author-controlled revision."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    text: str

    @property
    def etag(self) -> str:
        return content_hash(self.text)

    @property
    def model(self) -> ThreatModel:
        return io.loads(self.text)


class ConflictError(ValueError):
    """The file changed between reading and committing an edit."""


class HistoryError(RuntimeError):
    """The YAML is saved, but recording its git history failed."""


class FileStore:
    def __init__(
        self,
        root: Path,
        git: bool | None = None,
        guard: Callable[[Path], AbstractContextManager] = lambda _: nullcontext(),
        durable: bool = False,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.guard = guard
        self.durable = durable
        self._identity: list[str] = []  # git -c user.name/email when none is configured
        if git is None:
            git = self._in_git_repo()
        elif git and not self._in_git_repo():
            self._git("init", "-q")
        self.git = git
        if self.git and not self._git("config", "user.name", check=False).stdout.strip():
            self._identity = ["-c", "user.name=tmodel", "-c", "user.email=tmodel@localhost"]

    # Paths -------------------------------------------------------------------

    def path(self, model_id: str) -> Path:
        if not MODEL_ID.match(model_id):
            raise ValueError(f"bad model id {model_id!r}")
        return self.root / f"{model_id}.yaml"

    # Read --------------------------------------------------------------------

    def list_models(self) -> list[ModelInfo]:
        out = []
        for p in sorted(self.root.glob("*.yaml")):
            mid = p.stem
            if not MODEL_ID.match(mid):
                continue
            try:
                m = io.load(p)
                out.append(ModelInfo(mid, m.system, m.revision, len(m.threats)))
            except Exception as e:  # invalid file still listed, so it can be fixed in the YAML tab
                out.append(ModelInfo(mid, "(invalid)", 0, 0, error=str(e).splitlines()[0]))
        return out

    def exists(self, model_id: str) -> bool:
        return self.path(model_id).exists()

    def snapshot(self, model_id: str) -> Snapshot:
        return Snapshot(self.path(model_id).read_bytes().decode("utf-8"))

    def load(self, model_id: str) -> ThreatModel:
        return self.snapshot(model_id).model

    def read_text(self, model_id: str) -> str:
        return self.snapshot(model_id).text

    # Write -------------------------------------------------------------------

    def create(self, model_id: str, system: str) -> ThreatModel:
        p = self.path(model_id)
        if p.exists():
            raise FileExistsError(model_id)
        m = ThreatModel(
            schema_version=SCHEMA_VERSION,
            system=system,
            domains=[],
            surfaces=[],
            assets=[],
            damages=[],
            threats=[],
        )
        self.save(model_id, m, f"{model_id}: create", expected="")
        return m

    def save(
        self,
        model_id: str,
        model: ThreatModel,
        message: str,
        *,
        expected: str | None = None,
        dry_run: bool = False,
    ) -> str:
        """Canonical YAML; dry_run checks the same contracts but never writes."""
        return self._write(model_id, io.dumps(model), model, message, expected=expected, dry_run=dry_run)

    def save_text(
        self, model_id: str, text: str, message: str, *, expected: str | None = None
    ) -> ThreatModel:
        """Validate then write verbatim, keeping comments and layout."""
        m = io.loads(text)
        self._write(model_id, text if text.endswith("\n") else text + "\n", m, message, expected=expected)
        return m

    def _write(
        self,
        model_id: str,
        text: str,
        model: ThreatModel,
        message: str,
        *,
        expected: str | None,
        dry_run: bool = False,
    ) -> str:
        p = self.path(model_id)
        with self.guard(self.root):
            snapshot = self.snapshot(model_id) if p.exists() else None
            current = snapshot.etag if snapshot else ""
            if expected is not None and expected != current:
                raise ConflictError("The file changed before this save. Reload and review before retrying.")
            if snapshot:
                try:
                    before = snapshot.model
                except (ValidationError, yaml.YAMLError):
                    pass  # Allow the YAML editor to repair an invalid file.
                else:
                    check_lifecycle(before, model)
            if dry_run:
                return content_hash(text)
            self._replace(p, text)
            if self.git:
                try:
                    self._git("add", "--", str(p))
                    # A no-op save isn't a failed commit. Real failures must be visible.
                    changed = self._git("diff", "--cached", "--quiet", "--", str(p), check=False)
                    if changed.returncode == 1:
                        self._git("commit", "-q", "-m", message, "--", str(p))
                    elif changed.returncode != 0:
                        raise RuntimeError(changed.stderr.strip())
                except (subprocess.SubprocessError, OSError, RuntimeError) as e:
                    raise HistoryError(
                        "The model was saved, but its git commit failed. "
                        "Check the repository before continuing."
                    ) from e
        return content_hash(text)

    def _replace(self, path: Path, text: str) -> None:
        """Never truncate the live file; clean up even if writing/renaming fails."""
        fd, name = tempfile.mkstemp(prefix=".tmodel-", suffix=".tmp", dir=self.root)
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                if self.durable:
                    os.fsync(f.fileno())
            if path.exists():
                temp.chmod(path.stat().st_mode & 0o777)
            os.replace(temp, path)
            if self.durable:
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temp.unlink(missing_ok=True)

    # History -----------------------------------------------------------------

    def history(self, model_id: str, n: int = 20) -> list[Commit]:
        if not self.git:
            return []
        r = self._git(
            "log",
            f"-{n}",
            "--format=%h%x09%ad%x09%s",
            "--date=short",
            "--",
            str(self.path(model_id)),
            check=False,
        )
        out = []
        for line in r.stdout.splitlines():
            sha, date, msg = line.split("\t", 2)
            out.append(Commit(sha, date, msg))
        return out

    def diff(self, model_id: str, sha: str) -> str | None:
        """What commit `sha` changed in this model's file, or None if unknown."""
        if not self.git:
            return None
        r = self._git("show", "--format=", "--no-color", sha, "--", str(self.path(model_id)), check=False)
        return r.stdout if r.returncode == 0 else None

    def text_at(self, model_id: str, sha: str) -> str | None:
        """The model's file as it was after commit `sha`, or None if it did not exist."""
        if not self.git:
            return None
        r = self._git("show", f"{sha}:./{self.path(model_id).name}", check=False)
        return r.stdout if r.returncode == 0 else None

    # Git plumbing ------------------------------------------------------------

    def _in_git_repo(self) -> bool:
        r = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *self._identity, *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=15,
        )
