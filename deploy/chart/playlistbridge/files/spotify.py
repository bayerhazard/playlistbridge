"""Spotify Web API: OAuth (PKCE) und Playlist-Lesen."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import requests

from . import store

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "playlist-read-private playlist-read-collaborative user-read-private"


class SpotifyError(Exception):
    pass


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    headers: dict[str, str] = {}
    if client_secret:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise SpotifyError(f"Token-Tausch fehlgeschlagen ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _refresh(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    headers: dict[str, str] = {}
    if client_secret:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise SpotifyError(f"Token-Erneuerung fehlgeschlagen ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    # Spotify liefert beim Refresh meist keinen neuen refresh_token
    payload.setdefault("refresh_token", refresh_token)
    return payload


def access_token() -> str:
    token = store.get_token("spotify")
    if not token or not token.get("access_token"):
        raise SpotifyError("Spotify ist nicht verbunden.")
    if float(token.get("expires_at") or 0) - 60 <= time.time():
        client_id = store.get_setting("spotify_client_id")
        client_secret = store.get_setting("spotify_client_secret")
        payload = _refresh(client_id, client_secret, token.get("refresh_token") or "")
        store.save_token("spotify", payload, account=token.get("account") or "")
        token = store.get_token("spotify") or {}
    return token["access_token"]


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    token = access_token()
    for attempt in range(6):
        resp = requests.get(
            f"{API}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            time.sleep(min(wait, 30))
            continue
        if resp.status_code == 401 and attempt == 0:
            token = access_token()
            continue
        if resp.status_code >= 400:
            raise SpotifyError(f"Spotify {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    raise SpotifyError(f"Spotify {path}: Rate-Limit nicht aufgeloest.")


def me() -> dict[str, Any]:
    return _get("/me")


def playlists() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _get("/me/playlists", {"limit": 50, "offset": offset})
        items = page.get("items") or []
        for item in items:
            if not item:
                continue
            result.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "tracks": (item.get("tracks") or {}).get("total", 0),
                    "public": item.get("public"),
                    "owner": (item.get("owner") or {}).get("display_name")
                    or (item.get("owner") or {}).get("id"),
                    "image": ((item.get("images") or [{}])[0] or {}).get("url"),
                    "url": (item.get("external_urls") or {}).get("spotify"),
                }
            )
        if page.get("next") is None:
            break
        offset += len(items)
        if not items:
            break
    return result


def playlist_tracks(playlist_id: str, on_progress=None) -> dict[str, Any]:
    meta = _get(f"/playlists/{playlist_id}", {"fields": "name,description,external_urls"})
    tracks: list[dict[str, Any]] = []
    offset = 0
    total = 0
    while True:
        page = _get(
            f"/playlists/{playlist_id}/tracks",
            {"limit": 100, "offset": offset, "additional_types": "track"},
        )
        total = page.get("total", 0)
        for entry in page.get("items") or []:
            track = (entry or {}).get("track")
            if not track or track.get("type") not in (None, "track"):
                continue
            artists = ", ".join(a.get("name", "") for a in (track.get("artists") or []) if a)
            tracks.append(
                {
                    "name": track.get("name") or "",
                    "artists": artists,
                    "album": (track.get("album") or {}).get("name") or "",
                    "duration_ms": track.get("duration_ms") or 0,
                    "isrc": (track.get("external_ids") or {}).get("isrc") or "",
                    "url": (track.get("external_urls") or {}).get("spotify") or "",
                }
            )
        if on_progress:
            on_progress(len(tracks), total)
        if page.get("next") is None:
            break
        offset += len(page.get("items") or [])
        if not (page.get("items") or []):
            break
    return {"name": meta.get("name") or "Spotify-Playlist", "tracks": tracks}
