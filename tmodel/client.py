"""Small stdlib agent client. Never retries conflicting writes automatically."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .schema import ThreatModel


class ClientError(Exception):
    def __init__(self, status: int, detail: dict):
        self.status, self.detail = status, detail
        super().__init__(f"HTTP {status}: {detail}")


@dataclass(frozen=True)
class RemoteModel:
    model: ThreatModel
    etag: str


class Client:
    def __init__(self, base_url: str, *, headers: dict[str, str] | None = None, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout

    def _request(self, method: str, path: str, data=None, *, headers=None):
        h = {**self.headers, **(headers or {})}
        body = None if data is None else json.dumps(data).encode("utf-8")
        if body is not None:
            h["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.headers, response.read()
        except urllib.error.HTTPError as e:
            with e:
                raw = e.read()
                try:
                    detail = json.loads(raw)
                except (ValueError, UnicodeError):
                    detail = {"error": raw.decode("utf-8", "replace")}
                raise ClientError(e.code, detail) from e

    @staticmethod
    def _path(mid: str) -> str:
        return "/api/models/" + urllib.parse.quote(mid, safe="")

    def get(self, mid: str) -> RemoteModel:
        headers, body = self._request("GET", self._path(mid))
        return RemoteModel(ThreatModel.model_validate_json(body), headers["ETag"])

    def validate(self, model: ThreatModel) -> dict:
        _, body = self._request("POST", "/api/validate", model.model_dump(mode="json"))
        return json.loads(body)

    def put(self, mid: str, model: ThreatModel, *, etag: str, dry_run: bool = False) -> dict:
        """Replace an existing model; GET again after success to obtain its new ETag."""
        _, body = self._request(
            "PUT",
            self._path(mid) + ("?dry_run=1" if dry_run else ""),
            model.model_dump(mode="json"),
            headers={"If-Match": etag},
        )
        return json.loads(body)

    def create(self, mid: str, model: ThreatModel) -> dict:
        _, body = self._request(
            "PUT", self._path(mid), model.model_dump(mode="json"), headers={"If-None-Match": "*"}
        )
        return json.loads(body)

    def put_entity(
        self,
        mid: str,
        collection: str,
        data: dict,
        *,
        etag: str,
        eid: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """eid=None creates; otherwise replace that entity without renaming its ID."""
        path = self._path(mid) + "/" + urllib.parse.quote(collection, safe="")
        if eid is not None:
            path += "/" + urllib.parse.quote(eid, safe="")
        _, body = self._request(
            "POST" if eid is None else "PUT",
            path + ("?dry_run=1" if dry_run else ""),
            data,
            headers={"If-Match": etag},
        )
        return json.loads(body)

    def delete_entity(self, mid: str, collection: str, eid: str, *, etag: str, dry_run: bool = False) -> dict:
        path = (
            self._path(mid)
            + "/"
            + urllib.parse.quote(collection, safe="")
            + "/"
            + urllib.parse.quote(eid, safe="")
        )
        _, body = self._request(
            "DELETE", path + ("?dry_run=1" if dry_run else ""), headers={"If-Match": etag}
        )
        return json.loads(body)

    def snapshot(self, mid: str) -> tuple[bytes, str]:
        """Capture exact YAML and its verified SHA-256; callers retain it with findings."""
        headers, body = self._request("GET", self._path(mid) + "/snapshot")
        digest = headers["X-Snapshot-SHA256"]
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError("Snapshot hash mismatch")
        return body, digest
