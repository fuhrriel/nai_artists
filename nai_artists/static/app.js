/* nai_artists frontend.
   Session 4: matrix + lightbox + copy. Session 5: generate, fill, import, templates, queue drawer (SSE),
   battery gauge, missing-cell enqueue, "new template from image". Session 6: ratings (row chips, cell
   badges, lightbox keys), filter / sort, stale handling (per-cell / per-row / per-column / toolbar regen),
   regenerate, keyboard shortcuts + help. Session 8: sticky battery force + Anlas floor, artist labels
   (include / exclude filter), Gelbooru reference images (refs column, lightbox compare), column drag
   reorder, collapsed columns keep their cells aligned. Session 9 (shareable): one click enqueues at most one
   cell (POST /api/generate); the Generate tab only adds artist rows; no batch fill, no batch regen.
   State lives in the URL hash: #<tab>[/<artist_slug>/<template_id>][?sort=&dir=&q=&show=&inc=&exc=&lmode=];
   the path part opens the lightbox, the query part is the matrix view. localStorage holds only view
   preferences: active tab, hidden columns, column order, NSFW refs shown. */

"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const REFS_COL = "@refs";  // column id of the reference-images column (never a template id: those are [a-z0-9_-])

const state = {
  matrix: null,            // /api/matrix payload
  hidden: new Set(loadJSON("hiddenCols", [])),  // hidden column ids (templates and REFS_COL)
  order: loadJSON("colOrder", []),              // column ids in display order, see columns()
  showNsfw: loadJSON("showNsfw", false),        // questionable / explicit refs unblurred
  lb: null,                // {slug, template, base: bool, ref: null | index into the artist's refs} while the lightbox is open
  metaCache: new Map(),    // image_id -> /api/images/{id}/meta payload
  queue: null,             // last /api/jobs state (from SSE)
  battery: null,           // last /api/subscription payload
  templates: null,         // /api/templates payload (list with counts)
  settings: null,          // /api/settings payload
  importFiles: new Map(),  // filename -> File, kept so a no_match result can be re-imported / turned into a template
  drawerOpen: false,
  labelEdit: null,         // slug whose row header shows the label editor
  refsAsked: new Set(),    // slugs sent to /api/refs/fetch, not finished yet ("fetching…" cells)
  // matrix filter / sort, mirrored in the URL hash. inc / exc: label filter; lmode: inc needs "all" or "any"
  view: { sort: "name", dir: "asc", q: "", show: "all", inc: [], exc: [], lmode: "all" },
};

// --- small helpers ------------------------------------------------------------

function loadJSON(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); }
  catch { return fallback; }
}
function saveJSON(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
}
function saveHidden() {
  saveJSON("hiddenCols", [...state.hidden]);
}

let toastTimer = null;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("err", err);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, err ? 4000 : 1800);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } finally { ta.remove(); }
  }
  toast(`copied: ${text}`);
}

function artistLabel(a) {
  return a.slug === state.matrix.base_slug ? "no artist" : a.tags.join(" + ");
}
function artistTagText(a, bare) {
  return bare ? a.tags.join(", ") : a.tags.map(t => `artist:${t}`).join(", ");
}
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) e.setAttribute(k, v);
  }
  for (const c of children) if (c !== null && c !== undefined) e.append(c);
  return e;
}
function fmtDate(iso) {
  return iso ? iso.replace("T", " ").replace(/Z$/, "") : "";
}
function debounce(fn, ms) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// --- data ---------------------------------------------------------------------

