"""Playlist Bridge — FastAPI-Backend (Spotify -> TIDAL)."""

from __future__ import annotations

import os
import secrets
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import migrate, spotify, store, tidal

APP_BASE_PATH = os.environ.get("APP_BASE_PATH", "").rstrip("/")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

SETTING_KEYS = [
    "spotify_client_id",
    "spotify_client_secret",
    "tidal_client_id",
    "tidal_client_secret",
    "country_code",
    "public_base_url",
]

app = FastAPI(title="Playlist Bridge", docs_url=None, redoc_url=None)


class StripPrefix:
    """Entfernt den Olares-Preview-Prefix aus eingehenden Pfaden."""

    def __init__(self, asgi_app, prefix: str):
        self.app = asgi_app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if (
            self.prefix
            and scope.get("type") == "http"
            and scope.get("path", "").startswith(self.prefix)
        ):
            scope = dict(scope)
            new_path = scope["path"][len(self.prefix) :]
            scope["path"] = new_path or "/"
            scope["pb_prefix"] = self.prefix
            if scope.get("raw_path"):
                raw = scope["raw_path"].decode()
                scope["raw_path"] = raw[len(self.prefix) :].encode() or b"/"
        await self.app(scope, receive, send)


def _public_base(request: Request) -> str:
    override = store.get_setting("public_base_url").strip()
    if override:
        return override.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    prefix = request.scope.get("pb_prefix", "")
    return f"{proto}://{host}{prefix}".rstrip("/")


def _redirect_uri(request: Request, provider: str) -> str:
    return f"{_public_base(request)}/api/{provider}/callback"


@app.on_event("startup")
def _startup() -> None:
    store.init_db()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "app": "playlistbridge"}


@app.get("/api/state")
def state(request: Request) -> dict[str, Any]:
    settings = store.get_settings(SETTING_KEYS)
    spotify_token = store.get_token("spotify")
    tidal_token = store.get_token("tidal")
    return {
        "spotify": {
            "connected": bool(spotify_token and spotify_token.get("access_token")),
            "account": (spotify_token or {}).get("account") or "",
        },
        "tidal": {
            "connected": bool(tidal_token and tidal_token.get("access_token")),
            "account": (tidal_token or {}).get("account") or "",
        },
        "settings": {
            "spotify_client_id": settings.get("spotify_client_id", ""),
            "tidal_client_id": settings.get("tidal_client_id", ""),
            "country_code": settings.get("country_code", "DE") or "DE",
            "public_base_url": settings.get("public_base_url", ""),
            "has_spotify_secret": bool(settings.get("spotify_client_secret")),
            "has_tidal_secret": bool(settings.get("tidal_client_secret")),
        },
        "redirect_uris": {
            "spotify": _redirect_uri(request, "spotify"),
            "tidal": _redirect_uri(request, "tidal"),
        },
        "jobs": store.list_jobs(10),
    }


@app.post("/api/settings")
async def save_settings(request: Request) -> dict[str, Any]:
    body = await request.json()
    for key in SETTING_KEYS:
        if key not in body:
            continue
        value = body.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if key == "country_code":
            value = (value or "DE").upper()[:2]
        if key == "public_base_url":
            value = value.rstrip("/")
        store.set_setting(key, value)
    return {"ok": True}


@app.post("/api/disconnect/{provider}")
def disconnect(provider: str) -> dict[str, Any]:
    if provider not in ("spotify", "tidal"):
        raise HTTPException(status_code=400, detail="Unbekannter Anbieter")
    store.delete_token(provider)
    return {"ok": True}


@app.get("/api/{provider}/auth")
def auth_url(provider: str, request: Request) -> dict[str, Any]:
    if provider not in ("spotify", "tidal"):
        raise HTTPException(status_code=400, detail="Unbekannter Anbieter")
    client_id = store.get_setting(f"{provider}_client_id")
    if not client_id:
        raise HTTPException(
            status_code=400,
            detail=f"Client-ID für {provider.title()} fehlt. Bitte zuerst speichern.",
        )
    redirect_uri = _redirect_uri(request, provider)
    verifier, challenge = spotify.new_pkce()
    state_token = secrets.token_urlsafe(24)
    store.save_state(state_token, provider, verifier, redirect_uri)
    if provider == "spotify":
        url = spotify.authorization_url(client_id, redirect_uri, state_token, challenge)
    else:
        url = tidal.authorization_url(client_id, redirect_uri, state_token, challenge)
    return {"url": url, "redirect_uri": redirect_uri}


