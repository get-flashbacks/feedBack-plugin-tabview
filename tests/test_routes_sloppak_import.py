"""Regression tests for two routes.py fixes from a security audit:

1. `sloppak` must be imported qualified as `lib.sloppak`, not as a bare
   top-level `import sloppak` — a bare import caches into sys.modules
   under the generic key "sloppak", so if another installed plugin ever
   ships its own top-level sloppak.py that imports first, this route
   would silently get the wrong module. Simulated here by poisoning
   sys.modules["sloppak"] with a decoy and confirming the route still
   reaches the real (stubbed) lib.sloppak, never the decoy.

2. `/api/plugins/tabview/gp5/{filename:path}` must not leak exception
   text to the client on a conversion failure — only a generic message,
   with detail going to the server's own log (traceback.print_exc()).

The FastAPI `@app.get(...)` decorator doesn't transform the function it
wraps, so the registered endpoint can be called directly as a plain
function (no TestClient / ASGI transport needed) — matches this repo's
existing preference for testing without spinning up a live app
(conftest.py stubs `song` the same way for rs2gp.py).

`routes.setup()` does `from rs2gp import arrangement_to_gp5` at call
time, binding it into `_song_to_gp5`'s closure — so any stub for it must
be installed on the `rs2gp` module BEFORE `setup()` runs, not after.
`build_app()` is therefore a factory tests call once their monkeypatches
are in place, rather than an eagerly-evaluated fixture.
"""
import sys
import types

import pytest
from fastapi import FastAPI

import routes

ROUTE_PATH = "/api/plugins/tabview/gp5/{filename:path}"


def _find_endpoint(app: FastAPI, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"no route registered for {path}")


@pytest.fixture
def build_app(tmp_path):
    def _build():
        dlc = tmp_path / "dlc"
        dlc.mkdir()
        cache = tmp_path / "cache"

        app = FastAPI()
        context = {
            "get_dlc_dir": lambda: str(dlc),
            "get_sloppak_cache_dir": lambda: str(cache),
        }
        routes.setup(app, context)
        return app, dlc

    return _build


def _install_fake_lib_sloppak(monkeypatch, load_song_impl):
    """Stub sys.modules['lib'] / ['lib.sloppak'] so `from lib import sloppak`
    resolves without the real core lib/ directory present in this repo's
    standalone checkout."""
    fake_sloppak = types.ModuleType("lib.sloppak")
    fake_sloppak.load_song = load_song_impl
    fake_lib = types.ModuleType("lib")
    fake_lib.sloppak = fake_sloppak
    monkeypatch.setitem(sys.modules, "lib", fake_lib)
    monkeypatch.setitem(sys.modules, "lib.sloppak", fake_sloppak)


def test_qualified_import_is_not_shadowed_by_a_bare_sloppak_collision(build_app, monkeypatch):
    """A decoy bare `sloppak` module (simulating a different, unrelated
    plugin that did `import sloppak` for its own same-named file) must
    NOT be what this route ends up using."""
    decoy = types.ModuleType("sloppak")

    def _decoy_load_song(*a, **kw):
        raise AssertionError("route used the decoy top-level sloppak module, not lib.sloppak")
    decoy.load_song = _decoy_load_song
    monkeypatch.setitem(sys.modules, "sloppak", decoy)

    loaded_song = types.SimpleNamespace(arrangements=["arr0"])

    def _real_load_song(filename, dlc_path, cache_path):
        return types.SimpleNamespace(song=loaded_song)
    _install_fake_lib_sloppak(monkeypatch, _real_load_song)

    # rs2gp.arrangement_to_gp5 isn't stubbed by conftest.py — patch the
    # source module before setup() imports it, so we exercise only the
    # sloppak import-resolution path, not real GP5 byte generation.
    import rs2gp
    monkeypatch.setattr(rs2gp, "arrangement_to_gp5", lambda song, idx: b"GP5-BYTES")

    app, dlc = build_app()
    (dlc / "song.feedpak").mkdir()

    endpoint = _find_endpoint(app, ROUTE_PATH)
    resp = endpoint(filename="song.feedpak", arrangement=0)
    # If the decoy had been used, _decoy_load_song's AssertionError would
    # have been caught by the route's own except-Exception and turned into
    # a generic 500 — so also assert we got the real success path, not a
    # swallowed failure.
    assert resp.status_code == 200
    assert resp.body == b"GP5-BYTES"


def test_conversion_error_does_not_leak_exception_text_to_client(build_app, monkeypatch):
    secret_detail = "/home/attacker-visible/secret/internal/path/model.bin"

    def _raising_load_song(*a, **kw):
        raise RuntimeError(f"failed reading {secret_detail}")
    _install_fake_lib_sloppak(monkeypatch, _raising_load_song)

    app, dlc = build_app()
    (dlc / "song.feedpak").mkdir()

    endpoint = _find_endpoint(app, ROUTE_PATH)
    resp = endpoint(filename="song.feedpak", arrangement=0)

    assert resp.status_code == 500
    body = resp.body.decode() if isinstance(resp.body, (bytes, bytearray)) else resp.body
    assert secret_detail not in body
    assert "server logs" in body.lower()