/* fetch wrapper. opts: {method, json, form}. Errors carry .status and .detail (string or object). */
async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.json || opts.form ? "POST" : "GET") };
  if (opts.json !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  } else if (opts.form) {
    init.body = opts.form;
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch {}
    const err = new Error(`${path}: ${r.status} ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
    err.status = r.status;
    err.detail = detail;
    throw err;
  }
  return r.json();
}

async function loadMatrix() {
  try {
    state.matrix = await api("/api/matrix");
  } catch (e) {
    toast(e.message, true);
    return;
  }
  $("#gen-refs-wrap").hidden = !state.matrix.refs.configured;
  renderMatrix();
  renderLabelOptions();
  renderLabelFilter();
  if (!state.queue) renderQueuePill(state.matrix.queue);  // until the SSE stream delivers
  if (state.lb) renderLightbox();  // refresh the open cell too
}

function artistIndex(slug) {
  return state.matrix.artists.findIndex(a => a.slug === slug);
}
function templateIndex(tid) {
  return state.matrix.templates.findIndex(t => t.id === tid);
}
function cellOf(slug, tid) {
  const a = state.matrix.artists[artistIndex(slug)];
  return a ? (a.cells[tid] || null) : null;
}
function hasImage(cell) {
  return !!(cell && cell.image_id && !cell.file_missing);
}
/* Cell files keep their path across regenerations, so the browser would serve the old bytes.
   created_at changes on every (re)registration; use it as a cache buster. */
function fileUrl(cell, thumb = false) {
  return `/${thumb ? cell.thumb : cell.path}?v=${encodeURIComponent(cell.created_at || "")}`;
}

/* The battery gate answers 409 with {error, forceable, battery_percent, anlas, ...}. Ask, then retry
   forced; the override holds until the queue runs dry. `forceable: false` is the Anlas floor (battery
   empty, less than one image of Anlas): no override offered. */
async function withBatteryConfirm(fn) {
  try {
    return await fn(false);
  } catch (e) {
    if (e.status !== 409 || !e.detail || typeof e.detail !== "object") throw e;
    const d = e.detail;
    if (d.forceable === false) {
      const err = new Error(`battery empty and ${d.anlas} Anlas left (< ${d.anlas_per_image} per image): not forceable`);
      err.status = 409;
      err.detail = d;
      throw err;
    }
    const where = d.is_negative ? "battery is NEGATIVE" : `battery at ${d.battery_percent}%`;
    const anlas = d.anlas_mode
      ? `Every image now costs Anlas: ${d.anlas} left ≈ ${Math.floor(d.anlas / d.anlas_per_image)} image(s) at ${d.anlas_per_image}.`
      : `Anlas ${d.anlas}: only spent once the battery is empty; the queue stops below ${d.anlas_per_image}.`;
    const msg = `Battery guard: ${where} (threshold ${d.min_percent}%).\n${anlas}\n\nForce it? The override holds until the queue runs dry.`;
    if (!window.confirm(msg)) return null;
    return fn(true);
  }
}

// --- queue: pill, drawer, SSE ---------------------------------------------------

function pendingCount(q) {
  const c = q.counts || {};
  return (c.queued || 0) + (c.running || 0);
}

function queueText(q) {
  const pending = pendingCount(q);
  const f = q.forced ? " · forced" : "";
  if (q.paused) return `paused: ${q.pause_reason || "by user"}${pending ? ` · ${pending} pending` : ""}`;
  if (q.waiting) return `waiting ${waitLeft(q.waiting)}s (${q.waiting.reason}) · ${pending} pending${f}`;
  if (q.current) return `running job #${q.current} · ${pending} pending${f}`;
  return pending ? `${pending} queued${f}` : "queue idle";
}
function waitLeft(w) {
  return Math.max(0, Math.ceil(w.until ? w.until - Date.now() / 1000 : w.seconds));
}

function renderQueuePill(q) {
  const pill = $("#queue-pill");
  pill.classList.remove("paused", "busy", "forced");
  if (q.paused) pill.classList.add("paused");
  else if (q.waiting || q.current) pill.classList.add(q.forced ? "forced" : "busy");
  pill.title = q.forced ? "battery threshold overridden until the queue runs dry (the Anlas floor still applies)" : "queue state (click to open the queue drawer, q)";
  pill.textContent = queueText(q);
}

function renderDrawer() {
  const q = state.queue;
  const d = $("#drawer");
  d.hidden = !state.drawerOpen;
  if (!q || !state.drawerOpen) return;
  $("#drawer-title").textContent = q.paused ? "queue · paused" : q.current ? "queue · running" : "queue";
  $("#drawer-wait").textContent = q.waiting ? `pacing: ${waitLeft(q.waiting)}s (${q.waiting.reason})` : "";
  const reason = $("#drawer-reason");
  reason.hidden = !q.paused;
  reason.textContent = q.paused ? (q.pause_reason || "paused by user") : "";
  const c = q.counts || {};
  $("#drawer-counts").textContent =
    `queued ${c.queued || 0} · running ${c.running || 0} · done ${c.done || 0} · error ${c.error || 0} · skipped ${c.skipped || 0}` +
    (q.battery ? ` · battery ${q.battery.battery_percent}%${q.battery.is_negative ? " (negative)" : ""} · ${q.battery.anlas} Anlas` : "") +
    (q.forced ? " · FORCED: threshold ignored until the queue runs dry" : "");
  const rf = q.refs;
  const refsLine = $("#drawer-refs");
  refsLine.hidden = !rf || !(rf.current || rf.pending || rf.stopped || rf.last_error);
  refsLine.classList.toggle("bad", !!(rf && rf.stopped));
  if (rf) refsLine.textContent = rf.stopped ? `refs stopped: ${rf.stopped}`
    : `refs: ${rf.current ? `fetching ${rf.current}` : "idle"}${rf.pending ? ` · ${rf.pending} pending` : ""}${rf.last_error ? ` · last error: ${rf.last_error}` : ""}`;
  $("#q-pause").disabled = q.paused;
  $("#q-resume").disabled = !q.paused;
  $("#q-clear").disabled = !(c.queued > 0);

  const ol = $("#jobs");
  ol.replaceChildren();
  const jobs = q.jobs || [];
  const pending = jobs.filter(j => j.status === "queued" || j.status === "running");
  const finished = jobs.filter(j => !(j.status === "queued" || j.status === "running")).reverse();
  const line = (j, cls) => {
    const li = el("li", { class: `${j.status} ${cls}` },
      el("span", { class: "id" }, `#${j.id}`),
      el("span", { class: "st" }, j.status),
      el("span", {}, `${jobArtist(j)} × ${j.template_id}`));
    if (j.error) li.append(el("span", { class: "err" }, j.error));
    return li;
  };
  if (pending.length) ol.append(el("li", { class: "sep" }, `pending (${pending.length})`));
  for (const j of pending) ol.append(line(j, ""));
  if (finished.length) ol.append(el("li", { class: "sep" }, `recent (${finished.length})`));
  for (const j of finished) ol.append(line(j, "finished"));
  if (!jobs.length) ol.append(el("li", { class: "sep" }, "no jobs yet"));
}

function jobArtist(j) {
  if (state.matrix && j.slug === state.matrix.base_slug) return "no artist";
  return String(j.tag).split(",").map(t => t.trim()).filter(Boolean).join(" + ");  // combos are stored comma-joined
}

function toggleDrawer(open) {
  state.drawerOpen = open === undefined ? !state.drawerOpen : open;
  renderDrawer();
}

const reloadMatrixSoon = debounce(loadMatrix, 400);

function jobSignature(q) {
  return (q.jobs || []).map(j => `${j.id}:${j.status}`).join(",");
}

function handleQueueState(q) {
  const prev = state.queue;
  state.queue = q;
  if (q.battery) renderBattery(q.battery);
  renderQueuePill(q);
  renderDrawer();
  // A job changed state (enqueued, started, finished): the matrix's skeletons / thumbs are out of date.
  if (prev && jobSignature(prev) !== jobSignature(q)) reloadMatrixSoon();
  const rf = q.refs, prf = prev && prev.refs;
  if (rf) {
    if (!rf.pending && !rf.current) state.refsAsked.clear();
    if (prf && rf.done !== prf.done) reloadMatrixSoon();
    else if (prf && (rf.current !== prf.current || rf.pending !== prf.pending) && state.matrix) renderMatrix();  // "fetching…" cells
    if (rf.stopped && (!prf || prf.stopped !== rf.stopped)) toast(`refs: ${rf.stopped}`, true);
  }
}

let sse = null;
function connectSSE() {
  if (sse) sse.close();
  sse = new EventSource("/api/jobs/stream");
  sse.addEventListener("state", (ev) => {
    try { handleQueueState(JSON.parse(ev.data)); }
    catch (e) { console.error("bad state event", e); }
  });
  sse.addEventListener("error", () => {
    // EventSource reconnects on its own; just say so.
    const pill = $("#queue-pill");
    pill.classList.remove("busy");
    pill.classList.add("paused");
    pill.textContent = "queue: reconnecting…";
  });
}

// Countdown tick for the pacing wait without waiting for the next SSE event.
setInterval(() => {
  const q = state.queue;
  if (!q || !q.waiting) return;
  renderQueuePill(q);
  if (state.drawerOpen) $("#drawer-wait").textContent = `pacing: ${waitLeft(q.waiting)}s (${q.waiting.reason})`;
}, 1000);

async function queueAction(path) {
  try {
    // only resume goes through the battery gate (and takes {force}); pause/clear ignore the body
    const st = await withBatteryConfirm(force => api(path, { json: path.endsWith("resume") ? { force } : {} }));
    if (st) handleQueueState(st);
  } catch (e) {
    toast(e.message, true);
  }
}
$("#q-pause").addEventListener("click", () => queueAction("/api/jobs/pause"));
$("#q-resume").addEventListener("click", () => queueAction("/api/jobs/resume"));
$("#q-clear").addEventListener("click", () => {
  const n = state.queue ? (state.queue.counts || {}).queued || 0 : 0;
  if (window.confirm(`Drop ${n} queued job(s)? Running jobs finish; nothing generated is deleted.`)) queueAction("/api/jobs/clear");
});
$("#q-close").addEventListener("click", () => toggleDrawer(false));
$("#queue-pill").addEventListener("click", () => toggleDrawer());

// --- battery ------------------------------------------------------------------

function renderBattery(b) {
  state.battery = b;
  const pct = b.battery_percent;
  const low = b.is_negative || (pct !== null && pct !== undefined && pct <= minPercent());
  const pill = $("#battery-pill");
  pill.classList.remove("low", "ok", "warn");
  pill.classList.add(low ? "low" : pct !== null && pct < 25 ? "warn" : "ok");
  pill.textContent = b.is_negative ? `battery negative · ${b.anlas} Anlas` : `battery ${pct ?? "?"}% · ${b.anlas} Anlas`;

  const g = $("#gauge");
  g.classList.toggle("low", low);
  g.classList.toggle("warn", !low && pct !== null && pct < 25);
  $("#gauge-fill").style.width = `${b.is_negative ? 0 : Math.max(0, Math.min(100, pct || 0))}%`;
  $("#gauge-label").textContent = b.is_negative ? "NEGATIVE: V5 costs Anlas now" : `${pct ?? "?"} %`;
  const kv = $("#battery-kv");
  kv.replaceChildren();
  const row = (k, v) => kv.append(el("dt", {}, k), el("dd", {}, v));
  row("tier", `${b.tier ?? "?"}${b.active === false ? " (inactive)" : ""}`);
  row("Anlas", String(b.anlas ?? "?"));
  row("next %", b.time_until_next_percent != null ? `in ${Math.round(b.time_until_next_percent / 60)} min` : "?");
  row("guard", `suspend at ≤ ${minPercent()}% · Anlas floor ${anlasPerImage()}/image`);
  row("fetched", fmtDate(b.fetched_at));
}

function batterySettings() {
  return (state.settings && state.settings.settings && state.settings.settings.battery) || {};
}
function minPercent() {
  const b = batterySettings();
  return b.min_percent != null ? b.min_percent : 5;
}
function anlasPerImage() {
  const b = batterySettings();
  return b.anlas_per_image != null ? b.anlas_per_image : 30;
}

async function loadBattery(fresh = false) {
  try {
    renderBattery(await api(`/api/subscription${fresh ? "?fresh=1" : ""}`));
  } catch (e) {
    $("#battery-pill").textContent = "battery: n/a";
    $("#battery-pill").title = e.message;
    $("#gauge-label").textContent = "unavailable";
    $("#battery-kv").replaceChildren(el("dt", {}, "error"), el("dd", { class: "bad" }, e.message));
    toast(e.message, true);
  }
}
$("#battery-pill").addEventListener("click", () => loadBattery(true));
$("#battery-refresh").addEventListener("click", () => loadBattery(true));

async function loadSettings() {
  try { state.settings = await api("/api/settings"); }
  catch (e) { toast(e.message, true); return; }
  $("#settings-file").textContent = state.settings.file;
  const s = state.settings.settings;
  const lines = [];
  for (const [k, v] of Object.entries(s)) {
    if (typeof v === "object" && v !== null) {
      lines.push(`[${k}]`);
      for (const [k2, v2] of Object.entries(v)) lines.push(`  ${k2} = ${JSON.stringify(v2)}`);
    } else {
      lines.push(`${k} = ${typeof v === "string" && v.includes("\n") ? "'''\n" + v + "'''" : JSON.stringify(v)}`);
    }
  }
  $("#settings-view").textContent = lines.join("\n");
}

// --- templates (list + toggles) --------------------------------------------------

async function loadTemplates() {
  try { state.templates = await api("/api/templates"); }
  catch (e) { toast(e.message, true); return; }
  renderTemplatesTab();
  renderImportTemplateSelect();
}

function renderTemplatesTab() {
  const tp = state.templates;
  if (!tp) return;
  $("#tpl-dir").textContent = tp.dir;
  const tb = $("#tpl-table tbody");
  tb.replaceChildren();
  for (const t of tp.templates) {
    const toggle = (key) => el("input", {
      type: "checkbox", ...(t[key] ? { checked: "" } : {}),
      onchange: (ev) => patchTemplate(t.id, { [key]: ev.target.checked }),
    });
    tb.append(el("tr", { class: t.enabled ? "" : "disabled" },
      el("td", { class: "mono" }, t.id),
      el("td", {}, t.name),
      el("td", {}, toggle("primary")),
      el("td", {}, toggle("enabled")),
      el("td", {}, String(t.chars)),
      el("td", {}, String(t.images)),
      el("td", { class: t.stale ? "st-exists" : "" }, String(t.stale)),
      el("td", { class: "mono", title: t.hash }, t.hash.slice(0, 10)),
      el("td", { class: "mono" }, t.path),
    ));
  }
}

async function patchTemplate(id, changes) {
  try {
    await api(`/api/templates/${encodeURIComponent(id)}`, { method: "PATCH", json: changes });
    toast(`${id}: ${Object.entries(changes).map(([k, v]) => `${k}=${v}`).join(", ")}`);
  } catch (e) {
    toast(e.message, true);
  }
  await Promise.all([loadTemplates(), loadMatrix()]);
}

$("#tpl-refresh").addEventListener("click", loadTemplates);
$("#tpl-new-file").addEventListener("click", () => $("#tpl-file").click());
$("#tpl-file").addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  if (f) newTemplateDialog({ file: f, label: f.name });
  ev.target.value = "";
});

// --- generate ------------------------------------------------------------------

function showResult(box, text, err = false) {
  box.textContent = text;
  box.classList.toggle("err", err);
}

/* Add artists: matrix rows only. Nothing is generated; every cell of a new row starts missing (⟳ to generate). */
$("#gen-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const lines = $("#gen-artists").value.split("\n").map(l => l.trim()).filter(Boolean);
  const out = $("#gen-result");
  if (!lines.length) return showResult(out, "no artists given", true);
  const btn = $("#gen-submit");
  btn.disabled = true;
  try {
    const res = await api("/api/artists", { json: { artists: lines } });
    showResult(out, `${res.added.length} artist(s) added` + (res.existing.length ? `, ${res.existing.length} already in the matrix` : "") +
      (res.added.length ? ": " + res.added.join(", ") : "") + "\nClick ⟳ on a missing cell to generate it.");
    await loadMatrix();
    if ($("#gen-refs").checked && state.matrix && state.matrix.refs.configured) {
      // single artists never fetched
      const slugs = res.added.concat(res.existing).filter(sl => {
        const a = state.matrix.artists[artistIndex(sl)];
        return a && a.refs_eligible && !(a.refs_fetch && a.refs_fetch.fetched_at);
      });
      if (slugs.length) await fetchRefs(slugs);
    }
  } catch (e) {
    showResult(out, e.message, true);
  } finally {
    btn.disabled = false;
  }
});