def _finish_oauth(provider: str, code: str, state_token: str) -> str:
    pending = store.pop_state(state_token, provider)
    if not pending:
        raise HTTPException(status_code=400, detail="Login-Sitzung abgelaufen. Bitte erneut versuchen.")
    client_id = store.get_setting(f"{provider}_client_id")
    client_secret = store.get_setting(f"{provider}_client_secret")
    redirect_uri = pending["redirect"]
    if provider == "spotify":
        payload = spotify.exchange_code(client_id, client_secret, code, redirect_uri, pending["verifier"])
        store.save_token("spotify", payload)
        try:
            account = spotify.me().get("display_name") or spotify.me().get("id") or ""
            token = store.get_token("spotify") or {}
            store.save_token("spotify", {"access_token": token["access_token"], "expires_in": 0}, account=account)
        except Exception:  # noqa: BLE001
            pass
    else:
        payload = tidal.exchange_code(client_id, client_secret, code, redirect_uri, pending["verifier"])
        store.save_token("tidal", payload)
        try:
            info = tidal.me()
            attrs = (info.get("data") or {}).get("attributes") or {}
            account = attrs.get("username") or attrs.get("email") or ""
            token = store.get_token("tidal") or {}
            store.save_token("tidal", {"access_token": token["access_token"], "expires_in": 0}, account=account)
        except Exception:  # noqa: BLE001
            pass
    return provider


@app.get("/api/spotify/callback")
def spotify_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    prefix = request.scope.get("pb_prefix", "")
    if error or not code:
        return RedirectResponse(url=f"{prefix}/?fehler={error or 'abgebrochen'}", status_code=302)
    try:
        _finish_oauth("spotify", code, state)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"{prefix}/?fehler={str(exc)[:200]}", status_code=302)
    return RedirectResponse(url=f"{prefix}/?verbunden=spotify", status_code=302)


@app.get("/api/tidal/callback")
def tidal_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    prefix = request.scope.get("pb_prefix", "")
    if error or not code:
        return RedirectResponse(url=f"{prefix}/?fehler={error or 'abgebrochen'}", status_code=302)
    try:
        _finish_oauth("tidal", code, state)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"{prefix}/?fehler={str(exc)[:200]}", status_code=302)
    return RedirectResponse(url=f"{prefix}/?verbunden=tidal", status_code=302)


@app.get("/api/playlists")
def playlists() -> dict[str, Any]:
    try:
        return {"playlists": spotify.playlists()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/migrate")
async def start_migration(request: Request) -> dict[str, Any]:
    body = await request.json()
    playlist_id = (body.get("playlist_id") or "").strip()
    if not playlist_id:
        raise HTTPException(status_code=400, detail="playlist_id fehlt")
    if not store.get_token("spotify"):
        raise HTTPException(status_code=400, detail="Spotify ist nicht verbunden.")
    if not store.get_token("tidal"):
        raise HTTPException(status_code=400, detail="TIDAL ist nicht verbunden.")
    options = {
        "name": body.get("name") or "",
        "description": body.get("description") or "",
        "access_type": body.get("access_type") or "UNLISTED",
        "dry_run": bool(body.get("dry_run")),
    }
    job_id = store.create_job()
    threading.Thread(target=migrate.run, args=(job_id, playlist_id, options), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict[str, Any]:
    result = store.get_job(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    return result


@app.get("/api/jobs")
def jobs() -> dict[str, Any]:
    return {"jobs": store.list_jobs(20)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

# ASGI-Wrapper mit Preview-Prefix-Behandlung
asgi_app = StripPrefix(app, APP_BASE_PATH)
