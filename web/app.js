const view = document.getElementById("view");
const state = { documents: [], metrics: [], entities: [], poll: null };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, options) {
  const r = await fetch(path, options);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return r.json();
}

const KIND_CHIP = { corroborates: "chip-ok", reconciled: "chip-rec", supersedes: "chip-past", contradicts: "chip-bad" };
const GRADE_CHIP = { exact: "chip-ok", nearby: "chip-ok", tokens: "chip-warn", quarantined: "chip-bad" };
const chip = (text, cls) => `<span class="chip ${cls || "chip-past"}">${esc(text)}</span>`;
const kindChip = (kind) => chip(kind, KIND_CHIP[kind]);
const gradeChip = (c) => chip(c.grade || c.status, GRADE_CHIP[c.grade || c.status]);
const shortDoc = (name) => esc((name || "").replace(/\.pdf$/i, "").slice(0, 44));
const pageUrl = (c, highlight = true) => `/pages/${c.doc_id}/${c.located_page || c.page_no}.png?dpi=110${highlight && c.grade && c.grade !== "tokens" ? `&claim=${c.id}` : ""}`;

function value(c) {
  const bits = [c.value_raw, c.unit_raw].filter(Boolean).join(" ");
  const tail = [c.period_raw, c.scope_raw ? `[${c.scope_raw}]` : null, c.basis_raw && c.basis_raw !== "actual" ? `(${c.basis_raw})` : null].filter(Boolean).join(" ");
  return `<span class="font-semibold">${esc(bits)}</span> <span class="text-slate-500">${esc(tail)}</span>`;
}

function claimCard(c, opts = {}) {
  return `
    <div class="rounded border border-slate-200 bg-white p-4 ${opts.cls || ""}">
      <div class="flex items-baseline gap-2 text-xs text-slate-500 mono">
        <span>${shortDoc(c.filename)}</span><span>p${c.located_page || c.page_no}</span>${gradeChip(c)}
      </div>
      <div class="mt-1 text-sm"><span class="text-slate-600">${esc(c.subject)}</span> · <span class="mono text-xs text-slate-500">${esc(c.metric_key || c.metric_raw)}</span></div>
      <div class="mt-1 text-sm">${esc(c.label || "")}</div>
      <div class="mt-1">${value(c)}</div>
      <div class="mt-2 text-sm quote">${esc(c.quote)}</div>
      ${opts.image ? `<img class="mt-3 w-full rounded border border-slate-200" loading="lazy" src="${pageUrl(c)}" alt="page ${c.page_no}" />` : `<button class="mt-2 text-xs text-indigo-700 underline" onclick="showClaim(${c.id})">show page</button>`}
    </div>`;
}