/* One cell, one job: POST /api/generate. Missing / stale / file-missing cells go straight in (stale ones are
   overwritten in place); a fresh cell only after a confirm, and its image is deleted first (the rating stays). */
async function generateCell(a, t, cell) {
  if (cell && cell.job) return toast(`already pending: job #${cell.job.id} ${cell.job.status}`);
  const replace = hasImage(cell) && !cell.stale;
  if (replace && !window.confirm(`Regenerate ${artistLabel(a)} × ${t.id}?\n\nThe current image and thumbnail are deleted first, the cell rating stays.`)) return;
  try {
    const res = await withBatteryConfirm(force => api("/api/generate", { json: { artist: a.slug, template: t.id, replace, force } }));
    if (!res) return;
    toast(res.created ? `enqueued ${artistLabel(a)} × ${t.id} (job #${res.job.id})` : `already pending: job #${res.job.id} ${res.job.status}`);
    if (res.battery) renderBattery(res.battery);
    await loadMatrix();
  } catch (e) {
    toast(e.message, true);
  }
}

// tag suggest (NovelAI /ai/generate-image/suggest-tags via /api/suggest)
const sugInput = $("#gen-suggest");
const sugList = $("#gen-suggest-list");
let sugSeq = 0;
const suggest = debounce(async () => {
  const q = sugInput.value.trim();
  const seq = ++sugSeq;
  if (q.length < 2) { sugList.hidden = true; return; }
  let tags = [];
  try { tags = (await api(`/api/suggest?q=${encodeURIComponent(q)}`)).tags; }
  catch (e) { toast(e.message, true); return; }
  if (seq !== sugSeq) return;
  sugList.replaceChildren();
  for (const t of tags.slice(0, 20)) {
    const bare = String(t.tag).replace(/^artist:/i, "");
    sugList.append(el("li", { onclick: () => addArtistLine(bare) },
      el("span", {}, t.tag), el("span", { class: "n" }, t.count != null ? String(t.count) : "")));
  }
  sugList.hidden = !tags.length;
}, 250);
sugInput.addEventListener("input", suggest);
sugInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { sugList.hidden = true; }
  if (ev.key === "Enter") {
    ev.preventDefault();
    const first = $("li", sugList);
    if (!sugList.hidden && first) first.click();
    else if (sugInput.value.trim()) addArtistLine(sugInput.value.trim().replace(/^artist:/i, ""));
  }
});
document.addEventListener("click", (ev) => { if (!ev.target.closest(".suggest")) sugList.hidden = true; });

function addArtistLine(tag) {
  const ta = $("#gen-artists");
  const lines = ta.value.split("\n").map(s => s.trim()).filter(Boolean);
  if (!lines.some(l => l.replace(/^artist:\s*/i, "").toLowerCase() === tag.toLowerCase())) lines.push(tag);
  ta.value = lines.join("\n") + "\n";
  sugInput.value = "";
  sugList.hidden = true;
  toast(`added ${tag}`);
}

// --- import ---------------------------------------------------------------------

