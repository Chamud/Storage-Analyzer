/* Storage Analyzer UI. Plain JS, no build step. State: view, drive, minBytes, selection (node ids). */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const GB = 1073741824;
  const fmt = (n) => { n = Number(n || 0); if (n >= GB) return (n / GB).toFixed(n >= 100 * GB ? 0 : 1) + " GB"; if (n >= 1048576) return (n / 1048576).toFixed(0) + " MB"; if (n >= 1024) return (n / 1024).toFixed(0) + " KB"; return n + " B"; };
  const SAF = { safe: "Safe", review: "Review", keep: "Keep", unknown: "Unknown", rebuildable: "Review" };

  const urlView = new URLSearchParams(location.search).get("view");
  const st = {
    view: urlView || localStorage.getItem("sa.view") || "overview",
    drive: localStorage.getItem("sa.drive") || "",
    minBytes: Number(localStorage.getItem("sa.min") ?? 104857600),
    sel: new Set(JSON.parse(localStorage.getItem("sa.sel") || "[]")),
    state: null, cat: null, explore: { nodeId: null, detail: null }, pf: "all", jobTimer: null, cleaned: null,
  };
  const save = () => { localStorage.setItem("sa.view", st.view); localStorage.setItem("sa.drive", st.drive); localStorage.setItem("sa.min", st.minBytes); localStorage.setItem("sa.sel", JSON.stringify([...st.sel])); };

  async function api(path, body) {
    const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined);
    if (!r.ok) { let d = ""; try { d = (await r.json()).detail; } catch { d = r.statusText; } throw new Error(d); }
    return r.json();
  }
  const q = (params) => { const p = new URLSearchParams(); for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") p.set(k, v); return p.toString(); };
  const drv = () => st.drive || null;

  let toastT;
  function toast(msg, err) { const t = $("#toast"); t.textContent = msg; t.classList.toggle("err", !!err); t.classList.remove("hidden"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.add("hidden"), err ? 8000 : 4000); }

  // ---------------------------------------------------------------- selection
  function toggleSel(id, on) { id = Number(id); if (on === undefined) on = !st.sel.has(id); on ? st.sel.add(id) : st.sel.delete(id); st.cleaned = null; save(); updateCart(); }
  function updateCart() { const b = $("#nav-cart"); b.textContent = st.sel.size; b.classList.toggle("zero", !st.sel.size); }

  // ---------------------------------------------------------------- shared renderers
  const srcTag = (it) => it.source === "ai" ? `<span class="tag ai">AI · ${Math.round((it.confidence || 0) * 100)}% sure</span>` : it.source === "rule" ? `<span class="tag">Rule${it.rule_id ? ": " + esc(it.rule_id) : ""}</span>` : `<span class="tag">Not classified</span>`;
  function itemRow(it, opts = {}) {
    const keep = it.safety === "keep";
    const checked = st.sel.has(it.id) ? "checked" : "";
    const size = opts.size ?? it.excl_bytes ?? it.allocated;
    return `<div class="row" data-id="${it.id}">
      ${keep ? `<span class="lock" title="Protected">🔒</span>` : `<input type="checkbox" class="sel" data-id="${it.id}" ${checked} aria-label="Select ${esc(it.name)}">`}
      <div class="body">
        <div class="name"><a class="open-detail" data-id="${it.id}">${esc(it.name)}</a><span class="pill ${it.safety}">${SAF[it.safety] || it.safety}</span>${srcTag(it)}${it.admin ? '<span class="tag admin">needs admin</span>' : ""}</div>
        <div class="path" title="${esc(it.path)}">${esc(it.path)}</div>
        ${it.reason ? `<div class="why">${esc(it.reason)}</div>` : ""}
        ${it.action ? `<div class="how">How: ${esc(it.action)}</div>` : ""}
      </div>
      <div class="size">${fmt(size)}</div>
    </div>`;
  }
  function bindRows(root) {
    $$("input.sel", root).forEach((cb) => cb.addEventListener("change", () => toggleSel(cb.dataset.id, cb.checked)));
    $$(".open-detail", root).forEach((a) => a.addEventListener("click", () => openDetail(a.dataset.id)));
  }

  // ---------------------------------------------------------------- views
  const views = {
    overview: ["Overview", "What is using your space, and what you can safely remove"],
    projects: ["Projects", "Every code project found, with git status and the folders you can rebuild"],
    categories: ["Categories", "Everything sorted by what it is"],
    explore: ["Explore map", "Treemap coloured by safety, not file type. Click to drill down"],
    tidy: ["Tidy up", "Small leftovers that add up: grouped by category, selectable in bulk"],
    cleanup: ["Cleanup", "Check what will happen, then clean"],
  };

  function setView(v) {
    st.view = v; save();
    $$(".nav").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
    $$(".view").forEach((s) => s.classList.toggle("hidden", s.id !== "view-" + v));
    $("#title").textContent = views[v][0]; $("#subtitle").textContent = views[v][1];
    render();
  }

  async function loadState() {
    st.state = await api("/api/state");
    const s = st.state;
    $("#brand-sub").textContent = s.scan ? `scan #${s.scan.id} · ${s.scan.finished_at?.replace("T", " ").slice(0, 16)}` : "no scan yet";
    $("#side-foot").innerHTML = `Scanner: ${s.wiztree ? "WizTree (MFT mode)" : "built-in walker"}<br>${s.scan ? `${s.scan.nodes.toLocaleString()} folders/files` : ""}<br>AI: ${s.ai_available ? (s.ai_enabled ? "Claude Code, on" : "off") : "Claude Code not found"}`;
    $("#ai-toggle").setAttribute("aria-pressed", s.ai_enabled ? "true" : "false");
    $("#ai-label").textContent = !s.ai_available ? "not installed" : s.ai_enabled ? "Claude Code · on" : "off · rules only";
    $("#nav-projects").textContent = Object.values(s.projects || {}).reduce((a, x) => a + x.n, 0) || "";
    const seg = $("#drive-seg");
    seg.innerHTML = `<button data-d="" class="${st.drive ? "" : "on"}">All drives</button>` + s.drives.map((d) => `<button data-d="${d.letter}" class="${st.drive === d.letter ? "on" : ""}">${d.letter}:</button>`).join("");
    $$("button", seg).forEach((b) => b.addEventListener("click", () => { st.drive = b.dataset.d; save(); $$("button", seg).forEach((x) => x.classList.toggle("on", x === b)); render(); }));
    updateCart();
  }

  function render() {
    if (!st.state) return;
    ({ overview: renderOverview, projects: renderProjects, categories: renderCategories, explore: renderExplore, tidy: renderTidy, cleanup: renderCleanup })[st.view]();
  }

  // ---------------------------------------------------------------- overview
  async function renderOverview() {
    const el = $("#view-overview");
    const s = st.state;
    if (!s.scan) { el.innerHTML = `<div class="card"><h2>No scan yet</h2><p class="muted">Click <b>Rescan drives</b>. You will get one Windows admin prompt; WizTree then reads each drive in seconds.</p></div>`; return; }
    const drives = s.drives.filter((d) => !st.drive || d.letter === st.drive);
    const tot = st.drive ? (drives[0]?.totals || {}) : s.totals;
    const [wins, review, unknown] = await Promise.all([
      api("/api/items?" + q({ safety: "safe", drive: drv(), min_bytes: st.minBytes, limit: 40 })),
      api("/api/items?" + q({ safety: "review", drive: drv(), min_bytes: st.minBytes, limit: 8 })),
      api("/api/items?" + q({ safety: "unknown", drive: drv(), min_bytes: st.minBytes, limit: 8 })),
    ]);
    const safeN = Object.values(s.projects || {});
    const gitWarn = (s.projects?.warn?.n || 0), gitNone = (s.projects?.none?.n || 0);
    el.innerHTML = `
      <div class="cards">${drives.map((d) => {
        const t = d.totals || {}; const pc = (n) => (100 * (n || 0) / d.capacity).toFixed(2) + "%";
        return `<div class="card"><div class="hd"><div style="font-size:16px"><b>${d.letter}:</b> <span class="muted">${esc(d.label || d.fs)}</span></div><small>${fmt(d.free)} free of ${fmt(d.capacity)}</small></div>
          <div class="bar"><span style="width:${pc(t.safe)};background:var(--safe)"></span><span style="width:${pc(t.review)};background:var(--review)"></span><span style="width:${pc(t.unknown)};background:var(--unknown)"></span><span style="width:${pc(d.used - (t.safe || 0) - (t.review || 0) - (t.unknown || 0))};background:#4A5261"></span></div>
          <div class="legend"><span><i style="background:var(--safe)"></i>Safe ${fmt(t.safe)}</span><span><i style="background:var(--review)"></i>Review ${fmt(t.review)}</span><span><i style="background:var(--unknown)"></i>Unknown ${fmt(t.unknown)}</span><span><i style="background:#4A5261"></i>Other used</span></div></div>`;
      }).join("")}</div>
      <div class="hero"><div><small>You can safely free</small><div class="big">${fmt(tot.safe)}</div><div class="note">Re-downloadable, rebuilt automatically, or already deleted · another ${fmt(tot.review)} is worth a look · ${fmt(tot.unknown)} not classified yet</div></div>
        <div class="actions"><button class="btn safe" id="add-all-safe">Add all safe items to cleanup</button><button class="btn ghost" id="go-cleanup">Review cleanup (${st.sel.size})</button></div></div>
      <div class="cols">
        <div class="w list"><div class="hd"><h2>Quick wins</h2><small>Biggest safe items first · ${st.minBytes ? "items under " + fmt(st.minBytes) + " are hidden" : "showing all"}</small></div>
          ${wins.length ? wins.map((it) => itemRow(it)).join("") : `<div class="empty">Nothing safe above the size limit on this drive.</div>`}</div>
        <div class="n">
          <div class="card"><h2>Projects</h2><div class="muted">${s.stale?.n || 0} projects untouched for ${s.stale_days}+ days hold <b style="color:var(--text)">${fmt(s.stale?.bytes)}</b> of rebuildable folders.</div>
            ${gitWarn + gitNone ? `<div class="gitbox warn">⚠ ${gitWarn} projects have work that is not pushed, ${gitNone} have no git at all.</div>` : `<div class="gitbox ok">All projects are pushed.</div>`}
            <div class="actions"><button class="btn" data-go="projects">Open project breakdown</button><button class="btn warn" id="go-git">Show git issues</button></div></div>
          <div class="list"><div class="hd"><h2>Review first</h2><small>Large, but needs a decision</small></div>${review.map((it) => `<div class="row compact"><span class="dot ${it.safety}"></span><div class="body"><div class="name"><a class="open-detail" data-id="${it.id}">${esc(it.name)}</a></div><div class="path">${esc(it.action)}</div></div><div class="size">${fmt(it.excl_bytes)}</div></div>`).join("") || `<div class="empty">Nothing to review.</div>`}</div>
          <div class="list"><div class="hd"><h2>Not classified yet</h2><small>${s.unknown_count} items</small></div>${unknown.map((it) => `<div class="row compact"><span class="dot unknown"></span><div class="body"><div class="name"><a class="open-detail" data-id="${it.id}">${esc(it.name)}</a></div><div class="path">${esc(it.path)}</div></div><div class="size">${fmt(it.excl_bytes)}</div></div>`).join("") || `<div class="empty">Everything above your size limit has a category.</div>`}
            ${unknown.length ? `<div class="more"><button class="btn ai" id="ask-ai">${s.ai_available ? (s.ai_enabled ? "Ask AI to classify all unknown" : "Turn on AI and classify") : "Claude Code not installed"}</button></div>` : ""}</div>
        </div>
      </div>`;
    bindRows(el);
    $("#add-all-safe").onclick = async () => { const all = await api("/api/items?" + q({ safety: "safe", drive: drv(), limit: 5000 })); all.forEach((it) => st.sel.add(it.id)); st.cleaned = null; save(); updateCart(); toast(`Added ${all.length} safe items (${fmt(all.reduce((a, x) => a + x.excl_bytes, 0))})`); setView("cleanup"); };
    $("#go-cleanup").onclick = () => setView("cleanup");
    $$("[data-go]", el).forEach((b) => b.onclick = () => setView(b.dataset.go));
    $("#go-git").onclick = () => { st.pf = "git"; setView("projects"); };
    const ask = $("#ask-ai"); if (ask) ask.onclick = async () => { if (!s.ai_available) return; if (!s.ai_enabled) await setAi(true); runAi({}); };
  }

  // ---------------------------------------------------------------- projects
  async function renderProjects() {
    const el = $("#view-projects");
    const ps = await api("/api/projects?" + q({ drive: drv() }));
    const stale = ps.filter((p) => p.stale), git = { ok: 0, warn: 0, none: 0 };
    ps.forEach((p) => { git[p.git_state] = (git[p.git_state] || 0) + 1; });
    const shown = ps.filter((p) => st.pf === "all" || (st.pf === "stale" ? p.stale : st.pf === "active" ? !p.stale : p.git_state !== "ok"));
    const rb = (p, onlySafe) => p.rebuildable.filter((k) => !onlySafe || k.safety === "safe").reduce((a, k) => a + k.allocated, 0);
    el.innerHTML = `
      <div class="stats"><div class="stat"><small>Projects found</small><b>${ps.length}</b></div><div class="stat"><small>Stale (${st.state.stale_days}+ days)</small><b>${stale.length}</b></div><div class="stat"><small>Rebuildable, all projects</small><b>${fmt(ps.reduce((a, p) => a + rb(p), 0))}</b></div><div class="stat green"><small>Safe now (stale projects)</small><b>${fmt(ps.reduce((a, p) => a + rb(p, true), 0))}</b></div>
        <div class="stat"><small>Git status</small><div style="display:flex;gap:12px;flex-wrap:wrap;font-size:13px;margin-top:4px"><span><i class="dot safe" style="display:inline-block;vertical-align:-1px"></i> <b>${git.ok}</b> pushed</span><span><i class="dot review" style="display:inline-block;vertical-align:-1px"></i> <b>${git.warn}</b> not pushed</span><span><i class="dot keep" style="display:inline-block;vertical-align:-1px"></i> <b>${git.none}</b> no git</span></div></div></div>
      <div class="toolbar"><div class="seg" id="pf">${["all", "stale", "active", "git"].map((f) => `<button data-f="${f}" class="${st.pf === f ? "on" : ""}">${{ all: "All", stale: "Stale " + st.state.stale_days + "+ days", active: "Active", git: "Git issues" }[f]}</button>`).join("")}</div>
        <button class="btn safe" id="sel-stale">Select rebuildable folders in stale projects</button></div>
      <div class="list" style="overflow-x:auto"><div class="table">
        <div class="trow head"><span></span><span>Project</span><span>Type</span><span>Last touched</span><span>Git</span><span class="r">Total</span><span class="r">Rebuildable</span></div>
        ${shown.map((p) => projectRow(p)).join("") || `<div class="empty">No projects match this filter.</div>`}
      </div></div>
      <div class="muted">Projects are found by .git, package.json, pyproject.toml, *.sln, Cargo.toml, Unity ProjectSettings, *.uproject and build.gradle. "Last touched" is the newest source folder change or the last commit, ignoring build folders. Git status comes from git itself.</div>`;
    $$("#pf button").forEach((b) => b.onclick = () => { st.pf = b.dataset.f; renderProjects(); });
    $("#sel-stale").onclick = () => { let n = 0; ps.forEach((p) => p.rebuildable.forEach((k) => { if (k.safety === "safe") { st.sel.add(k.id); n++; } })); st.cleaned = null; save(); updateCart(); toast(`Selected ${n} folders`); renderProjects(); };
    $$(".trow[data-p]", el).forEach((row) => row.querySelector(".exp").onclick = () => { const b = row.nextElementSibling; b.classList.toggle("hidden"); row.querySelector(".caret").classList.toggle("open"); });
    bindRows(el);
    $$("input.selk", el).forEach((cb) => cb.addEventListener("change", () => toggleSel(cb.dataset.id, cb.checked)));
  }
  function projectRow(p) {
    const gi = p.git_info || {};
    const gitTag = { ok: "Pushed", warn: gi.remotes?.length ? "Not backed up" : "Never pushed", none: "No git", "n/a": "?" }[p.git_state];
    const gitCls = { ok: "safe", warn: "review", none: "keep", "n/a": "unknown" }[p.git_state];
    const gitShort = p.git_state === "none" ? "no .git folder" : gi.summary || "";
    const detail = p.git_state === "none" ? `No .git folder. ${p.types.length ? "This looks like a " + p.types.join("/") + " project: your source exists only on this PC." : "No project markers found; may be data rather than code."}` : `Branch ${esc(gi.branch || "?")} · ${gi.uncommitted ?? 0} uncommitted files · ${gi.remotes?.length ? (gi.unpushed ?? "?") + " commits not pushed to " + gi.remotes.join(", ") : "no remote: all commits exist only on this PC"}${gi.last_commit ? " · last commit " + gi.last_commit.slice(0, 10) : ""}`;
    const next = { ok: "Nothing. Source is safe on the remote.", warn: gi.remotes?.length ? "Commit and push." : "Add a remote (e.g. GitHub) and push.", none: "git init, commit, push.", "n/a": "" }[p.git_state];
    const rebuild = p.rebuildable.reduce((a, k) => a + k.allocated, 0);
    return `<div class="trow" data-p="${p.node_id}"><button class="exp" aria-label="Expand ${esc(p.name)}"><span class="caret">▶</span>
        <span style="min-width:0"><b>${esc(p.name)}</b><div class="path" title="${esc(p.path)}">${esc(p.path)}</div></span>
        <span class="muted">${esc(p.types.join(", ") || "—")}</span>
        <span>${p.last_touched ? p.last_touched.slice(0, 10) : "?"}${p.stale_days !== null ? ` <span class="muted">(${p.stale_days}d)</span>` : ""}<br><span class="pill ${p.stale ? "safe" : ""}" style="${p.stale ? "" : "background:#2E3542;color:#C9D1DC"}">${p.stale ? "Stale" : "Active"}</span></span>
        <span style="min-width:0"><span class="pill ${gitCls}">${gitTag}</span><div class="muted" style="font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(gitShort)}</div></span>
        <span class="r">${fmt(p.total_bytes)}</span><span class="r"><b>${p.rebuildable.length ? fmt(rebuild) : "–"}</b></span></button></div>
      <div class="pbody hidden">
        <div class="gitbox ${gitCls === "unknown" ? "na" : p.git_state}"><div><b>Git</b> <span class="pill ${gitCls}">${gitTag}</span> <span class="tag">${p.git_state === "none" ? (p.types.length ? "Rule: project markers found" : "No markers") : "From git status"}</span></div><div>${detail}</div>${next ? `<div>Next: ${next}</div>` : ""}</div>
        ${p.rebuildable.length ? p.rebuildable.map((k) => `<div class="kid"><input type="checkbox" class="selk" data-id="${k.id}" ${st.sel.has(k.id) ? "checked" : ""}><div class="body"><div><span class="mono">${esc(k.path.slice(p.path.length + 1))}</span> <span class="pill ${k.safety}">${SAF[k.safety]}</span></div><div class="why">${esc(k.reason)}</div></div><div class="size">${fmt(k.allocated)}</div></div>`).join("") : `<div class="muted">No rebuildable folders found. Nothing to clean here.</div>`}
      </div>`;
  }

  // ---------------------------------------------------------------- categories
  async function renderCategories() {
    const el = $("#view-categories");
    const cats = await api("/api/categories?" + q({ drive: drv() }));
    const names = st.state.categories || {};
    if (!st.cat || !cats.find((c) => c.category === st.cat)) st.cat = cats[0]?.category;
    const items = st.cat ? await api("/api/items?" + q({ category: st.cat, drive: drv(), min_bytes: st.minBytes, limit: 300 })) : [];
    el.innerHTML = `<div class="tiles">${cats.map((c) => { const w = (n) => (c.total ? 100 * n / c.total : 0).toFixed(1) + "%"; return `<button class="tile ${c.category === st.cat ? "on" : ""}" data-c="${c.category}"><span class="t"><b>${esc(names[c.category]?.name || c.category)}</b><b>${fmt(c.total)}</b></span><small>${esc(names[c.category]?.desc || "")}</small><span class="bar"><span style="width:${w(c.safe)};background:var(--safe)"></span><span style="width:${w(c.review)};background:var(--review)"></span><span style="width:${w(c.keep)};background:var(--keep)"></span><span style="width:${w(c.unknown)};background:var(--unknown)"></span></span><span class="sl">${c.safe ? fmt(c.safe) + " safe to free" : c.total ? "Nothing auto-safe" : "Empty"}</span></button>`; }).join("")}</div>
      <div class="list"><div class="hd"><h2>${esc(names[st.cat]?.name || st.cat || "")}</h2><div class="actions"><input class="search" id="cat-q" placeholder="filter by path…"><button class="btn small" id="cat-sel-safe">Select all safe</button><small>${items.length} items · ${fmt(items.reduce((a, x) => a + x.excl_bytes, 0))}</small></div></div><div id="cat-items">${items.map((it) => itemRow(it)).join("") || `<div class="empty">Nothing in this category on the selected drive above your size limit.</div>`}</div></div>`;
    $$(".tile", el).forEach((t) => t.onclick = () => { st.cat = t.dataset.c; renderCategories(); });
    $("#cat-sel-safe").onclick = () => { let n = 0; items.forEach((it) => { if (it.safety === "safe") { st.sel.add(it.id); n++; } }); st.cleaned = null; save(); updateCart(); renderCategories(); toast(`Selected ${n} items`); };
    $("#cat-q").oninput = (e) => { const v = e.target.value.toLowerCase(); $("#cat-items").innerHTML = items.filter((it) => it.path.toLowerCase().includes(v)).map((it) => itemRow(it)).join(""); bindRows($("#cat-items")); };
    bindRows(el);
  }

  // ---------------------------------------------------------------- explore (treemap)
  function squarify(items, x, y, w, h) {
    // Squarified treemap (Bruls, Huizing, van Wijk). items sorted by value desc; returns rects with x,y,w,h.
    const total = items.reduce((a, i) => a + i.value, 0) || 1;
    const scale = (w * h) / total;
    let rest = items.map((i) => ({ ...i, area: Math.max(i.value * scale, 0) }));
    const out = [];
    let rx = x, ry = y, rw = w, rh = h;
    const worst = (row, s) => { const sum = row.reduce((a, i) => a + i.area, 0); if (!sum) return Infinity; const mx = Math.max(...row.map((i) => i.area)), mn = Math.min(...row.map((i) => i.area)); return Math.max((s * s * mx) / (sum * sum), (sum * sum) / (s * s * mn)); };
    while (rest.length) {
      const side = Math.min(rw, rh);
      let row = [rest[0]], i = 1;
      while (i < rest.length && worst([...row, rest[i]], side) <= worst(row, side)) { row.push(rest[i]); i++; }
      rest = rest.slice(i);
      const sum = row.reduce((a, it) => a + it.area, 0);
      if (rw >= rh) { // vertical strip on the left
        const strip = sum / rh; let off = 0;
        for (const it of row) { const len = it.area / strip; out.push({ ...it, x: rx, y: ry + off, w: strip, h: len }); off += len; }
        rx += strip; rw -= strip;
      } else { // horizontal strip on top
        const strip = sum / rw; let off = 0;
        for (const it of row) { const len = it.area / strip; out.push({ ...it, x: rx + off, y: ry, w: len, h: strip }); off += len; }
        ry += strip; rh -= strip;
      }
      if (rw <= 0 || rh <= 0) break;
    }
    return out;
  }
  async function renderExplore() {
    const el = $("#view-explore");
    const ex = st.explore;
    const t = await api("/api/tree?" + q({ node_id: ex.nodeId, drive: ex.nodeId ? null : (st.drive || st.state.drives[0]?.letter) }));
    const kids = t.children.filter((k) => k.allocated > 0).map((k) => ({ ...k, value: k.allocated }));
    if (t.rest.count) kids.push({ id: null, name: `${t.rest.count} smaller items`, allocated: t.rest.allocated, value: t.rest.allocated, safety: "rest", is_dir: 0 });
    el.innerHTML = `<div class="crumbs">${t.crumbs.map((c, i) => `<a data-id="${c.id}">${esc(c.name)}</a>${i < t.crumbs.length - 1 ? " ›" : ""}`).join(" ")} <span class="muted">· ${fmt(t.node.allocated)}</span>
        <span style="margin-left:auto" class="legend"><span><i style="background:var(--safe-fill);border:1px solid var(--safe)"></i>Safe</span><span><i style="background:var(--review-fill);border:1px solid var(--review)"></i>Review</span><span><i style="background:var(--keep-fill);border:1px solid var(--keep)"></i>Keep</span><span><i style="background:var(--unknown-fill);border:1px solid var(--unknown)"></i>Unknown</span><span><i style="background:var(--none-fill);border:1px solid #353B46"></i>Look inside</span></span></div>
      <div class="map-wrap"><div class="map" id="map"></div><aside class="aside" id="map-aside"><div class="muted">Click a block for details. Double-click a folder to open it.</div></aside></div>`;
    $$(".crumbs a", el).forEach((a) => a.onclick = () => { ex.nodeId = Number(a.dataset.id); ex.detail = null; renderExplore(); });
    const map = $("#map"); const W = map.clientWidth, H = map.clientHeight;
    const rects = squarify(kids.sort((a, b) => b.value - a.value), 0, 0, W, H);
    map.innerHTML = rects.map((r) => `<button class="blk ${r.safety === "none" ? (r.is_dir ? "none" : "none") : r.safety} ${r.id === ex.detail ? "sel" : ""}" data-id="${r.id ?? ""}" data-dir="${r.is_dir ? 1 : 0}" style="left:${r.x + 1}px;top:${r.y + 1}px;width:${Math.max(r.w - 2, 1)}px;height:${Math.max(r.h - 2, 1)}px" title="${esc(r.name)} · ${fmt(r.allocated)}">${r.w > 60 && r.h > 28 ? `<b>${esc(r.name)}</b><small>${fmt(r.allocated)}</small>` : ""}</button>`).join("");
    $$(".blk", map).forEach((b) => {
      b.onclick = () => { if (!b.dataset.id) return; ex.detail = Number(b.dataset.id); $$(".blk", map).forEach((x) => x.classList.toggle("sel", x === b)); showAside(ex.detail); };
      b.ondblclick = () => { if (b.dataset.id && b.dataset.dir === "1") { ex.nodeId = Number(b.dataset.id); ex.detail = null; renderExplore(); } };
    });
    if (ex.detail) showAside(ex.detail);
  }
  async function showAside(id) {
    const d = await api("/api/node/" + id);
    const c = d.own || d.classification || {};
    const a = $("#map-aside");
    a.innerHTML = `<div class="k">SELECTED</div><div><b style="font-size:17px">${esc(d.name)}</b><div class="path">${esc(d.path)}</div></div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b style="font-size:22px">${fmt(d.allocated)}</b> ${c.safety ? `<span class="pill ${c.safety}">${SAF[c.safety]}</span>` : `<span class="tag">Look inside</span>`}${d.own ? "" : c.safety ? '<span class="tag">inherited from parent</span>' : ""}</div>
      ${d.is_dir ? `<div class="muted">${(d.files || 0).toLocaleString()} files · ${(d.folders || 0).toLocaleString()} folders · modified ${(d.mtime || "").slice(0, 10)}</div>` : `<div class="muted">file · modified ${(d.mtime || "").slice(0, 10)}</div>`}
      ${c.category ? `<div><div class="k">Category</div><div class="v">${esc(st.state.categories?.[c.category]?.name || c.category)}</div></div>` : ""}
      ${c.reason ? `<div><div class="k">Why</div><div class="v">${esc(c.reason)}</div></div>` : ""}
      ${c.action ? `<div><div class="k">Recommended action</div><div class="v">${esc(c.action)}</div></div>` : ""}
      ${d.own ? `<div><div class="k">Decided by</div><div class="v">${srcTag(d.own)}</div></div>` : ""}
      <div class="actions" style="margin-top:auto">${d.protected ? `<span class="muted">🔒 ${esc(d.protected)}</span>` : `<button class="btn safe small" id="as-sel">${st.sel.has(d.id) ? "Remove from cleanup" : "Add to cleanup"}</button>`}${d.is_dir ? `<button class="btn small" id="as-open">Open folder</button>` : ""}<button class="btn small" id="as-more">Details</button></div>`;
    const s = $("#as-sel"); if (s) s.onclick = () => { toggleSel(d.id); showAside(id); };
    const o = $("#as-open"); if (o) o.onclick = () => { st.explore.nodeId = d.id; st.explore.detail = null; renderExplore(); };
    $("#as-more").onclick = () => openDetail(d.id);
  }

  // ---------------------------------------------------------------- tidy up (small items grouped)
  async function renderTidy() {
    const el = $("#view-tidy");
    const names = st.state.categories || {};
    const all = await api("/api/items?" + q({ safety: "safe", drive: drv(), limit: 5000, order: "excl_bytes" }));
    const small = all.filter((it) => it.excl_bytes < st.minBytes);
    const groups = {};
    small.forEach((it) => { (groups[it.category] ||= []).push(it); });
    const gs = Object.entries(groups).sort((a, b) => b[1].reduce((x, y) => x + y.excl_bytes, 0) - a[1].reduce((x, y) => x + y.excl_bytes, 0));
    el.innerHTML = `<div class="card"><h2>Safe items under ${fmt(st.minBytes) || "the limit"}</h2><div class="muted">${small.length} items · ${fmt(small.reduce((a, x) => a + x.excl_bytes, 0))} in total. Individually too small to list on the Overview, together worth cleaning. ${st.minBytes ? "" : "Set a size limit above to see grouping."}</div></div>
      ${gs.map(([cat, its]) => `<div class="list"><div class="hd"><h2>${esc(names[cat]?.name || cat)}</h2><div class="actions"><small>${its.length} items · ${fmt(its.reduce((a, x) => a + x.excl_bytes, 0))}</small><button class="btn small safe" data-cat="${cat}">Select all</button><button class="btn small" data-exp="${cat}">Show list</button></div></div><div class="hidden" id="tidy-${cat}">${its.map((it) => itemRow(it)).join("")}</div></div>`).join("") || `<div class="empty">No small safe items. Lower the limit or rescan.</div>`}`;
    $$("[data-cat]", el).forEach((b) => b.onclick = () => { groups[b.dataset.cat].forEach((it) => st.sel.add(it.id)); st.cleaned = null; save(); updateCart(); toast(`Selected ${groups[b.dataset.cat].length} items`); renderTidy(); });
    $$("[data-exp]", el).forEach((b) => b.onclick = () => $("#tidy-" + b.dataset.exp).classList.toggle("hidden"));
    bindRows(el);
  }

  // ---------------------------------------------------------------- cleanup
  async function renderCleanup() {
    const el = $("#view-cleanup");
    const ids = [...st.sel];
    const plan = ids.length ? await api("/api/cleanup/plan", { node_ids: ids }) : [];
    const ok = plan.filter((a) => !a.refused);
    const total = ok.reduce((a, x) => a + x.bytes, 0);
    const byM = (m) => ok.filter((a) => a.method === m).length;
    const log = await api("/api/log?limit=30");
    el.innerHTML = `${st.cleaned ? `<div class="gitbox ok" role="status">${esc(st.cleaned)}</div>` : ""}
      <div class="cols"><div class="w list"><div class="hd"><h2>Selected for cleanup</h2><div class="actions"><small>${plan.length} items</small><button class="btn small" id="clear-sel">Clear</button></div></div>
        ${plan.map((a) => `<div class="row"><div class="body"><div class="name">${esc(a.path.split("\\").pop() || a.path)} <span class="pill ${a.safety}">${SAF[a.safety] || a.safety}</span>${a.admin ? '<span class="tag admin">needs admin</span>' : ""}</div><div class="path">${esc(a.path)}</div><div class="how">${a.refused ? `<span class="failed">Refused: ${esc(a.refused)}</span>` : { recycle: "Moves to the Recycle Bin", command: "Runs: " + esc(a.command), manual: "Manual step: " + esc(a.command || a.reason), none: "Nothing to do" }[a.method]}</div></div><div class="size">${fmt(a.bytes)}</div><button class="btn small" data-rm="${a.node_id}" aria-label="Remove">✕</button></div>`).join("") || `<div class="empty">Nothing selected yet. Tick items in Overview, Projects, Categories or Tidy up.</div>`}</div>
      <div class="n"><div class="card"><small class="muted">You will free</small><div class="cart-total">${fmt(total)}</div>
          <div class="muted">${byM("recycle")} to the Recycle Bin (undo possible)<br>${byM("command")} through a built-in cleanup command<br>${byM("manual")} done by you in another app<br>${ok.filter((a) => a.admin).length} need admin: one Windows prompt</div>
          <label class="muted" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="dry" checked> Dry run first (show exactly what will happen)</label>
          <button class="btn safe" id="run" ${ok.length ? "" : "disabled"}>Clean ${ok.length} items</button><div id="run-out"></div></div>
        <div class="card"><h2>History</h2>${log.map((l) => `<div class="muted"><span class="${l.result}">${l.result}</span> ${l.ts.replace("T", " ").slice(0, 16)} · ${fmt(l.bytes)} · ${esc(l.path)}${l.detail ? ` <span style="color:#6c7480">— ${esc(l.detail).slice(0, 120)}</span>` : ""}</div>`).join("") || `<div class="muted">No cleanups yet.</div>`}</div></div></div>`;
    $("#clear-sel").onclick = () => { st.sel.clear(); save(); updateCart(); renderCleanup(); };
    $$("[data-rm]", el).forEach((b) => b.onclick = () => { toggleSel(b.dataset.rm, false); renderCleanup(); });
    $("#run").onclick = async () => {
      const dry = $("#dry").checked;
      if (!dry && !confirm(`Clean ${ok.length} items (${fmt(total)})? Files go to the Recycle Bin; commands run as listed.`)) return;
      $("#run").disabled = true; $("#run-out").innerHTML = `<div class="muted">Working…</div>`;
      try {
        const r = await api("/api/cleanup/run", { node_ids: ids, dry_run: dry });
        const lines = r.actions.map((a) => `<div class="muted"><span class="${a.result === "dry-run" ? "" : a.result}">${a.result}</span> ${esc(a.path)} — ${esc(a.detail || "")}</div>`).join("");
        if (dry) { $("#run-out").innerHTML = `<div class="gitbox na">Dry run: nothing was changed.</div>${lines}`; $("#run").disabled = false; }
        else { st.cleaned = `Cleaned ${r.actions.filter((a) => a.result === "ok").length} items and freed ${fmt(r.freed)}. ${r.actions.filter((a) => a.result === "failed").length ? "Some failed: see below." : "Rescan to update the numbers."}`; r.actions.forEach((a) => { if (a.result === "ok") st.sel.delete(a.node_id); }); save(); updateCart(); await renderCleanup(); $("#run-out").innerHTML = lines; }
      } catch (e) { toast(e.message, true); $("#run").disabled = false; }
    };
  }

  // ---------------------------------------------------------------- detail drawer
  async function openDetail(id) {
    const d = await api("/api/node/" + id);
    const c = d.own || d.classification || {};
    const ai = d.ai;
    const dr = $("#detail"); dr.classList.remove("hidden");
    $("#detail-inner").innerHTML = `<div style="display:flex;justify-content:space-between;gap:10px"><h2>${esc(d.name)}</h2><button class="btn small" id="dt-close">Close</button></div>
      <div class="path">${esc(d.path)}</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b style="font-size:22px">${fmt(d.allocated)}</b>${c.safety ? `<span class="pill ${c.safety}">${SAF[c.safety]}</span>` : ""}${d.own ? srcTag(d.own) : ""}${d.own?.admin ? '<span class="tag admin">needs admin</span>' : ""}</div>
      <div class="muted">${d.is_dir ? `${(d.files || 0).toLocaleString()} files · ${(d.folders || 0).toLocaleString()} folders · ` : ""}modified ${(d.mtime || "?").slice(0, 10)}${c.category ? " · " + esc(st.state.categories?.[c.category]?.name || c.category) : ""}</div>
      ${c.reason ? `<div><div class="k muted">Why</div><div>${esc(c.reason)}</div></div>` : ""}${c.action ? `<div><div class="k muted">Recommended action</div><div>${esc(c.action)}</div></div>` : ""}
      ${d.protected ? `<div class="gitbox none">🔒 Protected: ${esc(d.protected)}</div>` : ""}
      ${ai?.suggested_rule ? `<div><div class="k muted">AI suggested rule (saved to rules/user.yaml if you accept; auto-delete is never enabled by an AI rule)</div><pre class="rule">${esc(JSON.stringify(ai.suggested_rule, null, 1))}</pre><button class="btn small ai" id="dt-rule">Accept rule</button></div>` : ""}
      ${d.nested.length ? `<div><div class="k muted">Already classified inside</div>${d.nested.map((n) => `<div class="row compact"><span class="dot ${n.safety}"></span><div class="body"><div class="path">${esc(n.path.slice(d.path.length + 1))}</div></div><div class="size">${fmt(n.excl_bytes)}</div></div>`).join("")}</div>` : ""}
      <div class="actions">${d.protected ? "" : `<button class="btn safe small" id="dt-sel">${st.sel.has(d.id) ? "Remove from cleanup" : "Add to cleanup"}</button>`}${st.state.ai_available && d.own ? `<button class="btn ai small" id="dt-ai">Ask AI about this</button>` : ""}<button class="btn small" id="dt-exp">Show in Explorer</button><button class="btn small" id="dt-map">Show on map</button></div>`;
    $("#dt-close").onclick = () => dr.classList.add("hidden");
    const s = $("#dt-sel"); if (s) s.onclick = () => { toggleSel(d.id); openDetail(id); render(); };
    $("#dt-exp").onclick = () => api("/api/open", { path: d.path }).catch((e) => toast(e.message, true));
    $("#dt-map").onclick = () => { st.explore.nodeId = d.parent_id; st.explore.detail = d.id; dr.classList.add("hidden"); setView("explore"); };
    const r = $("#dt-rule"); if (r) r.onclick = async () => { try { await api("/api/rules/accept", { rule: ai.suggested_rule }); toast("Rule saved to rules/user.yaml. Re-classify to apply."); } catch (e) { toast(e.message, true); } };
    const ab = $("#dt-ai"); if (ab) ab.onclick = async () => { if (!st.state.ai_enabled) await setAi(true); dr.classList.add("hidden"); runAi({ node_ids: [d.id] }); };
  }
  $("#detail").addEventListener("click", (e) => { if (e.target === $("#detail")) $("#detail").classList.add("hidden"); });

  // ---------------------------------------------------------------- jobs (scan / ai)
  async function runAi(body) { try { await api("/api/ai/run", body); watchJob("Asking Claude Code…"); } catch (e) { toast(e.message, true); } }
  async function setAi(on) { const r = await api("/api/settings", { ai_enabled: on }); st.state.ai_enabled = r.ai_enabled; await loadState(); }
  function watchJob(title) {
    const box = $("#job"); box.classList.remove("hidden"); $("#job-title").textContent = title;
    clearInterval(st.jobTimer);
    st.jobTimer = setInterval(async () => {
      const j = await api("/api/jobs");
      const secs = j.started ? Math.max(0, Math.round((Date.now() - new Date(j.started)) / 1000)) : 0;
      $("#job-title").textContent = `${title} ${j.running ? `· running ${Math.floor(secs / 60)}m ${secs % 60}s` : "· finished"}`;
      $("#job-log").textContent = j.log.slice(-12).join("\n");
      if (!j.running) { clearInterval(st.jobTimer); setTimeout(() => box.classList.add("hidden"), 6000); if (j.error) toast(j.error, true); else toast(title.replace("…", "") + " finished"); await loadState(); render(); }
    }, 1000);
  }
  $("#rescan").onclick = async () => { try { await api("/api/scan", { admin: true }); watchJob("Scanning drives (approve the admin prompt)…"); } catch (e) { toast(e.message, true); } };
  $("#ai-toggle").onclick = async () => { if (!st.state.ai_available) { toast("Claude Code CLI not found on PATH. Install it to use the AI helper.", true); return; } await setAi(!st.state.ai_enabled); render(); };
  $("#min-bytes").value = String(st.minBytes);
  $("#min-bytes").onchange = (e) => { st.minBytes = Number(e.target.value); save(); render(); };
  $$(".nav").forEach((b) => b.onclick = () => setView(b.dataset.view));

  loadState().then(async () => { const j = await api("/api/jobs"); if (j.running) watchJob(j.kind + "…"); setView(st.view); }).catch((e) => toast(e.message, true));
})();
