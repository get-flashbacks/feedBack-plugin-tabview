"""Tab View plugin — serves Guitar Pro files converted from arrangements."""

import sys
import tempfile
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import Response

# Ensure the core `lib` package is importable — insert its PARENT (the core
# repo root), not `lib` itself. Inserting `lib` directly would make `import
# sloppak` resolve as a bare top-level name, caching into sys.modules under
# the generic key "sloppak" — the same collision class the core plugin-system
# guide warns about for any plugin shipping a same-named top-level module
# (whichever loads first wins, and there is no per-plugin namespacing for a
# bare top-level import). Inserting the repo root instead lets `sloppak` be
# imported qualified as `lib.sloppak`, matching the pattern the editor
# plugin already uses (`from lib import sloppak as sloppak_mod`) — `lib` has
# no `__init__.py` but still resolves as a PEP 420 namespace package.
_core_root = str(Path(__file__).resolve().parent.parent.parent)
if _core_root not in sys.path:
    sys.path.insert(0, _core_root)

# `sloppak` is loaded lazily inside the song-package branch below — older
# cores ship without lib/sloppak.py, and a top-level import here would
# disable Tab View entirely on those installs.

# Song-package suffixes: `.feedpak` is the current extension, `.sloppak` the
# legacy one — same on-disk format either way (mirrors core lib/sloppak.py
# SONG_EXTS, which can't be imported here without breaking the lazy import).
PAK_EXTS = (".feedpak", ".sloppak")


def setup(app: FastAPI, context: dict):
    get_dlc_dir = context["get_dlc_dir"]
    get_sloppak_cache = context.get("get_sloppak_cache_dir")

    from rs2gp import arrangement_to_gp5

    def _song_to_gp5(song, arrangement: int) -> Response:
        if not song.arrangements:
            return Response("No arrangements found", status_code=404)
        idx = max(0, min(arrangement, len(song.arrangements) - 1))
        gp5_bytes = arrangement_to_gp5(song, idx)
        return Response(
            content=gp5_bytes,
            media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="tab.gp5"'},
        )

    @app.get("/api/plugins/tabview/gp5/{filename:path}")
    def tabview_gp5(filename: str, arrangement: int = 0):
        dlc = get_dlc_dir()
        if not dlc:
            return Response("DLC folder not configured", status_code=500)

        song_path = Path(dlc) / filename

        # Path traversal guard: reject any filename that resolves outside dlc.
        dlc_resolved = Path(dlc).resolve()
        try:
            resolved = song_path.resolve()
        except Exception:
            return Response("Path resolution failed", status_code=400)
        if resolved != dlc_resolved and dlc_resolved not in resolved.parents:
            return Response("Path traversal not allowed", status_code=400)

        if not song_path.exists():
            return Response("File not found", status_code=404)

        try:
            # Song package (zip-form *.feedpak / *.sloppak or directory-form
            # ending in either suffix): use the sloppak loader directly — it
            # accepts both extensions. Only directories whose name ends with
            # a package suffix are treated as packages; any other input is
            # rejected with a clear error below.
            is_pak = filename.lower().endswith(PAK_EXTS) or (
                song_path.is_dir() and song_path.name.lower().endswith(PAK_EXTS)
            )
            if is_pak:
                try:
                    from lib import sloppak as sloppak_mod
                except ImportError:
                    return Response(
                        "Sloppak support requires a newer feedBack core (lib/sloppak.py). "
                        "Update the host.",
                        status_code=501,
                    )
                raw_cache = get_sloppak_cache() if get_sloppak_cache else None
                cache = Path(raw_cache) if raw_cache is not None else Path(tempfile.gettempdir()) / "sloppak_cache"
                cache.mkdir(parents=True, exist_ok=True)
                loaded = sloppak_mod.load_song(filename, Path(dlc), cache)
                return _song_to_gp5(loaded.song, arrangement)

            # Any non-package input is unsupported.
            return Response(
                "Only .feedpak / .sloppak songs are supported",
                status_code=400,
            )
        except Exception:
            import traceback
            # Full detail goes to the server's own log; the client only ever
            # sees a generic message. str(e) here could include internal
            # filesystem paths / library stack detail (e.g. from a parser
            # deep inside sloppak or rs2gp), which is minor info disclosure
            # to anyone able to reach this unauthenticated route with a
            # crafted .feedpak/.sloppak.
            traceback.print_exc()
            return Response("Conversion error — see server logs for details.", status_code=500)
