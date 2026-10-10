"""`ah view`: the JSON API over real sessions, and the local server's guarantees."""

import http.client
import json
import socket
import threading

import pytest

from agent_history import view
from agent_history.adapters import codex
from agent_history.adapters.opencode import normalize as opencode_normalize
from agent_history.store import Store
from test_codex_rollout import SHOP_CAPTURE
from test_opencode import CHILD, SESSION, SHOP_AFTER, delegating_session

SHOP = "/workspace/shop.py"


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "h.db")
    store.ingest(codex.normalize(SHOP_CAPTURE))
    yield store
    store.close()


def test_blame_view(store):
    data = view.blame_view(store, SHOP)
    assert data["path"] == SHOP and not data["on_disk"]
    fixed = next(line for line in data["lines"] if line["text"] == "    return price * (1 - percent / 100)")
    author = data["authors"][fixed["a"]]
    assert (author["kind"], author["name"], author["turn"]) == ("agent", "codex", 1)
    assert author["session"] == "20261004T132159Z-39de3d" and not author["key"].startswith("obs:")  # a recorded edit
    assert sum(data["totals"].values()) == len(data["lines"])


def test_story_view_marks_the_line(store):
    lines = view.blame_view(store, SHOP)["lines"]
    number = next(line["n"] for line in lines if line["text"] == "    return price * (1 - percent / 100)")
    data = view.story_view(store, SHOP, number)
    assert data["kind"] == "agent" and data["prompt"].startswith("apply_discount in shop.py is buggy")
    [origin] = [step for step in data["steps"] if step["origin"]]
    [change] = origin["changes"]
    assert change["rel"] == "shop.py" and change["lines"][change["marked"]] == "+    return price * (1 - percent / 100)"
    tests = [step for step in data["steps"] if step["label"] == "test"]
    assert tests and tests[-1]["summary"] == "3 tests passed" and "output" in tests[-1]
    with pytest.raises(LookupError):
        view.story_view(store, SHOP, 999)


def test_sub_agent_story_and_session(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shop.py").write_text(SHOP_AFTER)
    delegating_session(tmp_path / "opencode.db", cwd=str(repo))
    store = Store(tmp_path / "h.db")
    store.ingest(opencode_normalize(tmp_path / "opencode.db" / SESSION), "opencode-db")
    data = view.story_view(store, str(repo / "shop.py"), 6)
    assert data["author"]["turn"] == 1 and data["prompt"] == "Add median(); use a sub-agent."
    assert data["handoffs"] == [{"thread": CHILD, "prompt": "Add median() to shop.py"}]

    session = view.session_view(store, SESSION)
    parent, sub = session["turns"]
    assert parent["delegated_from"] is None and sub["delegated_from"] == parent["id"]
    [edit] = [step for step in sub["steps"] if step["label"] == "edit"]
    assert edit["changes"] == [{"path": str(repo / "shop.py"), "rel": "shop.py", "kind": "update", "line": 3}]
    assert [s["id"] for s in view.sessions_view(store)] == [SESSION]
    assert view.files(store)[0]["path"] == str(repo / "shop.py")


# -- the server ---------------------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path, store):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>ah</title>")
    (static / "assets" / "app-1234.js").write_text("console.log(1)")
    server = view.Server(0, str(tmp_path / "h.db"), static)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def get(server, path, host=None, method="GET", **headers):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    connection.request(method, path, headers={"Host": host or f"127.0.0.1:{server.port}", **headers})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, body


def test_server_serves_the_page_and_the_api(server):
    response, body = get(server, "/")
    assert response.status == 200 and b"<title>ah</title>" in body
    assert response.getheader("Cache-Control") == "no-cache"
    response, body = get(server, "/assets/app-1234.js")
    assert body == b"console.log(1)" and response.getheader("Content-Type").startswith("text/javascript")
    assert "immutable" in response.getheader("Cache-Control")
    response, body = get(server, f"/api/blame?path={SHOP}")
    assert response.status == 200 and json.loads(body)["path"] == SHOP
    response, body = get(server, f"/api/story?path={SHOP}&line=x")
    assert response.status == 400
    response, body = get(server, "/api/blame?path=/nowhere.py")
    assert response.status == 404 and "error" in json.loads(body)
    server.sync.run(server.db)
    response, body = get(server, "/api/status", host=f"localhost:{server.port}")
    status = json.loads(body)
    assert status["syncs"] == 1 and not status["syncing"] and status["version"] > 0


def test_server_refuses_other_hosts_writes_and_escapes(server):
    assert get(server, "/api/sessions", host="evil.example")[0].status == 403  # DNS rebinding
    assert get(server, "/api/sessions", host=f"evil.example:{server.port}")[0].status == 403
    assert get(server, "/api/sessions", method="POST")[0].status == 501
    assert get(server, "/../../etc/passwd")[0].status == 404
    assert get(server, "/%2e%2e/%2e%2e/etc/passwd")[0].status == 404


def test_server_takes_any_free_port_when_its_own_is_taken(tmp_path):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        server = view.Server(taken.getsockname()[1], str(tmp_path / "h.db"))
        try:
            assert server.port != taken.getsockname()[1]
        finally:
            server.server_close()


def test_urls_open_at_a_file_and_line(tmp_path):
    server = view.Server(0, str(tmp_path / "h.db"))
    try:
        base = f"http://127.0.0.1:{server.port}/"
        assert server.url() == base
        assert server.url("/w/shop.py:58") == base + "#/file/%2Fw%2Fshop.py?line=58"
        assert server.url("/w/shop.py") == base + "#/file/%2Fw%2Fshop.py"
    finally:
        server.server_close()
