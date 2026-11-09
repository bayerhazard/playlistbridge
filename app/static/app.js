(() => {
  "use strict";

  const BASE = (() => {
    let p = window.location.pathname;
    if (!p.endsWith("/")) p = p.replace(/\/[^/]*$/, "/");
    return p;
  })();

  const $ = (id) => document.getElementById(id);
  const api = (path, opts) => fetch(BASE + "api/" + path, opts);

  let state = null;
  let pollTimer = null;

  function toast(text, ms = 4000) {
    const el = $("toast");
    el.textContent = text;
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(() => (el.hidden = true), ms);
  }

  function setStatus(provider, connected, account) {
    const el = $(provider + "-status");
    const btn = $(provider + "-verbinden");
    const label = connected
      ? "verbunden" + (account ? " · " + account : "")
      : "nicht verbunden";
    el.innerHTML = `<span class="punkt ${connected ? "an" : "aus"}"></span> ${escapeHtml(label)}`;
    btn.textContent = connected ? "Trennen" : "Verbinden";
    btn.dataset.connected = connected ? "1" : "0";
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  async function loadState() {
    const res = await api("state");
    state = await res.json();
    setStatus("spotify", state.spotify.connected, state.spotify.account);
    setStatus("tidal", state.tidal.connected, state.tidal.account);

    const s = state.settings;
    $("spotify_client_id").value = s.spotify_client_id || "";
    $("tidal_client_id").value = s.tidal_client_id || "";
    $("country_code").value = s.country_code || "DE";
    $("public_base_url").value = s.public_base_url || "";
    $("spotify_client_secret").placeholder = s.has_spotify_secret
      ? "gespeichert – leer lassen zum Behalten"
      : "optional";
    $("tidal_client_secret").placeholder = s.has_tidal_secret
      ? "gespeichert – leer lassen zum Behalten"
      : "optional";

    $("ru-spotify").textContent = state.redirect_uris.spotify;
    $("ru-tidal").textContent = state.redirect_uris.tidal;

    if (state.spotify.connected) {
      await loadPlaylists();
    } else {
      $("playlist").innerHTML = '<option value="">Erst Spotify verbinden …</option>';
    }
    return state;
  }

  async function loadPlaylists() {
    const sel = $("playlist");
    sel.innerHTML = '<option value="">Lade Playlists …</option>';
    try {
      const res = await api("playlists");
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      const data = await res.json();
      const items = data.playlists || [];
      if (!items.length) {
        sel.innerHTML = '<option value="">Keine Playlists gefunden</option>';
        return;
      }
      sel.innerHTML = items
        .map(
          (p) =>
            `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)} (${p.tracks} Titel)</option>`
        )
        .join("");
    } catch (err) {
      sel.innerHTML = `<option value="">Fehler: ${escapeHtml(err.message)}</option>`;
    }
  }

  async function connect(provider) {
    const btn = $(provider + "-verbinden");
    if (btn.dataset.connected === "1") {
      if (!confirm(provider === "spotify" ? "Spotify trennen?" : "TIDAL trennen?")) return;
      await api("disconnect/" + provider, { method: "POST" });
      toast(provider + " getrennt.");
      await loadState();
      return;
    }
    try {
      const res = await api(provider + "/auth");
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      const data = await res.json();
      window.location.href = data.url;
    } catch (err) {
      toast("Verbindung fehlgeschlagen: " + err.message, 6000);
    }
  }

  async function saveSettings(ev) {
    ev.preventDefault();
    const body = {
      spotify_client_id: $("spotify_client_id").value.trim(),
      tidal_client_id: $("tidal_client_id").value.trim(),
      country_code: $("country_code").value.trim() || "DE",
      public_base_url: $("public_base_url").value.trim(),
    };
    const sp = $("spotify_client_secret").value;
    const td = $("tidal_client_secret").value;
    if (sp) body.spotify_client_secret = sp;
    if (td) body.tidal_client_secret = td;
    const res = await api("settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      toast("Speichern fehlgeschlagen.");
      return;
    }
    $("spotify_client_secret").value = "";
    $("tidal_client_secret").value = "";
    toast("Zugangsdaten gespeichert.");
    await loadState();
  }

  function showProgress(show) {
    $("fortschritt").hidden = !show;
    if (!show) {
      $("balken").style.width = "0%";
      $("meldung").textContent = "";
    }
  }

  function setProgress(progress, total, message) {
    const pct = total > 0 ? Math.min(100, Math.round((progress / total) * 100)) : 0;
    $("balken").style.width = pct + "%";
    $("meldung").textContent = message || `${progress}/${total}`;
  }

  async function startMigration(dryRun) {
    const playlistId = $("playlist").value;
    if (!playlistId) {
      toast("Bitte eine Playlist wählen.");
      return;
    }
    if (!state.spotify.connected || !state.tidal.connected) {
      toast("Bitte Spotify und TIDAL zuerst verbinden.");
      return;
    }
    const body = {
      playlist_id: playlistId,
      name: $("name").value.trim(),
      description: $("description").value.trim(),
      access_type: $("access_type").value,
      dry_run: dryRun,
    };
    $("ergebnis").hidden = true;
    showProgress(true);
    setProgress(0, 0, dryRun ? "Vorschau wird erstellt …" : "Migration startet …");
    $("starten").disabled = true;
    $("vorschau").disabled = true;

    try {
      const res = await api("migrate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      const { job_id } = await res.json();
      pollJob(job_id);
    } catch (err) {
      showProgress(false);
      $("starten").disabled = false;
      $("vorschau").disabled = false;
      toast("Start fehlgeschlagen: " + err.message, 6000);
    }
  }

  function pollJob(jobId) {
    clearInterval(pollTimer);
    const tick = async () => {
      let job;
      try {
        const res = await api("jobs/" + jobId);
        if (!res.ok) throw new Error("Job nicht gefunden");
        job = await res.json();
      } catch (err) {
        clearInterval(pollTimer);
        return;
      }
      setProgress(job.progress, job.total, job.message);
      if (job.status === "done" || job.status === "error") {
        clearInterval(pollTimer);
        $("starten").disabled = false;
        $("vorschau").disabled = false;
        renderResult(job);
      }
    };
    tick();
    pollTimer = setInterval(tick, 1200);
  }

  function renderResult(job) {
    const el = $("ergebnis");
    el.hidden = false;
    const r = job.result;
    if (!r) {
      el.innerHTML = `<p class="meldung">${escapeHtml(job.message || "Kein Ergebnis")}</p>`;
      return;
    }
    const rows = (r.tracks || [])
      .slice(0, 300)
      .map(
        (t) =>
          `<tr><td>${escapeHtml(t.name)}</td><td>${escapeHtml(t.artists)}</td>` +
          `<td class="${t.matched ? "ja" : "nein"}">${t.matched ? (t.method === "isrc" ? "ISRC" : "Suche") : "–"}</td></tr>`
      )
      .join("");

    let kopf = "";
    if (job.status === "error") {
      kopf = `<p class="meldung" style="color:var(--am-rot)">${escapeHtml(job.message)}</p>`;
    } else if (r.dry_run) {
      kopf = `<p class="meldung">Vorschau – es wurde noch keine TIDAL-Playlist angelegt.</p>`;
    } else if (r.tidal_url) {
      kopf = `<p class="meldung">Playlist angelegt: <a href="${escapeHtml(r.tidal_url)}" target="_blank" rel="noopener">${escapeHtml(r.tidal_url)}</a></p>`;
    }

    el.innerHTML = `
      ${kopf}
      <div class="zahlen">
        <div class="zahl"><b>${r.total ?? 0}</b><span>Spotify-Titel</span></div>
        <div class="zahl gut"><b>${r.matched ?? 0}</b><span>auf TIDAL gefunden</span></div>
        <div class="zahl schlecht"><b>${r.unmatched ?? 0}</b><span>nicht gefunden</span></div>
        <div class="zahl"><b>${escapeHtml(r.country || "")}</b><span>Markt</span></div>
      </div>
      <details>
        <summary>Titel-Details (${(r.tracks || []).length})</summary>
        <table>
          <thead><tr><th>Titel</th><th>Künstler</th><th>Treffer</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="3" class="leer">Keine Titel.</td></tr>'}</tbody>
        </table>
      </details>`;
  }

  function copyRedirects() {
    const text =
      "Spotify: " + state.redirect_uris.spotify + "\nTIDAL: " + state.redirect_uris.tidal;
    navigator.clipboard
      .writeText(text)
      .then(() => toast("Redirect-URIs kopiert."))
      .catch(() => toast("Kopieren nicht möglich."));
  }

  async function init() {
    $("spotify-verbinden").addEventListener("click", () => connect("spotify"));
    $("tidal-verbinden").addEventListener("click", () => connect("tidal"));
    $("einstellungen").addEventListener("submit", saveSettings);
    $("kopieren").addEventListener("click", copyRedirects);
    $("aktualisieren").addEventListener("click", loadPlaylists);
    $("vorschau").addEventListener("click", () => startMigration(true));
    $("starten").addEventListener("click", () => startMigration(false));

    const params = new URLSearchParams(window.location.search);
    if (params.get("verbunden")) toast(params.get("verbunden") + " verbunden.");
    if (params.get("fehler")) toast("Fehler: " + params.get("fehler"), 8000);
    if (params.get("verbunden") || params.get("fehler")) {
      window.history.replaceState({}, "", BASE);
    }

    await loadState();
  }

  init();
})();