const drop = $("#drop");
function addFiles(list) {
  for (const f of list) {
    if (!/\.png$/i.test(f.name) && f.type !== "image/png") { toast(`${f.name}: not a PNG, skipped`, true); continue; }
    state.importFiles.set(f.name, f);
  }
  renderImportCount();
}
function renderImportCount() {
  const n = state.importFiles.size;
  $("#imp-count").textContent = n ? `${n} file(s): ${[...state.importFiles.keys()].slice(0, 6).join(", ")}${n > 6 ? ", …" : ""}` : "no files selected";
  $("#imp-submit").disabled = !n;
  $("#imp-submit").textContent = n ? `Import ${n} file(s)` : "Import";
}
["dragenter", "dragover"].forEach(evn => drop.addEventListener(evn, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(evn => drop.addEventListener(evn, (ev) => { ev.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (ev) => addFiles(ev.dataTransfer.files));
$("#imp-files").addEventListener("change", (ev) => { addFiles(ev.target.files); ev.target.value = ""; });
$("#imp-clear").addEventListener("click", () => {
  state.importFiles.clear();
  renderImportCount();
  $("#imp-results").hidden = true;
  $("#imp-results tbody").replaceChildren();
});

function renderImportTemplateSelect() {
  const sel = $("#imp-template");
  const cur = sel.value;
  sel.replaceChildren(el("option", { value: "" }, "auto (match prompt)"));
  for (const t of (state.templates ? state.templates.templates : [])) sel.append(el("option", { value: t.id }, `${t.id} — ${t.name}`));
  sel.value = cur;
}

async function importFiles(files, { template = "", replace = false, dryRun = false } = {}) {
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  if (template) form.append("template", template);
  form.append("replace", String(replace));
  form.append("dry_run", String(dryRun));
  return api("/api/import", { form });
}

$("#imp-submit").addEventListener("click", async () => {
  const files = [...state.importFiles.values()];
  if (!files.length) return;
  const btn = $("#imp-submit");
  btn.disabled = true;
  try {
    const res = await importFiles(files, {
      template: $("#imp-template").value, replace: $("#imp-replace").checked, dryRun: $("#imp-dry").checked,
    });
    renderImportResults(res.results);
    const ok = res.results.filter(r => r.status === "imported" || r.status === "replaced").length;
    toast(`import: ${ok}/${res.results.length} registered`);
    await Promise.all([loadMatrix(), loadTemplates()]);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
});

function renderImportResults(results, replaceRows = false) {
  const table = $("#imp-results");
  const tb = $("tbody", table);
  if (!replaceRows) tb.replaceChildren();
  table.hidden = false;
  for (const r of results) {
    const who = r.artists && r.artists.length ? r.artists.join(" + ") : "(baseline)";
    const details = [];
    if (r.status === "no_match") details.push(r.template_id ? `closest: ${r.template_id}` : "no templates to match against");
    if (r.status === "no_metadata") details.push("no NovelAI metadata in the file");
    if (r.status === "exists") details.push("cell already exists; tick 'replace' or use the button");
    if (r.diffs && r.diffs.length) details.push(...r.diffs);
    if (r.inbox) details.push(`→ ${r.inbox}`);
    if (r.path && r.status !== "no_match") details.push(r.path);
    const actions = el("div", { class: "actions" });
    const file = state.importFiles.get(r.file);
    if (file && (r.status === "no_match" || r.status === "dry_run")) {
      const sel = el("select", { title: "force-assign to this template (params_match=false)" });
      for (const t of (state.templates ? state.templates.templates : [])) {
        sel.append(el("option", { value: t.id, ...(t.id === r.template_id ? { selected: "" } : {}) }, t.id));
      }
      actions.append(sel, el("button", { class: "btn small", onclick: () => reimport(r, file, sel.value, false) }, "assign"));
    }
    if (file && r.status === "exists") {
      actions.append(el("button", { class: "btn small", onclick: () => reimport(r, file, $("#imp-template").value, true) }, "replace"));
    }
    if (file && r.status !== "no_metadata") {
      actions.append(el("button", { class: "btn small", onclick: () => newTemplateDialog({ file, label: r.file }) }, "new template"));
    }
    const tr = el("tr", { dataset: { file: r.file } },
      el("td", { class: "mono" }, r.file),
      el("td", { class: `st-${r.status}` }, r.status),
      el("td", {}, r.template_id ? `${r.template_id} × ${who}${r.exact === false && r.status !== "no_match" ? " (forced)" : ""}` : ""),
      el("td", {}, r.params_match == null ? "" : r.params_match ? "match" : el("span", { class: "st-exists", title: r.mismatch_note || "" }, `≠ ${r.mismatch_note || ""}`)),
      el("td", { class: "diffs" }, details.join("\n")),
      el("td", {}, actions),
    );
    const old = replaceRows ? $(`tr[data-file="${CSS.escape(r.file)}"]`, tb) : null;
    if (old) old.replaceWith(tr); else tb.append(tr);
  }
}

async function reimport(r, file, template, replace) {
  try {
    const res = await importFiles([file], { template, replace });
    renderImportResults(res.results, true);
    toast(`${r.file}: ${res.results[0].status}`);
    await Promise.all([loadMatrix(), loadTemplates()]);
  } catch (e) {
    toast(e.message, true);
  }
}

// --- new template from image (dialog) ---------------------------------------------

const ntDlg = $("#nt");
let ntSource = null;  // {file} | {imageId, label}

function newTemplateDialog(src) {
  ntSource = src;
  $("#nt-source").textContent = `from ${src.label}`;
  $("#nt-id").value = "";
  $("#nt-name").value = "";
  $("#nt-primary").checked = false;
  $("#nt-force").checked = false;
  ntDlg.showModal();
  $("#nt-id").focus();
}
$("#nt-cancel").addEventListener("click", () => ntDlg.close("cancel"));
$("#nt-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!ntSource) return;
  const form = new FormData();
  form.append("id", $("#nt-id").value.trim());
  if ($("#nt-name").value.trim()) form.append("name", $("#nt-name").value.trim());
  form.append("primary", String($("#nt-primary").checked));
  form.append("force", String($("#nt-force").checked));
  if (ntSource.file) form.append("file", ntSource.file, ntSource.file.name);
  else form.append("image_id", String(ntSource.imageId));
  try {
    const res = await api("/api/templates/from-image", { form });
    ntDlg.close("ok");
    toast(`template ${res.template.id} created; cell ${res.cell.slug || "?"} × ${res.template.id} ${res.cell.status}`);
    await Promise.all([loadTemplates(), loadMatrix()]);
  } catch (e) {
    toast(e.message, true);
  }
});
$("#lb-newtpl").addEventListener("click", () => {
  const lb = state.lb;
  if (!lb) return;
  const cell = cellOf(lb.base ? state.matrix.base_slug : lb.slug, lb.template);
  if (!hasImage(cell)) return;
  newTemplateDialog({ imageId: cell.image_id, label: cell.path });
});

// --- ratings ------------------------------------------------------------------

const RATINGS = [[-1, "−1", "−1: broken / no visible effect"], [0, "0", "0: neutral"], [1, "+1", "+1: good"], [2, "+2", "+2: favourite"]];
const RATING_KEYS = { "-": -1, "0": 0, "1": 1, "2": 2 };

function ratingVal(r) {
  return r && r.value !== null && r.value !== undefined ? r.value : null;
}
function isBase(a) {
  return a.slug === state.matrix.base_slug;
}

/* Four chips; the active one is highlighted, clicking it again clears the rating. tid=null rates the artist row. */
function ratingChips(a, tid, big = false) {
  const cur = ratingVal(tid ? (a.cells[tid] && a.cells[tid].rating) : a.rating);
  const box = el("span", { class: big ? "chips big" : "chips" });
  for (const [v, label, title] of RATINGS) {
    box.append(el("button", {
      class: `chip${cur === v ? ` on v${v}` : ""}`, title: `${title}${cur === v ? " (click again to clear)" : ""}`,
      onclick: (ev) => { ev.stopPropagation(); rate(a, tid, cur === v ? null : v); },
    }, label));
  }
  return box;
}

/* PUT /api/ratings, then patch the matrix payload in place and re-render only what shows that rating.
   note === undefined keeps the stored note; value === null deletes the rating (note included). */
async function rate(a, tid, value, note) {
  if (isBase(a)) return;
  const cur = tid ? (a.cells[tid] ? a.cells[tid].rating : null) : a.rating;
  const body = { artist: a.slug, template: tid || null, value, note: note !== undefined ? note : (cur ? cur.note : null) };
  try {
    const res = await api("/api/ratings", { method: "PUT", json: body });
    if (tid) { if (a.cells[tid]) a.cells[tid].rating = res.rating; } else a.rating = res.rating;
    refreshRating(a, tid);
    const where = tid ? `${artistLabel(a)} × ${tid}` : artistLabel(a);
    toast(value === null ? `${where}: rating cleared` : `${where}: ${RATINGS.find(r => r[0] === value)[1]}${note ? " · note saved" : ""}`);
  } catch (e) {
    toast(e.message, true);
  }
}

function refreshRating(a, tid) {
  const tr = $(`#matrix tr[data-slug="${CSS.escape(a.slug)}"]`);
  if (tr) {
    if (tid) {
      const td = $(`td[data-tid="${CSS.escape(tid)}"]`, tr);
      const t = state.matrix.templates[templateIndex(tid)];
      if (td && t && !state.hidden.has(tid)) td.replaceChildren(renderCell(a, t, a.cells[tid]));
    } else {
      refreshRowHeader(a);
    }
  }
  if (state.lb && state.lb.slug === a.slug) renderLightboxRating();
}

async function editNote(a) {
  const cur = a.rating && a.rating.note ? a.rating.note : "";
  const note = window.prompt(`Note for ${artistLabel(a)} (stored with the artist rating):`, cur);
  if (note === null) return;
  await rate(a, null, ratingVal(a.rating) ?? 0, note.trim() || null);
}

// --- labels (user-defined style tags per artist) ------------------------------------

/* Mirror of db.normalize_label; commas are separators here, never part of a label. */
function normLabel(s) {
  return s.trim().toLowerCase().replace(/\s+/g, " ");
}

async function setLabels(a, labels) {
  try {
    const res = await api(`/api/artists/${encodeURIComponent(a.slug)}/labels`, { method: "PUT", json: { labels } });
    a.labels = res.labels;
    state.matrix.labels = res.all;
    renderLabelOptions();
    renderLabelFilter();
    if (state.labelEdit !== a.slug) refreshRowHeader(a);  // edited from the lightbox: keep the row behind it current
  } catch (e) {
    toast(e.message, true);
  }
}

/* Label edits of one artist run one after another, each on the labels the previous one left: typing
   "a, b" fast would otherwise send two PUTs built from the same stale list and lose "a". */
const labelChains = new Map();
function updateLabels(a, fn) {
  const next = (labelChains.get(a.slug) || Promise.resolve()).then(() => setLabels(a, fn(a.labels)));
  labelChains.set(a.slug, next);
  return next;
}

function renderLabelOptions() {
  $("#label-options").replaceChildren(...(state.matrix ? state.matrix.labels : []).map(l => el("option", { value: l.label })));
}

/* Chips with × plus one input (datalist autocomplete). Enter or "," adds (comma-separated input works too),
   Backspace on an empty input drops the last label, Esc / Enter on an empty input closes. Saves on every change;
   the matrix is not re-rendered while it is open, so a label-filtered view does not reshuffle under the cursor. */
function labelEditor(a, onClose) {
  const box = el("div", { class: "lbl-edit", onclick: (ev) => ev.stopPropagation() });
  const chips = el("span", { class: "lchips" });
  const input = el("input", { class: "lbl-input", list: "label-options", placeholder: "add label…", autocomplete: "off", spellcheck: "false" });
  const draw = () => chips.replaceChildren(...a.labels.map(l => el("span", { class: "lchip" }, l,
    el("button", { class: "x", type: "button", title: `remove ${l}`, onclick: async () => { await updateLabels(a, ls => ls.filter(x => x !== l)); draw(); input.focus(); } }, "×"))));
  input.addEventListener("keydown", async (ev) => {
    if (ev.key === "Enter" || ev.key === ",") {
      ev.preventDefault();
      const add = input.value.split(",").map(normLabel).filter(Boolean);
      input.value = "";
      if (add.length) { await updateLabels(a, ls => [...ls, ...add]); draw(); }
      else if (ev.key === "Enter" && onClose) onClose();
    } else if (ev.key === "Backspace" && !input.value && a.labels.length) {
      ev.preventDefault();
      await updateLabels(a, ls => ls.slice(0, -1));
      draw();
    } else if (ev.key === "Escape") {
      ev.preventDefault();  // also keeps the lightbox <dialog> from closing
      ev.stopPropagation();
      if (onClose) onClose(); else input.blur();
    }
  });
  if (onClose) {
    box.addEventListener("focusout", (ev) => { if (!box.contains(ev.relatedTarget)) setTimeout(() => { if (!box.contains(document.activeElement)) onClose(); }, 0); });
  }
  draw();
  box.append(chips, input);
  box.focusInput = () => input.focus();
  return box;
}

function openRowLabels(a) {
  state.labelEdit = a.slug;
  const th = refreshRowHeader(a);
  const ed = th && $(".lbl-edit", th);
  if (ed) ed.focusInput();
}
function closeRowLabels() {
  const slug = state.labelEdit;
  state.labelEdit = null;
  const a = slug && state.matrix.artists[artistIndex(slug)];
  if (a) refreshRowHeader(a);
  if (a && state.lb && state.lb.slug === a.slug) renderLightboxLabels();
}

/* Label chip in a row header: click includes it in the filter, shift-click excludes it (again: drops it). */
function labelChip(l) {
  const st = state.view.inc.includes(l) ? "inc" : state.view.exc.includes(l) ? "exc" : "";
  return el("span", {
    class: `lchip${st ? ` ${st}` : ""}`, title: `filter: click include · shift-click exclude${st ? " (again: remove)" : ""}`,
    onclick: (ev) => { ev.stopPropagation(); toggleLabelFilter(l, ev.shiftKey ? "exc" : "inc"); },
  }, l);
}

function toggleLabelFilter(l, kind) {
  const v = state.view;
  const on = v[kind].includes(l);
  const inc = v.inc.filter(x => x !== l), exc = v.exc.filter(x => x !== l);
  if (!on) (kind === "inc" ? inc : exc).push(l);
  setView({ inc, exc });
}

/* The toolbar button + popover: every label with its count, click cycles off → include → exclude → off. */
function renderLabelFilter() {
  const v = state.view;
  const n = v.inc.length + v.exc.length;
  const btn = $("#mx-labels");
  btn.textContent = n ? `labels (${n}) ▾` : "labels ▾";
  btn.classList.toggle("on", n > 0);
  $("#mx-label-chips").replaceChildren(
    ...v.inc.map(l => el("span", { class: "lchip inc", title: "included (click to drop)", onclick: () => toggleLabelFilter(l, "inc") }, `+${l}`)),
    ...v.exc.map(l => el("span", { class: "lchip exc", title: "excluded (click to drop)", onclick: () => toggleLabelFilter(l, "exc") }, `−${l}`)),
  );
  const pop = $("#label-pop");
  if (pop.hidden) return;
  const all = state.matrix ? state.matrix.labels : [];
  const list = $("#label-pop-list");
  list.replaceChildren();
  if (!all.length) list.append(el("li", { class: "dim" }, "no labels yet: # on a row, or l in the lightbox"));
  for (const { label, count } of all) {
    const st = v.inc.includes(label) ? "inc" : v.exc.includes(label) ? "exc" : "";
    list.append(el("li", {
      class: st, title: "click: off → include → exclude · shift-click: exclude",
      onclick: (ev) => {
        const inc = v.inc.filter(x => x !== label), exc = v.exc.filter(x => x !== label);
        const next = ev.shiftKey ? (st === "exc" ? "" : "exc") : ({ "": "inc", inc: "exc", exc: "" })[st];
        if (next === "inc") inc.push(label);
        if (next === "exc") exc.push(label);
        setView({ inc, exc });
      },
    }, el("span", { class: "mark" }, st === "inc" ? "+" : st === "exc" ? "−" : ""), el("span", {}, label), el("span", { class: "n" }, String(count))));
  }
  $("#label-mode").textContent = v.lmode === "any" ? "include: any" : "include: all";
}

$("#mx-labels").addEventListener("click", (ev) => {
  ev.stopPropagation();
  $("#label-pop").hidden = !$("#label-pop").hidden;
  renderLabelFilter();
});
$("#label-mode").addEventListener("click", () => setView({ lmode: state.view.lmode === "any" ? "all" : "any" }));
$("#label-clear").addEventListener("click", () => setView({ inc: [], exc: [] }));
document.addEventListener("click", (ev) => { if (!ev.target.closest("#label-pop, #mx-labels")) $("#label-pop").hidden = true; });

// --- matrix view: filter + sort --------------------------------------------------

const SORTS = ["name", "rating", "date"];
function defaultDir(sort) {
  return sort === "name" ? "asc" : "desc";
}

/* Columns in display order: [{id, t}] for templates, {id: REFS_COL, refs: true} for the reference images.
   The saved order (drag and drop, localStorage) wins; a column it does not know yet (new template, refs
   column switched on later) lands right after its natural predecessor. */
function columns() {
  const m = state.matrix;
  const natural = [...(refsColumnOn() ? [{ id: REFS_COL, refs: true }] : []), ...m.templates.map(t => ({ id: t.id, t }))];
  const byId = new Map(natural.map(c => [c.id, c]));
  const out = state.order.filter(id => byId.has(id));
  natural.forEach((c, i) => {
    if (out.includes(c.id)) return;
    const prev = natural.slice(0, i).reverse().find(x => out.includes(x.id));
    out.splice(prev ? out.indexOf(prev.id) + 1 : 0, 0, c.id);
  });
  return out.map(id => byId.get(id));
}
function orderedTemplates() {
  return columns().filter(c => c.t).map(c => c.t);
}
function visibleTemplateIds() {
  return orderedTemplates().filter(t => !state.hidden.has(t.id)).map(t => t.id);
}
function refsColumnOn() {
  const m = state.matrix;
  return !!m && ((m.refs && m.refs.configured) || m.artists.some(a => a.refs && a.refs.length));
}
function rowCounts(a, tids) {
  let stale = 0, missing = 0;
  for (const tid of tids) {
    const c = a.cells[tid];
    if (!c) missing++;
    else if (!c.image_id) { /* pending */ }
    else if (c.file_missing || c.stale) stale++;
  }
  return { stale, missing };
}

/* Baseline row first, then the artists that pass the filter, sorted. */
function visibleArtists() {
  const m = state.matrix, v = state.view;
  const q = v.q.trim().toLowerCase();
  const tids = visibleTemplateIds();
  const base = m.artists.find(a => isBase(a));
  let rows = m.artists.filter(a => !isBase(a));
  if (q) rows = rows.filter(a => a.tags.join(" + ").toLowerCase().includes(q) || (a.rating && a.rating.note && a.rating.note.toLowerCase().includes(q)));
  if (v.inc.length) rows = rows.filter(a => v.lmode === "any" ? v.inc.some(l => a.labels.includes(l)) : v.inc.every(l => a.labels.includes(l)));
  if (v.exc.length) rows = rows.filter(a => !v.exc.some(l => a.labels.includes(l)));
  if (v.show !== "all") {
    rows = rows.filter(a => {
      const r = ratingVal(a.rating);
      switch (v.show) {
        case "unrated": return r === null;
        case "rated": return r !== null;
        case "good": return r !== null && r >= 1;
        case "fav": return r === 2;
        case "bad": return r === -1;
        case "stale": return rowCounts(a, tids).stale > 0;
        case "missing": return rowCounts(a, tids).missing > 0;
        case "combo": return a.is_combo;
        case "single": return !a.is_combo;
        case "unlabeled": return !a.labels.length;
        case "norefs": return a.refs_eligible && !a.refs.length;
        default: return true;
      }
    });
  }
  const byName = (x, y) => x.tags.join(" + ").localeCompare(y.tags.join(" + "), undefined, { sensitivity: "base" });
  let cmp = byName;
  if (v.sort === "rating") cmp = (x, y) => ((ratingVal(y.rating) ?? -9) - (ratingVal(x.rating) ?? -9)) || byName(x, y);
  else if (v.sort === "date") cmp = (x, y) => (y.created_at || "").localeCompare(x.created_at || "") || byName(x, y);
  rows.sort(cmp);
  if (v.dir !== defaultDir(v.sort)) rows.reverse();
  return base ? [base, ...rows] : rows;
}

function setView(changes) {
  const v = state.view;
  if (changes.sort !== undefined && changes.sort !== v.sort && changes.dir === undefined) changes.dir = defaultDir(changes.sort);
  Object.assign(v, changes);
  syncViewControls();
  syncHash();
  if (state.matrix) renderMatrix();
}

function syncViewControls() {
  const v = state.view;
  if ($("#mx-q").value !== v.q) $("#mx-q").value = v.q;
  $("#mx-show").value = v.show;
  $("#mx-sort").value = v.sort;
  $("#mx-dir").textContent = v.dir === "asc" ? "↑" : "↓";
  $("#mx-dir").title = `sort ${v.dir === "asc" ? "ascending" : "descending"} (d flips)`;
  $("#mx-show").classList.toggle("on", v.show !== "all");
  renderLabelFilter();
}

function viewQuery() {
  const v = state.view, p = new URLSearchParams();
  if (v.sort !== "name") p.set("sort", v.sort);
  if (v.dir !== defaultDir(v.sort)) p.set("dir", v.dir);
  if (v.q) p.set("q", v.q);
  if (v.show !== "all") p.set("show", v.show);
  if (v.inc.length) p.set("inc", v.inc.join(","));
  if (v.exc.length) p.set("exc", v.exc.join(","));
  if (v.lmode !== "all") p.set("lmode", v.lmode);
  const qs = p.toString();
  return qs ? `?${qs}` : "";
}
function parseView(hash) {
  const i = hash.indexOf("?");
  const p = new URLSearchParams(i >= 0 ? hash.slice(i + 1) : "");
  const sort = SORTS.includes(p.get("sort")) ? p.get("sort") : "name";
  const dir = ["asc", "desc"].includes(p.get("dir")) ? p.get("dir") : defaultDir(sort);
  const show = [...$("#mx-show").options].some(o => o.value === p.get("show")) ? p.get("show") : "all";
  const list = (k) => (p.get(k) || "").split(",").map(normLabel).filter(Boolean);
  return { sort, dir, q: p.get("q") || "", show, inc: list("inc"), exc: list("exc"), lmode: p.get("lmode") === "any" ? "any" : "all" };
}
function viewKey(v) {
  return `${v.sort}|${v.dir}|${v.q}|${v.show}|${v.inc}|${v.exc}|${v.lmode}`;
}

$("#mx-q").addEventListener("input", debounce(() => setView({ q: $("#mx-q").value }), 150));
$("#mx-q").addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { ev.preventDefault(); if ($("#mx-q").value) setView({ q: "" }); else $("#mx-q").blur(); }
  if (ev.key === "Enter") $("#mx-q").blur();
});
$("#mx-show").addEventListener("change", () => setView({ show: $("#mx-show").value }));
$("#mx-sort").addEventListener("change", () => setView({ sort: $("#mx-sort").value }));
$("#mx-dir").addEventListener("click", () => setView({ dir: state.view.dir === "asc" ? "desc" : "asc" }));

// --- matrix -------------------------------------------------------------------

function renderMatrix() {
  const m = state.matrix;
  const table = $("#matrix");
  table.replaceChildren();
  const rows = visibleArtists();
  const tids = visibleTemplateIds();
  const cols = columns();

  const thead = el("thead");
  const hr = el("tr");
  hr.append(el("th", { class: "corner" }, el("span", { class: "dim" }, "artist ╲ template")));
  for (const c of cols) hr.append(columnHeader(c));
  thead.append(hr);
  table.append(thead);

  const tbody = el("tbody");
  let images = 0, rated = 0;
  for (const a of rows) {
    const tr = el("tr", { class: isBase(a) ? "base" : "", dataset: { slug: a.slug } });
    tr.append(renderRowHeader(a));
    if (ratingVal(a.rating) !== null) rated++;
    for (const c of cols) {
      const hidden = state.hidden.has(c.id);
      // A collapsed column keeps an empty cell per row, so every column after it stays under its own header.
      const td = el("td", { class: `${hidden ? "hidden" : ""}${c.refs ? " refs-col" : ""}`, dataset: { tid: c.id } });
      if (c.refs) {
        if (!hidden) td.append(renderRefsCell(a));
      } else {
        const cell = a.cells[c.id];
        if (hasImage(cell)) images++;
        if (!hidden) td.append(renderCell(a, c.t, cell));
      }
      tr.append(td);
    }
    tbody.append(tr);
  }
  table.append(tbody);

  const total = m.artists.length - 1;
  const shown = rows.length - 1;
  const stale = rows.reduce((s, a) => s + rowCounts(a, tids).stale, 0);
  $("#matrix-info").textContent =
    `${shown === total ? total : `${shown}/${total}`} artists × ${tids.length}/${m.templates.length} columns · ${images} images · ${rated} rated` +
    (stale ? ` · ${stale} stale` : "");
  $("#mx-nsfw-wrap").hidden = !refsColumnOn();
}

/* Column header: click hides / shows, drag reorders (drop on the left or right half of another header). */
function columnHeader(c) {
  const m = state.matrix;
  const hidden = state.hidden.has(c.id);
  const name = c.refs ? "refs" : c.t.name;
  const th = el("th", {
    class: `${hidden ? "hidden" : ""}${c.refs ? " refs-col" : ""}`, draggable: "true", dataset: { col: c.id },
    title: `${hidden ? "show" : "hide"} column ${c.refs ? "refs" : c.id} · drag to move`,
    onclick: () => toggleColumn(c.id),
  }, name);
  if (!hidden && c.refs) {
    const missing = m.artists.filter(a => a.refs_eligible && !(a.refs_fetch && a.refs_fetch.fetched_at)).length;
    th.append(el("span", { class: "tid" }, "gelbooru · top score"));
    if (m.refs.configured && missing) {
      th.append(el("span", { class: "cnt" }, el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); ev.stopPropagation(); fetchRefs("missing"); } }, `fetch ${missing} missing`)));
    } else if (!m.refs.configured) {
      th.append(el("span", { class: "cnt dim", title: "set GELBOORU_API_KEY and GELBOORU_USER_ID in .env" }, "not configured"));
    }
  } else if (!hidden) {
    const t = c.t;
    th.append(el("span", { class: "tid" }, t.id + (t.primary ? " · primary" : "")));
    const stale = m.artists.reduce((s, a) => s + (a.cells[t.id] && a.cells[t.id].image_id && (a.cells[t.id].stale || a.cells[t.id].file_missing) ? 1 : 0), 0);
    if (stale) th.append(el("span", { class: "cnt" }, `${stale} stale`));
  }
  th.addEventListener("dragstart", (ev) => {
    ev.dataTransfer.setData("text/x-col", c.id);
    ev.dataTransfer.effectAllowed = "move";
    th.classList.add("dragging");
  });
  th.addEventListener("dragend", () => $$("#matrix thead th").forEach(h => h.classList.remove("dragging", "drop-before", "drop-after")));
  th.addEventListener("dragover", (ev) => {
    if (!ev.dataTransfer.types.includes("text/x-col")) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "move";
    const r = th.getBoundingClientRect();
    const after = ev.clientX > r.left + r.width / 2;
    th.classList.toggle("drop-after", after);
    th.classList.toggle("drop-before", !after);
  });
  th.addEventListener("dragleave", () => th.classList.remove("drop-before", "drop-after"));
  th.addEventListener("drop", (ev) => {
    ev.preventDefault();
    const id = ev.dataTransfer.getData("text/x-col");
    const after = th.classList.contains("drop-after");
    th.classList.remove("drop-before", "drop-after");
    if (id && id !== c.id) moveColumn(id, c.id, after);
  });
  return th;
}

