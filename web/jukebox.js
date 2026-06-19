"use strict";
// Jukebox: play + curate the Star Citizen soundtrack decoded from the p4k. Owns all JUKE_*
// state and the <audio> element; the panel is lazily built on first open (initJukebox) and
// thereafter open/close just toggles the overlay. Music-extraction progress is pushed in via
// jukeApplyMusicState (called from the snapshot stream) — cached so a lazily-built panel
// catches up. Curation (rename/skip/reorder/shuffle) persists server-side via /api/music*.
import { $, esc, num, setHTML, toast } from "./dom.js";
import { postJSON, getJSON } from "./net.js";

// ---- jukebox: play + curate the game soundtrack decoded from the p4k ---- //
// A modal overlay (like Settings), opened from the sidebar's Jukebox button. Lazy-built once
// (initJukebox); thereafter open/close just toggles the overlay so the <audio> element — and any
// playing track — persists across opens. The soundtrack is decoded automatically in the
// background (no button); only the long-form *full songs* (~33) are kept. There are no real names
// in the data — hashed ids only — so the user curates: drag to reorder, rename, and hide duds.
// Curation persists server-side (POST /api/music/curate) and rides the SSE snapshot.
let JUKE_BUILT = false;       // panel skeleton injected?
let JUKE_TRACKS = [];         // manifest rows {id, file, duration, size, system, detail}, longest-first
let JUKE_CURATION = { order: [], skipped: [], names: {} };   // effective curation (default+local)
let JUKE_CUR = null;          // id of the track loaded in the player
let JUKE_PHASE = null;        // last-seen build phase (to catch the extracting->done edge)
let JUKE_SEEKING = false;     // user is dragging the seek bar (don't fight it with timeupdate)
let JUKE_SHUFFLE = false;     // shuffle playback order (persisted in localStorage)
let JUKE_HISTORY = [];        // ids in play order, capped — lets "previous" work under shuffle
let JUKE_RESTORED = false;    // playback state already restored from localStorage this load?
let JUKE_MUSIC = null;        // latest music-extraction state pushed via jukeApplyMusicState
// Playback intent: Play turns this on, Stop turns it off (persisted). When on, a reload resumes
// playing the saved/first track. Initialized from the boot cache so it's known before /api/music.
let JUKE_AUTOPLAY = (() => { try { return localStorage.getItem("jukeAutoplay") === "1"; } catch (_) { return false; } })();
let _jukeDragId = null;       // id of the row being dragged
let _jukeRestoreTime = null;  // pending seek (sec) to apply once the track's metadata loads
let _jukeSavedAt = 0;         // last currentTime (sec) we persisted, to throttle timeupdate saves
// Game-presence auto-pause: while the SC game is running, pause the jukebox so it doesn't fight
// the game's own audio, and resume on exit ONLY what we paused (never override a manual Stop or a
// track the user deliberately started during the game). Driven by the snapshot's game_running.
let JUKE_GAME_RUNNING = null;   // last game_running seen (null until the first snapshot)
let JUKE_PAUSED_BY_GAME = false; // our auto-pause is in effect (so the game-exit should resume it)

// Transport icons as inline SVG (currentColor) so they sit at one size and inherit the theme,
// instead of platform media glyphs that render at odd sizes / colors. 15px via .juke-ic.
const JUKE_IC = {
  prev: `<svg class="juke-ic juke-ic-solid" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 5v14L8 12z"/><rect x="5" y="5" width="2.6" height="14" rx="1"/></svg>`,
  next: `<svg class="juke-ic juke-ic-solid" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5v14l10-7z"/><rect x="16.4" y="5" width="2.6" height="14" rx="1"/></svg>`,
  play: `<svg class="juke-ic juke-ic-solid" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5v14l11-7z"/></svg>`,
  pause: `<svg class="juke-ic juke-ic-solid" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h3.2v14H7zM13.8 5H17v14h-3.2z"/></svg>`,
  stop: `<svg class="juke-ic juke-ic-solid" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>`,
};

