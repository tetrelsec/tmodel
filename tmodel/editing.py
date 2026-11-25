"""Validated entity mutations shared by human and agent interfaces.

Pure functions: callers provide a snapshot and commit through the store's CAS.
No HTTP, templates, filesystem, or host-specific behavior belongs here.
"""

from .schema import Entity, ThreatModel


def allocated_ids(model: ThreatModel) -> list[str]:
    """All identities unavailable for allocation, including retired entities."""
    return [e.id for items in model._collections().values() for e in items] + model.retired_ids


def entity_class(model: ThreatModel, collection: str) -> type[Entity]:
    try:
        return model.entity_classes()[collection]
    except KeyError as e:
        raise ValueError(f"unknown collection {collection!r}") from e


def put_entity(
    model: ThreatModel, collection: str, eid: str | None, data: dict
) -> tuple[ThreatModel, Entity]:
    cls = entity_class(model, collection)
    entity = cls.model_validate(data)
    items = getattr(model, collection)
    if eid is not None:
        if entity.id != eid:
            raise ValueError("Entity IDs are immutable; create a new entity instead.")
        if not any(e.id == eid for e in items):
            raise ValueError(f"unknown entity {eid}")
    elif any(e.id == entity.id for e in items):
        raise ValueError(f"{entity.id} already exists")
    full = model.model_dump(mode="json")
    full[collection] = [
        e.model_dump(mode="json")
        for e in ([entity if e.id == eid else e for e in items] if eid else [*items, entity])
    ]
    return type(model).model_validate(full), entity


def delete_entity(model: ThreatModel, collection: str, eid: str) -> ThreatModel:
    entity_class(model, collection)
    items = getattr(model, collection)
    if not any(e.id == eid for e in items):
        raise ValueError(f"unknown entity {eid}")
    full = model.model_dump(mode="json")
    full[collection] = [e.model_dump(mode="json") for e in items if e.id != eid]
    full["retired_ids"] = [*model.retired_ids, eid]
    # Whole-model reference validation also guards deletion of referenced entities.
    return type(model).model_validate(full)


def check_lifecycle(before: ThreatModel, after: ThreatModel) -> None:
    """Raw YAML/API replacements must retain retirement history too."""
    removed = set(before._index) - set(after._index)
    required = set(before.retired_ids) | removed
    if missing := required - set(after.retired_ids):
        raise ValueError(f"Keep deleted IDs in retired_ids: {', '.join(sorted(missing))}")