function moveColumn(id, target, after) {
  const ids = columns().map(c => c.id).filter(x => x !== id);
  ids.splice(ids.indexOf(target) + (after ? 1 : 0), 0, id);
  state.order = ids;
  saveJSON("colOrder", ids);
  renderMatrix();
}

function resetColumns() {
  state.order = [];
  state.hidden.clear();
  saveJSON("colOrder", []);
  saveHidden();
  renderMatrix();
  toast("columns: default order, all shown");
}
$("#mx-cols-reset").addEventListener("click", resetColumns);

function refreshRowHeader(a) {
  const th = $(`#matrix tr[data-slug="${CSS.escape(a.slug)}"] > th`);
  if (!th) return null;
  const nth = renderRowHeader(a);
  th.replaceWith(nth);
  return nth;
}

function renderRowHeader(a) {
  const base = isBase(a);
  const th = el("th", { title: base ? "artist-less baseline" : "" });
  const tag = el("span", {
    class: "tag", title: base ? "artist-less baseline" : "click: copy artist:tag · shift-click: copy bare tag",
    onclick: (ev) => { if (!base) copyText(artistTagText(a, ev.shiftKey)); },
  }, artistLabel(a));
  th.append(tag);
  if (a.is_combo) th.append(el("span", { class: "sub" }, "combo"));
  if (a.rating && a.rating.note) th.append(el("span", { class: "note", title: a.rating.note }, a.rating.note));
  if (!base && state.labelEdit === a.slug) th.append(labelEditor(a, closeRowLabels));
  else if (a.labels.length) th.append(el("div", { class: "lchips" }, ...a.labels.map(labelChip)));
  const tools = el("div", { class: "row-tools" });
  if (!base) {
    tools.append(ratingChips(a, null),
      el("button", { class: "tool", title: a.rating && a.rating.note ? `edit note: ${a.rating.note}` : "add a note", onclick: (ev) => { ev.stopPropagation(); editNote(a); } }, "✎"),
      el("button", { class: `tool${state.labelEdit === a.slug ? " on" : ""}`, title: "edit labels", onclick: (ev) => { ev.stopPropagation(); if (state.labelEdit === a.slug) closeRowLabels(); else openRowLabels(a); } }, "#"));
  }
  th.append(tools);
  return th;
}

