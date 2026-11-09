"""
A tiny in-process fake of the Plex and Lidarr HTTP APIs, just enough of each for
the playlist, vault and Lidarr code paths. Tests change `FakeServices.state`
(and its `mode` flags) to simulate failures.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse


def fresh_state() -> Dict[str, Any]:
    return {
        # Plex
        "machine_id": "machine-abc",
        "playlists": {},          # ratingKey -> {"title", "items": [keys], "smart": bool, "type": "audio"}
        "next_playlist_id": 100,
        "vault_albums": [],       # list of Plex album metadata dicts (type=9)
        "album_tracks": {},       # album ratingKey -> list of track metadata dicts
        "library_tracks": [],     # list of Plex track metadata dicts (type=10)
        # Lidarr
        "lidarr_down": False,
        "lidarr_artists": [],     # artists already in Lidarr: {"id", "artistName", "foreignArtistId"}
        "lidarr_albums": {},      # artistId -> [album dicts]
        "lidarr_lookup": [],      # results for /artist/lookup
        "lidarr_album_lookup": [],  # results for /album/lookup
        "lidarr_add_error": None, # (status, body) to return from POST /artist
        "lidarr_commands": [],
        "lidarr_added": [],
        # Failure injection
        "mode": {},
        # Request log: (method, path)
        "log": [],
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeServices/1.0"

    def log_message(self, *args):  # keep test output quiet
        pass

    @property
    def state(self) -> Dict[str, Any]:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, code: int, obj: Any = None) -> None:
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drop_connection(self) -> None:
        self.close_connection = True
        self.wfile.flush()
        self.connection.close()

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"null") if length else None

    def _handle(self, method: str) -> None:
        st = self.state
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path
        mode = st["mode"]
        with self.server.lock:  # type: ignore[attr-defined]
            st["log"].append((method, path))

        if path.startswith("/api/v1/"):
            return self._lidarr(method, path, q)

        if path == "/identity":
            return self._send(200, {"MediaContainer": {"machineIdentifier": st["machine_id"]}})

        if path.startswith("/library/sections/") and path.endswith("/all"):
            kind = q.get("type", [""])[0]
            if kind == "9":
                if mode.get("vault_fail"):
                    return self._send(500, {})
                start = int(q.get("X-Plex-Container-Start", ["0"])[0])
                size = int(q.get("X-Plex-Container-Size", ["50"])[0])
                page = st["vault_albums"][start:start + size]
                return self._send(200, {"MediaContainer": {"totalSize": len(st["vault_albums"]), "Metadata": page}})
            if kind == "10":
                start = int(q.get("X-Plex-Container-Start", ["0"])[0])
                size = int(q.get("X-Plex-Container-Size", [str(len(st["library_tracks"]) or 1)])[0])
                if mode.get("tracks_fail_at") is not None and start >= mode["tracks_fail_at"]:
                    return self._send(500, {})
                page = st["library_tracks"][start:start + size]
                return self._send(200, {"MediaContainer": {"totalSize": len(st["library_tracks"]), "Metadata": page}})

        if path.startswith("/library/metadata/") and path.endswith("/children"):
            rk = path.split("/")[3]
            return self._send(200, {"MediaContainer": {"Metadata": st["album_tracks"].get(rk, [])}})

        if path == "/playlists" and method == "GET":
            if mode.get("list_fail"):
                return self._send(500, {})
            md = [
                {
                    "ratingKey": k,
                    "title": v["title"],
                    "playlistType": v.get("type", "audio"),
                    "smart": v.get("smart", False),
                    "leafCount": len(v["items"]),
                }
                for k, v in st["playlists"].items()
            ]
            return self._send(200, {"MediaContainer": {"Metadata": md}})

        if path == "/playlists" and method == "POST":
            pid = str(st["next_playlist_id"])
            st["next_playlist_id"] += 1
            keys = q["uri"][0].rsplit("/", 1)[1].split(",")
            st["playlists"][pid] = {"title": q["title"][0], "items": keys}
            return self._send(200, {"MediaContainer": {"Metadata": [
                {"ratingKey": pid, "title": q["title"][0], "leafCount": len(keys)}
            ]}})

        parts = path.strip("/").split("/")
        if parts[0] == "playlists" and len(parts) >= 2 and parts[1] in st["playlists"]:
            pid = parts[1]
            pl = st["playlists"][pid]
            if len(parts) == 3 and method == "DELETE":
                time.sleep(mode.get("delay", 0))
                pl["items"] = []
                return self._send(200)
            if len(parts) == 3 and method == "PUT":
                time.sleep(mode.get("delay", 0))
                if mode.get("put_fail_n", 0) > 0:
                    mode["put_fail_n"] -= 1
                    return self._send(500, {})
                pl["items"] += q["uri"][0].rsplit("/", 1)[1].split(",")
                if mode.pop("put_drop_response", False):
                    return self._drop_connection()
                return self._send(200)
            if len(parts) == 2 and method == "GET":
                return self._send(200, {"MediaContainer": {"Metadata": [
                    {"ratingKey": pid, "title": pl["title"], "leafCount": len(pl["items"])}
                ]}})

        self._send(404, {"error": f"fake has no route for {method} {path}"})

    def _lidarr(self, method: str, path: str, q: Dict[str, List[str]]) -> None:
        st = self.state
        if st["lidarr_down"]:
            return self._send(503, {"error": "down"})
        if path == "/api/v1/artist" and method == "GET":
            return self._send(200, st["lidarr_artists"])
        if path == "/api/v1/album":
            artist_id = int(q.get("artistId", ["0"])[0])
            return self._send(200, st["lidarr_albums"].get(artist_id, []))
        if path == "/api/v1/artist/lookup":
            return self._send(200, st["lidarr_lookup"])
        if path == "/api/v1/album/lookup":
            return self._send(200, st["lidarr_album_lookup"])
        if path == "/api/v1/artist" and method == "POST":
            if st["lidarr_add_error"]:
                code, body = st["lidarr_add_error"]
                return self._send(code, body)
            body = self._body()
            st["lidarr_added"].append(body)
            return self._send(201, {**body, "id": 999})
        if path == "/api/v1/command" and method == "POST":
            st["lidarr_commands"].append(self._body())
            return self._send(201, {"id": 1})
        if path in ("/api/v1/rootfolder", "/api/v1/qualityprofile", "/api/v1/metadataprofile"):
            return self._send(200, [{"id": 1, "path": "/music"}])
        self._send(404, {"error": f"fake lidarr has no route for {method} {path}"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


class FakeServices:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.state = fresh_state()  # type: ignore[attr-defined]
        self.server.lock = threading.Lock()  # type: ignore[attr-defined]
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def state(self) -> Dict[str, Any]:
        return self.server.state  # type: ignore[attr-defined]

    def reset(self) -> None:
        self.server.state = fresh_state()  # type: ignore[attr-defined]

    def requests(self, method: str = None, path_prefix: str = "") -> List[tuple]:
        return [
            (m, p) for m, p in self.state["log"]
            if (method is None or m == method) and p.startswith(path_prefix)
        ]

    def stop(self) -> None:
        self.server.shutdown()
