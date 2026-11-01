import json

import pytest
from fastapi.testclient import TestClient

from app.db import _url_key
from app.main import app, get_pg, get_valkey


class FakeValkey:
    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key):
        return self.data.get(key)

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)

    def pipeline(self, transaction=True):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.ops = []

    def set(self, key, value, ex=None):
        self.ops.append((key, value))

    def execute(self):
        self.client.data.update(self.ops)


fake_valkey = FakeValkey()
app.dependency_overrides[get_valkey] = lambda: fake_valkey
app.dependency_overrides[get_pg] = lambda: None
client = TestClient(app)


@pytest.fixture(autouse=True)
def no_postgres_row(monkeypatch):
    monkeypatch.setattr("app.main.get_row_by_url_from_postgres", lambda pg, url: None)
    monkeypatch.setattr(
        "app.main.insert_row_into_postgres",
        lambda pg, url: {"id": "new123", "url": url, "created_at": "t"},
    )


def test_shortener_creates_new_row():
    response = client.post("/shortener", json={"url": "https://example.com/a/b"})
    assert response.status_code == 200
    assert response.json() == {"id": "new123"}
    assert fake_valkey.data[_url_key("https://example.com/a/b")] == "new123"
    assert json.loads(fake_valkey.data["id:new123"])["url"] == "https://example.com/a/b"


def test_shortener_rejects_invalid_url():
    response = client.post("/shortener", json={"url": "not a url"})
    assert response.status_code == 422


def test_shortener_returns_cached_id():
    fake_valkey.data[_url_key("https://example.com/cached")] = "abc123"
    response = client.post("/shortener", json={"url": "https://example.com/cached"})
    assert response.status_code == 200
    assert response.json() == {"id": "abc123"}


def test_shortener_returns_id_from_postgres(monkeypatch):
    row = {"id": "pg1234", "url": "https://example.com/db", "created_at": "t"}
    monkeypatch.setattr("app.main.get_row_by_url_from_postgres", lambda pg, url: row)
    response = client.post("/shortener", json={"url": "https://example.com/db"})
    assert response.status_code == 200
    assert response.json() == {"id": "pg1234"}
    assert fake_valkey.data[_url_key("https://example.com/db")] == "pg1234"
    assert json.loads(fake_valkey.data["id:pg1234"]) == row


ROW = {"id": "pg1234", "url": "https://example.com/db", "created_at": "t"}


def test_get_by_id_from_cache():
    fake_valkey.data["id:pg1234"] = json.dumps(ROW)
    response = client.get("/shortener/pg1234")
    assert response.status_code == 200
    assert response.json() == ROW


def test_get_by_id_falls_back_to_postgres(monkeypatch):
    fake_valkey.data.clear()
    monkeypatch.setattr("app.main.get_row_by_id_from_postgres", lambda pg, i: ROW)
    response = client.get("/shortener/pg1234")
    assert response.json() == ROW
    assert "id:pg1234" in fake_valkey.data


def test_get_by_id_not_found(monkeypatch):
    fake_valkey.data.clear()
    monkeypatch.setattr("app.main.get_row_by_id_from_postgres", lambda pg, i: None)
    assert client.get("/shortener/missing").status_code == 404


def test_get_by_url_from_postgres(monkeypatch):
    fake_valkey.data.clear()
    monkeypatch.setattr("app.main.get_row_by_url_from_postgres", lambda pg, u: ROW)
    response = client.get("/shortener", params={"url": "https://example.com/db"})
    assert response.status_code == 200
    assert response.json() == ROW


def test_get_by_url_not_found():
    fake_valkey.data.clear()
    response = client.get("/shortener", params={"url": "https://example.com/none"})
    assert response.status_code == 404


def test_delete_removes_row_and_cache(monkeypatch):
    fake_valkey.data.clear()
    fake_valkey.data["id:pg1234"] = json.dumps(ROW)
    fake_valkey.data[_url_key(ROW["url"])] = "pg1234"
    monkeypatch.setattr("app.main.delete_row_from_postgres", lambda pg, i: ROW)
    response = client.delete("/shortener/pg1234")
    assert response.status_code == 204
    assert fake_valkey.data == {}


def test_delete_not_found(monkeypatch):
    monkeypatch.setattr("app.main.delete_row_from_postgres", lambda pg, i: None)
    assert client.delete("/shortener/missing").status_code == 404


def test_redirect_to_original_url():
    fake_valkey.data["id:pg1234"] = json.dumps(ROW)
    response = client.get("/pg1234", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == ROW["url"]


def test_redirect_not_found(monkeypatch):
    fake_valkey.data.clear()
    monkeypatch.setattr("app.main.get_row_by_id_from_postgres", lambda pg, i: None)
    assert client.get("/missing", follow_redirects=False).status_code == 404


def test_health_not_shadowed_by_redirect():
    assert client.get("/health").json() == {"status": "ok"}