function enqueueButton(a, t, cell) {
  return el("button", { class: "enq", title: `generate ${artistLabel(a)} × ${t.id}`, onclick: (ev) => { ev.stopPropagation(); generateCell(a, t, cell); } }, "⟳");
}

function renderCell(a, t, cell) {
  if (!cell) {
    return el("div", { class: "cell missing", title: `${a.slug} / ${t.id}: not generated` }, "missing", enqueueButton(a, t));
  }
  if (!cell.image_id) {  // pending job, no image yet
    const j = cell.job || {};
    return el("div", { class: `cell job ${j.status || ""}`, title: j.error || "" }, j.status || "queued");
  }
  const box = el("div", { class: "cell" + (cell.file_missing ? " missing" : " has-img") + (cell.stale ? " stale" : "") });
  if (cell.file_missing) {
    box.append("file missing", enqueueButton(a, t, cell));
  } else {
    box.append(el("img", { loading: "lazy", src: fileUrl(cell, true), alt: `${a.slug} / ${t.id}` }));
    box.addEventListener("click", () => openLightbox(a.slug, t.id));
    if (!cell.job) {
      box.append(el("button", {
        class: "regen" + (cell.stale ? " stale" : ""),
        title: cell.stale ? "stale: regenerate in place (the old image stays until the new one lands)" : "delete this image and regenerate (asks first)",
        onclick: (ev) => { ev.stopPropagation(); generateCell(a, t, cell); },
      }, "⟳"));
    }
  }
  const badges = el("div", { class: "badges" });
  if (cell.stale) badges.append(el("span", { class: "badge stale", title: `generated under an older template/settings hash (${cell.template_hash.slice(0, 8)}… ≠ ${t.hash.slice(0, 8)}…)` }, "stale"));
  if (!cell.params_match) badges.append(el("span", { class: "badge mismatch", title: cell.mismatch_note || "params differ" }, "≠"));
  if (cell.source === "imported") badges.append(el("span", { class: "badge imported", title: "imported, not generated here" }, "imp"));
  if (cell.job) badges.append(el("span", { class: "badge", title: `job #${cell.job.id} ${cell.job.status}` }, cell.job.status));
  if (badges.childElementCount) box.append(badges);
  const rv = ratingVal(cell.rating);
  if (rv !== null) box.append(el("span", { class: `rate v${rv}`, title: `cell rating ${RATINGS.find(r => r[0] === rv)[1]}${cell.rating.note ? ` · ${cell.rating.note}` : ""}` }, RATINGS.find(r => r[0] === rv)[1]));
  return box;
}

function toggleColumn(id) {
  if (state.hidden.has(id)) state.hidden.delete(id); else state.hidden.add(id);
  saveHidden();
  renderMatrix();
}

// --- reference images (Gelbooru) ------------------------------------------------------

const NSFW_RATINGS = new Set(["questionable", "explicit"]);
function refUrl(r, thumb = false) {
  return `/${thumb ? r.thumb_path : r.path}?v=${encodeURIComponent(r.created_at || "")}`;
}
function refTitle(r) {
  return `#${r.rank + 1} · score ${r.score ?? "?"} · ${r.rating || "unrated"}${r.width ? ` · ${r.width}×${r.height}` : ""}`;
}
/* One ref image; questionable / explicit ones are blurred unless "show nsfw refs" is on. */
function refImg(r, thumb = true) {
  return el("img", { class: NSFW_RATINGS.has(r.rating) ? "nsfw" : "", loading: "lazy", src: refUrl(r, thumb), alt: refTitle(r) });
}
function refsBusy(a) {
  const rf = state.queue && state.queue.refs;
  return state.refsAsked.has(a.slug) || !!(rf && rf.current === a.slug);
}

/* Mosaic: the best-scored ref big on the left, the next two stacked on the right. */
function renderRefsCell(a) {
  const m = state.matrix;
  if (!a.refs_eligible) return el("div", { class: "cell refs na", title: isBase(a) ? "the baseline has no reference art" : "combos have no reference art" }, "—");
  if (refsBusy(a)) return el("div", { class: "cell refs job running" }, "fetching…");
  const f = a.refs_fetch;
  if (!a.refs.length) {
    const box = el("div", { class: "cell refs missing", title: f && f.error ? f.error : "" }, f && f.fetched_at ? (f.error ? "fetch failed" : "none found") : "no refs");
    if (m.refs.configured) {
      box.append(el("button", { class: "enq", title: f && f.fetched_at ? `searched: ${f.query}\n(fetch again)` : "fetch refs from Gelbooru", onclick: (ev) => { ev.stopPropagation(); fetchRefs([a.slug]); } }, "⤓"));
      if (f && f.fetched_at) box.append(el("button", { class: "enq q", title: "search another booru tag", onclick: (ev) => { ev.stopPropagation(); editRefQuery(a); } }, "✎"));
    }
    return box;
  }
  const box = el("div", { class: `cell refs n${Math.min(a.refs.length, 3)}`, title: f ? `searched: ${f.query}` : "" });
  a.refs.slice(0, 3).forEach((r, i) => box.append(el("div", {
    class: "ref", title: refTitle(r),
    onclick: (ev) => { ev.stopPropagation(); openLightboxRef(a, i); },
  }, refImg(r), NSFW_RATINGS.has(r.rating) ? el("span", { class: "badge nsfw" }, r.rating[0].toUpperCase()) : null)));
  if (m.refs.configured) box.append(el("button", { class: "regen", title: `fetch again (searched: ${f ? f.query : "?"})`, onclick: (ev) => { ev.stopPropagation(); fetchRefs([a.slug]); } }, "⟳"));
  return box;
}

