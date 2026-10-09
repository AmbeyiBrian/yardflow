"""A small signed-in HTTP client for the finance endpoint tests (T15.4).

Real login, real tokens, the real tenant middleware: the tests assert what a
person on a phone would get back, including the 403s and 404s, so going through
the whole stack is the point (§4.17.7).
"""

from __future__ import annotations

import json
from typing import Any

from django.urls import reverse

PASSWORD = "a good long password"


class Api:
    """One user's requests, as JSON."""

    def __init__(self, client, user) -> None:  # type: ignore[no-untyped-def]
        self.client = client
        self.user = user
        response = client.post(
            reverse("v1:auth:login"),
            {"identifier": user.email, "password": PASSWORD},
            content_type="application/json",
            HTTP_HOST="silvertech.localhost",
        )
        assert response.status_code == 200, response.content
        self.auth = {
            "HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}",
            "HTTP_HOST": "silvertech.localhost",
        }

    def _send(self, verb: str, path: str, data: Any = None):  # type: ignore[no-untyped-def]
        kwargs: dict[str, Any] = dict(self.auth)
        if data is not None:
            kwargs["data"] = json.dumps(data)
            kwargs["content_type"] = "application/json"
        return getattr(self.client, verb)(f"/api/v1/{path}", **kwargs)

    def get(self, path: str, **params: Any):  # type: ignore[no-untyped-def]
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return self._send("get", f"{path}?{query}" if query else path)

    def post(self, path: str, data: Any = None):  # type: ignore[no-untyped-def]
        return self._send("post", path, {} if data is None else data)

    def patch(self, path: str, data: Any):  # type: ignore[no-untyped-def]
        return self._send("patch", path, data)

    def upload(self, data: dict[str, Any]):  # type: ignore[no-untyped-def]
        """A multipart POST to ``/attachments``."""
        return self.client.post("/api/v1/attachments", data, **self.auth)

    def delete(self, path: str):  # type: ignore[no-untyped-def]
        return self._send("delete", path)


def results(response) -> list[dict]:  # type: ignore[no-untyped-def]
    """The rows of a paginated list."""
    assert response.status_code == 200, response.content
    body = response.json()
    return body["results"] if isinstance(body, dict) else body


def error_code(response) -> str:  # type: ignore[no-untyped-def]
    return response.json()["error"]["code"]  # type: ignore[no-any-return]
