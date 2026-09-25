let snapshot = { mqtt_connected: false, daemon: "unknown", lights: {} };
let activeTab = "controls";
let logBusy = false;
let enrollment = { active: false, remaining_seconds: 0, candidates: [] };
let enrollmentCandidateSignature = "";
const enrollmentDrafts = new Map();

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

function esc(value) {
  return String(value ?? "—")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function isOnline(light) {
  return snapshot.mqtt_connected && snapshot.daemon === "online" && light.availability === "online";
}
function lightCard(name, light) {
  const online = isOnline(light);
  const brightness = Math.max(0, Math.min(100, Number(light["state/brightness"] || 0)));
  const isOn = String(light["state/on"] || "OFF").toUpperCase() === "ON";
  const origin = light["diagnostic/origin"] || "—";
  const lastChange = light["diagnostic/last_change"] || "—";
  return `<article class="light-card ${online ? "" : "offline"}" data-light="${esc(name)}">
    <div class="light-head">
      <div class="light-name">${esc(light.label || name)}</div>
      <div class="light-tools">
        <div class="dot ${online ? "online" : ""}"></div>
        <button class="manage-button" type="button" title="Manage light" aria-label="Manage ${esc(light.label || name)}">⋯</button>
        <div class="manage-menu">
          <button class="rename-light" type="button">Rename</button>
          <button class="delete-light danger" type="button">Delete</button>
        </div>
      </div>
    </div>
    <div class="brightness-value">${brightness}%</div>
    <input class="range" type="range" min="0" max="100" value="${brightness}" ${online ? "" : "disabled"}>
    <div class="card-actions">
      <button class="on ${isOn ? "active-on" : ""}" ${online ? "" : "disabled"}>On</button>
      <button class="secondary off ${!isOn ? "active-off" : ""}" ${online ? "" : "disabled"}>Off</button>
    </div>
    <div class="meta">
      <span>Origin: <b class="origin-${esc(origin)}">${esc(origin)}</b></span>
      <span>Last change: ${esc(lastChange)}</span>
    </div>
  </article>`;
}

function wireCards() {
  document.querySelectorAll(".light-card").forEach(card => {
    const name = card.dataset.light;
    card.querySelector(".on").onclick = () => command(name, { on: true });
    card.querySelector(".off").onclick = () => command(name, { on: false });
    const slider = card.querySelector(".range");
    slider.oninput = () => card.querySelector(".brightness-value").textContent = `${slider.value}%`;
    slider.onchange = () => command(name, { brightness: Number(slider.value) });
    const menu = card.querySelector(".manage-menu");
    card.querySelector(".manage-button").onclick = (event) => {
      event.stopPropagation();
      menu.classList.toggle("open");
    };
    card.querySelector(".rename-light").onclick = () => renameLight(name);
    card.querySelector(".delete-light").onclick = () => deleteLight(name);
  });
}

async function command(name, body) {
  try {
    await api(`/api/light/${encodeURIComponent(name)}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    await refresh();
  } catch (err) {
    document.getElementById("backend-health").textContent = `Command error: ${err.message}`;
  }
}

async function renameLight(name) {
  const light = snapshot.lights?.[name];
  if (!light) return;
  const current = light.label || name;
  const next = window.prompt(`Rename ${current}`, current);
  if (next === null || next.trim() === "" || next.trim() === current) return;
  try {
    await api(`/api/light/${encodeURIComponent(name)}/rename`, {
      method: "POST",
      body: JSON.stringify({ display_name: next.trim() }),
    });
    document.getElementById("backend-health").textContent = "Rename saved · controller restarting…";
  } catch (err) {
    document.getElementById("backend-health").textContent = `Rename error: ${err.message}`;
  }
}

async function deleteLight(name) {
  const light = snapshot.lights?.[name];
  if (!light) return;
  const label = light.label || name;
  const guid = light.guid || "";
  const message = `Delete ${label} from MKSI?\n\nGUID: ${guid}\n\nThis removes its Home Assistant discovery and forgets the dimmer until it is enrolled again. The physical dimmer is not factory-reset.`;
  if (!window.confirm(message)) return;
  try {
    await api(`/api/light/${encodeURIComponent(name)}`, {
      method: "DELETE",
      body: JSON.stringify({ guid }),
    });
    document.getElementById("backend-health").textContent = "Device deleted · controller restarting…";
  } catch (err) {
    document.getElementById("backend-health").textContent = `Delete error: ${err.message}`;
  }
}

function renderEnrollment() {
  const panel = document.getElementById("enrollment-panel");
  const add = document.getElementById("add-light");
  const summary = document.getElementById("enrollment-summary");
  const candidates = document.getElementById("enrollment-candidates");
  panel.classList.toggle("hidden", !enrollment.active);
  add.disabled = enrollment.active;
  add.textContent = enrollment.active ? "Enrollment active" : "+ Add Light";
  summary.textContent = enrollment.active ? `${enrollment.remaining_seconds}s remaining` : "";

  if (!enrollment.active) {
    enrollmentCandidateSignature = "";
    enrollmentDrafts.clear();
    candidates.innerHTML = "";
    return;
  }

  const items = enrollment.candidates || [];
  const signature = items.map(item => item.guid).join(";");
  if (signature === enrollmentCandidateSignature) return;

  candidates.querySelectorAll(".candidate").forEach(row => {
    const input = row.querySelector(".candidate-name");
    if (input) enrollmentDrafts.set(row.dataset.guid, input.value);
  });

  enrollmentCandidateSignature = signature;
  candidates.innerHTML = items.map(item => `
    <div class="candidate" data-guid="${esc(item.guid)}">
      <div><strong>New Varilight detected</strong><div><code>${esc(item.guid)}</code></div><div class="candidate-meta">Firmware ${esc(item.firmware || "—")} · ${esc(item.peer || "—")}</div></div>
      <input class="candidate-name" type="text" maxlength="64" placeholder="Light name">
      <button class="candidate-add" type="button">Add</button>
    </div>`).join("") || '<div class="enrollment-waiting">Waiting for an unknown device to connect…</div>';

  candidates.querySelectorAll(".candidate").forEach(row => {
    const input = row.querySelector(".candidate-name");
    input.value = enrollmentDrafts.get(row.dataset.guid) || "";
    input.oninput = () => enrollmentDrafts.set(row.dataset.guid, input.value);
    row.querySelector(".candidate-add").onclick = () => acceptEnrollment(row.dataset.guid, input.value);
    input.onkeydown = event => {
      if (event.key === "Enter") acceptEnrollment(row.dataset.guid, input.value);
    };
  });
}
async function startEnrollment() {
  try {
    enrollment = await api("/api/enrollment/start", { method: "POST", body: JSON.stringify({ seconds: 300 }) });
    renderEnrollment();
  } catch (err) {
    document.getElementById("backend-health").textContent = `Enrollment error: ${err.message}`;
  }
}

async function cancelEnrollment() {
  try {
    enrollment = await api("/api/enrollment/cancel", { method: "POST", body: "{}" });
    renderEnrollment();
  } catch (err) {
    document.getElementById("backend-health").textContent = `Enrollment error: ${err.message}`;
  }
}

async function acceptEnrollment(guid, displayName) {
  const name = String(displayName || "").trim();
  if (!name) return;
  try {
    await api("/api/enrollment/accept", { method: "POST", body: JSON.stringify({ guid, display_name: name }) });
    document.getElementById("backend-health").textContent = "Light added · controller restarting…";
  } catch (err) {
    document.getElementById("backend-health").textContent = `Add error: ${err.message}`;
  }
}

function render() {
  const health = document.getElementById("backend-health");
  const daemon = document.getElementById("daemon-state");
  health.textContent = snapshot.mqtt_connected ? "MQTT connected · local only" : "MQTT disconnected";
  daemon.textContent = `Controller ${snapshot.daemon}`;
  renderEnrollment();
  const entries = Object.entries(snapshot.lights || {});
  document.getElementById("lights").innerHTML = entries.map(([name, light]) => lightCard(name, light)).join("");
  wireCards();
  document.getElementById("inventory").innerHTML = entries.map(([name, light]) => {
    const online = isOnline(light) ? "online" : "offline";
    const origin = light["diagnostic/origin"] || "—";
    const lastChange = light["diagnostic/last_change"] || "—";
    const guid = light.guid || "—";
    const shortGuid = guid === "—" ? guid : `${guid.slice(0, 8)}…${guid.slice(-6)}`;
    return `<tr>
      <td>${esc(light.label || name)}</td>
      <td><code>${esc(shortGuid)}</code></td>
      <td>${esc(online)}</td>
      <td>${esc(origin)}</td>
      <td>${esc(lastChange)}</td>
    </tr>`;
  }).join("");
}

async function refresh() {
  try {
    const results = await Promise.all([api("/api/state"), api("/api/enrollment")]);
    snapshot = results[0];
    enrollment = results[1];
    render();
  } catch (err) {
    document.getElementById("backend-health").textContent = `UI error: ${err.message}`;
  }
}
function switchTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab-button").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach(panel => {
    panel.classList.toggle("active", panel.id === `tab-${name}`);
  });
  if (name === "logs") refreshLogs();
}

document.querySelectorAll(".tab-button").forEach(btn => {
  btn.onclick = () => switchTab(btn.dataset.tab);
});

function isRoutinePing(line) {
  return line.includes('"call_id":40') ||
    line.includes('"call_id":50') ||
    line.includes('"tx_kind":"ping"') ||
    line.includes('"tx_kind":"ping_result"') ||
    (line.includes("management_http") &&
      (line.includes('GET /devices HTTP/1.1') || line.includes('GET /enrollment HTTP/1.1')));
}

async function refreshLogs() {
  if (logBusy || activeTab !== "logs") return;
  logBusy = true;
  const out = document.getElementById("log-output");
  const source = document.getElementById("log-source").value;
  const lines = Number(document.getElementById("log-lines").value);
  const fetchLines = Math.min(250, lines * 5);
  try {
    const data = await api(`/api/logs?source=${encodeURIComponent(source)}&lines=${fetchLines}`);
    const atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 24;
    const useful = (data.text || "").split("\n").filter(line => line && !isRoutinePing(line));
    out.textContent = useful.slice(-lines).join("\n") || "(no non-ping journal entries)";
    if (atBottom) out.scrollTop = out.scrollHeight;
  } catch (err) {
    out.textContent = `Log error: ${err.message}`;
  } finally {
    logBusy = false;
  }
}

document.getElementById("add-light").onclick = startEnrollment;
document.getElementById("cancel-enrollment").onclick = cancelEnrollment;
document.getElementById("log-refresh").onclick = refreshLogs;
document.getElementById("log-source").onchange = refreshLogs;
document.getElementById("log-lines").onchange = refreshLogs;

refresh();
setInterval(refresh, 1500);
setInterval(() => {
  if (activeTab === "logs" && document.getElementById("log-auto").checked) refreshLogs();
}, 2500);
