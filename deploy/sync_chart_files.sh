#!/bin/sh
# Kopiert die App-Quellen in das Chart (ConfigMap) und erzeugt Base64-Schriftdateien.
set -eu
cd "$(dirname "$0")"
SRC=..
DST=chart/playlistbridge/files
mkdir -p "$DST"
rm -f "$DST"/*

copy() { cp "$SRC/$1" "$DST/$2"; }

copy app/__init__.py __init__.py
copy app/main.py main.py
copy app/store.py store.py
copy app/spotify.py spotify.py
copy app/tidal.py tidal.py
copy app/migrate.py migrate.py
copy app/static/index.html index.html
copy app/static/app.js app.js
copy app/static/style.css style.css

base64 "$SRC/app/static/fonts/Geist-Variable.woff2" | tr -d "\n" > "$DST/Geist-Variable.woff2.b64"
base64 "$SRC/app/static/fonts/GeistMono-Variable.woff2" | tr -d "\n" > "$DST/GeistMono-Variable.woff2.b64"

echo "Chart-Dateien synchronisiert nach $DST:"
ls -1 "$DST"