function setNav(name) {
  document.querySelectorAll("a.nav").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${name}`));
}

async function loadBasics() {
  const [docs, metrics, entities] = await Promise.all([api("/documents"), api("/metrics"), api("/entities")]);
  state.documents = docs.documents;
  state.relationCounts = docs.relations;
  state.metrics = metrics.metrics;
  state.entities = entities.entities;
}

async function documentsView() {
  const data = await api("/documents");
  state.documents = data.documents;
  const busy = data.documents.some((d) => ["queued", "ingested", "extracting", "verifying", "reconciling"].includes(d.status));
  const rows = data.documents.map((d) => `
    <tr class="border-t border-slate-100">
      <td class="py-2 pr-3 text-sm">${esc(d.filename)}<div class="text-xs text-slate-500">${esc(d.title || "")}</div></td>
      <td class="py-2 pr-3 text-sm">${esc(d.publisher || "")}<div class="text-xs text-slate-500">${esc(d.published_at || "date not found")}</div></td>
      <td class="py-2 pr-3 text-sm mono">${d.page_count}</td>
      <td class="py-2 pr-3 text-sm">${chip(d.status, d.status === "ready" || d.status === "extracted" ? "chip-ok" : d.status === "failed" ? "chip-bad" : "chip-warn")}${d.error ? `<div class="text-xs text-red-700">${esc(d.error)}</div>` : ""}</td>
      <td class="py-2 pr-3 text-sm mono">${d.claims}</td>
      <td class="py-2 pr-3 text-sm mono">${d.exact}</td>
      <td class="py-2 pr-3 text-sm mono">${d.weak}</td>
      <td class="py-2 pr-3 text-sm mono">${d.quarantined}</td>
      <td class="py-2 text-xs mono text-slate-500">${esc(d.model || "")}</td>
    </tr>`).join("");
  const rc = data.relations;
  const counts = (obj) => Object.entries(obj).map(([k, n]) => `${kindChip(k)} <span class="mono text-sm">${n}</span>`).join(" &nbsp; ");
  view.innerHTML = `
    <section class="grid gap-6 md:grid-cols-3">
      <form id="upload" class="rounded border border-slate-200 bg-white p-4 md:col-span-1">
        <h2 class="font-semibold">Add a PDF</h2>
        <p class="mt-1 text-sm text-slate-600">Pages are read by the model, every quote is re-found on its page, then claims are compared with everything already in the layer.</p>
        <input class="mt-3 block w-full text-sm" type="file" name="file" accept="application/pdf" required />
        <input class="mt-2 block w-full rounded border border-slate-300 px-2 py-1 text-sm" name="key" placeholder="Optional: your own Gemini API key, used for this upload only" />
        <button class="mt-3 rounded bg-indigo-800 px-3 py-1.5 text-sm text-white">Upload and process</button>
        <div id="upload-msg" class="mt-2 text-sm text-slate-600"></div>
      </form>
      <div class="rounded border border-slate-200 bg-white p-4 md:col-span-2">
        <h2 class="font-semibold">Relations across documents</h2>
        <div class="mt-2">${counts(rc.cross || {}) || '<span class="text-sm text-slate-500">none yet</span>'}</div>
        <h3 class="mt-3 text-sm font-semibold text-slate-600">Within a single document</h3>
        <div class="mt-1">${counts(rc.within || {}) || '<span class="text-sm text-slate-500">none yet</span>'}</div>
      </div>
    </section>
    <section class="mt-8 overflow-x-auto rounded border border-slate-200 bg-white p-4">
      <table class="w-full min-w-[900px]">
        <thead class="text-left text-xs uppercase tracking-wide text-slate-500"><tr>
          <th class="pb-2 pr-3">Document</th><th class="pb-2 pr-3">Publisher</th><th class="pb-2 pr-3">Pages</th><th class="pb-2 pr-3">Status</th>
          <th class="pb-2 pr-3">Claims</th><th class="pb-2 pr-3">Exact</th><th class="pb-2 pr-3">Weak</th><th class="pb-2 pr-3">Quarantined</th><th class="pb-2">Model</th>
        </tr></thead>
        <tbody>${rows || '<tr><td class="py-4 text-sm text-slate-500" colspan="9">No documents yet.</td></tr>'}</tbody>
      </table>
    </section>`;
  document.getElementById("upload").onsubmit = async (e) => {
    e.preventDefault();
    const form = e.target;
    const msg = document.getElementById("upload-msg");
    const body = new FormData();
    body.append("file", form.file.files[0]);
    const headers = form.key.value ? { "X-Gemini-Key": form.key.value } : {};
    msg.textContent = "Uploading…";
    try {
      const r = await api("/documents", { method: "POST", body, headers });
      msg.textContent = r.new ? `Queued ${r.page_count} pages as document ${r.id}.` : `Already in the layer as document ${r.id} (${r.status}).`;
      form.reset();
      documentsView();
    } catch (err) {
      msg.textContent = err.message;
    }
  };
  clearInterval(state.poll);
  if (busy) state.poll = setInterval(() => { if (location.hash === "#documents" || location.hash === "") documentsView(); else clearInterval(state.poll); }, 2500);
}

async function factsView() {
  await loadBasics();
  const params = state.factFilters || {};
  const docOptions = state.documents.map((d) => `<option value="${d.id}" ${String(params.doc) === String(d.id) ? "selected" : ""}>${shortDoc(d.filename)}</option>`).join("");
  view.innerHTML = `
    <div class="grid gap-6 lg:grid-cols-4">
      <section class="lg:col-span-3">
        <form id="filters" class="flex flex-wrap items-end gap-3 rounded border border-slate-200 bg-white p-3 text-sm">
          <label>Document<select name="doc" class="block rounded border border-slate-300 px-2 py-1"><option value="">all</option>${docOptions}</select></label>
          <label>Metric<input name="metric" list="metric-list" value="${esc(params.metric || "")}" class="block w-56 rounded border border-slate-300 px-2 py-1" /></label>
          <label>Entity<input name="entity" list="entity-list" value="${esc(params.entity || "")}" class="block w-44 rounded border border-slate-300 px-2 py-1" /></label>
          <label>Grade<select name="grade" class="block rounded border border-slate-300 px-2 py-1"><option value="">any</option>${["exact", "nearby", "tokens"].map((g) => `<option ${params.grade === g ? "selected" : ""}>${g}</option>`).join("")}</select></label>
          <label>Search<input name="q" value="${esc(params.q || "")}" class="block w-48 rounded border border-slate-300 px-2 py-1" placeholder="label or quote" /></label>
          <button class="rounded bg-indigo-800 px-3 py-1.5 text-white">Filter</button>
        </form>
        <datalist id="metric-list">${state.metrics.map((m) => `<option value="${esc(m.key)}"></option>`).join("")}</datalist>
        <datalist id="entity-list">${state.entities.map((e) => `<option value="${esc(e.name_canon)}"></option>`).join("")}</datalist>
        <div id="facts" class="mt-4"></div>
      </section>
      <aside class="rounded border border-slate-200 bg-white p-4">
        <h2 class="font-semibold">Metric registry</h2>
        <p class="mt-1 text-xs text-slate-500">${state.metrics.length} keys, grown from the documents. Click one to filter.</p>
        <ul class="mt-2 max-h-[70vh] overflow-y-auto text-sm">
          ${state.metrics.map((m) => `<li class="flex justify-between gap-2 border-t border-slate-100 py-1"><button class="text-left mono text-xs text-indigo-800" onclick="setMetric('${esc(m.key)}')">${esc(m.key)}${m.aliases.length ? `<span class="text-slate-400"> +${m.aliases.length}</span>` : ""}</button><span class="mono text-xs text-slate-500">${m.claim_count}</span></li>`).join("")}
        </ul>
      </aside>
    </div>`;
  document.getElementById("filters").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    state.factFilters = Object.fromEntries([...f.entries()].filter(([, v]) => v));
    renderFacts();
  };
  renderFacts();
}

async function renderFacts() {
  const params = new URLSearchParams(state.factFilters || {});
  params.set("limit", "300");
  const data = await api(`/claims?${params}`);
  const rows = data.claims.map((c) => `
    <tr class="border-t border-slate-100 cursor-pointer hover:bg-slate-50" onclick="showClaim(${c.id})">
      <td class="py-1.5 pr-3 text-xs mono text-slate-500">${shortDoc(c.filename)} p${c.located_page || c.page_no}</td>
      <td class="py-1.5 pr-3 text-sm">${esc(c.subject)}</td>
      <td class="py-1.5 pr-3 text-xs mono">${esc(c.metric_key || c.metric_raw)}</td>
      <td class="py-1.5 pr-3 text-sm">${esc(c.label || "")}</td>
      <td class="py-1.5 pr-3 text-sm">${value(c)}</td>
      <td class="py-1.5">${gradeChip(c)}</td>
    </tr>`).join("");
  document.getElementById("facts").innerHTML = `
    <div class="overflow-x-auto rounded border border-slate-200 bg-white p-3">
      <div class="mb-2 text-xs text-slate-500">${data.claims.length} claims shown</div>
      <table class="w-full min-w-[800px]"><tbody>${rows || '<tr><td class="py-3 text-sm text-slate-500">Nothing matches.</td></tr>'}</tbody></table>
    </div>`;
}

function setMetric(key) {
  state.factFilters = { ...(state.factFilters || {}), metric: key };
  factsView();
}

async function relationsView() {
  await loadBasics();
  const p = state.relationFilters || { cross: "true" };
  view.innerHTML = `
    <form id="rfilters" class="flex flex-wrap items-end gap-3 rounded border border-slate-200 bg-white p-3 text-sm">
      <label>Kind<select name="kind" class="block rounded border border-slate-300 px-2 py-1"><option value="">all</option>${["corroborates", "reconciled", "supersedes", "contradicts"].map((k) => `<option ${p.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select></label>
      <label>Confidence<select name="confidence" class="block rounded border border-slate-300 px-2 py-1"><option value="">any</option>${["high", "low", "review"].map((k) => `<option ${p.confidence === k ? "selected" : ""}>${k}</option>`).join("")}</select></label>
      <label>Metric<input name="metric" list="metric-list" value="${esc(p.metric || "")}" class="block w-56 rounded border border-slate-300 px-2 py-1" /></label>
      <label class="flex items-center gap-2 pb-1"><input type="checkbox" name="cross" value="true" ${p.cross === "true" ? "checked" : ""} /> across documents only</label>
      <button class="rounded bg-indigo-800 px-3 py-1.5 text-white">Filter</button>
    </form>
    <datalist id="metric-list">${state.metrics.map((m) => `<option value="${esc(m.key)}"></option>`).join("")}</datalist>
    <div id="relations" class="mt-4 grid gap-3"></div>`;
  document.getElementById("rfilters").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    state.relationFilters = Object.fromEntries([...f.entries()].filter(([, v]) => v));
    if (!state.relationFilters.cross) state.relationFilters.cross = "false";
    renderRelations();
  };
  renderRelations();
}

function relationRow(r) {
  const side = (c) => `<div class="text-sm"><div class="text-xs mono text-slate-500">${shortDoc(c.filename)} p${c.located_page || c.page_no} ${gradeChip(c)}</div><div>${esc(c.subject)} · ${esc(c.label || c.metric_raw)}</div><div>${value(c)}</div></div>`;
  return `
    <div class="grid cursor-pointer gap-4 rounded border border-slate-200 bg-white p-4 hover:bg-slate-50 md:grid-cols-[1fr_auto_1fr]" onclick="showRelation(${r.id})">
      ${side(r.a)}
      <div class="text-center text-xs text-slate-600 max-w-[16rem]">${kindChip(r.kind)}${r.dimension ? ` <span class="mono">${esc(r.dimension)}</span>` : ""}${r.confidence !== "high" ? ` ${chip(r.confidence, r.confidence === "review" ? "chip-bad" : "chip-warn")}` : ""}<div class="mt-1">${esc(r.explanation || "")}</div></div>
      ${side(r.b)}
    </div>`;
}

async function renderRelations() {
  const params = new URLSearchParams(state.relationFilters || { cross: "true" });
  params.set("limit", "300");
  const data = await api(`/relations?${params}`);
  document.getElementById("relations").innerHTML = `<div class="text-xs text-slate-500">${data.relations.length} relations shown</div>` + (data.relations.map(relationRow).join("") || '<div class="text-sm text-slate-500">Nothing matches.</div>');
}

async function quarantineView() {
  const data = await api("/quarantine?limit=500");
  const rows = data.claims.map((c) => `
    <tr class="border-t border-slate-100">
      <td class="py-1.5 pr-3 text-xs mono text-slate-500">${shortDoc(c.filename)} p${c.page_no}</td>
      <td class="py-1.5 pr-3 text-xs mono">${esc(c.quarantine_reason)}</td>
      <td class="py-1.5 pr-3 text-sm">${esc(c.subject)} · ${esc(c.label || c.metric_raw)}</td>
      <td class="py-1.5 pr-3 text-sm">${value(c)}</td>
      <td class="py-1.5 pr-3 text-sm quote">${esc(c.quote)}</td>
      <td class="py-1.5"><button class="text-xs text-indigo-700 underline" onclick="showClaim(${c.id})">cited page</button></td>
    </tr>`).join("");
  view.innerHTML = `
    <section class="rounded border border-slate-200 bg-white p-4">
      <h2 class="font-semibold">Quarantine</h2>
      <p class="mt-1 text-sm text-slate-600">Claims the model produced whose quote could not be re-found on the cited page or its neighbours. They never became facts. ${data.claims.length} in the queue.</p>
      <div class="mt-3 overflow-x-auto"><table class="w-full min-w-[900px]"><tbody>${rows || '<tr><td class="py-3 text-sm text-slate-500">Empty.</td></tr>'}</tbody></table></div>
    </section>`;
}

async function answerView() {
  await loadBasics();
  const p = state.answerQuery || {};
  view.innerHTML = `
    <section class="rounded border border-slate-200 bg-white p-4">
      <h2 class="font-semibold">Answer</h2>
      <p class="mt-1 text-sm text-slate-600">The current value for an entity, metric and period, with everything it superseded and why. Nothing here is generated: it is a lookup over the relations table.</p>
      <form id="aform" class="mt-3 flex flex-wrap items-end gap-3 text-sm">
        <label>Entity<input name="entity" list="entity-list" value="${esc(p.entity || "")}" class="block w-48 rounded border border-slate-300 px-2 py-1" required /></label>
        <label>Metric<input name="metric" list="metric-list" value="${esc(p.metric || "")}" class="block w-56 rounded border border-slate-300 px-2 py-1" required /></label>
        <label>Period<input name="period" value="${esc(p.period || "")}" class="block w-40 rounded border border-slate-300 px-2 py-1" placeholder="FY25, 2024-25, Q4 FY24" /></label>
        <button class="rounded bg-indigo-800 px-3 py-1.5 text-white">Ask</button>
      </form>
      <datalist id="metric-list">${state.metrics.map((m) => `<option value="${esc(m.key)}"></option>`).join("")}</datalist>
      <datalist id="entity-list">${state.entities.map((e) => `<option value="${esc(e.name_canon)}"></option>`).join("")}</datalist>
      <div id="answer" class="mt-4"></div>
    </section>`;
  document.getElementById("aform").onsubmit = (e) => {
    e.preventDefault();
    state.answerQuery = Object.fromEntries(new FormData(e.target).entries());
    renderAnswer();
  };
  if (p.entity && p.metric) renderAnswer();
}

async function renderAnswer() {
  const q = state.answerQuery;
  const params = new URLSearchParams(Object.fromEntries(Object.entries(q).filter(([, v]) => v)));
  const box = document.getElementById("answer");
  let data;
  try { data = await api(`/answer?${params}`); } catch (err) { box.innerHTML = `<div class="text-sm text-red-700">${esc(err.message)}</div>`; return; }
  if (!data.found) { box.innerHTML = `<div class="text-sm text-slate-600">${esc(data.reason)}</div>`; return; }
  const list = (title, items, render) => items.length ? `<h3 class="mt-5 text-sm font-semibold text-slate-600">${title}</h3><div class="mt-2 grid gap-3 md:grid-cols-2">${items.map(render).join("")}</div>` : "";
  box.innerHTML = `
    <div class="text-xs mono text-slate-500">block ${esc(data.block)} · ${data.members} grounded claims</div>
    <h3 class="mt-3 text-sm font-semibold text-slate-600">Current</h3>
    <div class="mt-2">${claimCard(data.current, { image: true })}</div>
    ${list("Superseded", data.history, (h) => `<div>${claimCard(h.claim)}<div class="mt-1 text-xs text-slate-600">${esc(h.explanation)}</div></div>`)}
    ${list("Corroborated by", data.corroborated_by, (x) => `<div>${claimCard(x.claim)}${x.explanation ? `<div class="mt-1 text-xs text-slate-600">${esc(x.explanation)}</div>` : ""}</div>`)}
    ${list("Contradicted by (for review)", data.contradicted_by, (x) => `<div>${claimCard(x.claim)}<div class="mt-1 text-xs text-slate-600">${esc(x.explanation)}</div></div>`)}
    ${list("Reconciled with", data.reconciled_with, (x) => `<div>${claimCard(x.claim)}<div class="mt-1 text-xs text-slate-600">${esc(x.dimension)}: ${esc(x.explanation)}</div></div>`)}`;
}

async function showClaim(id) {
  const c = await api(`/claims/${id}`);
  openModal(`
    <div class="flex items-start justify-between gap-4"><h2 class="font-semibold">${esc(c.subject)} · ${esc(c.label || c.metric_raw)}</h2><button class="text-sm text-slate-500" onclick="closeModal()">close</button></div>
    <div class="mt-3 grid gap-4 md:grid-cols-[2fr_3fr]">
      <div>${claimCard(c)}
        <dl class="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
          ${[["entity", c.entity], ["metric", c.metric_key], ["period", c.period_start ? `${c.period_start} → ${c.period_end}` : c.period_raw], ["unit", c.unit_canon], ["value", c.value_canon ?? c.value_text], ["scope", c.scope_canon], ["basis", c.basis_canon], ["grade", c.grade || c.quarantine_reason], ["publisher", c.publisher], ["published", c.published_at], ["model", c.model]].map(([k, v]) => `<dt class="text-slate-500">${k}</dt><dd class="mono">${esc(v ?? "")}</dd>`).join("")}
        </dl>
      </div>
      <img class="w-full rounded border border-slate-200" src="${pageUrl(c)}" alt="page ${c.page_no}" />
    </div>`);
}

async function showRelation(id) {
  const r = await api(`/relations/${id}`);
  openModal(`
    <div class="flex items-start justify-between gap-4">
      <div>${kindChip(r.kind)}${r.dimension ? ` <span class="mono text-xs">${esc(r.dimension)}</span>` : ""} ${r.confidence !== "high" ? chip(r.confidence, r.confidence === "review" ? "chip-bad" : "chip-warn") : ""}<div class="mt-1 text-sm text-slate-700">${esc(r.explanation || "")}</div></div>
      <button class="text-sm text-slate-500" onclick="closeModal()">close</button>
    </div>
    <div class="mt-4 grid gap-4 md:grid-cols-2">${claimCard(r.a, { image: true })}${claimCard(r.b, { image: true })}</div>`);
}

function openModal(html) {
  document.getElementById("modal-body").innerHTML = html;
  document.getElementById("modal").classList.remove("hidden");
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
}

const VIEWS = { documents: documentsView, facts: factsView, relations: relationsView, quarantine: quarantineView, answer: answerView };

const DETAIL = { relation: ["relations", showRelation], claim: ["facts", showClaim] };

async function route() {
  const [path, query] = location.hash.replace("#", "").split("?");
  const [head, id] = path.split("/");
  const [name, open] = DETAIL[head] || [head || "documents", null];
  if (query) {
    const params = Object.fromEntries(new URLSearchParams(query).entries());
    if (name === "facts") state.factFilters = params;
    if (name === "relations") state.relationFilters = { cross: "true", ...params };
    if (name === "answer") state.answerQuery = params;
  }
  setNav(name);
  closeModal();
  clearInterval(state.poll);
  view.innerHTML = '<div class="text-sm text-slate-500">Loading…</div>';
  try {
    await (VIEWS[name] || documentsView)();
    if (open && id) await open(Number(id));
  } catch (err) { view.innerHTML = `<div class="text-sm text-red-700">${esc(err.message)}</div>`; }
}

api("/health").then((h) => { document.getElementById("health").textContent = `${h.model} · ${h.db}`; }).catch(() => {});
window.addEventListener("hashchange", route);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
route();
