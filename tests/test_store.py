"""Persistence guarantees, including failures that happy-path HTTP tests cannot see."""

import os
import subprocess
from pathlib import Path

import pytest

from tmodel import io
from tmodel.web.app import server_lock
from tmodel.web.store import ConflictError, FileStore, HistoryError

SAMPLE = Path(__file__).parent.parent / "examples" / "minimal.yaml"


def test_failed_replace_keeps_original_and_cleans_temp(tmp_path, monkeypatch):
    store = FileStore(tmp_path, git=False, durable=True, guard=server_lock)
    path = store.path("sample")
    path.write_bytes(SAMPLE.read_bytes())
    before = path.read_bytes()

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        store.save("sample", io.load(SAMPLE), "save")
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_commit_failure_is_explicit_but_file_is_saved(tmp_path, monkeypatch):
    store = FileStore(tmp_path, git=True)
    original = store._git

    def git(*args, **kwargs):
        if args[0] == "commit":
            raise subprocess.CalledProcessError(1, "git commit", stderr="hook failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_git", git)
    with pytest.raises(HistoryError, match="was saved"):
        store.save("sample", io.load(SAMPLE), "save")
    assert store.load("sample").system == io.load(SAMPLE).system


def test_stale_snapshot_and_duplicate_create_are_rejected(tmp_path):
    store = FileStore(tmp_path, git=False, guard=server_lock)
    store.create("sample", "first")
    snapshot = store.snapshot("sample")
    updated = snapshot.model
    updated.system = "second"
    store.save("sample", updated, "update", expected=snapshot.etag)
    with pytest.raises(ConflictError):
        store.save("sample", snapshot.model, "stale", expected=snapshot.etag)
    with pytest.raises(ConflictError):
        store.save("sample", snapshot.model, "create again", expected="")
    assert store.load("sample").system == "second"


def test_two_writers_only_one_can_commit_same_snapshot(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = FileStore(tmp_path, git=False, guard=server_lock)
    store.create("sample", "first")
    barrier = Barrier(2)

    def write(name):
        other = FileStore(tmp_path, git=False, guard=server_lock)
        snapshot = other.snapshot("sample")
        model = snapshot.model
        model.system = name
        barrier.wait(timeout=5)
        try:
            other.save("sample", model, name, expected=snapshot.etag)
            return "saved"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["one", "two"]))
    assert sorted(results) == ["conflict", "saved"]


def test_snapshot_preserves_crlf_bytes(tmp_path):
    import hashlib

    store = FileStore(tmp_path, git=False)
    raw = SAMPLE.read_bytes().replace(b"\n", b"\r\n")
    store.path("sample").write_bytes(raw)
    snapshot = store.snapshot("sample")
    assert snapshot.text.encode("utf-8") == raw
    assert snapshot.etag == hashlib.sha256(raw).hexdigest()


def test_noop_save_does_not_make_a_commit(tmp_path):
    store = FileStore(tmp_path, git=True)
    model = io.load(SAMPLE)
    store.save("sample", model, "first")
    store.save("sample", model, "same")
    assert len(store.history("sample")) == 1
