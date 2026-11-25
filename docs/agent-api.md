# Agent API

Use the authenticated **server** for concurrent agents. The browser build shares
these routes, but its local storage/folder mirror is not a multi-writer backend.
Authentication and authorization are supplied by your deployment, not by this
API. All authorized users can edit all models.

## Python client

No additional dependencies:

```python
from tmodel.client import Client

client = Client(
    "https://tmodel.example.com",
    headers={
        # Add the deployment's Authorization header if needed; never commit tokens.
        "X-Tmodel-Actor": "mitigation-agent",
        "X-Tmodel-Run": "run-2026-01",
        "X-Tmodel-Reason": "Add a verification goal",
    },
)
opened = client.get("example")
proposal = {"id": "CG_VERIFY", "name": "Verify secure boot"}
review = client.put_entity("example", "goals", proposal, etag=opened.etag, dry_run=True)
# Review findings before committing; the original condition is still required.
client.put_entity("example", "goals", proposal, etag=opened.etag)
raw_yaml, sha256 = client.snapshot("example")
# Retain raw_yaml with the finding; cite (model ID, sha256, threat/goal ID).
```

A 412 `ClientError` means GET again and review/rebase your proposal. The client
never automatically retries writes. Get a fresh ETag after each successful save.
`Client.put` replaces a whole model; `create` is explicitly create-only;
`delete_entity` retires the ID. Every mutation can be validated without changing
files/history using `dry_run=True` (creation can be previewed using raw HTTP).

## HTTP contracts

| Method | Path | Contract |
|---|---|---|
| GET | `/api/models` | Model summaries |
| GET | `/api/models/{mid}` | JSON model, exact quoted `ETag` |
| GET | `/api/models/{mid}/snapshot` | Verbatim YAML, `X-Snapshot-SHA256`, matching `ETag` |
| POST | `/api/validate` | JSON model → validation and lint, no save |
| PUT | `/api/models/{mid}` | Replace model, exact `If-Match`; or create with `If-None-Match: *` |
| POST | `/api/models/{mid}/{collection}` | Create one entity, model `If-Match` |
| PUT | `/api/models/{mid}/{collection}/{eid}` | Replace entity, same immutable ID, model `If-Match` |
| DELETE | `/api/models/{mid}/{collection}/{eid}` | Delete/retire entity, model `If-Match` |

All write routes accept `?dry_run=1`: same preconditions, lifecycle checks and
validation, but no model/git write. A successful preview returns lint findings
and `candidate_sha256`, **not** a new live ETag. A successful real write returns
the new ETag and lint findings. Whole-model replacements must retain `retired_ids`
and explicitly add removed IDs; use entity DELETE to handle this automatically.

Errors use `error: {code, message}`. Validation errors add
`issues: [{code, path, message}]`, where path is a list of field names/indices.
Typical statuses: malformed JSON 400, missing model/entity 404, invalid model or
operation 422, missing precondition 428, stale precondition 412. A 503 with
`error.code = history_failed` and `saved = true` means YAML **was saved**, but git
recording failed: GET and inspect before taking further action. Other storage
failures can also occur after replacement; always reload before retrying.

`X-Tmodel-Actor`, `X-Tmodel-Run`, `X-Tmodel-Reason` are optional printable values
(up to 200 characters each), recorded in git commit messages. They are
**self-reported provenance**, not authenticated identity. Without git there is no
persistent provenance log.

Snapshot hashes identify exact UTF-8 YAML contents, including comments, not the
author-controlled revision. Capture and retain the returned bytes: the server
does not archive snapshots independently of git, and this endpoint always returns
the current file.
