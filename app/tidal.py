"""TIDAL Web API (JSON:API v2): OAuth (PKCE), Katalog-Suche und Playlists."""

from __future__ import annotations

import base64
import time
from typing import Any
from urllib.parse import quote, urlencode

import requests

from . import store
from .spotify import new_pkce

AUTHORIZE_URL = "https://login.tidal.com/authorize"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
API = "https://openapi.tidal.com/v2"
SCOPES = "playlists.read playlists.write user.read search.read"

_JSONAPI = "application/vnd.api+json"


class TidalError(Exception):
    pass


def authorization_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _token_request(data: dict[str, Any], client_secret: str) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if client_secret:
        basic = base64.b64encode(f"{data['client_id']}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise TidalError(f"Token-Anfrage fehlgeschlagen ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str
) -> dict[str, Any]:
    return _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        client_secret,
    )


def _refresh(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    payload = _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        client_secret,
    )
    payload.setdefault("refresh_token", refresh_token)
    return payload


def access_token() -> str:
    token = store.get_token("tidal")
    if not token or not token.get("access_token"):
        raise TidalError("TIDAL ist nicht verbunden.")
    if float(token.get("expires_at") or 0) - 60 <= time.time():
        client_id = store.get_setting("tidal_client_id")
        client_secret = store.get_setting("tidal_client_secret")
        payload = _refresh(client_id, client_secret, token.get("refresh_token") or "")
        store.save_token("tidal", payload, account=token.get("account") or "")
        token = store.get_token("tidal") or {}
    return token["access_token"]


def _request(
    method: str,
    path: str,
    *,
    params: list[tuple[str, Any]] | dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = access_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": _JSONAPI}
    if body is not None:
        headers["Content-Type"] = _JSONAPI
    for attempt in range(6):
        resp = requests.request(
            method, f"{API}{path}", params=params, json=body, headers=headers, timeout=40
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2") or 2)
            time.sleep(min(wait, 30))
            continue
        if resp.status_code == 401 and attempt == 0:
            token = access_token()
            headers["Authorization"] = f"Bearer {token}"
            continue
        if resp.status_code >= 400:
            raise TidalError(f"TIDAL {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()
    raise TidalError(f"TIDAL {method} {path}: Rate-Limit nicht aufgeloest.")


def me() -> dict[str, Any]:
    try:
        return _request("GET", "/users/me")
    except TidalError:
        return {}


def lookup_isrcs(isrcs: list[str], country: str) -> dict[str, str]:
    """Bis zu 20 ISRCs pro Anfrage. Rueckgabe: ISRC (gross) -> TIDAL-Track-ID."""
    if not isrcs:
        return {}
    params: list[tuple[str, Any]] = [("countryCode", country)]
    for isrc in isrcs:
        params.append(("filter[isrc]", isrc))
    data = _request("GET", "/tracks", params=params)
    mapping: dict[str, str] = {}
    for track in data.get("data") or []:
        attrs = track.get("attributes") or {}
        isrc = (attrs.get("isrc") or "").upper()
        if isrc and isrc not in mapping:
            mapping[isrc] = track.get("id")
    return mapping


def search_track(query: str, country: str) -> str | None:
    if not query.strip():
        return None
    try:
        data = _request(
            "GET",
            "/searchResults",
            params=[("filter[query]", query), ("countryCode", country), ("include", "tracks")],
        )
    except TidalError:
        try:
            data = _request(
                "GET",
                f"/searchResults/{quote(query, safe='')}",
                params=[("countryCode", country), ("include", "tracks")],
            )
        except TidalError:
            return None
    included = data.get("included") or []
    for item in included:
        if item.get("type") == "tracks" and item.get("id"):
            return item["id"]
    rel = (((data.get("data") or {}).get("relationships") or {}).get("tracks") or {}).get("data") or []
    if rel:
        return rel[0].get("id")
    return None


def create_playlist(name: str, description: str, country: str, access_type: str = "UNLISTED") -> str:
    body = {
        "data": {
            "type": "playlists",
            "attributes": {
                "name": name,
                "description": description,
                "accessType": access_type,
            },
        }
    }
    data = _request("POST", "/playlists", params={"countryCode": country}, body=body)
    playlist_id = ((data.get("data") or {}).get("id"))
    if not playlist_id:
        raise TidalError(f"Playlist wurde angelegt, aber keine ID erhalten: {data}")
    return playlist_id


def add_items(playlist_id: str, track_ids: list[str], country: str, chunk: int = 50) -> int:
    added = 0
    for start in range(0, len(track_ids), chunk):
        part = track_ids[start : start + chunk]
        body = {"data": [{"type": "tracks", "id": tid} for tid in part]}
        _request(
            "POST",
            f"/playlists/{playlist_id}/relationships/items",
            params={"countryCode": country},
            body=body,
        )
        added += len(part)
        time.sleep(0.15)
    return added


def playlist_url(playlist_id: str) -> str:
    return f"https://tidal.com/playlist/{playlist_id}"
