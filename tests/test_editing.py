"""The same entity contracts apply to forms and agents."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from tmodel import io
from tmodel.editing import allocated_ids, check_lifecycle, delete_entity, put_entity
from tmodel.schema import Goal
from tmodel.web.forms import next_id
from tmodel.web.store import FileStore

SAMPLE = Path(__file__).parent.parent / "examples" / "minimal.yaml"


def test_immutable_ids_and_retirement():
    model = io.load(SAMPLE)
    model, _ = put_entity(model, "goals", None, {"id": "CG_99", "name": "new"})
    with pytest.raises(ValueError, match="immutable"):
        put_entity(model, "goals", "CG_99", {"id": "CG_OTHER", "name": "changed"})
    deleted = delete_entity(model, "goals", "CG_99")
    assert "CG_99" in deleted.retired_ids
    assert next_id(Goal, allocated_ids(deleted)) == "CG_100"
    with pytest.raises(ValidationError, match="retired"):
        put_entity(deleted, "goals", None, {"id": "CG_99", "name": "reuse"})
    check_lifecycle(model, deleted)
    with pytest.raises(ValueError, match="retired_ids"):
        check_lifecycle(deleted, io.load(SAMPLE))


def test_raw_replacement_cannot_drop_retirement(tmp_path):
    store = FileStore(tmp_path, git=False)
    model, _ = put_entity(io.load(SAMPLE), "goals", None, {"id": "CG_99", "name": "new"})
    store.save("sample", model, "create")
    with pytest.raises(ValueError, match="retired_ids"):
        store.save_text("sample", SAMPLE.read_text(), "silently remove ID")
    deleted = delete_entity(model, "goals", "CG_99")
    store.save("sample", deleted, "delete")
    with pytest.raises(ValueError, match="retired_ids"):
        store.save("sample", io.load(SAMPLE), "drop tombstone")


def test_referenced_entity_cannot_be_deleted():
    model = io.load(SAMPLE)
    with pytest.raises(ValidationError, match="unknown"):
        delete_entity(model, "domains", model.assets[0].domain)