async function fetchRefs(artists, override) {
  try {
    const res = await api("/api/refs/fetch", { json: { artists, ...(override !== undefined ? { override } : {}) } });
    const asked = Array.isArray(artists) ? artists : state.matrix.artists
      .filter(a => a.refs_eligible && (artists === "all" || !(a.refs_fetch && a.refs_fetch.fetched_at))).map(a => a.slug);
    if (res.added) asked.forEach(s => state.refsAsked.add(s));
    toast(res.added ? `refs: ${res.added} artist(s) queued` : "refs: nothing to fetch");
    if (state.matrix) renderMatrix();
    if (state.lb) renderLightboxRefs();
  } catch (e) {
    toast(e.message, true);
  }
}

function editRefQuery(a) {
  const f = a.refs_fetch;
  const cur = f && f.override ? f.override : "";
  const q = window.prompt(`Gelbooru tag to search for ${artistLabel(a)} (empty = derive it from the artist tag).\n` +
    `Last search: ${f && f.query ? f.query : "none"}`, cur);
  if (q === null) return;
  fetchRefs([a.slug], q.trim());
}

/* Open the lightbox on this artist with ref i next to the first visible generated image of the row. */
function openLightboxRef(a, i) {
  const tids = visibleTemplateIds();
  const tid = tids.find(t => hasImage(a.cells[t])) || tids[0] || state.matrix.templates[0].id;
  openLightbox(a.slug, tid);
  state.lb.ref = i;
  renderLightbox();
}

function setShowNsfw(on) {
  state.showNsfw = on;
  saveJSON("showNsfw", on);
  document.body.classList.toggle("show-nsfw", on);
  $("#mx-nsfw").checked = on;
  if (state.lb) renderLightboxRefs();
}
$("#mx-nsfw").addEventListener("change", (ev) => setShowNsfw(ev.target.checked));
// Clicking a blurred ref in the lightbox reveals just that one.
$("#lb-ref").addEventListener("click", (ev) => { if (ev.target.matches("img.nsfw")) ev.target.classList.add("revealed"); });

// --- lightbox -----------------------------------------------------------------

const dlg = $("#lb");

function lbHash() {
  const lb = state.lb;
  return `#matrix${lb ? `/${encodeURIComponent(lb.slug)}/${encodeURIComponent(lb.template)}` : ""}${viewQuery()}`;
}
/* Keep the URL and the Matrix tab link in step with the lightbox + view state (replaceState: no hashchange). */
function syncHash() {
  const onMatrix = $("#tab-matrix").classList.contains("active");
  $('#tabs a[data-tab="matrix"]').href = `#matrix${viewQuery()}`;
  if (onMatrix) history.replaceState(null, "", lbHash());
}

function openLightbox(slug, tid) {
  state.lb = { slug, template: tid, base: false, ref: null };
  syncHash();
  if (!dlg.open) dlg.showModal();
  renderLightbox();
}

function closeLightbox() {
  state.lb = null;
  if (dlg.open) dlg.close();
  syncHash();
}

dlg.addEventListener("close", () => {  // Esc or dlg.close()
  if (state.lb) closeLightbox();
});

function lbOwnArtist() {
  return state.lb ? state.matrix.artists[artistIndex(state.lb.slug)] : null;
}

async function renderLightbox() {
  const lb = state.lb;
  if (!lb) return;
  const m = state.matrix;
  const showSlug = lb.base ? m.base_slug : lb.slug;
  const a = m.artists[artistIndex(showSlug)];
  const t = m.templates[templateIndex(lb.template)];
  const cell = a && t ? a.cells[t.id] : null;
  const img = $("#lb-img");
  const own = lbOwnArtist();

  $("#lb-artist").replaceChildren(
    own ? artistLabel(own) : lb.slug,
    el("span", { class: "sub" }, `${t ? t.name : lb.template} (${lb.template})` + (lb.base ? " · showing baseline" : "")),
  );
  $("#lb-base").classList.toggle("on", lb.base);
  $("#lb-base").disabled = !own || isBase(own) || !hasImage(cellOf(m.base_slug, lb.template));
  $("#lb-copy").disabled = !own || isBase(own);
  $("#lb-newtpl").disabled = !hasImage(cell);
  const ownCell = own && t ? own.cells[t.id] : null;
  $("#lb-regen").disabled = !own || !t || !!(ownCell && ownCell.job);
  $("#lb-regen").textContent = ownCell && (ownCell.stale || ownCell.file_missing) ? "regen stale" : "regenerate";
  renderLightboxRating();
  renderLightboxLabels();
  renderLightboxRefs();

  img.hidden = !hasImage(cell);
  if (!hasImage(cell)) {
    img.removeAttribute("src");
    img.alt = "no image";
    $("#lb-caption").textContent = cell && cell.file_missing ? "file missing on disk" : "no image for this cell";
    $("#lb-open").removeAttribute("href");
    $("#lb-meta").replaceChildren();
    $("#lb-prompts").replaceChildren();
    return;
  }

  img.src = fileUrl(cell);
  img.alt = `${showSlug} / ${t.id}`;
  $("#lb-open").href = fileUrl(cell);
  $("#lb-caption").textContent = `${cell.path} · ${cell.meta.width || "?"}×${cell.meta.height || "?"}`;

  const dl = $("#lb-meta");
  dl.replaceChildren();
  const row = (k, v, bad = false) => dl.append(el("dt", {}, k), el("dd", { class: bad ? "bad" : "" }, v));
  row("seed", String(cell.seed));
  row("source", cell.source);
  row("params", cell.params_match ? "match" : (cell.mismatch_note || "mismatch"), !cell.params_match);
  row("template hash", cell.stale ? `stale (${cell.template_hash.slice(0, 8)}… ≠ ${t.hash.slice(0, 8)}…)` : "current", cell.stale);
  row("created", fmtDate(cell.created_at));
  const mt = cell.meta || {};
  row("gen", [mt.steps && `${mt.steps} steps`, mt.scale && `cfg ${mt.scale}`, mt.sampler, mt.noise_schedule].filter(Boolean).join(" · "));

  const pr = $("#lb-prompts");
  pr.replaceChildren(el("h3", {}, "prompt"), el("pre", { class: "dim" }, "loading…"));
  const meta = await imageMeta(cell);
  if (state.lb !== lb) return;  // navigated away while fetching
  pr.replaceChildren();
  if (!meta || !meta.comment) {
    pr.append(el("h3", {}, "prompt"), el("pre", { class: "dim" }, "no metadata"));
    return;
  }
  const c = meta.comment;
  const v4 = c.v4_prompt && c.v4_prompt.caption;
  pr.append(el("h3", {}, "prompt"), el("pre", {}, (v4 && v4.base_caption) || c.prompt || ""));
  const chars = (v4 && v4.char_captions) || [];
  if (chars.length) {
    pr.append(el("h3", {}, "characters"));
    for (const ch of chars) {
      const center = ch.centers && ch.centers[0];
      pr.append(el("pre", {}, `${center ? `(${center.x}, ${center.y}) ` : ""}${ch.char_caption}`));
    }
  }
  pr.append(el("h3", {}, "negative"), el("pre", {}, c.uc || ""));
}

/* Ratings always target the lightbox's own artist (lb.slug), even while the baseline is toggled in. */
function renderLightboxRating() {
  const box = $("#lb-rating");
  box.replaceChildren();
  const own = lbOwnArtist();
  const lb = state.lb;
  if (!own || !lb || isBase(own)) return;
  const cell = own.cells[lb.template];
  box.append(el("span", { class: "lbl" }, "cell"), cell && cell.image_id ? ratingChips(own, lb.template, true) : el("span", { class: "dim" }, "no image"));
  box.append(el("span", { class: "lbl" }, "artist"), ratingChips(own, null, true));
  const note = own.rating && own.rating.note;
  box.append(el("div", { class: "note-line" },
    el("span", {}, "note"), el("span", { class: "txt" }, note || "—"),
    el("button", { class: "btn small", onclick: () => editNote(own) }, note ? "edit" : "add")));
}

function renderLightboxLabels() {
  const box = $("#lb-labels");
  const own = lbOwnArtist();
  box.replaceChildren();
  box.hidden = !own || isBase(own);
  if (box.hidden) return;
  box.append(el("span", { class: "lbl-title dim" }, "labels"), labelEditor(own));
}

/* The ref shown next to the generated image (lb.ref is kept while walking ↑/↓, clamped per artist). */
function lbRef() {
  const lb = state.lb, own = lbOwnArtist();
  if (!lb || lb.ref === null || !own || !own.refs.length) return null;
  return own.refs[Math.min(lb.ref, own.refs.length - 1)];
}

