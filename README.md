# Playlist Bridge

Olares app to migrate Spotify playlists to TIDAL.

- Connect Spotify and TIDAL with OAuth 2.0 (PKCE).
- Client credentials are entered in the app UI and stored locally (SQLite on `/Data`).
- Track matching: ISRC lookup first (`GET /tracks?filter[isrc]=`), fallback to search.
- Preview (dry run) shows which tracks were matched before anything is written.
- Then creates a TIDAL playlist and adds the matched tracks.

## Layout

```
app/                 FastAPI backend + static frontend
  main.py            HTTP API, OAuth callbacks, static serving
  spotify.py         Spotify Web API client
  tidal.py           TIDAL Web API (JSON:API v2) client
  migrate.py         Migration job
  store.py           SQLite storage (settings, tokens, jobs)
  static/            UI (Geist fonts, Hanseatenblau + Gold)
deploy/
  chart/playlistbridge/   Helm chart v3 (OlaresManifest, deployment, service)
  OlaresManifest.yaml     Root manifest (identical values)
  icon.png                512x512
Dockerfile
```

## Build

```
docker build --network=host -t ghcr.io/bayerhazard/playlistbridge:26.11.1 .
docker push ghcr.io/bayerhazard/playlistbridge:26.11.1
```

## OAuth redirect URIs

After install, open the app and copy the two redirect URIs shown in the UI:

- `https://<app-domain>/api/spotify/callback`
- `https://<app-domain>/api/tidal/callback`

Register them in your Spotify app (developer.spotify.com) and your TIDAL app
(developer.tidal.com). TIDAL scopes used: `playlists.read playlists.write user.read search.read`.

## Local development

```
PB_DB=/tmp/pb.db uvicorn app.main:asgi_app --host 0.0.0.0 --port 8098
```

For Olares preview (served under `/__preview/<port>/`), set
`APP_BASE_PATH=/__preview/<port>`.