function jukeFmt(sec) {
  if (sec == null) return "—";
  const s = Math.round(sec), m = Math.floor(s / 60);
  return m + ":" + String(s % 60).padStart(2, "0");
}

// Like jukeFmt but for long spans (whole-playlist totals): H:MM:SS, or M:SS under an hour.
function jukeFmtLong(sec) {
  const s = Math.round(sec || 0), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

// A stable per-track handle from the longest-first manifest rank (M-01…), used as the default
// label until the user renames a track. Independent of the curated playlist order.
function jukeRank(id) {
  const i = JUKE_TRACKS.findIndex(t => t.id === id);
  return i < 0 ? "M-??" : "M-" + String(i + 1).padStart(2, "0");
}
function jukeName(id) { return JUKE_CURATION.names[id] || jukeRank(id); }
function jukeSkipped(id) { return JUKE_CURATION.skipped.includes(id); }

// Default sort: by System, then Detail (empties last), with longest-first as the final
// tiebreak. This is the canonical order for any track the user hasn't manually placed.
function jukeCmp(a, b) {
  const as = a.system || "￿", bs = b.system || "￿";
  if (as !== bs) return as.localeCompare(bs);
  const ad = a.detail || "￿", bd = b.detail || "￿";
  if (ad !== bd) return ad.localeCompare(bd);
  return (b.duration || 0) - (a.duration || 0);
}

// Track ids in effective playlist order: curated order first (existing ids only), then any
// manifest songs not yet in the order (new after a patch), sorted by System, Detail.
function jukeOrderedIds() {
  const have = new Set(JUKE_TRACKS.map(t => t.id));
  const seen = new Set();
  const out = [];
  for (const id of JUKE_CURATION.order) if (have.has(id) && !seen.has(id)) { out.push(id); seen.add(id); }
  for (const t of JUKE_TRACKS.filter(t => !seen.has(t.id)).slice().sort(jukeCmp)) out.push(t.id);
  return out;
}

export function initJukebox() {
  if (!JUKE_BUILT) {
    setHTML("jukeboxBody",
      `<div class="juke-bar">
        <span class="juke-status" id="jukeStatus"></span>
        <span class="juke-total" id="jukeTotal"></span>
      </div>
      <ul class="juke-list" id="jukeList"></ul>`);
    setHTML("jukeboxFoot",
      `<div class="juke-player">
        <div class="juke-now" id="jukeNow">Nothing playing</div>
        <div class="juke-transport">
          <button class="juke-nav juke-shuf" id="jukeShuffle" title="Shuffle" aria-label="Shuffle" aria-pressed="false"><svg class="juke-ic" viewBox="0 0 24 24" aria-hidden="true"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg></button>
          <button class="juke-nav" id="jukePrev" title="Previous" aria-label="Previous track">${JUKE_IC.prev}</button>
          <button class="juke-play" id="jukePlay" title="Play" aria-label="Play" disabled>${JUKE_IC.play}</button>
          <button class="juke-nav" id="jukeStop" title="Stop" aria-label="Stop" disabled>${JUKE_IC.stop}</button>
          <button class="juke-nav" id="jukeNext" title="Next" aria-label="Next track">${JUKE_IC.next}</button>
          <span class="juke-time" id="jukeCur">0:00</span>
          <input class="juke-seek" id="jukeSeek" type="range" min="0" max="100" step="0.1" value="0" aria-label="Seek" disabled>
          <span class="juke-time" id="jukeDur">0:00</span>
          <audio id="jukeAudio" preload="metadata"></audio>
        </div>
      </div>`);
    $("jukePrev").onclick = () => jukeStep(-1);
    $("jukeNext").onclick = () => jukeStep(1);
    $("jukePlay").onclick = jukeToggle;
    $("jukeStop").onclick = jukeStop;
    JUKE_SHUFFLE = (() => { try { return localStorage.getItem("jukeShuffle") === "1"; } catch (_) { return false; } })();
    $("jukeShuffle").onclick = jukeToggleShuffle;
    jukeReflectShuffle();
    const a = $("jukeAudio");
    a.onended = () => jukeStep(1);
    // Any play (manual or our game-exit resume) clears the auto-pause claim, so a track the user
    // deliberately starts during the game isn't re-paused/double-resumed by jukeOnGameRunning.
    a.onplay = () => { JUKE_PAUSED_BY_GAME = false; jukeSetPlaying(true); jukePersist(); };
    a.onpause = () => { jukeSetPlaying(false); jukePersist(); };
    a.onloadedmetadata = () => {
      const s = $("jukeSeek");
      s.max = a.duration || 0; s.disabled = false;
      if (_jukeRestoreTime != null) {           // resuming a saved position after a reload
        a.currentTime = Math.min(_jukeRestoreTime, a.duration || _jukeRestoreTime);
        _jukeRestoreTime = null;
      }
      s.value = a.currentTime || 0;
      $("jukeCur").textContent = jukeFmt(a.currentTime);
      $("jukeDur").textContent = jukeFmt(a.duration);
    };
    a.ontimeupdate = () => {
      if (JUKE_SEEKING) return;                 // don't yank the thumb out from under a drag
      $("jukeSeek").value = a.currentTime || 0;
      $("jukeCur").textContent = jukeFmt(a.currentTime);
      const f = $("jukeMiniFill");           // mirror progress onto the mini player's bar
      if (f && a.duration) f.style.width = (100 * a.currentTime / a.duration) + "%";
      if (Math.abs((a.currentTime || 0) - _jukeSavedAt) >= 5) jukePersist();  // throttle ~5s
    };
    const seek = $("jukeSeek");
    seek.oninput = () => { JUKE_SEEKING = true; $("jukeCur").textContent = jukeFmt(+seek.value); };
    seek.onchange = () => { a.currentTime = +seek.value; JUKE_SEEKING = false; jukePersist(); };
    window.addEventListener("pagehide", jukePersist);   // last-chance save on navigate/close
    jukeBuildMini();
    jukeInitMediaSession();
    JUKE_BUILT = true;
    if (JUKE_MUSIC) jukeApplyMusicState(JUKE_MUSIC);  // reflect an in-flight build (latest pushed state)
  }
  jukeLoad();
}

// Run onPrimary() only when this tab owns the jukebox. Exactly one tab holds the
// "starlogger-juke-primary" lock at a time; we hold it for the tab's lifetime (a
// never-resolving promise) so it auto-releases on close and the next open tab becomes
// primary -- so playback can survive the primary tab closing. Without this, every tab
// builds its own player and auto-resumes, so multiple tabs play music at once. Degrades
// to sole-owner if Web Locks is unavailable (or errors), so music is never locked out.
export function claimJukeboxPrimary(onPrimary) {
  const locks = navigator.locks;
  if (!locks || !locks.request) { onPrimary(); return; }
  locks.request("starlogger-juke-primary", () => {
    onPrimary();
    return new Promise(() => {});            // hold the lock until this tab goes away
  }).catch(() => onPrimary());
}

export function openJukebox() {
  initJukebox();                       // lazy-build + refresh the track list
  const ov = $("jukeboxOverlay");
  ov.classList.remove("hide");
  ov.setAttribute("aria-hidden", "false");
  try { localStorage.setItem("jukeOpen", "1"); } catch (_) {}   // reopen on next load
  jukeUpdateMini();                    // modal now owns the transport → hide the mini
}

export function closeJukebox() {
  const ov = $("jukeboxOverlay");      // just hides it — the <audio> keeps playing
  ov.classList.add("hide");
  ov.setAttribute("aria-hidden", "true");
  try { localStorage.setItem("jukeOpen", "0"); } catch (_) {}
  jukeUpdateMini();                    // a track may still be playing → reveal the mini player
}

// A compact transport pinned just above the sidebar Jukebox button, shown while a track is
// loaded and the full modal is closed — so playback stays controllable without reopening it.
function jukeBuildMini() {
  const nav = $("navjukebox");
  if (!nav || $("jukeMini")) return;
  nav.insertAdjacentHTML("beforebegin",
    `<div id="jukeMini" class="sb-mini hide">
      <button class="sb-mini-now" id="jukeMiniNow" title="Open Jukebox" aria-label="Open Jukebox">
        <span class="sb-mini-eq" aria-hidden="true"><i></i><i></i><i></i></span>
        <span class="sb-mini-title" id="jukeMiniTitle"></span>
      </button>
      <div class="sb-mini-ctrls">
        <button class="sb-mini-btn" id="jukeMiniPrev" title="Previous" aria-label="Previous track">${JUKE_IC.prev}</button>
        <button class="sb-mini-btn" id="jukeMiniPlay" title="Play/pause" aria-label="Play or pause">${JUKE_IC.play}</button>
        <button class="sb-mini-btn" id="jukeMiniNext" title="Next" aria-label="Next track">${JUKE_IC.next}</button>
      </div>
      <div class="sb-mini-bar" aria-hidden="true"><div class="sb-mini-fill" id="jukeMiniFill"></div></div>
    </div>`);
  $("jukeMiniNow").onclick = openJukebox;
  $("jukeMiniPrev").onclick = () => jukeStep(-1);
  $("jukeMiniPlay").onclick = jukeToggle;
  $("jukeMiniNext").onclick = () => jukeStep(1);
}

// Reflect now-playing state onto the mini player and toggle its visibility (track loaded AND
// modal closed). Cheap; called on track change, play/pause, and open/close.
function jukeUpdateMini() {
  const mini = $("jukeMini");
  if (!mini) return;
  const modalOpen = !$("jukeboxOverlay")?.classList.contains("hide");
  const show = !!JUKE_CUR && !modalOpen;
  mini.classList.toggle("hide", !show);
  if (!show) return;
  $("jukeMiniTitle").textContent = jukeName(JUKE_CUR);
  const a = $("jukeAudio"), playing = a && !a.paused;
  $("jukeMiniPlay").innerHTML = playing ? JUKE_IC.pause : JUKE_IC.play;
  mini.classList.toggle("playing", !!playing);
}

async function jukeLoad() {
  try {
    const d = await getJSON("/api/music");
    JUKE_TRACKS = (d && d.tracks) || [];
    const c = (d && d.curation) || {};
    JUKE_CURATION = { order: c.order || [], skipped: c.skipped || [], names: c.names || {} };
  } catch (_) {
    JUKE_TRACKS = [];
  }
  renderJukeList();
  if (!JUKE_RESTORED) { JUKE_RESTORED = true; jukeRestore(); }   // resume saved playback, once
}

// On first load: restore the saved track at its position, and decide whether to play. With
// autoplay on (the user last hit Play, not Stop), start playing (the saved track, or the first
// track if none); with it off, the saved track is restored paused. Autoplay may be blocked until
// a gesture — then play() rejects and we just stay paused at the restored spot.
function jukeRestore() {
  // If the game is already running when this tab restores playback (mid-game reload), load the
  // track PAUSED and mark it ours so the game-exit resumes it — don't autoplay over the game.
  const hold = JUKE_GAME_RUNNING === true;
  let st;
  try { st = JSON.parse(localStorage.getItem("jukeState") || "null"); } catch (_) { st = null; }
  if (st && st.id && JUKE_TRACKS.some(t => t.id === st.id)) {
    jukeLoadTrack(st.id, { time: +st.time || 0, autoplay: JUKE_AUTOPLAY && !hold });
    if (JUKE_AUTOPLAY && hold) JUKE_PAUSED_BY_GAME = true;
  } else if (JUKE_AUTOPLAY) {
    const first = jukeOrderedIds().find(id => !jukeSkipped(id));   // nothing saved → start the playlist
    if (first) { jukeLoadTrack(first, { time: 0, autoplay: !hold }); if (hold) JUKE_PAUSED_BY_GAME = true; }
  }
}

function renderJukeList() {
  const list = $("jukeList");
  if (!list) return;
  if (!JUKE_TRACKS.length) {
    list.innerHTML = `<li class="juke-empty">Music is being prepared in the background — the full songs are decoded from your game files once. This list fills in automatically when it's ready.</li>`;
    return;
  }
  const byId = Object.fromEntries(JUKE_TRACKS.map(t => [t.id, t]));
  const visible = jukeOrderedIds();          // every track is shown; skipped ones stay, greyed
  const total = visible.reduce((s, id) => s + (byId[id]?.duration || 0), 0);
  const tot = $("jukeTotal");
  if (tot) tot.textContent = `${visible.length} track${visible.length === 1 ? "" : "s"} · ${jukeFmtLong(total)}`;
  const rows = visible
    .map(id => {
      const t = byId[id]; if (!t) return "";
      const skip = jukeSkipped(id);
      // context: where this cue plays in-game, mined from the music switch hierarchy — a readable
      // hint the FNV-hashed ids otherwise lack. Shown as a "System · Detail" subtitle.
      const ctxText = [t.system || "", t.detail || ""].filter(Boolean).join(" · ");
      const ctx = ctxText
        ? `<span class="juke-context" title="${esc(ctxText)}">${esc(ctxText)}</span>` : "";
      return `<li class="juke-row${skip ? " skipped-row" : ""}" draggable="true" data-id="${esc(id)}" data-dur="${t.duration || 0}" data-file="${esc(t.file)}">` +
        `<span class="juke-grip" title="Drag to reorder" aria-hidden="true">⠿</span>` +
        `<span class="juke-num" aria-hidden="true"></span>` +
        `<div class="juke-title" title="${esc("#" + id)}">` +
          `<span class="juke-name">${esc(jukeName(id))}</span>${ctx}` +
        `</div>` +
        `<span class="juke-dur">${jukeFmt(t.duration)}</span>` +
        `<button class="juke-act juke-rename" title="Rename" aria-label="Rename track">✎</button>` +
        `<button class="juke-act juke-skip" title="${skip ? "Un-skip" : "Skip in playback"}" aria-label="${skip ? "Un-skip" : "Skip"} track">${skip ? "↺" : "⏭"}</button>` +
        `</li>`;
    }).join("");
  list.innerHTML = rows;
  list.querySelectorAll(".juke-row").forEach(r => {
    const id = r.dataset.id;
    r.querySelector(".juke-title").onclick = () => jukePlay(id);
    r.querySelector(".juke-dur").onclick = () => jukePlay(id);
    r.querySelector(".juke-rename").onclick = (e) => { e.stopPropagation(); jukeRename(id); };
    r.querySelector(".juke-skip").onclick = (e) => { e.stopPropagation(); jukeToggleSkip(id); };
    r.ondragstart = (e) => { _jukeDragId = id; r.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; };
    r.ondragend = () => { r.classList.remove("dragging"); _jukeDragId = null; jukeCommitOrder(); };
    r.ondragover = (e) => { e.preventDefault(); jukeDragOver(r, e.clientY); };
  });
  const pb = $("jukePlay"), sb = $("jukeStop");
  if (pb) pb.disabled = !JUKE_TRACKS.length;   // can start playback once there are songs
  if (sb) sb.disabled = !JUKE_TRACKS.length;
  jukeRenumber();
  if (JUKE_CUR) _jukeHighlight(JUKE_CUR);
}

// Stamp 1-based playlist positions into the .juke-num cells from current DOM order. Called after
// render and live during a drag (rows move without a re-render), so the numbers always match.
function jukeRenumber() {
  $("jukeList")?.querySelectorAll(".juke-row").forEach((r, i) => {
    const n = r.querySelector(".juke-num"); if (n) n.textContent = i + 1;
  });
}

// Live-reorder the DOM as a row is dragged over another, so the drop lands where you see it.
function jukeDragOver(target, y) {
  const list = $("jukeList");
  const dragging = list?.querySelector(".juke-row.dragging");
  if (!dragging || dragging === target) return;
  const rect = target.getBoundingClientRect();
  const before = y < rect.top + rect.height / 2;
  list.insertBefore(dragging, before ? target : target.nextSibling);
  jukeRenumber();
}

// Persist the current on-screen order. Every track (skipped included) is in the list, so the
// DOM order is the full order — no splicing needed.
function jukeCommitOrder() {
  const order = [...($("jukeList")?.querySelectorAll(".juke-row") || [])].map(r => r.dataset.id);
  JUKE_CURATION.order = order;          // optimistic
  jukeRenumber();
  jukeSave({ order });
}

async function jukeRename(id) {
  const cur = JUKE_CURATION.names[id] || "";
  const name = window.prompt(`Name for ${jukeRank(id)} (#${id}):`, cur);
  if (name === null) return;            // cancelled
  const trimmed = name.trim();
  if (trimmed) JUKE_CURATION.names[id] = trimmed; else delete JUKE_CURATION.names[id];
  renderJukeList();
  if (JUKE_CUR === id) jukeNowPlayingLabel(id);
  await jukeSave({ names: { [id]: trimmed } });
}

function jukeToggleSkip(id) {
  const skip = jukeSkipped(id);
  JUKE_CURATION.skipped = skip
    ? JUKE_CURATION.skipped.filter(x => x !== id)
    : [...JUKE_CURATION.skipped, id];
  renderJukeList();
  jukeSave({ skipped: JUKE_CURATION.skipped });
}

async function jukeSave(patch) {
  try { await postJSON("/api/music/curate", patch); }
  catch (_) { toast("Couldn't save jukebox change", "err"); }
}

function jukeNowPlayingLabel(id) {
  const t = JUKE_TRACKS.find(x => x.id === id);
  const dur = t ? jukeFmt(t.duration) : "";
  const now = $("jukeNow");
  if (now) now.textContent = `${jukeName(id)} · ${dur}`;
  jukeUpdateMini();
  if ("mediaSession" in navigator) {       // OS/lock-screen "now playing" card
    navigator.mediaSession.metadata = new MediaMetadata({
      title: jukeName(id), artist: "Star Citizen", album: "Soundtrack",
    });
  }
}

// Load a track into the player. autoplay=false (with time>0) is used to restore a saved
// position paused; the seek is applied in onloadedmetadata via _jukeRestoreTime. Looks the file
// up from the manifest (not the DOM) so it works even for skipped tracks or before the modal opens.
function jukeLoadTrack(id, { time = 0, autoplay = true } = {}) {
  const t = JUKE_TRACKS.find(x => x.id === id);
  if (!t) return;
  const audio = $("jukeAudio");
  audio.src = "/music/" + encodeURIComponent(t.file);
  _jukeRestoreTime = time > 0 ? time : null;
  JUKE_CUR = id;
  JUKE_HISTORY.push(id);
  if (JUKE_HISTORY.length > 50) JUKE_HISTORY.shift();
  _jukeHighlight(id);
  jukeNowPlayingLabel(id);
  if (autoplay) audio.play().catch(() => {});   // autoplay may be blocked until a gesture
  jukePersist();
}

function jukePlay(id) { jukeSetAutoplay(true); jukeLoadTrack(id, { time: 0, autoplay: true }); }

// Playback intent, driven by the Play/Stop buttons (and any explicit play). Persisted so a
// reload knows whether to resume. See jukeRestore.
function jukeSetAutoplay(on) {
  JUKE_AUTOPLAY = on;
  try { localStorage.setItem("jukeAutoplay", on ? "1" : "0"); } catch (_) {}
}

// Save the now-playing track, position, and play state so a reload can resume them. While a
// restore is pending (metadata not loaded, currentTime still 0) use the target seek, so a save
// in that window doesn't clobber the saved position with 0.
function jukePersist() {
  try {
    if (!JUKE_CUR) { localStorage.removeItem("jukeState"); return; }
    const a = $("jukeAudio");
    _jukeSavedAt = _jukeRestoreTime != null ? _jukeRestoreTime : (a.currentTime || 0);
    localStorage.setItem("jukeState", JSON.stringify({ id: JUKE_CUR, time: _jukeSavedAt, playing: !a.paused }));
  } catch (_) {}
}

function jukeToggleShuffle() {
  JUKE_SHUFFLE = !JUKE_SHUFFLE;
  try { localStorage.setItem("jukeShuffle", JUKE_SHUFFLE ? "1" : "0"); } catch (_) {}
  jukeReflectShuffle();
}

function jukeReflectShuffle() {
  const b = $("jukeShuffle");
  if (!b) return;
  b.classList.toggle("on", JUKE_SHUFFLE);
  b.setAttribute("aria-pressed", JUKE_SHUFFLE ? "true" : "false");
}

// Toggle play/pause; with nothing loaded yet, start the first visible track. Starting playback
// (here or via a track click) turns autoplay ON — only Stop turns it off.
function jukeToggle() {
  const a = $("jukeAudio");
  if (!JUKE_CUR) { jukeSetAutoplay(true); jukeStep(1); return; }
  if (a.paused) { jukeSetAutoplay(true); a.play().catch(() => {}); } else jukeUserPause();
}

// Stop: halt playback, rewind to the start, and turn autoplay OFF so a reload won't resume.
function jukeStop() {
  const a = $("jukeAudio");
  a.pause();
  a.currentTime = 0;
  JUKE_PAUSED_BY_GAME = false;   // a deliberate Stop overrides any game auto-pause -> don't resume
  jukeSetAutoplay(false);
  jukePersist();
}

// A user-initiated pause -- the Play/Pause button, the mini player, or the OS media controls --
// pauses AND drops the resume intent so an app update / reload won't restart it, unlike the game
// auto-pause (which keeps the intent so it can resume on game exit).
function jukeUserPause() {
  jukeSetAutoplay(false);
  JUKE_PAUSED_BY_GAME = false;   // a deliberate pause also overrides any game auto-pause claim
  const a = $("jukeAudio");
  if (a) a.pause();
}

// Reflect playing state onto the play/pause button + the OS media session.
function jukeSetPlaying(on) {
  const btn = $("jukePlay");
  if (btn) {
    btn.innerHTML = on ? JUKE_IC.pause : JUKE_IC.play;
    btn.title = on ? "Pause" : "Play";
    btn.setAttribute("aria-label", on ? "Pause" : "Play");
  }
  jukeUpdateMini();
  if ("mediaSession" in navigator) navigator.mediaSession.playbackState = on ? "playing" : "paused";
}

// Wire hardware/lock-screen media keys (play/pause/prev/next/seek) to the jukebox, once.
function jukeInitMediaSession() {
  if (!("mediaSession" in navigator)) return;
  const ms = navigator.mediaSession, a = () => $("jukeAudio");
  const set = (act, fn) => { try { ms.setActionHandler(act, fn); } catch (_) {} };
  set("play", () => a().play().catch(() => {}));
  set("pause", () => jukeUserPause());
  set("stop", () => jukeStop());
  set("previoustrack", () => jukeStep(-1));
  set("nexttrack", () => jukeStep(1));
  set("seekto", (d) => { if (d.seekTime != null) a().currentTime = d.seekTime; });
}

function _jukeHighlight(id) {
  $("jukeList")?.querySelectorAll(".juke-row").forEach(r =>
    r.classList.toggle("playing", r.dataset.id === id));
}

// Step to the previous/next playable track; auto-advance (audio 'ended') reuses this with
// dir=+1 and simply stops at the end of the list. Skipped tracks stay in the list but are
// bypassed here (a manually-played skipped track still steps on to its next playable neighbor).
// Under shuffle, "next" picks a random other playable track and "previous" walks the history.
function jukeStep(dir) {
  const allRows = [...($("jukeList")?.querySelectorAll(".juke-row") || [])];
  const playable = allRows.filter(r => !jukeSkipped(r.dataset.id));
  if (!playable.length) return;
  if (JUKE_SHUFFLE) {
    if (dir > 0) {
      const pool = playable.filter(r => r.dataset.id !== JUKE_CUR);
      const bag = pool.length ? pool : playable;
      jukePlay(bag[Math.floor(Math.random() * bag.length)].dataset.id);
      return;
    }
    JUKE_HISTORY.pop();                                   // drop current
    const prev = JUKE_HISTORY.pop();                      // jukePlay re-pushes it
    if (prev && playable.some(r => r.dataset.id === prev)) { jukePlay(prev); return; }
    // no history → fall through to sequential previous
  }
  let i = allRows.findIndex(r => r.dataset.id === JUKE_CUR);
  if (i < 0) { jukePlay((dir > 0 ? playable[0] : playable[playable.length - 1]).dataset.id); return; }
  for (i += dir; i >= 0 && i < allRows.length; i += dir) {   // walk to the next non-skipped neighbor
    if (!jukeSkipped(allRows[i].dataset.id)) { jukePlay(allRows[i].dataset.id); return; }
  }
  // off either end → stop
}

// Reflect the server's background-build state (pushed in every snapshot) onto the jukebox.
export function jukeApplyMusicState(m) {
  JUKE_MUSIC = m;   // cache the latest pushed music state so a lazily-built panel can catch up
  const wasExtracting = JUKE_PHASE === "extracting";
  JUKE_PHASE = m.phase;
  const st = $("jukeStatus");
  if (st) {
    st.classList.remove("err");
    if (m.phase === "extracting") st.textContent = m.total ? `Preparing music… ${m.done}/${m.total}` : "Preparing music…";
    else if (m.phase === "error") { st.textContent = m.error || "Music build failed."; st.classList.add("err"); }
    else st.textContent = "";
  }
  if (m.phase === "done" && wasExtracting) jukeLoad();   // finished just now → pull the fresh list in
}

// React to the game launching/exiting (snapshot.game_running, pushed every snapshot from the
// stream). On launch: pause the jukebox if it's playing so it doesn't fight the game's audio,
// remembering we did so. On exit: resume ONLY if our auto-pause is still in effect — a manual
// Stop/play during the game clears it (jukeStop / the onplay handler), so we never fight the user.
// Edge-triggered, but the launch branch also catches the "already running on first snapshot" case
// (it pauses whatever is currently playing); jukeRestore covers the inverse load-order.
export function jukeOnGameRunning(running) {
  running = !!running;
  if (running === JUKE_GAME_RUNNING) return;   // unchanged (or a same-valued re-delivery)
  const first = JUKE_GAME_RUNNING === null;
  JUKE_GAME_RUNNING = running;
  const a = $("jukeAudio");
  if (running) {
    if (a && JUKE_CUR && !a.paused) { a.pause(); JUKE_PAUSED_BY_GAME = true; }
  } else {
    // game exited (ignore the first-ever snapshot reporting "not running" — nothing to resume)
    if (!first && JUKE_PAUSED_BY_GAME && a && JUKE_CUR) a.play().catch(() => {});
    JUKE_PAUSED_BY_GAME = false;
  }
}

// Apply a freshly-received live snapshot — from the SSE push or a manual refresh().