function renderLightboxRefs() {
  const lb = state.lb, own = lbOwnArtist();
  const r = lbRef();
  const fig = $("#lb-ref");
  fig.hidden = !r;
  $(".lb-fig").classList.toggle("pair", !!r);
  fig.replaceChildren();
  if (r) {
    fig.append(refImg(r, false), el("figcaption", {}, `ref ${refTitle(r)} · `,
      el("a", { href: r.post_url, target: "_blank", rel: "noopener noreferrer" }, "post ↗")));
  }
  const box = $("#lb-refs");
  box.replaceChildren();
  box.hidden = !own || !own.refs_eligible;
  if (box.hidden) return;
  box.append(el("h3", {}, "refs · gelbooru"));
  if (own.refs.length) {
    const strip = el("div", { class: "lb-ref-strip" });
    own.refs.forEach((x, i) => strip.append(el("button", {
      class: `lb-ref-thumb${r && lb.ref === i ? " on" : ""}`, title: `${refTitle(x)}${r && lb.ref === i ? " (click: hide)" : ""}`,
      onclick: () => { lb.ref = r && lb.ref === i ? null : i; renderLightboxRefs(); },
    }, refImg(x), el("span", { class: "n" }, String(x.score ?? "?")))));
    box.append(strip);
  } else {
    const f = own.refs_fetch;
    box.append(el("p", { class: "dim small" }, refsBusy(own) ? "fetching…" : f && f.fetched_at ? (f.error ? `fetch failed: ${f.error}` : "none found") : "not fetched yet"));
  }
  const f = own.refs_fetch;
  if (f && f.query) box.append(el("p", { class: "dim small" }, `searched: ${f.query}`));
  const acts = el("div", { class: "row wrap" });
  if (state.matrix.refs.configured) {
    acts.append(
      el("button", { class: "btn small", ...(refsBusy(own) ? { disabled: "" } : {}), onclick: () => fetchRefs([own.slug]) }, own.refs.length ? "fetch again" : "fetch"),
      el("button", { class: "btn small", onclick: () => editRefQuery(own) }, "search as…"));
  }
  acts.append(el("button", { class: "btn small", title: "questionable / explicit refs are blurred unless shown", onclick: () => setShowNsfw(!state.showNsfw) },
    state.showNsfw ? "blur nsfw" : "show nsfw"));
  box.append(acts);
}

/* r in the lightbox: off → ref 1 → ref 2 → … → off. */
function cycleRef() {
  const lb = state.lb, own = lbOwnArtist();
  if (!lb || !own || !own.refs.length) return toast(own && own.refs_eligible ? "no refs for this artist yet" : "no refs for this row");
  const cur = lbRef() ? Math.min(lb.ref, own.refs.length - 1) : null;
  lb.ref = cur === null ? 0 : cur + 1 < own.refs.length ? cur + 1 : null;
  renderLightboxRefs();
}

/* Keyed on id + created_at: an in-place regeneration keeps the image id but rewrites the file (and its prompt). */
async function imageMeta(cell) {
  const key = `${cell.image_id}@${cell.created_at}`;
  if (state.metaCache.has(key)) return state.metaCache.get(key);
  let meta = null;
  try { meta = await api(`/api/images/${cell.image_id}/meta`); }
  catch (e) { toast(e.message, true); }
  state.metaCache.set(key, meta);
  return meta;
}

/* Arrow navigation: ←/→ walk the row (same artist, other templates), ↑/↓ walk the column
   (same template, other artists, in the matrix's current sort + filter order). Cells without an image
   and hidden columns are skipped. */
function navigate(dir) {
  const lb = state.lb;
  if (!lb) return;
  const m = state.matrix;
  if (dir === "left" || dir === "right") {
    const ts = orderedTemplates().filter(t => !state.hidden.has(t.id));
    let i = ts.findIndex(t => t.id === lb.template);
    const step = dir === "right" ? 1 : -1;
    for (let k = 0; k < ts.length; k++) {
      i = (i + step + ts.length) % ts.length;
      if (hasImage(cellOf(lb.slug, ts[i].id))) { lb.template = ts[i].id; lb.base = false; break; }
    }
  } else {
    const rows = visibleArtists();
    let i = rows.findIndex(a => a.slug === lb.slug);
    if (i < 0) { rows.unshift(m.artists[artistIndex(lb.slug)]); i = 0; }  // current row filtered out: still walk the visible ones
    const step = dir === "down" ? 1 : -1;
    const n = rows.length;
    for (let k = 0; k < n; k++) {
      i = (i + step + n) % n;
      if (hasImage(cellOf(rows[i].slug, lb.template))) { lb.slug = rows[i].slug; lb.base = false; break; }
    }
  }
  syncHash();
  renderLightbox();
}

function toggleBase() {
  const lb = state.lb;
  if (!lb || $("#lb-base").disabled) return;
  lb.base = !lb.base;
  renderLightbox();
}

$$(".lb-nav").forEach(b => b.addEventListener("click", () => navigate(b.dataset.nav)));
$("#lb-close").addEventListener("click", closeLightbox);
$("#lb-base").addEventListener("click", toggleBase);
$("#lb-copy").addEventListener("click", () => {
  const a = lbOwnArtist();
  if (a) copyText(artistTagText(a, false));
});
$("#lb-regen").addEventListener("click", () => {
  const a = lbOwnArtist();
  const t = state.lb && state.matrix.templates[templateIndex(state.lb.template)];
  if (a && t) generateCell(a, t, a.cells[t.id]);
});

// --- keyboard -------------------------------------------------------------------

const helpDlg = $("#help");
function toggleHelp() {
  if (helpDlg.open) helpDlg.close(); else helpDlg.showModal();
}
$("#help-btn").addEventListener("click", toggleHelp);

document.addEventListener("keydown", (ev) => {
  if (ev.target && /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) return;
  if (ntDlg.open) return;
  if (helpDlg.open) { if (ev.key === "?") { ev.preventDefault(); helpDlg.close(); } return; }
  if (ev.key === "?") { ev.preventDefault(); toggleHelp(); return; }
  if (dlg.open) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const map = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" };
    const own = lbOwnArtist();
    const lb = state.lb;
    if (map[ev.key]) { ev.preventDefault(); navigate(map[ev.key]); }
    else if (ev.key === "b" || ev.key === "B") { ev.preventDefault(); toggleBase(); }
    else if (ev.key === "r") { ev.preventDefault(); cycleRef(); }
    else if (ev.key === "l") { const inp = $("#lb-labels .lbl-input"); if (inp) { ev.preventDefault(); inp.focus(); } }
    else if (ev.key === "c") { $("#lb-copy").click(); }
    else if (ev.key === "g") { $("#lb-regen").click(); }
    else if (ev.key === "n") { $("#lb-newtpl").click(); }
    else if (ev.key === "o") { const h = $("#lb-open").getAttribute("href"); if (h) window.open(h, "_blank", "noopener"); }
    else if (ev.key in RATING_KEYS && own && !isBase(own) && lb && own.cells[lb.template] && own.cells[lb.template].image_id) {
      ev.preventDefault();
      const v = RATING_KEYS[ev.key];
      rate(own, lb.template, ratingVal(own.cells[lb.template].rating) === v ? null : v);
    }
    else if ((ev.key === "x" || ev.key === "Backspace" || ev.key === "Delete") && own && !isBase(own) && lb && own.cells[lb.template] && ratingVal(own.cells[lb.template].rating) !== null) {
      ev.preventDefault();
      rate(own, lb.template, null);
    }
    return;
  }
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const tabs = $$("#tabs a").map(a => a.dataset.tab);
  if (ev.key === "r") loadMatrix();
  else if (ev.key === "q") toggleDrawer();
  else if (ev.key === "Escape") {
    if (!$("#label-pop").hidden) $("#label-pop").hidden = true;
    else if (state.drawerOpen) toggleDrawer(false);
  }
  else if (ev.key === "/") { ev.preventDefault(); if (!$("#tab-matrix").classList.contains("active")) location.hash = `#matrix${viewQuery()}`; $("#mx-q").focus(); $("#mx-q").select(); }
  else if (ev.key >= "1" && ev.key <= String(tabs.length)) { location.hash = tabs[Number(ev.key) - 1] === "matrix" ? `#matrix${viewQuery()}` : `#${tabs[Number(ev.key) - 1]}`; }
  else if ($("#tab-matrix").classList.contains("active")) {
    if (ev.key === "s") setView({ sort: SORTS[(SORTS.indexOf(state.view.sort) + 1) % SORTS.length] });
    else if (ev.key === "d") setView({ dir: state.view.dir === "asc" ? "desc" : "asc" });
  }
});

// --- tabs / hash routing -------------------------------------------------------

function setTab(name) {
  const known = $$("#tabs a").map(a => a.dataset.tab);
  if (!known.includes(name)) name = known[0];
  $$("#tabs a").forEach(a => a.classList.toggle("active", a.dataset.tab === name));
  $$(".tab").forEach(s => s.classList.toggle("active", s.id === `tab-${name}`));
  try { localStorage.setItem("tab", name); } catch {}
  if (name === "generate") { if (!state.battery) loadBattery(); if (!state.templates) loadTemplates(); }
  if (name === "templates") { loadTemplates(); if (!state.settings) loadSettings(); }
  if (name === "import" && !state.templates) loadTemplates();
}

function applyHash() {
  const h = location.hash.replace(/^#/, "");
  const path = h.split("?")[0];
  const [tab, slug, tid] = path.split("/");
  let name = tab;
  if (!name) {
    try { name = localStorage.getItem("tab") || "matrix"; } catch { name = "matrix"; }
  }
  setTab(name);
  if (name === "matrix") {
    const v = parseView(h);
    if (viewKey(v) !== viewKey(state.view)) {
      state.view = v;
      syncViewControls();
      if (state.matrix) renderMatrix();
    }
  }
  if (name === "matrix" && slug && tid && state.matrix) {
    const s = decodeURIComponent(slug), t = decodeURIComponent(tid);
    if (!state.lb || state.lb.slug !== s || state.lb.template !== t) openLightbox(s, t);
  } else if (state.lb) {
    closeLightbox();
  }
}

window.addEventListener("hashchange", applyHash);
$("#refresh").addEventListener("click", loadMatrix);
$("#cell-size").addEventListener("input", (ev) => {
  document.documentElement.style.setProperty("--cell", `${ev.target.value}px`);
});

(async () => {
  const h = location.hash.replace(/^#/, "");
  setTab(h.split("?")[0].split("/")[0] || "matrix");
  state.view = parseView(h);  // before the first render, so the matrix comes up already filtered / sorted
  setShowNsfw(state.showNsfw);
  syncViewControls();
  syncHash();
  connectSSE();
  await Promise.all([loadMatrix(), loadTemplates(), loadSettings(), loadBattery()]);
  applyHash();
})();
