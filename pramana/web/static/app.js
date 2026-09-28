const PRETTY = { nvidia: "NVIDIA", openrouter: "OpenRouter", deepseek: "DeepSeek", openai: "OpenAI", anthropic: "Anthropic",
                 gemini: "Gemini", groq: "Groq", dashscope: "Qwen (DashScope)", moonshot: "Moonshot", "openai-compatible": "your endpoint" };
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTok = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n || 0));
const fmtTime = (s) => (s < 60 ? Math.round(s) + "s" : Math.floor(s / 60) + "m " + String(Math.round(s % 60)).padStart(2, "0") + "s");
const api = async (path, body) => {
  const r = await fetch(path, body ? { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) } : {});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};

/* ---------------- theme ---------------- */
function toggleTheme() {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("pramana-theme", t); } catch (e) {}
}
try { const t = localStorage.getItem("pramana-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}

/* ---------------- views ---------------- */
let pickState = null, batchTimer = null, lastBatch = null;
function go(view, id) {
  ["home", "run", "pick", "batch"].forEach((v) => ($("#" + v).hidden = v !== view));
  if (batchTimer && view !== "batch") { clearInterval(batchTimer); batchTimer = null; }
  if (view === "batch") { history.replaceState(null, "", "#batch=" + id); openBatch(id); syncOffice(); return; }
  if (view === "home") { history.replaceState(null, "", "/"); stopStream(); loadRecent(); $("#prompt").focus(); }
  if (view === "run") { history.replaceState(null, "", "#run=" + id); openRun(id); }
  syncOffice();
}
/* the live office: loaded only in the view you are looking at (each copy follows every run) */
function syncOffice() {
  document.querySelectorAll("iframe.live-office").forEach((f) => {
    const view = f.closest(".view");
    const on = view && !view.hidden;
    if (on && !f.getAttribute("src")) f.setAttribute("src", f.dataset.src);
    if (!on && f.getAttribute("src")) f.removeAttribute("src");
  });
}

/* ---------------- model pill + settings ---------------- */
let modelInfo = null, mode = "env";
async function loadModel() {
  try {
    modelInfo = await api("/api/model");
    const pill = $("#model-pill");
    pill.classList.toggle("ok", !!modelInfo.ok);
    pill.classList.toggle("bad", !modelInfo.ok);
    const host = (modelInfo.base_url || "").replace(/^https?:\/\//, "").split("/")[0];
    const who = PRETTY[modelInfo.provider] || modelInfo.provider;
    $("#model-text").textContent = modelInfo.ok ? `${who} API · ${modelInfo.model}` : "No API key — click to set one";
    $("#model-pill").title = modelInfo.ok ? `endpoint: ${modelInfo.base_url}\nmodel: ${modelInfo.model}\nkey: ${modelInfo.key_source}` : "";
    const line = $("#api-line");
    if (line) line.innerHTML = modelInfo.ok ? `Using the <b>${esc(who)}</b> API (${esc(host || "local")}) · model <b>${esc(modelInfo.model)}</b> · key from <b>${esc(modelInfo.key_source)}</b>` : "";
  } catch (e) { $("#model-text").textContent = "model: unknown"; }
}
function openSettings() {
  $("#drawer").hidden = false;
  const app = modelInfo && modelInfo.app;
  // the Mac app has no shell to export AI_API_KEY from: pasting the key is its normal path
  setMode(modelInfo && modelInfo.provider === "claude-cli" ? "claude"
    : (!modelInfo || !modelInfo.ok || (app && modelInfo.key_source !== "AI_API_KEY")) ? "api" : "env");
  $("#drawer-note").innerHTML = app
    ? "Paste your API key once. Pramana keeps it on this Mac only (your Library folder, readable only by you) and sends it only to the model provider. A key exported as <code>AI_API_KEY</code> still takes priority."
    : "Pramana works with any provider. The key is only ever read from <code>AI_API_KEY</code>, or typed here for this session (kept in memory, never saved).";
  $("#forget-key").hidden = !(modelInfo && modelInfo.saved);
  $("#api-key").placeholder = modelInfo && modelInfo.saved ? "saved — paste a new key to replace it"
    : "paste a key (DeepSeek, OpenRouter, NVIDIA, OpenAI, Anthropic, …)";
  $("#model-test").textContent = modelInfo && modelInfo.ok ? `Now: ${modelInfo.model} via ${modelInfo.provider} (key: ${modelInfo.key_source})` : "No API key yet.";
}
function closeSettings() { $("#drawer").hidden = true; }
function setMode(m) {
  mode = m;
  document.querySelectorAll("#mode-seg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
  $("#mode-claude").hidden = m !== "claude";
  $("#mode-api").hidden = m !== "api";
}
function modelBody() {
  if (mode === "claude") return { mode: "claude", model: $("#claude-model").value };
  if (mode === "api") return { mode: "api", key: $("#api-key").value.trim(), model: $("#api-model").value.trim(), base_url: $("#api-base").value.trim() };
  return { mode: "env" };
}
async function saveModel() {
  await api("/api/model", modelBody());
  await loadModel(); loadModelPicker();
  $("#model-test").textContent = modelInfo.ok ? `✓ Using ${modelInfo.model} via ${modelInfo.provider}` : "Not configured yet.";
  if (modelInfo.ok) setTimeout(closeSettings, 700);
}
async function testModel() {
  $("#model-test").textContent = "Testing…";
  await api("/api/model", modelBody());
  const r = await api("/api/model/test", {});
  await loadModel();
  $("#model-test").innerHTML = r.ok ? `<span style="color:var(--ok)">✓ Connected in ${r.seconds}s</span>` : `<span style="color:var(--bad)">✗ ${esc(r.error)}</span>`;
}
$("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") closeSettings(); });
$("#gh-dlg").addEventListener("close", () => { if (walkQueue && walkQueue.length) setTimeout(nextWalk, 300); });

/* ---------------- model picker (composer) ---------------- */
async function loadModelPicker() {
  const sel = $("#model-select");
  let env = { models: [] };
  try { env = await api("/api/models"); } catch (e) {}
  const cur = modelInfo || {};
  let html = "";
  if (env.models && env.models.length) {
    html += `<optgroup label="${esc(PRETTY[env.provider] || env.provider)} — your key">` +
      env.models.map((m) => `<option value="env:${esc(m.id)}">${m.recommended ? "★ " : ""}${esc(m.id)}</option>`).join("") + "</optgroup>";
  }
  if (!html) html = `<option value="">${modelInfo && modelInfo.app ? "No API key yet — click the model button to paste one" : "No API key — set AI_API_KEY or click the model button"}</option>`;
  sel.innerHTML = html;
  const want = `env:${cur.model}`;
  if ([...sel.options].some((o) => o.value === want)) sel.value = want;
  else if (cur.model) { const o = new Option(`${cur.model} (${cur.provider})`, want); sel.add(o, 0); sel.value = want; }
}
async function pickModel(v) {
  if (!v) { openSettings(); return; }
  const id = v.slice(v.indexOf(":") + 1);
  await api("/api/model", { mode: "env", model: id });
  await loadModel();
  $("#understood").innerHTML = `Model set to <b>${esc(id)}</b> — checking it answers…`;
  const r = await api("/api/model/test", {});
  $("#understood").innerHTML = r.ok ? `✓ <b>${esc(id)}</b> answered in ${r.seconds}s` : `<span style="color:var(--bad)">✗ ${esc(id)} did not answer: ${esc(String(r.error).slice(0, 140))}</span>`;
}

/* ---------------- composer ---------------- */
const ISSUE_RE = /github\.com\/([\w.-]+)\/([\w.-]+)\/(issues|pull)\/(\d+)/;
const REPO_RE = /github\.com[/:]([\w.-]+)\/([\w.-]+?)(\.git)?(?=[/\s#?]|$)/;
function understood() {
  const p = $("#prompt").value, r = $("#repo").value.trim();
  let repo = r, how = "";
  if (!repo) {
    const m = p.match(ISSUE_RE) || p.match(REPO_RE);
    if (m) { repo = `${m[1]}/${m[2]}`; how = " (from your link)"; }
  }
  const issue = p.match(ISSUE_RE);
  let html = "";
  if (repo) html += `Repository: <b>${esc(repo)}</b>${how}`;
  if (issue) html += `${html ? " · " : ""}Issue <b>#${issue[4]}</b> will be read from GitHub`;
  $("#understood").innerHTML = html || "Plain words are fine — no issue link needed.";
}
$("#prompt").addEventListener("input", understood);
$("#repo").addEventListener("input", understood);
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !$("#home").hidden) startRun();
  if (e.key === "Escape") closeSettings();
});
async function startRun() {
  $("#form-error").textContent = "";
  const btn = $("#go-btn");
  btn.disabled = true;
  try {
    const r = await api("/api/runs", { prompt: $("#prompt").value, repo: $("#repo").value, test: $("#test").value, want_pr: $("#want-pr").checked, auto_pr: $("#auto-pr").checked });
    if (r.select_issues) { showPicker(r); return; }
    go("run", r.id);
  } catch (e) {
    $("#form-error").textContent = e.message;
  } finally { btn.disabled = false; }
}
async function loadDemos() {
  try {
    const demos = await api("/api/demos");
    $("#demo-chips").innerHTML = demos.map((d) => `<button class="chipbtn" onclick="startDemo('${esc(d.id)}')">${esc(d.id)}<span class="lang">${esc(d.language)}</span></button>`).join(" ");
    if (!demos.length) $(".demos").hidden = true;
  } catch (e) { $(".demos").hidden = true; }
}
async function startDemo(id) {
  try { const r = await api("/api/demo", { id }); go("run", r.id); } catch (e) { $("#form-error").textContent = e.message; }
}
async function loadRecent() {
  try {
    const runs = await api("/api/runs");
    $("#recent-wrap").hidden = !runs.length;
    $("#recent").innerHTML = runs.map((r) => {
      const st = pillOf(r.verdict || r.status);
      const when = new Date(r.created * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
      const mdl = r.model && r.model.model ? ` · ${esc(r.model.model)}` : "";
      const gh = (r.github || []).filter((g) => g.url).map((g) => ` · <a href="${esc(g.url)}" target="_blank" onclick="event.stopPropagation()">${g.kind === "pr" ? "PR" : "issue"}</a>`).join("");
      return `<div class="card recent-item" onclick="go('run','${r.id}')"><div><b>${esc(r.title)}</b><div class="muted small">${esc(r.repo)} · ${when}${mdl}${gh}</div></div>
        <span class="status-pill ${st.cls}">${st.label}</span></div>`;
    }).join("");
  } catch (e) {}
}

/* ---------------- run view ---------------- */
const STEPS = [["setup", "Setup"], ["intake", "Understand"], ["localize", "Locate"], ["reproduce", "Reproduce"], ["fix", "Fix"], ["verify", "Prove"], ["review", "Review"], ["done", "Done"]];
const ICON = { bash: "⌨", str_replace_editor: "✎", search: "⌕", find_definition: "ƒ", find_files: "▤", compare: "⇄", submit: "⚖" };
let es = null, cur = null, timer = null;
function stopStream() { if (es) { es.close(); es = null; } if (timer) { clearInterval(timer); timer = null; } }
function pillOf(s) {
  if (s === "verified") return { cls: "verified", label: "Verified fix" };
  if (s === "patched") return { cls: "patched", label: "Patched · unproven" };
  if (s === "no_patch" || s === "error") return { cls: "failed", label: s === "error" ? "Error" : "No fix" };
  if (s === "cancelled" || s === "stopped") return { cls: "failed", label: "Stopped" };
  return { cls: "running", label: "Working" };
}
function setStatus(s, label) {
  const p = pillOf(s);
  const el = $("#run-status");
  el.className = "status-pill " + p.cls;
  el.innerHTML = (p.cls === "running" ? '<span class="spin"></span>' : "") + esc(label || p.label);
}
function stepTo(name) {
  const idx = STEPS.findIndex((s) => s[0] === name);
  if (idx < 0) return;
  cur.step = Math.max(cur.step, idx);
  document.querySelectorAll("#stepper li").forEach((li, i) => {
    li.className = i < cur.step ? "done" : i === cur.step ? (name === "done" ? "done" : "active") : "";
  });
}
function line(icon, html, cls = "", t) {
  const feed = $("#feed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  const div = document.createElement("div");
  div.className = "ev-line " + cls;
  div.innerHTML = `<span class="ic">${icon}</span><span class="tx">${html}</span><span class="t">${t != null ? fmtTime(t) : ""}</span>`;
  feed.appendChild(div);
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}
function panel(id, html) { const el = $("#" + id); el.hidden = false; el.querySelector(".ev-body").innerHTML = html; }
function checksHtml(checks) {
  return (checks || []).map((c) => `<div class="check"><code>${esc((c.command || "").slice(0, 120))}</code><span class="badge ${esc(c.verdict)}">${esc((c.verdict || "").replace("_", " "))}</span></div>`).join("") || '<span class="muted">no checks</span>';
}
async function openRun(id) {
  stopStream();
  cur = { id, step: 0, calls: 0, tokens: 0, start: Date.now(), done: false };
  $("#stop-btn").hidden = false; $("#stop-btn").disabled = false; $("#wait-line").hidden = true;
  $("#feed").innerHTML = ""; $("#verdict").hidden = true; $("#diff-card").hidden = true;
  document.querySelectorAll(".ev").forEach((e) => (e.hidden = true));
  $("#stepper").innerHTML = STEPS.map((s) => `<li>${s[1]}</li>`).join("");
  ["#st-calls", "#st-tokens"].forEach((s) => ($(s).textContent = "0")); $("#st-steps").textContent = "–";
  const info = await api("/api/runs/" + id);
  cur.replay = info.status === "done" || info.status === "error" || info.status === "interrupted";
  cur.batchId = (info.batch_id || lastBatch) && info.title.startsWith("#") ? (info.batch_id || lastBatch) : null;
  $("#run-back").textContent = cur.batchId ? "← Back to the issues" : "← New task";
  $("#run-title").textContent = info.title;
  $("#run-repo").textContent = info.repo;
  $("#run-want-pr").checked = !!info.want_pr;
  cur.wantPr = !!info.want_pr;
  renderGhLinks(info.github || []);
  const rm = info.model && info.model.model ? info.model : modelInfo;   // the model this run used, not today's setting
  $("#run-model").textContent = rm ? `${PRETTY[rm.provider] || rm.provider} API · ${rm.model}` : "";
  cur.start = info.created * 1000;
  setStatus("running", "Starting");
  stepTo("setup");
  timer = setInterval(() => { if (!cur.done) $("#st-time").textContent = fmtTime((Date.now() - cur.start) / 1000); }, 1000);
  es = new EventSource(`/api/runs/${id}/events?since=0`);
  es.onmessage = (m) => handle(JSON.parse(m.data));
  es.onerror = () => { if (cur.done) stopStream(); };
  if (info.status === "interrupted") setStatus("error", "Interrupted (the app closed mid-run)");
}
function handle(e) {
  const k = e.kind, t = e.t;
  if (k !== "wait" && (k === "llm" || k === "tool_call" || k === "done")) $("#wait-line").hidden = true;
  switch (k) {
    case "wait":
      $("#wait-line").hidden = false;
      $("#wait-line").textContent = `⏳ Waiting for ${e.model}: ${e.seconds}s for this reply` + (e.seconds >= 45 ? " — the endpoint is slow; press Stop and pick another model if this keeps growing" : "");
      break;
    case "stage":
      if (e.name === "ready") { if (e.title) $("#run-title").textContent = e.title; line("✓", esc(e.message), "good"); }
      else { setStatus("running", "Preparing"); line("⚙", esc(e.message), "note"); }
      break;
    case "run":
      if (e.status === "start") { setStatus("running", "Working"); line("▶", `Run started · model ${esc(e.model)} via the ${esc(PRETTY[e.provider] || e.provider)} API`, "note", t); }
      break;
    case "phase": stepTo(e.name); break;
    case "triage":
      line("⚖", `Triage: looks <b>${esc(e.size)}</b> (${esc((e.reasons || []).join(", "))}) → ${esc(e.plan)}`, "note big", t);
      break;
    case "intake":
      panel("ev-project", `<b>${esc(e.language)}</b> · ${e.files} files<br><span class="muted">tests:</span> <code>${esc(e.test_command || "not found")}</code>` +
        ((e.notes || []).length ? `<ul>${e.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""));
      line("◎", `Read the project: ${esc(e.language)}, ${e.files} files`, "", t);
      break;
    case "snippet":
      line("▷", `Ran the code from your description on the original code → exit ${e.exit_code}: <code>${esc((e.last_line || "").slice(0, 120))}</code>`, e.exit_code ? "bad" : "", t);
      break;
    case "localized":
      panel("ev-where", `<ul>${(e.top || []).slice(0, 6).map((f) => `<li><code>${esc(f)}</code></li>`).join("")}</ul>`);
      line("⌖", `Located likely files in ${e.seconds}s: ${(e.top || []).slice(0, 3).map(esc).join(", ")}`, "", t);
      break;
    case "criteria":
      panel("ev-criteria", `<pre>${esc(e.text || "")}</pre>`);
      line("☑", "Predicted what a maintainer would test", "", t);
      break;
    case "attempt":
      if (e.status === "start" && e.attempt > 1) line("↻", `Attempt ${e.attempt}: fresh start with lessons from attempt ${e.attempt - 1}`, "note big", t);
      if (e.status === "end") line("■", `Attempt ${e.attempt} ended: ${esc(e.stop_reason)} · evidence ${esc(e.strength)} · ${e.steps} steps`, "big", t);
      break;
    case "step":
      $("#st-steps").textContent = `${e.step}${e.max_steps ? " / " + e.max_steps : ""}`;
      if (cur.step < 4 && e.step > 1) stepTo("fix");
      break;
    case "llm":
      cur.calls += 1; cur.tokens = e.total_tokens || cur.tokens;
      $("#st-calls").textContent = cur.calls; $("#st-tokens").textContent = fmtTok(cur.tokens);
      {
        const thought = String(e.text || "").replace(/<(tool|invoke|function_calls|tool_call)\b[\s\S]*?(<\/\1>|$)/g, "").replace(/<\/?[a-z_]+[^>]*>/gi, "").trim();
        if (thought) line("💭", esc(thought.slice(0, 260)) + (thought.length > 260 ? "…" : ""), "thought", t);
      }
      break;
    case "tool_call":
      line(ICON[e.name] || "•", `<span class="tool">${esc(e.name)}</span>${esc(String(e.brief || "").slice(0, 160))}`, "", t);
      break;
    case "tool_result": {
      const out = String(e.output || "");
      if (e.is_error) line("↳", esc(out.trim().split("\n")[0].slice(0, 180) || "error"), "bad");
      else if (e.name === "compare") { const v = out.split("\n").find((l) => l.startsWith("=>")); if (v) line("↳", esc(v.slice(3, 200)), "note"); }
      else if (e.name === "str_replace_editor" && e.meta && e.meta.edited) line("↳", esc(out.split("\n")[0].slice(0, 180)), "good");
      break;
    }
    case "checkpoint":
      if (e.strength === "strong") line("◆", `Harness checkpoint at step ${e.step}: the fix already carries proof — told the agent to submit`, "good", t);
      break;
    case "verify":
      stepTo("verify");
      panel("ev-proof", `<div class="muted small">Round ${e.round}: <b style="color:${e.accepted ? "var(--ok)" : "var(--bad)"}">${e.accepted ? "accepted" : "sent back to the agent"}</b> · evidence ${esc(e.strength)}</div>${checksHtml(e.checks)}`);
      line("⚖", `Proof gate round ${e.round}: ${e.accepted ? "ACCEPTED" : "REJECTED"} (evidence ${esc(e.strength)})`, e.accepted ? "good big" : "bad big", t);
      break;
    case "independent_test":
      if (e.status === "written") { panel("ev-indep", `Written in ${e.steps} steps without seeing the fix:<br><code>${esc(String(e.command || "").slice(0, 160))}</code>`); line("🧪", "A second agent wrote its own test without seeing the fix", "", t); }
      if (e.status === "ran") { const el = $("#ev-indep .ev-body"); $("#ev-indep").hidden = false; el.innerHTML += `<div class="check"><span>original → patched</span><span class="badge ${esc(e.verdict)}">${esc(e.verdict)}</span></div>`; line("🧪", `Independent test on original vs patched: ${esc(e.verdict)}`, e.verdict === "fixes" ? "good" : "", t); }
      if (e.status === "gave_up") line("🧪", "Independent test writer gave up (no test)", "thought", t);
      break;
    case "review":
      stepTo("review");
      panel("ev-review", `<b style="color:${e.verdict === "approve" ? "var(--ok)" : "var(--warn)"}">${esc(e.verdict)}</b>` + ((e.concerns || []).length ? `<ul>${e.concerns.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""));
      line("🔍", `Reviewer: ${esc(e.verdict)}`, e.verdict === "approve" ? "good" : "note", t);
      break;
    case "nudge": line("⚑", esc(e.message || "").slice(0, 220), "note", t); break;
    case "log": if (e.level !== "debug") line(e.level === "error" ? "✖" : e.level === "warn" ? "!" : "·", esc(e.message || "").slice(0, 240), e.level === "error" ? "bad" : e.level === "warn" ? "note" : "thought", t); break;
    case "github": renderGhLinks([...(cur.gh || []), e]); break;
    case "done": finish(); break;
  }
}
async function stopRun() { if (!cur) return; $("#stop-btn").disabled = true; try { await api(`/api/runs/${cur.id}/stop`, {}); } catch (e) {} }
async function stopBatch() { if (!lastBatch) return; $("#batch-stop").disabled = true; try { await api(`/api/batches/${lastBatch}/stop`, {}); } catch (e) {} }
async function finish() {
  cur.done = true;
  $("#stop-btn").hidden = true; $("#wait-line").hidden = true;
  stopStream();
  const info = await api("/api/runs/" + cur.id);
  if (info.status === "interrupted") { setStatus("error", "Interrupted"); return; }
  const r = info.result || {};
  stepTo("done");
  const p = pillOf(r.status);
  setStatus(r.status, p.label);
  const u = r.usage || {};
  if (r.elapsed_s) $("#st-time").textContent = fmtTime(r.elapsed_s);
  if (u.total_tokens) $("#st-tokens").textContent = fmtTok(u.total_tokens);
  if (u.calls) $("#st-calls").textContent = u.calls;
  const v = $("#verdict");
  v.hidden = false;
  v.className = "verdict " + p.cls;
  const head = { verified: ["✓", "Verified fix", "The change was proven: checks that failed on the original code pass on the patched code, and nothing that passed before broke."],
                 patched: ["!", "Patched, but not proven", "A change was made, but the harness could not prove it with a failing-then-passing check. Review it before using it."],
                 no_patch: ["✕", "No fix produced", "The agent did not arrive at a change it could stand behind."],
                 error: ["✕", "Something went wrong", ""] }[r.status] || ["•", r.status, ""];
  v.innerHTML = `<div class="big">${head[0]}</div><div><h3>${head[1]}</h3><p class="detail">${esc(r.summary || head[2])}${r.error ? `<br><code>${esc(r.error)}</code>` : ""}</p>
    <p class="detail small muted" style="margin-top:6px">${u.total_tokens ? fmtTok(u.total_tokens) + " tokens · " + (u.calls || 0) + " model calls · " : ""}${r.elapsed_s ? fmtTime(r.elapsed_s) : ""}${r.attempts ? " · " + r.attempts + " attempt(s)" : ""}</p></div>`;
  if (r.checks && r.checks.length) panel("ev-proof", checksHtml(r.checks));
  if (cur.wantPr && (r.status === "verified" || r.status === "patched") && !(info.github || []).some((g) => g.kind === "pr" && g.url) && !cur.replay) {
    setTimeout(() => openGithub("pr", true), 600);   // asked at the end, as requested before/while the run went
  }
  if (r.patch) {
    $("#diff-card").hidden = false;
    $("#diff").innerHTML = renderDiff(r.patch);
    $("#dl-patch").href = `/api/runs/${cur.id}/patch`;
    $("#open-report").href = `/api/runs/${cur.id}/report`;
    cur.repoPath = r.repo;
  }
}
function renderDiff(patch) {
  const files = patch.split(/^diff --git /m).filter(Boolean);
  return files.map((f) => {
    const lines = f.split("\n");
    const name = (lines[0].match(/ b\/(.+)$/) || [, lines[0]])[1];
    const body = lines.slice(1).filter((l) => !/^(index |--- |\+\+\+ |new file mode|deleted file mode)/.test(l)).map((l) => {
      const cls = l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
      return `<div class="dl ${cls}">${esc(l) || " "}</div>`;
    }).join("");
    return `<div class="dfile"><div class="dfile-name">${esc(name)}</div><div class="dlines">${body}</div></div>`;
  }).join("");
}
/* ---------------- issue picker + batch ---------------- */
function showPicker(r) {
  pickState = r;
  ["home", "run", "batch"].forEach((v) => ($("#" + v).hidden = true)); $("#pick").hidden = false;
  $("#pick-repo").textContent = r.repo;
  $("#pick-pr").checked = $("#want-pr").checked;
  $("#pick-auto").checked = $("#auto-pr").checked;
  const pre = new Set(r.preselect || []);
  $("#pick-list").innerHTML = r.issues.length ? r.issues.map((i) => `<label class="pick-row"><input type="checkbox" value="${i.number}" ${pre.has(i.number) ? "checked" : ""}>
      <span class="num">#${i.number}</span><span><span class="ttl">${esc(i.title)}</span>${(i.labels || []).map((l) => `<span class="lbl ${esc(l)}">${esc(l)}</span>`).join("")}
      <div class="muted small">${esc((i.body || "").split("\n")[0].slice(0, 160))}</div></span></label>`).join("")
    : `<p class="muted">No open issues on this repository — use your text as the task instead.</p>`;
}
function pickAll(on) { document.querySelectorAll("#pick-list input").forEach((c) => (c.checked = on)); }
async function useAsText() {
  try {
    const r = await api("/api/runs", { prompt: $("#prompt").value, repo: $("#repo").value, test: $("#test").value, want_pr: $("#want-pr").checked, as_text: true });
    go("run", r.id);
  } catch (e) { $("#pick-error").textContent = e.message; }
}
async function startBatch() {
  const nums = [...document.querySelectorAll("#pick-list input:checked")].map((c) => +c.value).sort((a, b) => a - b);
  try { const r = await api("/api/batch", { repo: pickState.repo, numbers: nums, want_pr: $("#pick-pr").checked && !$("#pick-auto").checked, auto_pr: $("#pick-auto").checked }); go("batch", r.id); }
  catch (e) { $("#pick-error").textContent = e.message; }
}
const DONE = new Set(["verified", "patched", "no_patch", "error", "interrupted", "cancelled"]);
let walkQueue = null, batchStart = null, batchEnd = null;
setInterval(() => { if (batchStart && !$("#batch").hidden) $("#batch-clock").textContent = fmtTime(((batchEnd || Date.now() / 1000) - batchStart)); }, 500);
async function openBatch(id) {
  lastBatch = id;
  const tick = async () => {
    let b;
    try { b = await api("/api/batches/" + id); } catch (e) { return; }
    $("#batch-title").textContent = `Fixing ${b.items.length} issue${b.items.length > 1 ? "s" : ""} in ${b.repo}`;
    const done = b.items.filter((x) => DONE.has(x.status)).length;
    $("#batch-progress").textContent = `${done} / ${b.items.length} done · ${b.items.filter((x) => x.status === "verified").length} verified · ${b.running_now || 0} running in parallel` + (b.auto_pr ? " · PRs open automatically" : "");
    $("#batch-model").textContent = modelInfo ? `${PRETTY[modelInfo.provider] || modelInfo.provider} API · ${modelInfo.model}` : "";
    const finished = b.status === "done" || b.status === "stopped" || String(b.status).startsWith("error");
    batchStart = b.created; batchEnd = b.ended;
    $("#batch-verified").textContent = b.items.filter((x) => x.status === "verified").length + " / " + b.items.length;
    $("#batch-parallel").textContent = b.running_now || 0;
    $("#batch-calls").textContent = b.items.reduce((a, x) => a + (x.calls || 0), 0);
    $("#batch-stop").hidden = finished;
    const el = $("#batch-status");
    el.className = "status-pill " + (finished ? (String(b.status).startsWith("error") ? "failed" : "verified") : "running");
    el.innerHTML = finished ? (String(b.status).startsWith("error") ? esc(b.status) : "All done") : '<span class="spin"></span>Working';
    $("#batch-rows").innerHTML = b.items.map((x) => {
      const p = pillOf(DONE.has(x.status) ? x.status : "running");
      const lbl = x.status === "queued" ? "Queued" : DONE.has(x.status) ? p.label : (x.phase ? x.phase.charAt(0).toUpperCase() + x.phase.slice(1) + "…" : "Working…");
      const pr = (x.github || []).find((g) => g.kind === "pr" && g.url);
      const prCell = pr ? `<a href="${esc(pr.url)}" target="_blank" onclick="event.stopPropagation()">open PR ↗</a>`
        : (x.branch ? `<button class="btn" onclick="event.stopPropagation();openGithub('pr',false,'${x.run_id}')">Create PR</button>` : '<span class="muted">–</span>');
      const live = !DONE.has(x.status) && x.status !== "queued" && x.now ? `<div class="now">↳ ${esc(x.now)}${x.calls ? ` · ${x.calls} model calls` : ""}</div>` : "";
      const sp = x.split || null;
      const tot = sp ? Object.values(sp).reduce((a, n) => a + n, 0) : 0;
      const bar = sp && tot > 0 ? `<div class="split">${["setup", "model", "tools", "proof"].map((k) => sp[k] > 0 ? `<span class="seg ${k}" style="flex:${sp[k]}" title="${k}: ${sp[k]}s"></span>` : "").join("")}</div>
        <div class="split-lbl">model ${Math.round(sp.model)}s · tools ${Math.round(sp.tools)}s · proof ${Math.round(sp.proof)}s${sp.setup ? ` · setup ${Math.round(sp.setup)}s` : ""} · ${x.calls || 0} calls</div>` : "";
      return `<tr class="${x.run_id ? "clickable" : ""}" onclick="${x.run_id ? `go('run','${x.run_id}')` : ""}"><td><b>#${x.number}</b> ${esc(x.title)}${live}</td>
        <td><span class="status-pill ${x.status === "queued" ? "" : p.cls}">${esc(lbl)}</span></td><td class="num">${x.elapsed_s ? fmtTime(x.elapsed_s) : ""}</td><td class="split-cell">${bar}</td>
        <td class="num">${x.tokens ? fmtTok(x.tokens) : ""}</td><td>${prCell}</td></tr>`;
    }).join("");
    if (finished) {
      clearInterval(batchTimer); batchTimer = null;
      if (b.want_pr && walkQueue === null) {          // one by one: offer each proven fix's pull request in turn
        walkQueue = b.items.filter((x) => x.branch && !(x.github || []).some((g) => g.kind === "pr" && g.url)).map((x) => x.run_id);
        nextWalk();
      }
    }
  };
  walkQueue = null;
  await tick();
  batchTimer = setInterval(tick, 1500);
}
function nextWalk() { if (walkQueue && walkQueue.length) openGithub("pr", true, walkQueue.shift()); }
function runBack() { if (cur && cur.batchId) go("batch", cur.batchId); else go("home"); }

/* ---------------- GitHub: pull request / issue ---------------- */
let ghKind = "pr", ghPreview = null;
function renderGhLinks(list) {
  cur.gh = list;
  $("#gh-links").innerHTML = list.map((g) => g.url ? `✓ ${g.kind === "pr" ? "Pull request" : "Issue"} ${g.updated ? "updated" : "created"}: <a href="${esc(g.url)}" target="_blank">${esc(g.url)}</a>`
    : `<span style="color:var(--bad)">✗ ${g.kind === "pr" ? "Pull request" : "Issue"} failed: ${esc(String(g.error || "").slice(0, 200))}</span>`).join("<br>");
}
async function toggleWantPr(on) { cur.wantPr = on; try { await api(`/api/runs/${cur.id}/flags`, { want_pr: on }); } catch (e) {} }
let ghRun = null;
async function openGithub(kind, askedAtEnd, runId) {
  ghKind = kind;
  ghRun = runId || cur.id;
  const dlg = $("#gh-dlg");
  $("#gh-title-h").textContent = (askedAtEnd ? "Fix ready — " : "") + (kind === "pr" ? "Create pull request" : "Create issue");
  $("#gh-where").innerHTML = "Checking GitHub…"; $("#gh-title").value = ""; $("#gh-desc").value = ""; $("#gh-status").textContent = "";
  $("#gh-go").disabled = true;
  dlg.showModal();
  try {
    ghPreview = await api(`/api/runs/${ghRun}/github?kind=${kind}`);
  } catch (e) { ghPreview = { ok: false, error: e.message }; }
  if (!ghPreview.ok) { $("#gh-where").innerHTML = `<span style="color:var(--bad)">${esc(ghPreview.error || "Not available.")}</span>`; return; }
  const p = ghPreview;
  $("#gh-where").innerHTML = kind === "pr"
    ? `Repository <b>${esc(p.repo)}</b> · into <code>${esc(p.base)}</code> from <code>${esc(p.branch)}</code><br>Push to: <b>${esc(p.push_to)}</b> · as <b>${esc(p.user)}</b><br>Files: ${(p.files || []).map((f) => `<code>${esc(f)}</code>`).join(" ")}`
    : `Repository <b>${esc(p.repo)}</b> · as <b>${esc(p.user)}</b>`;
  $("#gh-title").value = p.title || ""; $("#gh-desc").value = p.body || "";
  $("#gh-go").disabled = false;
  $("#gh-go").textContent = kind === "pr" ? "Create pull request on GitHub" : "Create issue on GitHub";
}
async function submitGithub() {
  const btn = $("#gh-go"); btn.disabled = true;
  $("#gh-status").textContent = ghKind === "pr" ? "Creating the branch, pushing, opening the pull request…" : "Creating the issue…";
  try {
    const r = await api(`/api/runs/${ghRun}/github`, { kind: ghKind, title: $("#gh-title").value, body: $("#gh-desc").value,
      branch: ghPreview && ghPreview.branch, files: ghPreview && ghPreview.files });
    if (r.ok) { $("#gh-status").innerHTML = `✓ ${r.updated ? "Already open (updated)" : "Created"}: <a href="${esc(r.url)}" target="_blank">${esc(r.url)}</a>`; }
    else { $("#gh-status").innerHTML = `<span style="color:var(--bad)">✗ ${esc(String(r.error || "failed").slice(0, 300))}</span>`; btn.disabled = false; }
  } catch (e) { $("#gh-status").innerHTML = `<span style="color:var(--bad)">✗ ${esc(e.message)}</span>`; btn.disabled = false; }
}

function copyPath() { if (cur && cur.repoPath) navigator.clipboard.writeText(cur.repoPath); }

/* ---------------- stop everything ---------------- */
async function stopAll() {
  const b = $("#stop-all"); b.disabled = true; b.textContent = "Stopping…";
  try { const r = await api("/api/stop-all", {}); b.textContent = `Stopped ${r.runs} run(s)`; } catch (e) { b.textContent = "■ Stop everything"; }
  setTimeout(() => { b.disabled = false; b.textContent = "■ Stop everything"; pollActive(); }, 2500);
}
async function pollActive() {
  try {
    const runs = await api("/api/runs");
    $("#stop-all").hidden = !runs.some((r) => !["done", "error", "interrupted"].includes(r.status));
  } catch (e) {}
}
setInterval(pollActive, 3000);
document.addEventListener("keydown", (e) => { if (e.key === "." && (e.metaKey || e.ctrlKey)) stopAll(); });   // ⌘. = stop everything

/* ---------------- boot ---------------- */
loadModel().then(() => { loadModelPicker(); if (modelInfo && !modelInfo.ok) openSettings(); });   // first launch: ask for the key
loadDemos(); understood();
window.addEventListener("focus", () => loadModel());   // a key set elsewhere shows up as soon as you come back
async function forgetKey() { await api("/api/model", { mode: "forget" }); $("#api-key").value = ""; await loadModel(); openSettings(); }
const m = location.hash.match(/run=([\w-]+)/), mb = location.hash.match(/batch=([\w-]+)/);
if (m) go("run", m[1]); else if (mb) go("batch", mb[1]); else go("home");
