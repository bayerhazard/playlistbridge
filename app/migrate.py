"""Migrations-Lauf: Spotify-Playlist -> TIDAL-Playlist."""

from __future__ import annotations

import time
from typing import Any, Iterable

from . import spotify, store, tidal


def _chunks(seq: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def run(job_id: str, playlist_id: str, options: dict[str, Any]) -> None:
    try:
        dry_run = bool(options.get("dry_run"))
        country = (store.get_setting("country_code") or "DE").upper()

        store.update_job(job_id, status="running", message="Spotify-Playlist wird gelesen …")

        def spotify_progress(done: int, total: int) -> None:
            store.update_job(
                job_id,
                progress=done,
                total=total,
                message=f"Spotify: {done}/{total} Titel gelesen",
            )

        data = spotify.playlist_tracks(playlist_id, on_progress=spotify_progress)
        tracks = data["tracks"]
        total = len(tracks)

        if total == 0:
            store.update_job(
                job_id, status="done", result={"total": 0, "matched": 0, "tracks": []},
                message="Playlist ist leer.",
            )
            return

        store.update_job(
            job_id,
            total=total,
            progress=0,
            message=f"{total} Titel gelesen. TIDAL-Abgleich über ISRC …",
        )

        # 1) ISRC-Abgleich (beste Qualität), gebuendelt zu 20
        isrc_pairs = [(i, t["isrc"]) for i, t in enumerate(tracks) if t.get("isrc")]
        isrc_map: dict[str, str] = {}
        for batch in _chunks(isrc_pairs, 20):
            found = tidal.lookup_isrcs([isrc for _, isrc in batch], country)
            isrc_map.update(found)
            store.update_job(
                job_id,
                message=f"TIDAL-ISRC-Abgleich: {min(len(isrc_map), total)} Treffer",
            )
            time.sleep(0.1)

        # 2) Zuordnung + optionaler Namens-/Kuenstler-Fallback
        items: list[dict[str, Any]] = []
        matched = 0
        for i, t in enumerate(tracks):
            tidal_id = isrc_map.get((t.get("isrc") or "").upper())
            method = "isrc" if tidal_id else ""
            if not tidal_id:
                query = f'{t["name"]} {t["artists"]}'.strip()
                tidal_id = tidal.search_track(query, country)
                method = "search" if tidal_id else ""
            if tidal_id:
                matched += 1
            items.append(
                {
                    "name": t["name"],
                    "artists": t["artists"],
                    "album": t["album"],
                    "isrc": t.get("isrc") or "",
                    "matched": bool(tidal_id),
                    "method": method,
                    "tidal_id": tidal_id or "",
                    "url": t.get("url") or "",
                }
            )
            store.update_job(
                job_id,
                progress=len(items),
                message=f"Abgleich: {len(items)}/{total} · {matched} Treffer",
            )

        unmatched = [x for x in items if not x["matched"]]

        if dry_run:
            store.update_job(
                job_id,
                status="done",
                result={
                    "dry_run": True,
                    "playlist": data["name"],
                    "total": total,
                    "matched": matched,
                    "unmatched": len(unmatched),
                    "country": country,
                    "tracks": items,
                },
                message=f"Vorschau fertig: {matched}/{total} Treffer.",
            )
            return

        if matched == 0:
            store.update_job(
                job_id,
                status="error",
                result={
                    "total": total,
                    "matched": 0,
                    "unmatched": len(unmatched),
                    "tracks": items,
                },
                message="Kein Titel konnte auf TIDAL gefunden werden.",
            )
            return

        # 3) TIDAL-Playlist anlegen und Titel hinzufuegen
        name = (options.get("name") or data["name"] or "Spotify-Import").strip()
        description = (options.get("description") or "").strip()
        if not description:
            description = "Importiert aus Spotify mit Playlist Bridge"
        access_type = options.get("access_type") or "UNLISTED"
        if access_type not in ("PUBLIC", "UNLISTED"):
            access_type = "UNLISTED"

        store.update_job(job_id, message="TIDAL-Playlist wird angelegt …")
        tidal_playlist_id = tidal.create_playlist(name, description, country, access_type)

        matched_ids = [x["tidal_id"] for x in items if x["matched"]]
        store.update_job(
            job_id,
            message=f"Titel werden hinzugefügt: 0/{len(matched_ids)}",
        )
        tidal.add_items(tidal_playlist_id, matched_ids, country)

        store.update_job(
            job_id,
            status="done",
            result={
                "dry_run": False,
                "playlist": name,
                "tidal_playlist_id": tidal_playlist_id,
                "tidal_url": tidal.playlist_url(tidal_playlist_id),
                "total": total,
                "matched": matched,
                "unmatched": len(unmatched),
                "country": country,
                "tracks": items,
            },
            message=f"Fertig: {matched}/{total} Titel übernommen.",
        )
    except Exception as exc:  # noqa: BLE001
        store.update_job(job_id, status="error", message=f"Fehler: {exc}")
