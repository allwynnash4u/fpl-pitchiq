"use strict";

const FORMATIONS = [
  { key: "3-4-3", def: 3, mid: 4, fwd: 3 },
  { key: "3-5-2", def: 3, mid: 5, fwd: 2 },
  { key: "4-3-3", def: 4, mid: 3, fwd: 3 },
  { key: "4-4-2", def: 4, mid: 4, fwd: 2 },
  { key: "4-5-1", def: 4, mid: 5, fwd: 1 },
  { key: "5-3-2", def: 5, mid: 3, fwd: 2 },
  { key: "5-4-1", def: 5, mid: 4, fwd: 1 },
];
const DEFAULT_FORMATION = "4-4-2";
const SAVED_FORMATION_KEY = "fpl-ai-formation";
const ASSISTANT_HISTORY_KEY = "fpl-copilot-chat-v1";

const state = {
  status: null,
  teams: [],
  profile: null,
  calibration: null,
  transfers: null,
  squadOptimizer: null,
  dashboard: null,
  dashboardLoading: null,
  assistantReady: false,
  assistantBusy: false,
  assistantHistoryLoaded: false,
  assistantScenarioPlayers: [],
  assistantScenarioSquad: [],
  backtestReport: null,
  backtestLoading: null,
  ledger: null,
  ledgerLoading: null,
  chipPlan: null,
  chipPlanLoading: null,
  lastProjectionTrigger: null,
  currentSquad: [],
  projectionCache: new Map(),
  projectionCacheTimestamp: 0,
  selectedFormation: DEFAULT_FORMATION,
  transferOutSet: new Set(),
  selectedPlayerProjection: null,
  projectionCacheByHorizon: {
    1: new Map(),
    3: new Map(),
    6: new Map(),
  },
  projectionCacheLastLoaded: 0,
  squadDisplayMode: "pitch",
};
const VIEWS = ["overview", "team", "optimizer", "projections", "assistant", "chips", "backtesting", "ledger", "players", "fixtures", "quality", "pricing"];

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function notice(message, error = false) {
  const node = $("#notice");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.remove("hidden");
  window.clearTimeout(notice.timer);
  notice.timer = window.setTimeout(() => node.classList.add("hidden"), error ? 9000 : 5000);
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function signed(value, suffix = "") {
  const number = Number(value || 0);
  return `${number > 0 ? "+" : ""}${number.toFixed(1)}${suffix}`;
}

function clamp(value, min, max) {
  return Math.min(Math.max(Number(value), min), max);
}

function initialsFromName(name) {
  if (!name) return "??";
  const words = String(name).split(/\s+/).filter(Boolean);
  const first = words[0] ? words[0][0].toUpperCase() : "";
  const second = words[1] ? words[1][0].toUpperCase() : "";
  const combined = `${first}${second}`.trim();
  return combined || "??";
}

function resolvePlayerPosition(position) {
  return position === "GK" ? "GKP" : position;
}

function parseFormation(key, fallback = DEFAULT_FORMATION) {
  const found = FORMATIONS.find((item) => item.key === key);
  return found || FORMATIONS.find((item) => item.key === fallback) || FORMATIONS[0];
}

function formationFromLineup(starters) {
  const counts = { DEF: 0, MID: 0, FWD: 0 };
  starters.forEach((player) => {
    const position = resolvePlayerPosition(player.player_position);
    if (counts[position] !== undefined) counts[position] += 1;
  });
  const exact = FORMATIONS.find((item) => item.def === counts.DEF && item.mid === counts.MID && item.fwd === counts.FWD);
  return exact ? exact.key : `${counts.DEF}-${counts.MID}-${counts.FWD}`;
}

function isFormationAllowed(formation, lineup) {
  const target = parseFormation(formation);
  const counts = { DEF: 0, MID: 0, FWD: 0, GKP: 0 };
  lineup.forEach((player) => {
    const position = resolvePlayerPosition(player.player_position);
    if (counts[position] !== undefined) counts[position] += 1;
  });
  return counts.GKP === 1 && counts.DEF === target.def && counts.MID === target.mid && counts.FWD === target.fwd;
}

function resolveLineupFromFormation(starters, formation) {
  const config = parseFormation(formation);
  const grouped = { GKP: [], DEF: [], MID: [], FWD: [] };
  starters.forEach((player) => {
    const position = resolvePlayerPosition(player.player_position);
    if (grouped[position]) grouped[position].push(player);
  });
  return {
    GK: grouped.GKP.slice(0, 1),
    DEF: grouped.DEF.slice(0, config.def),
    MID: grouped.MID.slice(0, config.mid),
    FWD: grouped.FWD.slice(0, config.fwd),
  };
}

function estimatePitchScale(field, lineupRows) {
  const counts = [
    Math.max(1, lineupRows.GK.length),
    lineupRows.DEF.length || 1,
    lineupRows.MID.length || 1,
    lineupRows.FWD.length || 1,
  ];
  const maxRow = Math.max(...counts);
  const rect = field.getBoundingClientRect();
  const width = Math.max(rect.width, 280);
  const baseCardWidth = 96;
  const baseCardHeight = 106;
  const availablePerPlayer = (width * 0.86) / maxRow;
  const scale = clamp((availablePerPlayer * 0.84) / baseCardWidth, 0.56, 1);
  field.style.setProperty("--pitch-player-scale", `${scale.toFixed(3)}`);
  field.style.setProperty("--pitch-card-width", `${Math.round(baseCardWidth * scale)}px`);
  field.style.setProperty("--pitch-card-height", `${Math.round(baseCardHeight * scale)}px`);
  field.style.setProperty("--pitch-font-scale", `${clamp(scale + 0.08, 0.68, 1).toFixed(3)}`);
  return scale;
}

function mirrorPitchScale(source, destination) {
  const scale = source.style.getPropertyValue("--pitch-player-scale");
  const width = source.style.getPropertyValue("--pitch-card-width");
  const height = source.style.getPropertyValue("--pitch-card-height");
  const font = source.style.getPropertyValue("--pitch-font-scale");
  if (scale) destination.style.setProperty("--pitch-player-scale", scale);
  if (width) destination.style.setProperty("--pitch-card-width", width);
  if (height) destination.style.setProperty("--pitch-card-height", height);
  if (font) destination.style.setProperty("--pitch-font-scale", font);
}

function slotPositions(count, span = 82) {
  if (count <= 1) return [50];
  const spanWidth = clamp(span, 50, 94);
  const safeStart = (100 - spanWidth) / 2;
  const safeEnd = 100 - safeStart;
  const availableSpan = safeEnd - safeStart;
  return Array.from({ length: count }, (_, index) => safeStart + (availableSpan * index) / (count - 1));
}

function text(node, value, fallback = "—") {
  node.textContent = value === null || value === undefined || value === "" ? fallback : String(value);
}

function td(row, value, className = "") {
  const cell = document.createElement("td");
  cell.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  if (className) cell.className = className;
  row.appendChild(cell);
  return cell;
}

function setView(name, { updateHash = true, scroll = true } = {}) {
  if (!VIEWS.includes(name)) name = "overview";
  if (updateHash && window.location.hash !== `#${name}`) {
    window.location.hash = name;
    return;
  }
  $$(".nav-item").forEach((item) => {
    const active = item.dataset.view === name;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $("#transfer-bar")?.classList.toggle("compact", !["overview", "team"].includes(name));
  const titles = { overview: "Your best FPL move", team: "My FPL team", optimizer: "Plan & optimize", assistant: "Ask FPL Copilot", chips: "Chip planner", backtesting: "Model performance", ledger: "Decision ledger", players: "Player data", fixtures: "Fixture planner", quality: "Data quality", projections: "Predicted points", pricing: "Plans" };
  text($("#page-title"), titles[name]);
  if (name === "players") loadPlayers();
  if (name === "fixtures") loadFixtures();
  if (name === "quality") loadQuality();
  if (name === "projections") loadProjections();
  if (name === "optimizer") loadSquadOptimizer();
  if (name === "assistant") loadAssistant();
  if (name === "chips") loadChipPlanner();
  if (name === "backtesting") loadBacktestReport();
  if (name === "ledger") loadDecisionLedger();
  if (name === "overview") loadDashboard();
  if (scroll) window.scrollTo({ top: 0, behavior: "auto" });
}

async function loadStatus() {
  const result = await api("/api/status");
  state.status = result;
  const event = result.planning_event || result.current_event || result.next_event;
  text($("#dashboard-gameweek"), event ? `GW${event.id}` : "—");
  text($("#quality-errors"), result.quality.errors);
  text($("#quality-warnings"), result.quality.warnings);
  text($("#quality-info"), result.quality.info);
  const indicator = $("#sync-indicator");
  const syncAge = result.last_sync?.completed_at ? Date.now() - new Date(result.last_sync.completed_at).getTime() : Infinity;
  const needsRefresh = result.last_sync?.used_stale_data || !Number.isFinite(syncAge) || syncAge > 12 * 60 * 60 * 1000;
  indicator.className = `status-pill ${!result.ready || result.quality.errors ? "bad" : needsRefresh ? "warning" : "good"}`;
  indicator.textContent = !result.ready || result.quality.errors ? "Data update required" : needsRefresh ? "Official data needs refresh" : "Official data current";
  const values = result.last_sync ? [
    formatDate(result.last_sync.completed_at),
    result.last_sync.bootstrap_source,
    result.last_sync.fixtures_source,
    result.last_sync.used_stale_data ? "Yes — review freshness" : "No",
  ] : ["Never", "—", "—", "—"];
  $$("#sync-details dd").forEach((node, index) => text(node, values[index]));
  if (!result.ready) notice("No valid local dataset is installed. Use Refresh official data to download it.", true);
}

function updateDeadlineClock(deadline) {
  window.clearInterval(updateDeadlineClock.timer);
  const clock = $("#dashboard-deadline-clock");
  const dateNode = $("#dashboard-deadline-date");
  const render = () => {
    if (!deadline) {
      text(clock, "—");
      text(dateNode, "Deadline unavailable");
      return;
    }
    const target = new Date(deadline);
    const delta = target.getTime() - Date.now();
    text(dateNode, formatDate(deadline));
    if (!Number.isFinite(delta) || delta <= 0) {
      text(clock, "Deadline passed");
      clock.classList.add("past");
      return;
    }
    clock.classList.remove("past");
    const totalMinutes = Math.floor(delta / 60_000);
    const days = Math.floor(totalMinutes / 1440);
    const hours = Math.floor((totalMinutes % 1440) / 60);
    const minutes = totalMinutes % 60;
    text(clock, days > 0 ? `${days}d ${hours}h` : `${hours}h ${minutes}m`);
  };
  render();
  updateDeadlineClock.timer = window.setInterval(render, 60_000);
}

function renderDashboardChart(players, horizon) {
  const root = $("#dashboard-squad-chart");
  root.replaceChildren();
  if (!players || !players.length) {
    const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "Import your team to compare all 15 squad projections."; root.appendChild(empty);
    return;
  }
  const max = Math.max(...players.map((player) => Number(player.expected_points || 0)), 1);
  players.forEach((player) => {
    const row = document.createElement("div"); row.className = `dashboard-bar-row${player.starter ? " starter" : " bench"}`;
    const label = document.createElement("div"); label.className = "dashboard-bar-label";
    const name = document.createElement("strong"); name.textContent = player.name;
    const meta = document.createElement("small"); meta.textContent = `${player.position} · ${player.team}${player.starter ? " · XI" : " · Bench"}`;
    label.append(name, meta);
    const track = document.createElement("div"); track.className = "dashboard-bar-track";
    const fill = document.createElement("span"); fill.style.width = `${Math.max(2, Number(player.expected_points || 0) / max * 100)}%`;
    track.appendChild(fill);
    const value = document.createElement("strong"); value.className = "dashboard-bar-value"; value.textContent = Number(player.expected_points || 0).toFixed(1);
    row.append(label, track, value);
    root.appendChild(row);
  });
  root.setAttribute("aria-label", `${horizon}-gameweek expected points for the imported squad, ordered highest to lowest`);
}

function renderDashboardAlerts(alerts) {
  const root = $("#dashboard-alerts");
  root.replaceChildren();
  text($("#dashboard-alert-count"), alerts.length);
  const status = $("#dashboard-alert-status");
  status.className = `status-pill ${alerts.some((item) => item.severity === "danger") ? "bad" : alerts.length ? "warning" : "good"}`;
  text(status, alerts.length ? `${alerts.length} item${alerts.length === 1 ? "" : "s"}` : "Squad clear");
  if (!alerts.length) {
    const empty = document.createElement("p"); empty.className = "dashboard-clear"; empty.textContent = "No material availability, rotation or transfer alerts in the current snapshot."; root.appendChild(empty);
    return;
  }
  alerts.forEach((alert) => {
    const item = document.createElement("article"); item.className = `dashboard-alert ${alert.severity}`;
    const dot = document.createElement("i");
    const content = document.createElement("div");
    const player = document.createElement("strong"); player.textContent = alert.player;
    const message = document.createElement("p"); message.textContent = alert.message;
    content.append(player, message); item.append(dot, content); root.appendChild(item);
  });
}

function renderDashboardFixtures(outlook) {
  const root = $("#dashboard-fixtures");
  root.replaceChildren();
  const makeGroup = (title, rows, className) => {
    const group = document.createElement("div"); group.className = "dashboard-fixture-group";
    const heading = document.createElement("span"); heading.className = `dashboard-fixture-heading ${className}`; heading.textContent = title;
    group.appendChild(heading);
    (rows || []).forEach((row) => {
      const item = document.createElement("article"); item.className = "dashboard-fixture-row";
      const player = document.createElement("div");
      const name = document.createElement("strong"); name.textContent = row.name;
      const path = document.createElement("small"); path.textContent = row.label;
      player.append(name, path);
      const score = document.createElement("span"); score.textContent = Number(row.score).toFixed(0);
      item.append(player, score); group.appendChild(item);
    });
    root.appendChild(group);
  };
  makeGroup("Best run", outlook?.best, "good");
  makeGroup("Most difficult", outlook?.difficult, "warning");
}

function renderDashboardRebuild(optimized, horizon) {
  const root = $("#dashboard-rebuild");
  root.replaceChildren();
  if (!optimized || optimized.status !== "ready") {
    text($("#dashboard-rebuild-formation"), "—");
    const empty = document.createElement("p"); empty.className = "empty";
    empty.textContent = optimized?.status === "verification-required"
      ? "Squad plan withheld until your current team inputs are verified in My team."
      : "The optimized rebuild is unavailable until projections are ready.";
    root.appendChild(empty);
    return;
  }
  text($("#dashboard-rebuild-formation"), optimized.formation);
  const score = document.createElement("div"); score.className = "dashboard-rebuild-score";
  const number = document.createElement("strong"); number.textContent = Number(optimized.total_expected_points).toFixed(1);
  const label = document.createElement("span"); label.textContent = `${horizon}-GW XI + captain`;
  score.append(number, label);
  const details = document.createElement("dl"); details.className = "dashboard-rebuild-details";
  const add = (term, value) => { const row = document.createElement("div"); const dt = document.createElement("dt"); dt.textContent = term; const dd = document.createElement("dd"); dd.textContent = value; row.append(dt, dd); details.appendChild(row); };
  add("Captain", optimized.captain?.name || "—");
  add("Vice-captain", optimized.vice_captain?.name || "—");
  add("Squad cost", optimized.squad_cost == null ? "—" : `£${Number(optimized.squad_cost).toFixed(1)}m`);
  if (optimized.comparison?.has_imported_squad) add("Versus current team", `${optimized.comparison.players_kept} kept · ${optimized.comparison.changes} changes`);
  const warning = document.createElement("p"); warning.className = "dashboard-rebuild-warning"; warning.textContent = "Use as a wildcard or free-hit planning lens—not as an instruction to take multiple hits.";
  root.append(score, details, warning);
}

function renderSquadRating(data) {
  const rating = data.squad_rating || {};
  const ready = rating.status === "ready" && Number.isFinite(Number(rating.score)) && data.transfers?.decision_safety !== "refresh_required";
  const freshness = data.freshness || {};
  const age = freshness.last_sync ? Date.now() - new Date(freshness.last_sync).getTime() : Infinity;
  const stale = freshness.used_stale_data || !Number.isFinite(age) || age > 12 * 60 * 60 * 1000;
  const root = $("#squad-rating");
  root.classList.toggle("stale", ready && stale);
  text($("#squad-rating-score"), ready ? `${rating.score}/100` : "—");
  const unavailable = {
    squad_required: "Connect your complete 15-player squad and set a captain to see its rating.",
    projection_unavailable: "A current projection is missing for a squad player, so no rating is shown.",
    benchmark_unavailable: "There are too few comparable available players to calculate a fair rating.",
  };
  text($("#squad-rating-subtitle"), ready ? `${rating.horizon}-GW · your squad` : data.transfers?.decision_safety === "refresh_required" ? "Refresh needed" : "Squad needed");
  text($("#squad-rating-method"), ready ? rating.method : data.transfers?.decision_safety === "refresh_required" ? "The rating is withheld until official data is refreshed." : unavailable[rating.status] || "Rating unavailable until the current squad and projections are ready.");
  text($("#squad-rating-xi"), ready ? `${rating.xi_score}/100` : "—");
  text($("#squad-rating-bench"), ready ? `${rating.bench_score}/100` : "—");
  text($("#squad-rating-xpts"), ready ? `${Number(data.current_squad.horizon_expected_points).toFixed(1)} over ${rating.horizon} GW${rating.horizon === 1 ? "" : "s"}` : "—");
  const counts = rating.peer_counts || {};
  text($("#squad-rating-peers"), ready ? `Comparison pool: ${["GKP", "DEF", "MID", "FWD"].map((position) => `${counts[position]} ${position}`).join(" · ")}.` : "", "");
  text($("#squad-rating-freshness"), ready ? `${stale ? "Official data needs refresh; this rating may change. " : "Official data current. "}Snapshot: ${formatDate(freshness.last_sync)}. ${data.manager.imported_gameweek ? `Squad imported from GW${data.manager.imported_gameweek}.` : ""}` : "", "");
}

function renderDashboard(data) {
  state.dashboard = data;
  renderSquadRating(data);
  const horizon = Number(data.filters.horizon);
  $("#dashboard-horizon").value = String(horizon);
  $("#dashboard-risk").value = data.filters.risk;
  $("#strategy-horizon").value = String(horizon);
  $("#strategy-risk").value = data.filters.risk;
  text($("#dashboard-team-name"), data.manager.team_name ? `${data.manager.team_name}: your best move, explained.` : "Your best FPL move, explained.");
  text($("#dashboard-gameweek"), data.planning_event ? `GW${data.planning_event.id}` : "—");
  text($("#dashboard-manager-context"), data.manager.squad_connected ? `Squad from GW${data.manager.imported_gameweek || "—"}` : "Connect your team");
  text($("#dashboard-current-xpts"), data.manager.squad_connected ? Number(data.current_squad.horizon_expected_points).toFixed(1) : "—");
  text($("#dashboard-current-window"), `${horizon}-GW XI + captain`);
  text($("#dashboard-bank"), data.manager.bank == null ? "—" : `£${Number(data.manager.bank).toFixed(1)}m`);
  text($("#dashboard-free-transfers"), data.manager.free_transfers ?? "—");

  const decision = data.decision;
  const card = $("#dashboard-decision");
  card.className = `panel dashboard-decision ${decision.action}`;
  text($("#dashboard-decision-title"), decision.title);
  text($("#dashboard-decision-why"), decision.summary);
  text($("#dashboard-decision-gain"), decision.action === "transfer" ? `${signed(decision.net_gain, " pts")} net gain` : decision.action === "hold" ? "Bank the transfer" : decision.action === "verification_required" ? "Decision withheld" : "Team input required");
  text($("#dashboard-decision-confidence"), decision.confidence ? `Model signal ${Math.round(Number(decision.confidence) * 100)}/100 · heuristic` : "Model signal pending");
  text($("#dashboard-decision-window"), `${horizon}-GW · ${data.filters.risk}`);
  updateDeadlineClock(data.planning_event?.deadline_time);

  const reliability = data.reliability || {};
  const reliabilityValue = reliability.reliability_score;
  text($("#dashboard-reliability"), reliabilityValue == null ? "Collecting" : `${Math.round(Number(reliabilityValue) * 100)}/100`);
  text($("#dashboard-reliability-note"), `${reliability.evaluated_events || 0} evaluated GWs · internal score`);
  renderDashboardChart(data.current_squad.ranking, horizon);
  renderDashboardAlerts(data.current_squad.alerts || []);
  renderDashboardFixtures(data.current_squad.fixture_outlook);
  renderDashboardRebuild(data.optimized_squad, horizon);

  const freshness = data.freshness || {};
  const age = freshness.last_sync ? Date.now() - new Date(freshness.last_sync).getTime() : Infinity;
  const stale = freshness.used_stale_data || !Number.isFinite(age) || age > 12 * 60 * 60 * 1000;
  const sourceStatus = $("#dashboard-data-status");
  sourceStatus.className = `status-pill ${stale ? "warning" : "good"}`;
  text(sourceStatus, stale ? "Refresh recommended" : "Sources current");
  text($("#dashboard-source-note"), `${data.sources.map((source) => source.name).join(" · ")}. ${stale ? "The official snapshot is stale or unverified; refresh before acting." : "Official snapshot is within the 12-hour review window."}`);
  if (data.transfers) renderTransferResult(data.transfers);
}

async function loadDashboard(force = false) {
  if (state.dashboard && !force) {
    renderDashboard(state.dashboard);
    return;
  }
  if (state.dashboardLoading) return state.dashboardLoading;
  const params = new URLSearchParams({
    horizon: $("#dashboard-horizon")?.value || state.profile?.horizon || "6",
    risk: $("#dashboard-risk")?.value || state.profile?.risk_preference || "balanced",
  });
  const submit = $("#dashboard-filters button");
  if (submit) { submit.disabled = true; submit.textContent = "Updating…"; }
  state.dashboardLoading = api(`/api/dashboard?${params}`)
    .then((data) => renderDashboard(data))
    .catch((error) => notice(`Dashboard unavailable: ${error.message}`, true))
    .finally(() => {
      state.dashboardLoading = null;
      if (submit) { submit.disabled = false; submit.textContent = "Update dashboard"; }
    });
  return state.dashboardLoading;
}

async function applyPlanningPreferences(source) {
  const horizon = $(source === "dashboard" ? "#dashboard-horizon" : "#strategy-horizon").value;
  const risk = $(source === "dashboard" ? "#dashboard-risk" : "#strategy-risk").value;
  $("#dashboard-horizon").value = horizon;
  $("#strategy-horizon").value = horizon;
  $("#dashboard-risk").value = risk;
  $("#strategy-risk").value = risk;
  try {
    if (state.profile) {
      const result = await api("/api/planning-preferences", {
        method: "POST",
        body: JSON.stringify({ horizon, risk_preference: risk }),
      });
      state.profile = result.profile;
      $("#horizon").value = horizon;
      $("#risk").value = risk;
    }
    state.dashboard = null;
    state.squadOptimizer = null;
    state.chipPlan = null;
    await loadDashboard(true);
    if (window.location.hash === "#optimizer") await loadSquadOptimizer(true);
    if (window.location.hash === "#chips") await loadChipPlanner(true);
    updateAssistantContext();
  } catch (error) {
    notice(`Planning settings were not saved: ${error.message}`, true);
    if (state.profile) await loadProfile();
  }
}

async function loadTeams() {
  const result = await api("/api/teams");
  state.teams = result.teams;
  const select = $("#player-team");
  const current = select.value;
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "All clubs";
  select.appendChild(all);
  result.teams.forEach((team) => {
    const option = document.createElement("option");
    option.value = team.id;
    option.textContent = team.name;
    select.appendChild(option);
  });
  select.value = current;
  const projectionSelect = $("#projection-team");
  const projectionCurrent = projectionSelect.value;
  projectionSelect.replaceChildren();
  const projectionAll = document.createElement("option");
  projectionAll.value = "";
  projectionAll.textContent = "All clubs";
  projectionSelect.appendChild(projectionAll);
  result.teams.forEach((team) => {
    const option = document.createElement("option");
    option.value = team.id;
    option.textContent = team.name;
    projectionSelect.appendChild(option);
  });
  projectionSelect.value = projectionCurrent;
}

function updateTransferOutSignals() {
  const set = new Set();
  (state.transfers?.suggestions || []).forEach((suggestion) => {
    if (suggestion.action === "transfer" && suggestion.sell?.id !== undefined) {
      set.add(Number(suggestion.sell.id));
    }
  });
  state.transferOutSet = set;
}

async function loadProjectionCache(force = false) {
  const now = Date.now();
  if (!force && now - state.projectionCacheLastLoaded < 60_000) return;
  const cache = {
    1: new Map(),
    3: new Map(),
    6: new Map(),
  };
  const payloads = await Promise.all([1, 3, 6].map(async (horizon) => {
    const params = new URLSearchParams({ horizon: String(horizon), limit: "1000", sort: "xpts" });
    const response = await api(`/api/projections?${params}`);
    return { horizon, rows: response.players || [] };
  }));
  payloads.forEach(({ horizon, rows }) => {
    rows.forEach((row) => cache[horizon].set(Number(row.id), row));
  });
  state.projectionCacheByHorizon = cache;
  state.projectionCacheLastLoaded = now;
}

function expectedPlayerProjection(player, horizon) {
  return Number(state.projectionCacheByHorizon[horizon]?.get(Number(player.id))?.expected_points || 0);
}

function projectionSnapshot(playerId) {
  const cache1 = state.projectionCacheByHorizon[1]?.get(Number(playerId)) || {};
  const cache3 = state.projectionCacheByHorizon[3]?.get(Number(playerId)) || {};
  const cache6 = state.projectionCacheByHorizon[6]?.get(Number(playerId)) || {};
  return {
    player_id: Number(playerId),
    expected_1: Number(cache1.expected_points || 0),
    expected_3: Number(cache3.expected_points || 0),
    expected_6: Number(cache6.expected_points || 0),
    h1: Number(cache1.expected_points || 0),
    h3: Number(cache3.expected_points || 0),
    h6: Number(cache6.expected_points || 0),
    ...cache1,
  };
}

function teamCodeForPlayer(player) {
  const direct = Number(player.team_code || 0);
  if (direct > 0) return direct;
  const team = state.teams.find((item) => item.short_name === player.team);
  return Number(team?.code || 0);
}

function shirtUrlForPlayer(player) {
  const teamCode = teamCodeForPlayer(player);
  if (!teamCode) return "";
  const goalkeeper = resolvePlayerPosition(player.player_position) === "GKP" ? "_1" : "";
  return `https://fantasy.premierleague.com/dist/img/shirts/standard/shirt_${teamCode}${goalkeeper}-66.png`;
}

function outlookFromProjection(player, row = {}) {
  if (player.status !== "a") return { className: "outlook-danger", label: "Injury / rotation risk" };
  const start = Number(row.start_probability || row.startProbability || 0);
  const noPlay = Number(row.no_play_probability || row.noPlayProbability || 0);
  if (start >= 0.75 && noPlay <= 0.10) return { className: "outlook-strong", label: "Strong outlook" };
  if (start >= 0.55 && noPlay <= 0.20) return { className: "outlook-medium", label: "Good run expected" };
  if (start >= 0.40 && noPlay <= 0.35) return { className: "outlook-soft", label: "Moderate concern" };
  return { className: "outlook-weak", label: "Rotation risk" };
}

function makePitchPlayerCard(player, options = {}) {
  const projection = projectionSnapshot(player.id) || {};
  const card = document.createElement("button");
  card.type = "button";
  card.className = `pitch-player-card`;
  card.dataset.playerId = String(player.id ?? "");
  card.setAttribute("aria-label", `Inspect ${player.web_name}, ${resolvePlayerPosition(player.player_position)}, ${player.team}`);
  if (player.is_captain) card.classList.add("captain");
  if (player.is_vice_captain) card.classList.add("vice");
  if (player.status !== "a") card.classList.add("injured");
  if (state.transferOutSet.has(Number(player.id))) card.classList.add("transfer-out");

  if (player.is_captain || player.is_vice_captain) {
    const role = document.createElement("span");
    role.className = "pitch-role";
    role.textContent = player.is_captain ? "C" : "V";
    card.appendChild(role);
  }
  if (player.status !== "a") {
    const availability = document.createElement("span");
    availability.className = "pitch-availability";
    availability.textContent = "!";
    availability.title = player.news || "Availability concern";
    card.appendChild(availability);
  }

  const shirtFrame = document.createElement("span");
  shirtFrame.className = "pitch-shirt-frame";
  const fallback = document.createElement("span");
  fallback.className = "pitch-shirt-fallback";
  fallback.textContent = initialsFromName(player.web_name);
  const shirtUrl = shirtUrlForPlayer(player);
  if (shirtUrl) {
    const shirt = document.createElement("img");
    shirt.className = "pitch-shirt";
    shirt.alt = "";
    shirt.loading = "lazy";
    shirt.decoding = "async";
    shirt.src = shirtUrl;
    shirt.addEventListener("load", () => card.classList.add("shirt-loaded"), { once: true });
    shirt.addEventListener("error", () => shirt.remove(), { once: true });
    shirtFrame.append(shirt, fallback);
  } else {
    shirtFrame.appendChild(fallback);
  }

  const name = document.createElement("span");
  name.className = "pitch-nameplate";
  name.textContent = player.web_name;

  const expected = document.createElement("span");
  expected.className = "pitch-statplate";
  const gw = Number(projection.h1 || 0);
  const six = Number(projection.h6 || 0);
  const priceLabel = document.createElement("span");
  priceLabel.className = "pitch-price";
  priceLabel.textContent = `£${Number(player.current_price).toFixed(1)}m`;
  const pointsLabel = document.createElement("span");
  pointsLabel.className = "pitch-points";
  pointsLabel.textContent = six > 0 ? `${gw.toFixed(1)} pts` : "Syncing";
  expected.setAttribute("aria-label", `${priceLabel.textContent}, ${pointsLabel.textContent} expected next gameweek`);
  expected.append(priceLabel, pointsLabel);

  const extra = document.createElement("span");
  extra.className = "pitch-player-extra";
  extra.textContent = six > 0
    ? `${player.team} · £${Number(player.current_price).toFixed(1)}m · 6GW ${six.toFixed(1)}`
    : `${player.team} · £${Number(player.current_price).toFixed(1)}m`;

  card.title = player.status === "a"
    ? `${player.web_name}: ${gw.toFixed(1)} xPts next GW, ${six.toFixed(1)} over 6 GWs`
    : `${player.web_name}: ${player.news || "availability concern"}`;
  card.append(shirtFrame, name, expected, extra);
  if (options.inspect !== false) {
    card.addEventListener("click", () => loadTeamPlayerDetail(player.id, player.web_name, card));
  }
  return card;
}

function placeLine(canvas, players, yPercent, lineName, span = 82, cardOptions = {}) {
  const slots = players.length || 1;
  players.forEach((player, index) => {
    const card = makePitchPlayerCard(player, { ...cardOptions, ...(lineName.startsWith("SUB") ? { benchLabel: lineName } : {}) });
    card.style.left = `${slotPositions(slots, span)[index]}%`;
    card.style.top = `${yPercent}%`;
    card.style.setProperty("--pitch-y", `${yPercent}%`);
    card.dataset.slotLine = lineName;
    canvas.appendChild(card);
  });
}

function renderFormationSelector(starters) {
  const select = $("#formation-select");
  if (!select) return;
  const selected = state.selectedFormation;
  const current = localStorage.getItem(SAVED_FORMATION_KEY) || selected || formationFromLineup(starters);
  select.replaceChildren();
  FORMATIONS.forEach((formation) => {
    const option = document.createElement("option");
    option.value = formation.key;
    option.textContent = formation.key;
    option.disabled = !isFormationAllowed(formation.key, starters);
    option.title = option.disabled ? "Not supported by current starting XI position counts" : "";
    select.appendChild(option);
  });
  const exact = FORMATIONS.find((formation) => formation.key === current) ? current : formationFromLineup(starters);
  state.selectedFormation = isFormationAllowed(exact, starters) ? exact : formationFromLineup(starters);
  select.value = state.selectedFormation;
  select.disabled = !starters.length;
  if (!isFormationAllowed(state.selectedFormation, starters)) {
    select.value = formationFromLineup(starters);
  }
  localStorage.setItem(SAVED_FORMATION_KEY, state.selectedFormation);
}

function validateLineupDisplay(starting, bench, field, lineupRows = {}) {
  const starterCount = starting.length === 11;
  const benchCount = (bench || []).filter(Boolean).length >= 4;
  let overlapRisk = false;
  if (field && lineUpHasPlayers(lineupRows)) {
    const rows = [...field.querySelectorAll(".pitch-player-card")].reduce((groups, card) => {
      const key = card.dataset.slotLine || "unknown";
      if (!groups[key]) groups[key] = [];
      groups[key].push(card.getBoundingClientRect());
      return groups;
    }, {});
    overlapRisk = Object.values(rows).some((row) => row.some((box, index) => row.some((other, otherIndex) => (
      index !== otherIndex && box.left < other.right - 2 && box.right > other.left + 2
    ))));
  }
  return {
    allVisible: starterCount && benchCount && !overlapRisk,
    benchMissing: !benchCount,
    overlapRisk,
  };
}

function lineUpHasPlayers(lineupRows = {}) {
  return (lineupRows.GK?.length || 0) + (lineupRows.DEF?.length || 0) + (lineupRows.MID?.length || 0) + (lineupRows.FWD?.length || 0) > 0;
}

function updateTeamSummary(stats, lineupMeta) {
  text($("#team-expected-gw"), stats.expectedGw);
  text($("#team-expected-6"), stats.expected6Gw);
  text($("#team-value"), stats.teamValue);
  text($("#team-bank"), stats.bank);
  text($("#team-free-transfers"), stats.freeTransfers);
  text($("#pitch-formation"), stats.formation);
  if (lineupMeta?.benchMissing || !lineupMeta?.allVisible) {
    text($("#pitch-hint"), "Alignment adjusted to keep all 15 players visible.");
  }
}

function computeTeamSummary(squad) {
  const profile = state.profile || {};
  let expectedGw = 0;
  let expected6 = 0;
  let teamValue = 0;
  squad.forEach((player) => {
    teamValue += Number(player.current_price || 0);
    const row = projectionSnapshot(player.id);
    const gw = Number(row?.expected_1 || row?.h1 || 0);
    const six = Number(row?.expected_6 || row?.h6 || 0);
    expectedGw += gw;
    expected6 += six;
  });
  return {
    expectedGw: squad.length ? expectedGw.toFixed(1) : "—",
    expected6Gw: squad.length ? expected6.toFixed(1) : "—",
    teamValue: squad.length ? `£${teamValue.toFixed(1)}m` : "—",
    bank: profile && profile.bank !== null && profile.bank !== undefined ? `£${Number(profile.bank).toFixed(1)}m` : "—",
    freeTransfers: profile && profile.free_transfers !== null && profile.free_transfers !== undefined ? String(profile.free_transfers) : "—",
    formation: state.selectedFormation,
  };
}

function renderTeamTransfers() {
  const root = $("#team-transfer-strip");
  const context = $("#team-transfer-context");
  const weakness = $("#team-weakness");
  if (!root || !context || !weakness) return;
  root.replaceChildren();
  weakness.textContent = "";
  if (!state.transfers || !state.transfers.suggestions || !state.transfers.suggestions.length) {
    context.textContent = "Waiting for optimizer";
    return;
  }
  if (state.transfers.decision_safety === "refresh_required") {
    context.textContent = "Decision withheld · refresh official data";
    const message = document.createElement("p");
    message.className = "empty";
    message.textContent = "Transfer opportunities are hidden until official data is refreshed.";
    root.appendChild(message);
    weakness.textContent = "NO CURRENT ASSESSMENT · Refresh official data.";
    return;
  }
  const suggestions = state.transfers.suggestions;
  context.textContent = `GW${state.transfers.planning_event} · ${state.transfers.horizon}-GW plan · ${state.transfers.free_transfers} free transfer${state.transfers.free_transfers === 1 ? "" : "s"} · choose one alternative · extra moves −4 pts each${state.transfers.decision_safety === "estimate_only" ? " · affordability estimated" : ""}`;
  suggestions.slice(0, 3).forEach((suggestion, index) => root.appendChild(transferCard(suggestion, state.transfers.horizon, index + 1)));

  const weaknessCandidate = suggestions.find((suggestion) => suggestion.action === "transfer" && suggestion.recommended);
  if (!weaknessCandidate) {
    const bestAlternative = suggestions.find((suggestion) => suggestion.action === "transfer");
    weakness.textContent = bestAlternative
      ? `NO TRANSFER REQUIRED · Best available route is only ${signed(bestAlternative.net_expected_gain, " pts")} across ${state.transfers.horizon} GWs and does not clear the ${Number(state.transfers.decision_threshold).toFixed(1)}-point action threshold.`
      : "NO TRANSFER REQUIRED · No positive expected-value legal route is available.";
    return;
  }
  const sellGap = Number(weaknessCandidate.sell.expected_points || 0);
  const buyGap = Number(weaknessCandidate.buy.expected_points || 0);
  const difference = buyGap - sellGap;
  weakness.textContent = `BIGGEST WEAKNESS · ${weaknessCandidate.sell.name} (${weaknessCandidate.sell.team}) is projected ${difference >= 0 ? "behind" : "ahead"} the benchmark by ${Math.abs(difference).toFixed(1)} pts over ${state.transfers.horizon} GWs.`;
  const rec = document.createElement("p");
  rec.className = "team-weakness-recommendation";
  rec.textContent = `RECOMMENDED FIX: ${weaknessCandidate.sell.name} → ${weaknessCandidate.buy.name}`;
  weakness.prepend(rec);
}

function setSquadDisplayMode(mode = "pitch") {
  const nextMode = mode === "list" ? "list" : "pitch";
  state.squadDisplayMode = nextMode;
  const hasSquad = (state.currentSquad || []).length > 0;
  $("#pitch-board")?.classList.toggle("hidden", nextMode !== "pitch");
  $("#team-summary")?.classList.toggle("hidden", !hasSquad || nextMode !== "list");
  const pitchButton = $("#pitch-view-button");
  const listButton = $("#list-view-button");
  pitchButton?.classList.toggle("active", nextMode === "pitch");
  listButton?.classList.toggle("active", nextMode === "list");
  pitchButton?.setAttribute("aria-pressed", String(nextMode === "pitch"));
  listButton?.setAttribute("aria-pressed", String(nextMode === "list"));
}

function renderSquad(squad) {
  const pitchBoard = $("#pitch-board");
  const pitchHint = $("#pitch-hint");
  const summary = $("#team-summary");
  const body = $("#squad-body");
  summary.classList.toggle("hidden", !squad.length);
  body.replaceChildren();
  pitchBoard.replaceChildren();
  state.currentSquad = squad;
  updateTransferOutSignals();

  if (!squad.length) {
    text(pitchHint, "Import your team to view the layout.");
    const empty = document.createElement("p");
    empty.className = "pitch-empty";
    empty.textContent = "No squad loaded. Save your Team ID above to generate the pitch.";
    pitchBoard.appendChild(empty);
    summary.classList.add("hidden");
    updateTeamSummary({ expectedGw: "—", expected6Gw: "—", teamValue: "—", bank: "—", freeTransfers: "—", formation: "—" }, { allVisible: false });
    renderTeamTransfers();
    return;
  }

  const starters = [...squad].filter((player) => Number(player.squad_position) <= 11).sort((a, b) => Number(a.squad_position) - Number(b.squad_position));
  const bench = [...squad].filter((player) => Number(player.squad_position) > 11).sort((a, b) => Number(a.squad_position) - Number(b.squad_position));
  const impliedFormation = formationFromLineup(starters);
  state.selectedFormation = formationFromLineup(starters);
  if (localStorage.getItem(SAVED_FORMATION_KEY) && isFormationAllowed(localStorage.getItem(SAVED_FORMATION_KEY), starters)) {
    state.selectedFormation = localStorage.getItem(SAVED_FORMATION_KEY);
  } else if (isFormationAllowed(impliedFormation, starters)) {
    state.selectedFormation = impliedFormation;
  }

  renderFormationSelector(starters);
  const lineup = resolveLineupFromFormation(starters, state.selectedFormation);
  const expectedStarters = lineup.GK.length + lineup.DEF.length + lineup.MID.length + lineup.FWD.length;
  const benchPlayers = [...bench];
  while (benchPlayers.length < 4) benchPlayers.push(null);
  const field = document.createElement("div");
  field.className = "pitch-field";
  const squadLayer = document.createElement("div");
  squadLayer.className = "pitch-squad";

  const benchWrap = document.createElement("div");
  benchWrap.className = "pitch-bench";
  const benchTitle = document.createElement("div");
  benchTitle.className = "pitch-bench-title";
  benchTitle.textContent = "BENCH";
  const benchTrack = document.createElement("div");
  benchTrack.className = "pitch-bench-track";
  for (let index = 0; index < 4; index += 1) {
    const player = benchPlayers[index];
    const slot = document.createElement("div");
    slot.className = "pitch-bench-slot";
    if (!player) {
      const ghost = document.createElement("div");
      ghost.className = "pitch-player-card muted";
      ghost.textContent = index === 0 ? "GKP slot" : `Sub ${index} slot`;
      slot.appendChild(ghost);
      benchTrack.appendChild(slot);
      continue;
    }
    const label = document.createElement("span");
    label.className = "pitch-bench-order";
    label.textContent = index === 0 ? "GKP" : `${index}. ${resolvePlayerPosition(player.player_position)}`;
    const card = makePitchPlayerCard(player);
    slot.append(label, card);
    benchTrack.appendChild(slot);
  }
  benchWrap.append(benchTitle, benchTrack);

  const container = document.createElement("div");
  container.className = "pitch-layout";
  container.append(field, benchWrap);
  pitchBoard.appendChild(container);

  const lineMap = [
    [lineup.GK.slice(0, 1), 17, 11],
    [lineup.DEF, 39, lineup.DEF.length],
    [lineup.MID, 61, lineup.MID.length],
    [lineup.FWD, 83, lineup.FWD.length],
  ];
  const maxRowCount = lineMap.reduce((acc, [, , count]) => Math.max(acc, count), 1);
  const span = clamp(84 - (maxRowCount - 1) * 2, 74, 88);
  field.appendChild(squadLayer);
  field.style.setProperty("--pitch-line-span", `${span}`);
  placeLine(squadLayer, lineMap[0][0], lineMap[0][1], "GK", span);
  placeLine(squadLayer, lineMap[1][0], lineMap[1][1], "DEF", span);
  placeLine(squadLayer, lineMap[2][0], lineMap[2][1], "MID", span);
  placeLine(squadLayer, lineMap[3][0], lineMap[3][1], "FWD", span);

  estimatePitchScale(field, lineup);
  mirrorPitchScale(field, container);
  mirrorPitchScale(field, benchWrap);
  const validation = validateLineupDisplay(starters, benchPlayers, field, lineup);
  const benchText = `${Math.min(bench.length, 4)} / 4`;
  text(pitchHint, `Formation ${state.selectedFormation} · ${expectedStarters} starters · ${benchText} bench`);

  if (!validation.allVisible) {
    const noticeItem = document.createElement("p");
    noticeItem.className = "pitch-validation";
    noticeItem.textContent = validation.overlapRisk ? "Auto-layout scaled for complete visibility on this screen." : "Auto-layout adjusted for complete visibility.";
    field.appendChild(noticeItem);
  }

  const summaryStats = computeTeamSummary(squad);
  updateTeamSummary(summaryStats, validation);
  renderTeamTransfers();
  $("#team-stats")?.classList.remove("hidden");
  setSquadDisplayMode(state.squadDisplayMode);

  squad.forEach((player) => {
    const row = document.createElement("tr");
    td(row, player.web_name, "player-name");
    td(row, player.player_position);
    td(row, player.team);
    td(row, `£${Number(player.current_price).toFixed(1)}m`);
    td(row, player.status === "a" ? "Available" : (player.news || player.status));
    td(row, player.total_points);
    let role = player.squad_position > 11 ? `Bench ${player.squad_position - 11}` : "Starter";
    if (player.is_captain) role = "Captain";
    else if (player.is_vice_captain) role = "Vice-captain";
    td(row, role, player.is_captain ? "captain" : "subtle");
    body.appendChild(row);
  });
}

function optimizerPitchPlayer(player) {
  return {
    id: Number(player.id),
    web_name: player.name,
    player_position: player.position,
    team: player.team,
    current_price: Number(player.price),
    status: "a",
    is_captain: player.role === "captain",
    is_vice_captain: player.role === "vice-captain",
  };
}

function renderOptimizerComparison(result) {
  const root = $("#optimizer-comparison");
  root.replaceChildren();
  const comparison = result.comparison || {};
  if (!comparison.has_imported_squad) {
    const message = document.createElement("p");
    message.className = "optimizer-note";
    message.textContent = "This is a clean £100.0m build. Import your current team to see who would be kept, sold and bought.";
    root.appendChild(message);
    return;
  }
  const headline = document.createElement("div");
  headline.className = "optimizer-change-summary";
  const kept = document.createElement("strong"); kept.textContent = `${comparison.players_kept} kept`;
  const changed = document.createElement("span"); changed.textContent = `${comparison.changes} change${comparison.changes === 1 ? "" : "s"} · cap ${result.maximum_recommended_changes ?? comparison.changes}`;
  headline.append(kept, changed);

  const lists = document.createElement("div");
  lists.className = "optimizer-change-lists";
  const addList = (label, players, className) => {
    const section = document.createElement("div");
    const title = document.createElement("span"); title.className = "optimizer-change-label"; title.textContent = label;
    const chips = document.createElement("div"); chips.className = "optimizer-chips";
    (players || []).forEach((player) => {
      const chip = document.createElement("span");
      chip.className = `optimizer-chip ${className}`;
      chip.textContent = player.name;
      chips.appendChild(chip);
    });
    section.append(title, chips);
    lists.appendChild(section);
  };
  addList("OUT", comparison.transfers_out, "out");
  addList("IN", comparison.transfers_in, "in");
  root.append(headline, lists);
}

function renderOptimizerPitch(result) {
  const root = $("#optimizer-pitch");
  root.replaceChildren();
  const starters = (result.starting_xi || []).map(optimizerPitchPlayer);
  const bench = (result.bench || []).map(optimizerPitchPlayer);
  const lineup = {
    GK: starters.filter((player) => resolvePlayerPosition(player.player_position) === "GKP"),
    DEF: starters.filter((player) => player.player_position === "DEF"),
    MID: starters.filter((player) => player.player_position === "MID"),
    FWD: starters.filter((player) => player.player_position === "FWD"),
  };
  const field = document.createElement("div"); field.className = "pitch-field";
  const squadLayer = document.createElement("div"); squadLayer.className = "pitch-squad";
  field.appendChild(squadLayer);
  const rows = [
    [lineup.GK, 17, "GK"],
    [lineup.DEF, 39, "DEF"],
    [lineup.MID, 61, "MID"],
    [lineup.FWD, 83, "FWD"],
  ];
  const maxRow = Math.max(...rows.map(([players]) => players.length), 1);
  const span = clamp(84 - (maxRow - 1) * 2, 74, 88);
  rows.forEach(([players, y, label]) => placeLine(squadLayer, players, y, label, span, { inspect: false }));

  const benchWrap = document.createElement("div"); benchWrap.className = "pitch-bench";
  const benchTitle = document.createElement("div"); benchTitle.className = "pitch-bench-title"; benchTitle.textContent = "BENCH ORDER";
  const benchTrack = document.createElement("div"); benchTrack.className = "pitch-bench-track";
  bench.forEach((player, index) => {
    const slot = document.createElement("div"); slot.className = "pitch-bench-slot";
    const label = document.createElement("span"); label.className = "pitch-bench-order";
    label.textContent = index === 0 && player.player_position === "GKP" ? "GKP" : `${index}. ${player.player_position}`;
    slot.append(label, makePitchPlayerCard(player, { inspect: false }));
    benchTrack.appendChild(slot);
  });
  benchWrap.append(benchTitle, benchTrack);
  const layout = document.createElement("div"); layout.className = "pitch-layout"; layout.append(field, benchWrap);
  root.appendChild(layout);
  estimatePitchScale(field, lineup);
  mirrorPitchScale(field, layout);
  mirrorPitchScale(field, benchWrap);
}

function renderSquadOptimizer(result) {
  state.squadOptimizer = result;
  const summary = $("#optimizer-summary");
  const context = $("#optimizer-context");
  const pitch = $("#optimizer-pitch");
  if (!result || result.status !== "ready") {
    const message = result?.message || "The optimizer could not produce a squad.";
    text(summary, result?.data_warnings?.length ? `${message} ${result.data_warnings.join("; ")}` : message);
    text(context, result?.status === "verification-required" ? "Decision withheld · verify My team" : "Needs projections");
    pitch.replaceChildren();
    const empty = document.createElement("p"); empty.className = "pitch-empty"; empty.textContent = message;
    pitch.appendChild(empty);
    $("#optimizer-comparison").replaceChildren();
    $("#optimizer-constraints").replaceChildren();
    $("#optimizer-metrics").querySelectorAll(".metric strong, .metric small").forEach((node) => { node.textContent = "—"; });
    return;
  }
  const cooldown = result.wildcard_cooldown ? " A Wildcard was used last Gameweek, so the model applies a stronger continuity lock." : "";
  text(summary, `Continuity-first ${result.horizon}-GW plan: ${result.comparison?.players_kept ?? 0} current players protected, at most ${result.maximum_recommended_changes} change${result.maximum_recommended_changes === 1 ? "" : "s"}, with a ${Number(result.continuity_penalty_per_change || 0).toFixed(1)}-point internal churn penalty for replacing an owned player.${cooldown}`);
  text(context, `GW${result.planning_event} · ${result.formation} · ${result.method}`);
  const metrics = [...$("#optimizer-metrics").querySelectorAll(".metric")];
  text(metrics[0].querySelector("strong"), `£${Number(result.squad_cost).toFixed(1)}m`);
  text(metrics[0].querySelector("small"), `£${Number(result.bank_remaining).toFixed(1)}m remaining`);
  text(metrics[1].querySelector("strong"), Number(result.total_expected_points).toFixed(1));
  text(metrics[1].querySelector("small"), `${result.horizon}-GW XI + captain`);
  text(metrics[2].querySelector("strong"), result.formation);
  text(metrics[2].querySelector("small"), `${result.captain.name} (C) · ${result.vice_captain.name} (V)`);
  text(metrics[3].querySelector("strong"), `${Math.round(Number(result.confidence) * 100)}/100`);
  text(metrics[3].querySelector("small"), "heuristic model signal, not success probability");
  renderOptimizerPitch(result);
  renderOptimizerComparison(result);
  const constraints = $("#optimizer-constraints");
  constraints.replaceChildren();
  addDefinition(constraints, "Squad", "2 GKP · 5 DEF · 5 MID · 3 FWD");
  addDefinition(constraints, "Budget", `${result.budget_assumption} · £${Number(result.budget).toFixed(1)}m`);
  addDefinition(constraints, "Club limit", "Maximum 3 players per club");
  addDefinition(constraints, "Starting XI", `Best legal ${result.formation} formation`);
  addDefinition(constraints, "Availability", result.constraints.availability_filter);
}

async function loadSquadOptimizer(force = false) {
  if (state.squadOptimizer && !force) {
    renderSquadOptimizer(state.squadOptimizer);
    return;
  }
  const button = $("#run-squad-optimizer");
  button.disabled = true;
  button.textContent = "Optimizing 15 slots…";
  text($("#optimizer-summary"), "Searching legal combinations, formations, bench orders and captaincy choices…");
  try {
    await loadProjectionCache();
    const params = new URLSearchParams({
      horizon: $("#strategy-horizon")?.value || state.profile?.horizon || "6",
      risk: $("#strategy-risk")?.value || state.profile?.risk_preference || "balanced",
    });
    const result = await api(`/api/squad-optimizer?${params}`);
    renderSquadOptimizer(result);
  } catch (error) {
    renderSquadOptimizer({ status: "error", message: error.message });
    notice(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Run squad optimizer";
  }
}

async function loadProfile() {
  const result = await api("/api/profile");
  state.profile = result.profile;
  if (result.profile) {
    const p = result.profile;
    $("#team-id").value = p.team_id ?? "";
    $("#my-team-source-link").href = p.team_id
      ? `https://fantasy.premierleague.com/api/my-team/${encodeURIComponent(p.team_id)}/`
      : "https://fantasy.premierleague.com/";
    $("#current-gameweek").value = p.current_gameweek ?? "";
    $("#bank").value = p.bank ?? "";
    $("#free-transfers").value = p.free_transfers ?? 1;
    $("#horizon").value = p.horizon ?? 6;
    $("#risk").value = p.risk_preference ?? "balanced";
    $("#strategy-horizon").value = p.horizon ?? 6;
    $("#strategy-risk").value = p.risk_preference ?? "balanced";
    if (!state.dashboard) {
      $("#dashboard-horizon").value = p.horizon ?? 6;
      $("#dashboard-risk").value = p.risk_preference ?? "balanced";
    }
    text($("#team-name"), p.team_name || "Imported team");
    text($("#imported-gw"), p.imported_gameweek ? `Squad from GW${p.imported_gameweek}` : "Preferences saved");
    const warning = $("#team-warning");
    warning.classList.toggle("hidden", !p.source_warning);
    text(warning, p.source_warning, "");
    const pendingStatus = $("#pending-transfer-status");
    if (pendingStatus) {
      pendingStatus.textContent = Number(p.imported_gameweek || 0) < Number(p.current_gameweek || 0)
        ? `Official picks are available only through GW${p.imported_gameweek}. Record any completed GW${p.current_gameweek} transfer below to update this local squad now.`
        : `The imported squad is published through GW${p.imported_gameweek || p.current_gameweek || "—"}. Use this only for a newer transfer that official FPL has not exposed.`;
    }
  }
  try {
    await loadProjectionCache();
  } catch (error) {
    notice(`Pitch projections are temporarily stale: ${error.message}`, true);
  }
  renderSquad(result.squad || []);
  renderManagerVerification(result.profile, result.squad || []);
  populateManualTransferControls();
  updateAssistantContext();
}

function renderManagerVerification(profile, squad) {
  const root = $("#manager-verification-prices");
  if (!root) return;
  root.replaceChildren();
  $("#verified-bank").value = profile?.bank ?? "";
  $("#verified-free-transfers").value = profile?.free_transfers ?? "";
  $("#verified-squad-current").checked = false;
  for (const player of squad) {
    const label = document.createElement("label");
    const name = document.createElement("span");
    name.textContent = `${player.web_name} · £${Number(player.current_price).toFixed(1)}m market`;
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0.1";
    input.max = String(player.current_price);
    input.step = "0.1";
    input.required = true;
    input.dataset.playerId = String(player.id);
    input.setAttribute("aria-label", `${player.web_name} exact selling price in millions`);
    input.placeholder = "Selling price";
    if (player.selling_price != null) input.value = (Number(player.selling_price) / 10).toFixed(1);
    label.append(name, input);
    root.appendChild(label);
  }
  const confirmed = profile?.manager_confirmation;
  text($("#manager-verification-status"), confirmed ? `Prices synced ${formatDate(confirmed.confirmed_at)}` : "Optional price sync");
  $("#manager-verification-status").className = `status-pill ${confirmed ? "good" : "neutral"}`;
  $("#manager-verification-form button").disabled = squad.length !== 15;
}

async function submitManagerVerification(event) {
  event.preventDefault();
  const button = $("#manager-verification-form button");
  const sellingPrices = {};
  $$("#manager-verification-prices input").forEach((input) => { sellingPrices[input.dataset.playerId] = input.value; });
  button.disabled = true;
  button.textContent = "Verifying…";
  try {
    await api("/api/confirm-manager-context", {
      method: "POST",
      body: JSON.stringify({
        bank: $("#verified-bank").value,
        free_transfers: $("#verified-free-transfers").value,
        selling_prices: sellingPrices,
        squad_current: $("#verified-squad-current").checked,
      }),
    });
    state.dashboard = null;
    state.chipPlan = null;
    state.squadOptimizer = null;
    state.ledger = null;
    await loadProfile();
    await loadDashboard(true);
    notice("Manager inputs confirmed for this planning gameweek. Check the decision-readiness status before acting.");
  } catch (error) { notice(`Could not confirm inputs: ${error.message}`, true); }
  finally { button.disabled = false; button.textContent = "Confirm decision inputs"; }
}

async function submitMyTeamSnapshot(event) {
  event.preventDefault();
  const button = $("#my-team-snapshot-form button");
  let snapshot;
  try {
    snapshot = JSON.parse($("#my-team-snapshot").value.trim());
  } catch {
    notice("The pasted FPL data is not valid JSON. Copy the entire response from the official My Team data page.", true);
    return;
  }
  button.disabled = true;
  button.textContent = "Importing live team…";
  try {
    await api("/api/import-my-team-snapshot", {
      method: "POST",
      body: JSON.stringify({ snapshot }),
    });
    $("#my-team-snapshot").value = "";
    state.dashboard = null;
    state.chipPlan = null;
    state.squadOptimizer = null;
    state.ledger = null;
    await loadProfile();
    await loadDashboard(true);
    notice("Live FPL squad, selling prices, bank and free transfers imported. Decision inputs are now ready if official data is current.");
  } catch (error) {
    notice(`Could not import FPL My Team data: ${error.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = "Import live team in one step";
  }
}

function populateManualTransferControls() {
  const outgoing = $("#manual-transfer-out");
  const incoming = $("#manual-transfer-in");
  const submit = $("#manual-transfer-form button");
  if (!outgoing || !incoming || !submit) return;
  const selected = outgoing.value;
  outgoing.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = state.currentSquad.length ? "Choose player" : "Import squad first";
  outgoing.appendChild(placeholder);
  [...state.currentSquad]
    .sort((a, b) => Number(a.squad_position) - Number(b.squad_position))
    .forEach((player) => {
      const option = document.createElement("option");
      option.value = player.id;
      option.textContent = `${player.web_name} · ${resolvePlayerPosition(player.player_position)} · ${player.team}`;
      outgoing.appendChild(option);
    });
  outgoing.value = [...outgoing.options].some((option) => option.value === selected) ? selected : "";
  outgoing.disabled = !state.currentSquad.length;
  submit.disabled = !state.currentSquad.length;
  populateManualTransferTargets();
}

function populateManualTransferTargets() {
  const outgoing = $("#manual-transfer-out");
  const incoming = $("#manual-transfer-in");
  if (!outgoing || !incoming) return;
  const seller = state.currentSquad.find((player) => Number(player.id) === Number(outgoing.value));
  incoming.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = seller ? `Choose ${resolvePlayerPosition(seller.player_position)}` : "Choose outgoing player first";
  incoming.appendChild(placeholder);
  incoming.disabled = !seller;
  if (!seller) return;
  const owned = new Set(state.currentSquad.map((player) => Number(player.id)));
  const position = resolvePlayerPosition(seller.player_position);
  [...state.projectionCacheByHorizon[6].values()]
    .filter((player) => resolvePlayerPosition(player.position) === position && !owned.has(Number(player.id)))
    .sort((a, b) => String(a.web_name).localeCompare(String(b.web_name)))
    .forEach((player) => {
      const option = document.createElement("option");
      option.value = player.id;
      option.textContent = `${player.web_name} · ${player.team} · £${Number(player.price).toFixed(1)}m`;
      incoming.appendChild(option);
    });
}

async function submitManualTransfer(event) {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  button.textContent = "Updating…";
  try {
    const result = await api("/api/manual-transfer", {
      method: "POST",
      body: JSON.stringify({
        sell_player_id: Number($("#manual-transfer-out").value),
        buy_player_id: Number($("#manual-transfer-in").value),
      }),
    });
    state.squadOptimizer = null;
    state.dashboard = null;
    state.chipPlan = null;
    state.ledger = null;
    notice(`Local squad updated: ${result.sell.name} → ${result.buy.name}.`);
    await loadProfile();
    await loadTransfers();
    await loadDashboard(true);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Update local squad";
  }
}

function updateAssistantContext(context = null) {
  const label = $("#assistant-context-label");
  if (!label) return;
  const event = context?.planning_event || state.status?.planning_event?.id || state.status?.planning_event || "—";
  const horizon = context?.horizon || state.dashboard?.filters?.horizon || state.profile?.horizon || 6;
  const risk = context?.risk || state.dashboard?.filters?.risk || state.profile?.risk_preference || "balanced";
  const team = state.profile?.team_name || "Imported squad";
  label.textContent = `${team} · GW${event} · ${horizon}-GW · ${risk}`;
}

function assistantQuestionButton(question, compact = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = compact ? "assistant-follow-up" : "assistant-starter";
  button.textContent = question;
  button.addEventListener("click", () => askAssistant(question));
  return button;
}

function assistantScenarioLabel(player) {
  return `${player.web_name} · ${player.position} · ${player.team} · £${Number(player.price).toFixed(1)}m`;
}

function populateAssistantDatalist(selector, players) {
  const root = $(selector);
  root.replaceChildren();
  players.forEach((player) => {
    const option = document.createElement("option");
    option.value = assistantScenarioLabel(player);
    root.appendChild(option);
  });
}

function resolveAssistantScenarioPlayer(input, players) {
  const typed = String(input.value || "").trim().toLocaleLowerCase();
  const matches = players.filter((player) => assistantScenarioLabel(player).toLocaleLowerCase() === typed || player.web_name.toLocaleLowerCase() === typed);
  if (matches.length === 1) {
    input.setCustomValidity("");
    return matches[0];
  }
  input.setCustomValidity("Choose one player from the suggested list.");
  input.reportValidity();
  return null;
}

function assistantScenarioOwnedPlayers() {
  const ids = new Set(state.assistantScenarioSquad.map((player) => Number(player.id)));
  return state.assistantScenarioPlayers.filter((player) => ids.has(Number(player.id)));
}

function assistantScenarioStarterPlayers() {
  const ids = new Set(state.assistantScenarioSquad.filter((player) => Number(player.squad_position) <= 11).map((player) => Number(player.id)));
  return state.assistantScenarioPlayers.filter((player) => ids.has(Number(player.id)));
}

function updateAssistantScenarioBuyOptions() {
  const sellerInput = $("#assistant-scenario-sell");
  const seller = state.assistantScenarioPlayers.find((player) => assistantScenarioLabel(player).toLocaleLowerCase() === sellerInput.value.trim().toLocaleLowerCase() || player.web_name.toLocaleLowerCase() === sellerInput.value.trim().toLocaleLowerCase());
  const owned = new Set(state.assistantScenarioSquad.map((player) => Number(player.id)));
  const eligible = seller
    ? state.assistantScenarioPlayers.filter((player) => !owned.has(Number(player.id)) && Number(player.position_id) === Number(seller.position_id))
    : [];
  populateAssistantDatalist("#assistant-buy-options", eligible);
  $("#assistant-scenario-buy").value = "";
}

function updateAssistantScenarioForm() {
  const type = $("#assistant-scenario-type").value;
  const visible = {
    transfer_routes: [],
    transfer_swap: ["sell", "buy"],
    compare_players: ["left", "right"],
    captain_swap: ["captain"],
  }[type] || [];
  $$("[data-scenario-field]").forEach((field) => {
    const active = visible.includes(field.dataset.scenarioField);
    field.classList.toggle("hidden", !active);
    field.querySelector("input").disabled = !active;
  });
  const labels = {
    transfer_routes: "Compare routes",
    transfer_swap: "Test transfer",
    compare_players: "Compare players",
    captain_swap: "Test captain",
  };
  $("#assistant-scenario-form .primary").textContent = labels[type];
  const status = $("#assistant-scenario-status");
  status.textContent = type === "transfer_routes"
    ? "Uses the current free-transfer count, hit cost and optimizer routes."
    : state.assistantScenarioPlayers.length
      ? "Type a name and choose a matching player from the suggestions."
      : "Loading current player data…";
}

async function loadAssistantScenarioData() {
  await loadProjectionCache();
  const squad = await api("/api/squad");
  state.assistantScenarioSquad = squad.squad || [];
  state.assistantScenarioPlayers = [...(state.projectionCacheByHorizon[6]?.values() || [])]
    .sort((left, right) => left.web_name.localeCompare(right.web_name));
  populateAssistantDatalist("#assistant-player-options", state.assistantScenarioPlayers);
  populateAssistantDatalist("#assistant-squad-options", assistantScenarioOwnedPlayers());
  populateAssistantDatalist("#assistant-starter-options", assistantScenarioStarterPlayers());
  updateAssistantScenarioBuyOptions();
  updateAssistantScenarioForm();
}

function assistantScenarioSelection(selector, players) {
  return resolveAssistantScenarioPlayer($(selector), players);
}

function submitAssistantScenario() {
  const type = $("#assistant-scenario-type").value;
  const all = state.assistantScenarioPlayers;
  const owned = assistantScenarioOwnedPlayers();
  const starters = assistantScenarioStarterPlayers();
  const ownedIds = new Set(owned.map((player) => Number(player.id)));
  let scenario;
  let question;
  if (type === "transfer_routes") {
    scenario = { type };
    question = "Should I roll, make one move, or take a hit?";
  } else if (type === "transfer_swap") {
    const sell = assistantScenarioSelection("#assistant-scenario-sell", owned);
    if (!sell) return;
    const buy = assistantScenarioSelection("#assistant-scenario-buy", all.filter((player) => !ownedIds.has(Number(player.id)) && Number(player.position_id) === Number(sell.position_id)));
    if (!buy) return;
    scenario = { type, sell_id: sell.id, buy_id: buy.id };
    question = `What if I sell ${sell.web_name} for ${buy.web_name}?`;
  } else if (type === "compare_players") {
    const left = assistantScenarioSelection("#assistant-scenario-left", all);
    if (!left) return;
    const right = assistantScenarioSelection("#assistant-scenario-right", all);
    if (!right) return;
    scenario = { type, left_id: left.id, right_id: right.id };
    question = `Compare ${left.web_name} and ${right.web_name}.`;
  } else if (type === "captain_swap") {
    const player = assistantScenarioSelection("#assistant-scenario-captain", starters);
    if (!player) return;
    scenario = { type, player_id: player.id };
    question = `What if I captain ${player.web_name}?`;
  }
  askAssistant(question, scenario);
}

function appendAssistantUserMessage(question) {
  const root = $("#assistant-messages");
  const message = document.createElement("article");
  message.className = "chat-message user-message";
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  const copy = document.createElement("p");
  copy.textContent = question;
  bubble.appendChild(copy);
  const avatar = document.createElement("div");
  avatar.className = "chat-avatar";
  avatar.textContent = "You";
  message.append(bubble, avatar);
  root.appendChild(message);
}

function appendAssistantThinking() {
  const root = $("#assistant-messages");
  const message = document.createElement("article");
  message.id = "assistant-thinking";
  message.className = "chat-message assistant-message";
  const avatar = document.createElement("div");
  avatar.className = "chat-avatar";
  avatar.textContent = "AI";
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble assistant-thinking";
  bubble.innerHTML = "<i></i><i></i><i></i><span class=\"sr-only\">Calculating answer</span>";
  message.append(avatar, bubble);
  root.appendChild(message);
  root.scrollTop = root.scrollHeight;
}

function appendAssistantAnswer(result, error = false, historical = false) {
  $("#assistant-thinking")?.remove();
  const root = $("#assistant-messages");
  const message = document.createElement("article");
  message.className = `chat-message assistant-message${error ? " error" : ""}`;
  const avatar = document.createElement("div");
  avatar.className = "chat-avatar";
  avatar.textContent = "AI";
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  const label = document.createElement("span");
  label.className = "eyebrow";
  label.textContent = error ? "Unable to calculate" : `${String(result.intent || "answer").replaceAll("_", " ")} · grounded`;
  const title = document.createElement("h3");
  title.textContent = result.headline || "Answer unavailable";
  title.tabIndex = -1;
  const copy = document.createElement("p");
  copy.textContent = result.answer || "The calculation could not be completed.";
  bubble.append(label, title, copy);
  if (historical) {
    const warning = document.createElement("p");
    warning.className = "assistant-history-warning";
    warning.textContent = "Saved answer · may be outdated. Ask again for a current calculation.";
    bubble.appendChild(warning);
  }

  if (result.table?.headers?.length && result.table?.rows?.length) {
    const wrap = document.createElement("div");
    wrap.className = "assistant-table-wrap";
    const table = document.createElement("table");
    table.className = "assistant-table";
    const caption = document.createElement("caption");
    caption.className = "sr-only";
    caption.textContent = `${result.headline || "Scenario"} comparison`;
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    result.table.headers.forEach((value) => {
      const cell = document.createElement("th");
      cell.scope = "col";
      cell.textContent = value;
      headRow.appendChild(cell);
    });
    head.appendChild(headRow);
    const body = document.createElement("tbody");
    result.table.rows.forEach((row) => {
      const tr = document.createElement("tr");
      row.forEach((value, index) => {
        const cell = document.createElement(index === 0 ? "th" : "td");
        if (index === 0) cell.scope = "row";
        cell.textContent = value;
        tr.appendChild(cell);
      });
      body.appendChild(tr);
    });
    table.append(caption, head, body);
    wrap.appendChild(table);
    bubble.appendChild(wrap);
  }

  if (result.evidence?.length) {
    const evidence = document.createElement("div");
    evidence.className = "assistant-evidence";
    result.evidence.forEach((item) => {
      const card = document.createElement("div");
      const source = document.createElement("small"); source.textContent = item.source;
      const value = document.createElement("strong"); value.textContent = item.value;
      const evidenceLabel = document.createElement("span"); evidenceLabel.textContent = item.label;
      card.append(evidenceLabel, value, source); evidence.appendChild(card);
    });
    bubble.appendChild(evidence);
  }
  if (result.sources?.length) {
    const sources = document.createElement("div");
    sources.className = "assistant-sources";
    const prefix = document.createElement("span"); prefix.textContent = "Sources"; sources.appendChild(prefix);
    result.sources.forEach((source) => {
      const chip = document.createElement("small"); chip.textContent = source; sources.appendChild(chip);
    });
    bubble.appendChild(sources);
  }
  if (result.caveats?.length) {
    const caveat = document.createElement("p");
    caveat.className = "assistant-caveat";
    caveat.textContent = `Keep in mind: ${result.caveats.join(" ")}`;
    bubble.appendChild(caveat);
  }
  if (result.follow_up_questions?.length && !error && !historical) {
    const followUps = document.createElement("div");
    followUps.className = "assistant-follow-ups";
    result.follow_up_questions.slice(0, 3).forEach((question) => followUps.appendChild(assistantQuestionButton(question, true)));
    bubble.appendChild(followUps);
  }
  message.append(avatar, bubble);
  root.appendChild(message);
  if (!historical) {
    title.focus({ preventScroll: true });
    message.scrollIntoView({ behavior: "smooth", block: "start" });
    if (result.context) updateAssistantContext(result.context);
  }
}

function assistantHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(ASSISTANT_HISTORY_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter((item) => item && typeof item.question === "string" && item.answer && typeof item.answer === "object").slice(-20) : [];
  } catch { return []; }
}

function saveAssistantExchange(question, answer) {
  try {
    const history = assistantHistory();
    history.push({ question, answer, saved_at: new Date().toISOString() });
    localStorage.setItem(ASSISTANT_HISTORY_KEY, JSON.stringify(history.slice(-20)));
    $("#assistant-history-status").textContent = `${Math.min(history.length, 20)} saved exchange${history.length === 1 ? "" : "s"} in this browser. Older answers may be outdated.`;
  } catch {
    $("#assistant-history-status").textContent = "Browser storage unavailable; this chat will not be saved.";
  }
}

function restoreAssistantHistory() {
  if (state.assistantHistoryLoaded) return;
  state.assistantHistoryLoaded = true;
  const history = assistantHistory();
  if (!history.length) return;
  $("#assistant-messages").replaceChildren();
  history.forEach((item) => {
    appendAssistantUserMessage(item.question);
    appendAssistantAnswer(item.answer, false, true);
  });
  $("#assistant-history-status").textContent = `${history.length} saved exchange${history.length === 1 ? "" : "s"} in this browser. Older answers may be outdated.`;
}

function clearAssistantHistory() {
  try { localStorage.removeItem(ASSISTANT_HISTORY_KEY); } catch { /* Browser storage may be disabled. */ }
  $("#assistant-messages").replaceChildren();
  $("#assistant-history-status").textContent = "Chat cleared. New answers will be saved in this browser only.";
  $("#assistant-question").focus();
}

async function askAssistant(question, scenario = null) {
  const value = String(question || $("#assistant-question")?.value || "").trim();
  if (!value || state.assistantBusy) return;
  state.assistantBusy = true;
  const input = $("#assistant-question");
  const submit = $("#assistant-form button");
  input.value = "";
  input.disabled = true;
  submit.disabled = true;
  appendAssistantUserMessage(value);
  appendAssistantThinking();
  try {
    const result = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        question: value,
        horizon: Number(state.dashboard?.filters?.horizon || state.profile?.horizon || 6),
        risk: state.dashboard?.filters?.risk || state.profile?.risk_preference || "balanced",
        ...(scenario ? { scenario } : {}),
      }),
    });
    appendAssistantAnswer(result);
    saveAssistantExchange(value, result);
  } catch (error) {
    appendAssistantAnswer({ headline: "I could not calculate that answer", answer: error.message, intent: "error", evidence: [], sources: [], caveats: [] }, true);
  } finally {
    state.assistantBusy = false;
    input.disabled = false;
    submit.disabled = false;
  }
}

async function loadAssistant() {
  restoreAssistantHistory();
  updateAssistantContext();
  try {
    await loadAssistantScenarioData();
  } catch (error) {
    $("#assistant-scenario-status").textContent = `Scenario data unavailable: ${error.message}`;
  }
  if (state.assistantReady) return;
  try {
    const result = await api("/api/chat-suggestions");
    const root = $("#assistant-starters");
    root.replaceChildren();
    result.questions.forEach((question) => root.appendChild(assistantQuestionButton(question)));
    state.assistantReady = true;
  } catch (error) {
    notice(`Assistant suggestions unavailable: ${error.message}`, true);
  }
}

function makePlayerWhyText(player, row) {
  const transferFocus = state.transfers?.suggestions?.find(
    (entry) => entry.action === "transfer" && Number(entry.sell?.id) === Number(player.id),
  );
  if (transferFocus) {
    return `Transfer engine flags this role as improvable: ${transferFocus.sell.name} → ${transferFocus.buy.name}.`;
  }
  const signal = outlookFromProjection(player, row || {});
  if (row?.start_probability >= 0.8 && row?.no_play_probability <= 0.06) {
    return `Projected as a reliable starter; currently no immediate reason to replace this pick.`;
  }
  if (signal.className === "outlook-strong" || signal.className === "outlook-medium") {
    return `Role and shape fit are currently valid, but monitor form and fixture variance before rotating this pick.`;
  }
  return "Current team shape keeps this player in the XI, though monitoring is advised over the next fixture block.";
}

function fixtureDifficultyText(payload) {
  const next = payload?.per_gameweek?.[0];
  if (!next || !next.fixtures || !next.fixtures.length) return { score: "—", fixtures: "No fixture loaded yet" };
  const scores = next.fixtures.map((fixture) => Number(fixture.position_fixture_score || 0));
  const avg = scores.length ? scores.reduce((total, value) => total + value, 0) / scores.length : 0;
  const fixtures = next.fixtures.map((fixture) => `${fixture.opponent} ${fixture.venue}`).join(" · ");
  return { score: Number(avg).toFixed(0), fixtures };
}

async function loadTeamPlayerDetail(playerId, playerName, trigger = null) {
  const player = state.currentSquad.find((entry) => Number(entry.id) === Number(playerId));
  if (!player) return;
  const row = projectionSnapshot(player.id);
  const detailPanel = $("#team-player-detail");
  const summary = $("#team-player-summary");
  const metricGrid = $("#team-player-metrics");
  const statList = $("#team-player-stats");
  const why = $("#team-player-why");
  text($("#team-player-name"), `${playerName} · ${resolvePlayerPosition(player.player_position)} · ${player.team}`);
  metricGrid.replaceChildren();
  statList.replaceChildren();
  detailPanel.classList.remove("hidden");
  if (trigger) trigger.focus();

  summary.textContent = `${player.team} · £${Number(player.current_price || 0).toFixed(1)}m · ${player.status === "a" ? "Available" : (player.news || player.status || "Injury risk")}`;
  addMetric(metricGrid, "GW+1", Number(row?.expected_1 || 0).toFixed(1), "Projected points");
  addMetric(metricGrid, "Next 3 GWs", Number(row?.expected_3 || 0).toFixed(1), "Projected points");
  addMetric(metricGrid, "Next 6 GWs", Number(row?.expected_6 || 0).toFixed(1), "Projected points");
  addMetric(metricGrid, "Expected minutes", Number(row?.expected_minutes || 0).toFixed(0), "per fixture");
  addMetric(metricGrid, "Start probability", `${Math.round(Number(row?.start_probability || 0) * 100)}%`, "next fixture");

  addDefinition(statList, "Fixture difficulty", "—", "Loading");
  addDefinition(statList, "Injury status", player.status === "a" ? "Available" : (player.news || player.status || "Injury update"), player.status === "a" ? "Active" : "Needs review");
  addDefinition(statList, "Rotation risk", `${Math.round(Number(row?.no_play_probability || 0) * 100)}%`, "no-play probability");
  addDefinition(statList, "Form", `${Number(player.total_points || 0).toFixed(0)} PTS`, `minutes ${Number(player.minutes || 0)}`);
  addDefinition(statList, "xG", row?.rates_per_90?.expected_goals ? Number(row.rates_per_90.expected_goals).toFixed(2) : "—", "per 90");
  addDefinition(statList, "xA", row?.rates_per_90?.expected_assists ? Number(row.rates_per_90.expected_assists).toFixed(2) : "—", "per 90");
  addDefinition(statList, "Ownership", `${Number(player.selected_by_percent || 0).toFixed(1)}%`, "current ownership");
  addDefinition(statList, "Price", `£${Number(player.current_price || 0).toFixed(1)}m`, "current");

  try {
    const projection = await api(`/api/projection/player/${playerId}`);
    const payload = projection?.projection || {};
    const fixtureMeta = fixtureDifficultyText(payload);
    const confidence = payload.confidence === undefined ? "—" : `${Math.round(Number(payload.confidence) * 100)}/100`;
    addDefinition(statList, "Fixture difficulty", `${fixtureMeta.score}/100`, fixtureMeta.fixtures);
    addDefinition(statList, "Model signal", confidence, "heuristic evidence score, not outcome probability");
    addMetric(metricGrid, "Next 1 GW", Number(payload.horizons?.["1"]?.expected || row?.expected_1 || 0).toFixed(1), "Projected points");
  } catch (error) {
    addDefinition(statList, "Model signal", "—", "Projection data unavailable");
  }
  text(why, makePlayerWhyText(player, row || {}));
  window.scrollTo({ top: detailPanel.offsetTop - 14, behavior: "auto" });
}

async function loadPlayers() {
  const params = new URLSearchParams({ limit: "200", sort: $("#player-sort").value });
  if ($("#player-search").value.trim()) params.set("q", $("#player-search").value.trim());
  if ($("#player-team").value) params.set("team", $("#player-team").value);
  if ($("#player-position").value) params.set("position", $("#player-position").value);
  try {
    const result = await api(`/api/players?${params}`);
    const body = $("#players-body");
    body.replaceChildren();
    if (!result.players.length) {
      const row = document.createElement("tr");
      const cell = td(row, "No matching player records.", "empty");
      cell.colSpan = 10;
      body.appendChild(row);
      return;
    }
    result.players.forEach((player) => {
      const row = document.createElement("tr");
      td(row, player.web_name, "player-name");
      td(row, player.position);
      td(row, player.team);
      td(row, `£${Number(player.price).toFixed(1)}m`);
      td(row, `${Number(player.selected_by_percent || 0).toFixed(1)}%`);
      td(row, player.total_points);
      td(row, player.minutes);
      td(row, player.expected_goals === null ? "—" : Number(player.expected_goals).toFixed(2));
      td(row, player.expected_assists === null ? "—" : Number(player.expected_assists).toFixed(2));
      td(row, player.status === "a" ? "Available" : (player.news || player.status), player.status === "a" ? "subtle" : "");
      body.appendChild(row);
    });
  } catch (error) { notice(error.message, true); }
}

async function loadFixtures() {
  const params = new URLSearchParams({ horizon: $("#fixture-horizon").value });
  if ($("#fixture-start").value) params.set("start", $("#fixture-start").value);
  try {
    const result = await api(`/api/fixtures?${params}`);
    const root = $("#fixture-grid");
    root.replaceChildren();
    const groups = new Map();
    result.fixtures.forEach((fixture) => {
      const key = fixture.event_id;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(fixture);
    });
    if (!groups.size) {
      const empty = document.createElement("div");
      empty.className = "panel empty";
      empty.textContent = "No scheduled fixtures in this range.";
      root.appendChild(empty);
    }
    groups.forEach((fixtures, gameweek) => {
      const panel = document.createElement("article");
      panel.className = "panel fixture-week";
      const heading = document.createElement("h3");
      heading.textContent = `Gameweek ${gameweek}`;
      panel.appendChild(heading);
      const list = document.createElement("div");
      list.className = "fixture-list";
      fixtures.forEach((fixture) => {
        const item = document.createElement("div");
        item.className = "fixture";
        const home = document.createElement("span"); home.className = "home"; home.textContent = fixture.team_h;
        const vs = document.createElement("span"); vs.className = "vs"; vs.textContent = fixture.finished ? `${fixture.team_h_score}–${fixture.team_a_score}` : "vs";
        const away = document.createElement("span"); away.textContent = fixture.team_a;
        const fdr = document.createElement("span"); fdr.className = "fdr"; fdr.textContent = `FDR ${fixture.team_h_difficulty}/${fixture.team_a_difficulty}`;
        item.append(home, vs, away, fdr);
        list.appendChild(item);
      });
      panel.appendChild(list);
      root.appendChild(panel);
    });
  } catch (error) { notice(error.message, true); }
}

async function loadQuality() {
  try {
    const result = await api("/api/quality");
    const root = $("#quality-list");
    root.replaceChildren();
    if (!result.issues.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No issues recorded. Refresh data to run validation.";
      root.appendChild(empty);
      return;
    }
    result.issues.forEach((issue) => {
      const item = document.createElement("div"); item.className = `quality-item ${issue.severity}`;
      const severity = document.createElement("span"); severity.className = "severity"; severity.textContent = issue.severity;
      const code = document.createElement("span"); code.className = "quality-code"; code.textContent = issue.code;
      const message = document.createElement("span"); message.textContent = `${issue.message}${issue.entity_id ? ` (${issue.entity} ${issue.entity_id})` : ""}`;
      item.append(severity, code, message);
      root.appendChild(item);
    });
  } catch (error) { notice(error.message, true); }
}

async function loadProjections() {
  const horizon = $("#projection-horizon").value;
  const params = new URLSearchParams({ horizon, limit: "300", sort: $("#projection-sort").value });
  if ($("#projection-search").value.trim()) params.set("q", $("#projection-search").value.trim());
  if ($("#projection-team").value) params.set("team", $("#projection-team").value);
  if ($("#projection-position").value) params.set("position", $("#projection-position").value);
  text($("#projection-xpts-heading"), `xPts · ${horizon} GW${horizon === "1" ? "" : "s"}`);
  text($("#projection-value-heading"), `Value · ${horizon} GW${horizon === "1" ? "" : "s"}`);
  $("#projection-detail").classList.add("hidden");
  $("#projections-body").setAttribute("aria-busy", "true");
  try {
    const result = await api(`/api/projections?${params}`);
    const run = result.projection;
    text($("#projection-model"), run ? `${run.model_version} · GW${run.planning_event}` : "Not generated");
    text($("#projection-generated"), run ? `Generated ${formatDate(run.created_at)} from ${run.player_count} player records.` : "Refresh official data to generate projections.");
    if (!run) text($("#projection-calibration"), "Unavailable");
    const body = $("#projections-body");
    body.replaceChildren();
    text($("#projection-result-count"), `${result.players.length} ranked player${result.players.length === 1 ? "" : "s"} shown.`);
    if (!result.players.length) {
      const row = document.createElement("tr");
      const cell = td(row, run ? "No matching projections." : "No projection run exists. Refresh official data first.", "empty");
      cell.colSpan = 13;
      body.appendChild(row);
      return;
    }
    result.players.forEach((player) => {
      const row = document.createElement("tr");
      const nameCell = document.createElement("td");
      const playerButton = document.createElement("button");
      playerButton.type = "button";
      playerButton.className = "player-link";
      playerButton.textContent = player.web_name;
      playerButton.setAttribute("aria-label", `Inspect ${player.web_name} projection`);
      playerButton.addEventListener("click", () => loadProjectionDetail(player.id, playerButton));
      nameCell.appendChild(playerButton);
      row.appendChild(nameCell);
      td(row, player.position);
      td(row, player.team);
      td(row, `£${Number(player.price).toFixed(1)}m`);
      td(row, Number(player.expected_points).toFixed(2), "captain");
      td(row, `${Number(player.value).toFixed(2)} pts/£m`);
      td(row, Number(player.expected_minutes).toFixed(0));
      td(row, `${(Number(player.start_probability) * 100).toFixed(0)}%`);
      td(row, Number(player.floor_6).toFixed(1));
      td(row, Number(player.median_6).toFixed(1));
      td(row, Number(player.ceiling_6).toFixed(1));
      td(row, `${(Number(player.confidence) * 100).toFixed(0)}/100`);
      const actionCell = document.createElement("td");
      const actionButton = document.createElement("button");
      actionButton.type = "button";
      actionButton.className = "ghost compact-button";
      actionButton.textContent = "Details";
      actionButton.addEventListener("click", () => loadProjectionDetail(player.id, actionButton));
      actionCell.appendChild(actionButton);
      row.appendChild(actionCell);
      body.appendChild(row);
    });
  } catch (error) { notice(error.message, true); }
  finally { $("#projections-body").removeAttribute("aria-busy"); }
}

function addMetric(root, label, value, sublabel) {
  const article = document.createElement("article"); article.className = "metric";
  const span = document.createElement("span"); span.textContent = label;
  const strong = document.createElement("strong"); strong.textContent = value;
  const small = document.createElement("small"); small.textContent = sublabel;
  article.append(span, strong, small); root.appendChild(article);
}

function addReliabilityMetric(root, label, value) {
  const item = document.createElement("div");
  item.className = "reliability-metric";
  const span = document.createElement("span"); span.textContent = label;
  const strong = document.createElement("strong"); strong.textContent = value;
  item.append(span, strong); root.appendChild(item);
}

function renderCalibration(calibration) {
  const value = calibration || { status: "collecting", evaluated_events: 0, sample_size: 0 };
  state.calibration = value;
  const calibrated = value.status === "calibrated";
  const badge = $("#projection-calibration");
  badge.className = `status-pill ${calibrated ? "good" : "warning"}`;
  badge.textContent = calibrated ? "Bias adjustment active" : "Calibration collecting";
  const statusBadge = $("#reliability-status");
  statusBadge.className = `status-pill ${calibrated ? "good" : "warning"}`;
  text(statusBadge, calibrated ? "Bias adjustment active" : "Building evidence");
  text($("#reliability-message"), calibrated
    ? `Bias-adjusted using ${value.evaluated_events} completed gameweeks (${value.sample_size} player-gameweeks). This is not yet proof that transfer advice beats simpler strategies.`
    : `The system needs at least 3 completed gameweeks and 100 player-gameweeks before it changes forecasts. It currently has ${value.evaluated_events || 0} gameweeks and ${value.sample_size || 0} observations.`);
  const metrics = $("#reliability-metrics");
  metrics.replaceChildren();
  addReliabilityMetric(metrics, "MAE", value.mae == null ? "Pending" : `${Number(value.mae).toFixed(2)} pts`);
  addReliabilityMetric(metrics, "Rank correlation", value.rank_correlation == null ? "Pending" : Number(value.rank_correlation).toFixed(2));
  addReliabilityMetric(metrics, "Start Brier", value.start_brier == null ? "Pending" : Number(value.start_brier).toFixed(3));
  addReliabilityMetric(metrics, "Range coverage", value.interval_coverage == null ? "Pending" : `${(Number(value.interval_coverage) * 100).toFixed(0)}%`);
  addReliabilityMetric(metrics, "Internal score", value.reliability_score == null ? "Pending" : `${(Number(value.reliability_score) * 100).toFixed(0)}/100`);
  const transferSafety = state.transfers?.decision_safety === "refresh_required";
  $("#transfer-reliability").className = `status-pill ${transferSafety ? "bad" : calibrated ? "good" : "warning"}`;
  text($("#transfer-reliability"), transferSafety ? "Verify before acting" : calibrated ? `${value.evaluated_events || 0} GWs · bias-adjusted` : "Pitch IQ evidence provisional");
}

async function loadCalibration() {
  try {
    const result = await api("/api/calibration");
    renderCalibration(result.calibration);
  } catch (error) {
    renderCalibration(null);
    notice(error.message, true);
  }
}

function transferCard(suggestion, horizon, rank = 0) {
  const card = document.createElement("article");
  const hold = suggestion.action === "do_nothing";
  card.className = `transfer-card${hold ? " hold" : ""}${suggestion.recommended ? " recommended" : ""}`;
  const top = document.createElement("div"); top.className = "transfer-card-top";
  const label = document.createElement("span"); label.className = "transfer-rank";
  label.textContent = suggestion.recommended ? suggestion.budget_assumption && suggestion.budget_assumption !== "Official selling price" ? "Potential single move · price check" : "Recommended single move" : hold ? "Hold baseline" : `Alternative single move ${rank}`;
  const flag = document.createElement("span"); flag.className = "transfer-flag";
  flag.textContent = suggestion.recommended ? suggestion.budget_assumption && suggestion.budget_assumption !== "Official selling price" ? "Provisional" : "Best move" : hold ? "No transfer" : suggestion.position || "Option";
  top.append(label, flag); card.appendChild(top);
  const move = document.createElement("strong"); move.className = "transfer-move";
  move.textContent = hold ? "Do Nothing" : `${suggestion.sell.name} → ${suggestion.buy.name}`;
  const gain = document.createElement("span"); gain.className = "transfer-gain";
  gain.textContent = hold ? "Keep the squad · preserve flexibility" : `${signed(suggestion.net_expected_gain, " pts")} over ${horizon} GW${horizon === 1 ? "" : "s"}`;
  const windows = document.createElement("span"); windows.className = "transfer-windows";
  windows.textContent = hold
    ? `Model signal ${Math.round(Number(suggestion.confidence || 0) * 100)}/100 · heuristic`
    : `1 GW ${signed(suggestion.gain_1)} · 3 GW ${signed(suggestion.gain_3)} · 6 GW ${signed(suggestion.gain_6)}`;
  const why = document.createElement("p"); why.className = "transfer-why";
  why.textContent = hold ? suggestion.why : `Why: ${suggestion.why}`;
  card.append(move, gain, windows, why);
  if (!hold) {
    const evidence = document.createElement("div"); evidence.className = "transfer-evidence";
    const confidence = document.createElement("span"); confidence.textContent = `Model signal ${Math.round(Number(suggestion.confidence || 0) * 100)}/100 · heuristic`;
    const minutes = document.createElement("span"); minutes.textContent = `${signed(suggestion.expected_minutes_delta, " xMin")} per GW`;
    const fixtures = document.createElement("span"); fixtures.textContent = `${signed(suggestion.fixture_swing)} fixture swing`;
    evidence.append(confidence, minutes, fixtures); card.appendChild(evidence);
    if (suggestion.sell.average_fdr != null && suggestion.buy.average_fdr != null) {
      const fdr = document.createElement("span");
      fdr.textContent = `Official FDR ${Number(suggestion.sell.average_fdr).toFixed(1)} → ${Number(suggestion.buy.average_fdr).toFixed(1)} (lower is easier)`;
      evidence.appendChild(fdr);
    }
    const budget = document.createElement("small"); budget.className = "transfer-budget";
    const estimatedPrice = suggestion.budget_assumption !== "Official selling price";
    budget.textContent = `£${Number(suggestion.sell.price).toFixed(1)}m → £${Number(suggestion.buy.price).toFixed(1)}m · £${Number(suggestion.bank_after).toFixed(1)}m left${suggestion.hit_cost ? ` · includes −${suggestion.hit_cost} hit` : ""}${estimatedPrice ? " · current-price estimate" : ""}`;
    card.appendChild(budget);
    if ((suggestion.risks || []).length) {
      const risk = document.createElement("small"); risk.className = "transfer-risk"; risk.textContent = `Watch: ${suggestion.risks.join(" · ")}`; card.appendChild(risk);
    }
  }
  return card;
}

function renderSquadFdr(result) {
  const root = $("#transfer-squad-fdr");
  if (!root) return;
  root.replaceChildren();
  const fdr = result?.squad_fdr;
  if (result?.status === "team-required") {
    root.textContent = "Import your squad to see your starting XI’s upcoming official FDR.";
    return;
  }
  if (fdr?.status !== "ready") {
    root.textContent = "Starting XI official FDR is unavailable for this planning window.";
    return;
  }
  const title = document.createElement("strong"); title.textContent = `Your XI · ${fdr.horizon}-GW official FDR ${Number(fdr.average).toFixed(1)}/5`;
  const counts = document.createElement("span");
  counts.textContent = `${fdr.favourable_count} favourable (1–2) · ${fdr.difficult_count} difficult (4–5) · ${fdr.blank_player_weeks} blank player-GWs`;
  const note = document.createElement("small");
  note.textContent = "Lower is easier. Average covers starting-XI player-fixtures; doubles count twice, blanks are excluded. The projection model uses a separate position-aware fixture score.";
  root.append(title, counts, note);
}

function renderTransferResult(result) {
  const root = $("#transfer-suggestions");
  const comparisonRoot = $("#transfer-route-comparison");
  comparisonRoot.replaceChildren();
  state.transfers = result;
  renderSquadFdr(result);
  const verificationStatus = $("#manager-verification-status");
  if (verificationStatus && result.status === "ready") {
    verificationStatus.className = `status-pill ${result.decision_safety === "ready" ? "good" : "neutral"}`;
    text(verificationStatus, result.decision_safety === "ready" ? "Exact prices available" : "Optional price sync");
  }
  root.replaceChildren();
  if (result.status === "team-required") {
    text($("#transfer-context"), "Import your 15-player FPL squad to unlock recommendations.");
    const empty = document.createElement("article"); empty.className = "transfer-card empty";
    const title = document.createElement("strong"); title.textContent = "Your squad is the missing input";
    const message = document.createElement("p"); message.textContent = result.message;
    const button = document.createElement("button"); button.type = "button"; button.className = "ghost compact-button"; button.textContent = "Import squad";
    button.addEventListener("click", () => setView("team"));
    empty.append(title, message, button); root.appendChild(empty);
    return;
  }
  text($("#transfer-context"), `GW${result.planning_event} · ${result.horizon}-GW plan · £${Number(result.bank).toFixed(1)}m bank · ${result.free_transfers} free transfer${result.free_transfers === 1 ? "" : "s"} · ${result.risk_preference}`);
  text($("#transfer-rule-note"), result.decision_safety === "refresh_required" ? "Recommendations are withheld until official data is refreshed." : `The cards above are alternative single moves, not a combined plan. You gain one free transfer per Gameweek and can bank up to five. With ${result.free_transfers} available now, each move beyond that allowance costs −4 points.${result.decision_safety === "estimate_only" ? " Selling prices are estimated, so confirm affordability in FPL before acting." : ""}`);
  root.title = result.confidence_definition || "";
  const safetyBadge = $("#transfer-reliability");
  if (result.decision_safety === "refresh_required") {
    safetyBadge.className = "status-pill bad";
    text(safetyBadge, "Refresh data");
  } else if (result.decision_safety === "estimate_only") {
    safetyBadge.className = "status-pill warning";
    text(safetyBadge, "Price estimate");
  }
  (result.suggestions || []).forEach((suggestion, index) => root.appendChild(transferCard(suggestion, result.horizon, index + 1)));
  const comparison = result.route_comparison || {};
  if (comparison.roll && result.decision_safety !== "refresh_required") {
    const title = document.createElement("strong"); title.textContent = "Route comparison · this Gameweek";
    const line = document.createElement("p");
    const single = comparison.single?.net_expected_gain;
    const double = comparison.double?.net_expected_gain;
    line.textContent = `Roll: 0 moves, ${comparison.roll.next_week_free_transfers} free transfers next GW · Best single: ${single == null ? "unavailable" : signed(single, " pts")}, ${comparison.single.next_week_free_transfers} next GW · Best legal pair found: ${double == null ? result.decision_safety === "estimate_only" ? "unavailable without exact selling prices" : "unavailable" : signed(double, " pts")}, ${comparison.double.hit_cost ? `−${comparison.double.hit_cost} hit, ` : ""}${comparison.double.next_week_free_transfers} next GW.`;
    const caution = document.createElement("small");
    caution.textContent = `${comparison.note || ""} ${result.decision_safety === "estimate_only" ? "Check exact selling prices in FPL before acting on any estimated route." : "A pair is a scenario, not an endorsed move."}`;
    comparisonRoot.append(title, line, caution);
    if (result.two_transfer_plan) {
      const route = document.createElement("p");
      route.textContent = result.two_transfer_plan.moves.map((move) => `${move.sell.name} → ${move.buy.name}`).join(" + ");
      comparisonRoot.appendChild(route);
    }
  }
  if (result.decision_safety === "refresh_required") {
    const safetyCard = root.querySelector(".transfer-card.recommended");
    if (safetyCard) {
      text(safetyCard.querySelector(".transfer-rank"), "Decision gate");
      text(safetyCard.querySelector(".transfer-flag"), "Verify inputs");
      text(safetyCard.querySelector(".transfer-move"), "Decision withheld");
      text(safetyCard.querySelector(".transfer-gain"), "No transfer recommendation is action-ready");
      text(safetyCard.querySelector(".transfer-windows"), "—");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ghost compact";
      button.textContent = "Verify my team";
      button.addEventListener("click", () => { setView("team"); $("#manager-verification")?.scrollIntoView({ block: "start" }); });
      safetyCard.appendChild(button);
    }
  }
  const evidence = $("#transfer-evidence-note");
  if (evidence) {
    const calibration = result.calibration || {};
    const calibrationLabel = calibration.status === "calibrated" ? `${calibration.evaluated_events || 0} GWs evaluated` : "confidence capped while backtests collect";
    const warningLabel = result.decision_safety === "refresh_required" ? "DO NOT ACT" : "CHECK PRICE";
    const sourceNote = ` · official data ${formatDate(result.official_data_updated_at)}${result.data_warnings?.length ? ` · ${warningLabel}: ${result.data_warnings.join("; ")}` : ""}`;
    text(evidence, `${result.model_name || "FPL Pitch IQ"} · forecast ${formatDate(result.projection_updated_at || result.generated_at)}${sourceNote} · ${calibrationLabel} · action threshold ${Number(result.decision_threshold).toFixed(1)} pts`);
  }
}

async function loadTransfers() {
  const root = $("#transfer-suggestions");
  root.setAttribute("aria-busy", "true");
  try {
    renderTransferResult(await api("/api/transfer-suggestions"));
  } catch (error) {
    root.replaceChildren();
    renderSquadFdr(null);
    const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "Transfer suggestions are temporarily unavailable."; root.appendChild(empty);
    notice(error.message, true);
  } finally { root.removeAttribute("aria-busy"); }
}

function addDefinition(root, label, value) {
  const row = document.createElement("div");
  const term = document.createElement("dt"); term.textContent = label;
  const detail = document.createElement("dd"); detail.textContent = value;
  row.append(term, detail); root.appendChild(row);
}

async function loadProjectionDetail(playerId, trigger = null) {
  try {
    state.lastProjectionTrigger = trigger;
    const result = await api(`/api/projection/player/${playerId}`);
    const projection = result.projection;
    const horizon = Number($("#projection-horizon").value);
    const horizonData = projection.horizons[String(horizon)];
    $("#projection-detail").classList.remove("hidden");
    text($("#projection-detail-name"), `${projection.player.name} · ${projection.player.price.toFixed(1)}m`);
    const summary = $("#projection-summary-grid");
    summary.replaceChildren();
    addMetric(summary, "Expected points", Number(horizonData.expected).toFixed(2), `next ${horizon} gameweek${horizon === 1 ? "" : "s"}`);
    addMetric(summary, "Value", `${(Number(horizonData.expected) / projection.player.price).toFixed(2)}`, "points per £1m");
    addMetric(summary, "Expected minutes", Number(projection.minutes.expected_minutes).toFixed(0), "per fixture");
    addMetric(summary, "Start probability", `${(projection.minutes.start_probability * 100).toFixed(0)}%`, "next fixture");
    const modelInputs = $("#projection-model-inputs");
    modelInputs.replaceChildren();
    addDefinition(modelInputs, "60+ minute probability", `${(projection.minutes.sixty_probability * 100).toFixed(0)}%`);
    addDefinition(modelInputs, "No-play probability", `${(projection.minutes.no_play_probability * 100).toFixed(0)}%`);
    addDefinition(modelInputs, "Availability factor", `${(projection.minutes.availability_factor * 100).toFixed(0)}%`);
    addDefinition(modelInputs, "Observed team games", projection.minutes.observed_team_games);
    addDefinition(modelInputs, "Team minutes constraint", `${(projection.minutes.team_capacity_factor * 100).toFixed(0)}% of independent estimate`);
    addDefinition(modelInputs, "Projection range", `${Number(horizonData.floor).toFixed(1)} floor · ${Number(horizonData.median).toFixed(1)} median · ${Number(horizonData.ceiling).toFixed(1)} ceiling`);
    addDefinition(modelInputs, "Model signal", `${(projection.confidence * 100).toFixed(0)}/100 · ${projection.confidence_label === "calibrated" ? "bias adjustment active" : "calibration collecting"}`);
    addDefinition(modelInputs, "Role adjustment", `Penalties ×${Number(projection.role_adjustments.penalty_taker_xg_multiplier).toFixed(2)} · set pieces ×${Number(projection.role_adjustments.set_piece_xa_multiplier).toFixed(2)}`);
    const components = $("#projection-components");
    components.replaceChildren();
    text($("#projection-components-heading"), `${horizon}-gameweek components`);
    const selectedComponents = {};
    projection.per_gameweek.slice(0, horizon).forEach((week) => {
      Object.entries(week.components).forEach(([name, value]) => {
        selectedComponents[name] = (selectedComponents[name] || 0) + Number(value);
      });
    });
    Object.entries(selectedComponents).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).forEach(([name, value]) => {
      const item = document.createElement("div"); item.className = "component";
      const label = document.createElement("span"); label.textContent = name.replaceAll("_", " ");
      const score = document.createElement("strong"); score.textContent = `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}`;
      item.append(label, score); components.appendChild(item);
    });
    const gameweeks = $("#projection-gameweeks");
    gameweeks.replaceChildren();
    projection.per_gameweek.forEach((week) => {
      const row = document.createElement("tr");
      const fixtureLabel = week.fixtures.length ? week.fixtures.map((fixture) => `${fixture.opponent} (${fixture.venue})`).join(", ") : "Blank";
      const playerXg = week.fixtures.reduce((sum, fixture) => sum + fixture.expected_goals, 0);
      const playerXa = week.fixtures.reduce((sum, fixture) => sum + fixture.expected_assists, 0);
      const goalChance = 1 - week.fixtures.reduce((product, fixture) => product * (1 - fixture.goal_probability), 1);
      const assistChance = 1 - week.fixtures.reduce((product, fixture) => product * (1 - fixture.assist_probability), 1);
      const teamXg = week.fixtures.reduce((sum, fixture) => sum + fixture.expected_goals_for, 0);
      const clean = week.fixtures.length ? week.fixtures.reduce((sum, fixture) => sum + fixture.clean_sheet_probability, 0) / week.fixtures.length : 0;
      td(row, `GW${week.event_id}`); td(row, fixtureLabel); td(row, Number(week.expected_points).toFixed(2));
      const fixtureScore = week.fixtures.length ? week.fixtures.reduce((sum, fixture) => sum + fixture.position_fixture_score, 0) / week.fixtures.length : 0;
      td(row, `${playerXg.toFixed(2)} · ${(goalChance * 100).toFixed(0)}%`); td(row, `${playerXa.toFixed(2)} · ${(assistChance * 100).toFixed(0)}%`); td(row, teamXg.toFixed(2)); td(row, `${(clean * 100).toFixed(0)}%`); td(row, week.fixtures.length ? fixtureScore.toFixed(0) : "—");
      gameweeks.appendChild(row);
    });
    await new Promise((resolve) => window.requestAnimationFrame(() => window.requestAnimationFrame(resolve)));
    const detailPanel = $("#projection-detail");
    const detailTop = detailPanel.getBoundingClientRect().top + window.scrollY - 16;
    window.scrollTo({ top: detailTop, behavior: "auto" });
  } catch (error) { notice(error.message, true); }
}

async function refreshData() {
  const button = $("#refresh-button");
  button.disabled = true;
  button.textContent = "Refreshing…";
  try {
    const result = await api("/api/update", { method: "POST", body: JSON.stringify({ force: true }) });
    const summary = result.summary;
    state.squadOptimizer = null;
    state.dashboard = null;
    state.chipPlan = null;
    state.ledger = null;
    notice(`Updated ${summary.players} players, ${summary.fixtures} fixtures, and ${summary.player_gameweeks} gameweek observations; projections, calibration, transfers, and the squad optimizer are ready.` + (summary.team_import_warning ? ` ${summary.team_import_warning}` : ""));
    await loadAll();
    if (window.location.hash === "#overview") await loadDashboard(true);
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; button.textContent = "Refresh official data"; }
}

async function submitTeam(event) {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  button.textContent = "Importing…";
  const body = {
    team_id: $("#team-id").value,
    current_gameweek: $("#current-gameweek").value,
    bank: $("#bank").value,
    free_transfers: $("#free-transfers").value,
    horizon: $("#horizon").value,
    risk_preference: $("#risk").value,
  };
  try {
    const result = await api("/api/import-team", { method: "POST", body: JSON.stringify(body) });
    notice(`Imported ${result.result.pick_count} players for ${result.result.team_name}.`);
    await loadProfile();
    await loadTransfers();
    state.squadOptimizer = null;
    state.dashboard = null;
    state.chipPlan = null;
    state.ledger = null;
    await loadDashboard(true);
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; button.textContent = "Save and import squad"; }
}

function backtestEmpty(message) {
  const empty = document.createElement("p");
  empty.className = "backtest-empty";
  empty.textContent = message;
  return empty;
}

function renderBacktestBenchmarks(benchmarks) {
  const root = $("#backtest-benchmarks");
  root.replaceChildren();
  const ready = benchmarks.filter((item) => item.average_points != null);
  const max = Math.max(...ready.map((item) => Number(item.average_points)), 1);
  benchmarks.forEach((item) => {
    const row = document.createElement("article");
    row.className = `backtest-benchmark${item.key === "model" ? " model" : ""}${item.status !== "ready" ? " collecting" : ""}`;
    const identity = document.createElement("div");
    const label = document.createElement("strong"); label.textContent = item.label;
    const meta = document.createElement("small"); meta.textContent = item.status === "ready" ? `${item.events} evaluated GW${item.events === 1 ? "" : "s"}` : "Awaiting a compatible pre-deadline snapshot";
    identity.append(label, meta);
    const track = document.createElement("div"); track.className = "backtest-benchmark-track";
    const fill = document.createElement("span"); fill.style.width = item.average_points == null ? "0" : `${Math.max(3, Number(item.average_points) / max * 100)}%`;
    track.appendChild(fill);
    const value = document.createElement("div"); value.className = "backtest-benchmark-value";
    const score = document.createElement("strong"); score.textContent = item.average_points == null ? "Collecting" : Number(item.average_points).toFixed(1);
    const detail = document.createElement("small"); detail.textContent = item.average_points == null ? "" : "actual XI pts / GW";
    value.append(score, detail);
    row.append(identity, track, value); root.appendChild(row);
  });
  root.setAttribute("aria-label", benchmarks.map((item) => `${item.label}: ${item.average_points == null ? "collecting data" : `${Number(item.average_points).toFixed(1)} actual points per gameweek`}`).join(". "));
}

function renderBacktestEvents(events) {
  const root = $("#backtest-events-chart");
  root.replaceChildren();
  if (!events.length) {
    root.appendChild(backtestEmpty("No completed forecast gameweek yet. This is expected before the first stored deadline passes."));
    return;
  }
  const maxMae = Math.max(...events.map((item) => Number(item.mae)), 1);
  events.forEach((item) => {
    const row = document.createElement("article"); row.className = "backtest-event-row";
    const event = document.createElement("strong"); event.textContent = `GW${item.event}`;
    const bar = document.createElement("div"); bar.className = "backtest-event-track";
    const fill = document.createElement("span"); fill.style.width = `${Math.max(4, Number(item.mae) / maxMae * 100)}%`; bar.appendChild(fill);
    const metrics = document.createElement("small");
    metrics.textContent = `MAE ${Number(item.mae).toFixed(2)} · bias ${Number(item.bias) >= 0 ? "+" : ""}${Number(item.bias).toFixed(2)} · rank ${item.rank_correlation == null ? "—" : Number(item.rank_correlation).toFixed(2)}`;
    row.append(event, bar, metrics); root.appendChild(row);
  });
  root.setAttribute("aria-label", `Gameweek accuracy. ${events.map((item) => `Gameweek ${item.event}: mean absolute error ${Number(item.mae).toFixed(2)}, bias ${Number(item.bias).toFixed(2)}, rank correlation ${item.rank_correlation == null ? "unavailable" : Number(item.rank_correlation).toFixed(2)}`).join(". ")}`);
}

function renderBacktestCalibration(bins) {
  const root = $("#backtest-calibration-chart");
  root.replaceChildren();
  if (!bins.length) {
    root.appendChild(backtestEmpty("Start-probability bins appear after completed player-gameweek forecasts are available."));
    return;
  }
  bins.forEach((bin) => {
    const row = document.createElement("article"); row.className = "backtest-calibration-row";
    const label = document.createElement("strong"); label.textContent = `${Math.round(Number(bin.lower) * 100)}–${Math.round(Number(bin.upper) * 100)}%`;
    const bars = document.createElement("div"); bars.className = "backtest-calibration-bars";
    const predicted = document.createElement("span"); predicted.className = "predicted"; predicted.style.width = `${Number(bin.predicted) * 100}%`; predicted.title = `Predicted ${(Number(bin.predicted) * 100).toFixed(0)}%`;
    const actual = document.createElement("span"); actual.className = "actual"; actual.style.width = `${Number(bin.actual) * 100}%`; actual.title = `Actual ${(Number(bin.actual) * 100).toFixed(0)}%`;
    bars.append(predicted, actual);
    const values = document.createElement("small"); values.textContent = `Pred ${(Number(bin.predicted) * 100).toFixed(0)}% · actual ${(Number(bin.actual) * 100).toFixed(0)}% · n=${bin.count}`;
    row.append(label, bars, values); root.appendChild(row);
  });
  root.setAttribute("aria-label", `Starting probability calibration. ${bins.map((bin) => `${Math.round(Number(bin.lower) * 100)} to ${Math.round(Number(bin.upper) * 100)} percent bin: predicted ${Math.round(Number(bin.predicted) * 100)} percent, actual ${Math.round(Number(bin.actual) * 100)} percent, ${bin.count} samples`).join(". ")}`);
}

function renderBacktestDecisions(decisions) {
  const root = $("#backtest-decisions");
  root.replaceChildren();
  const cards = [
    ["Transfers", decisions.transfers, "profitable after costs"],
    ["Hit decisions", decisions.hits, "positive after 4-point cost"],
    ["6-GW transfer gain", decisions.long_term_transfers, "fully completed windows"],
    ["Captaincy", decisions.captaincy, "best scorer in imported XI"],
  ];
  cards.forEach(([label, data, description]) => {
    const card = document.createElement("article");
    const eyebrow = document.createElement("span"); eyebrow.textContent = label;
    const score = document.createElement("strong");
    const rate = data?.success_rate;
    score.textContent = rate == null ? "Collecting" : `${Math.round(Number(rate) * 100)}%`;
    const note = document.createElement("small");
    const evaluated = Number(data?.evaluated || 0);
    note.textContent = evaluated ? `${evaluated} decision${evaluated === 1 ? "" : "s"} · ${description}` : `No finished saved decision · ${description}`;
    card.append(eyebrow, score, note); root.appendChild(card);
  });
}

function renderBacktestReport(report) {
  state.backtestReport = report;
  const summary = report.summary || {};
  const calibrated = report.status === "calibrated";
  const lift = summary.predictive_value_vs_best_baseline;
  const status = $("#backtest-status");
  status.className = `status-pill ${calibrated ? "good" : "warning"}`;
  text(status, calibrated ? "Bias-adjusted" : "Collecting");
  text($("#backtest-events"), summary.evaluated_events || 0);
  text($("#backtest-samples"), summary.sample_size || 0);
  text($("#backtest-mae"), summary.mae == null ? "—" : Number(summary.mae).toFixed(2));
  text($("#backtest-rank"), summary.rank_correlation == null ? "—" : Number(summary.rank_correlation).toFixed(2));
  text($("#backtest-reliability"), summary.reliability_score == null ? "—" : `${Math.round(Number(summary.reliability_score) * 100)}/100`);
  text($("#backtest-lift"), lift == null ? "—" : `${Number(lift) >= 0 ? "+" : ""}${Number(lift).toFixed(1)}`);
  const model = report.model || {};
  text($("#backtest-model-version"), model.version || "Model version unavailable");
  text($("#backtest-model-cutoff"), model.deadline_time ? `Planning GW${model.planning_event || "—"} · deadline ${formatDate(model.deadline_time)}` : "No deadline snapshot loaded");
  const maeCi = summary.mae_ci95;
  const brierCi = summary.start_brier_ci95;
  text($("#backtest-mae-ci"), maeCi ? `MAE ${Number(maeCi[0]).toFixed(2)}–${Number(maeCi[1]).toFixed(2)}` : "MAE —");
  text($("#backtest-brier-ci"), brierCi ? `Start Brier ${Number(brierCi[0]).toFixed(3)}–${Number(brierCi[1]).toFixed(3)}` : "Start Brier —");
  if (lift != null) {
    text($("#backtest-verdict-title"), lift > 0 ? "The model is beating the best simple baseline" : lift < 0 ? "A simple baseline currently leads" : "The model is level with the best baseline");
    text($("#backtest-verdict-copy"), `Across compatible completed gameweeks, the projected-points XI scores ${Math.abs(Number(lift)).toFixed(1)} actual points per GW ${lift >= 0 ? "more than" : "fewer than"} the strongest available baseline. Treat this as provisional until the calibration threshold is reached.`);
  } else {
    text($("#backtest-verdict-title"), "Collecting historical evidence");
    text($("#backtest-verdict-copy"), `The engine has ${summary.evaluated_events || 0} completed forecast gameweeks and ${summary.sample_size || 0} player samples. Benchmark results start only from compatible pre-deadline snapshots—missing history is never reconstructed.`);
  }
  renderBacktestBenchmarks(report.benchmarks || []);
  renderBacktestEvents(report.events || []);
  renderBacktestCalibration(report.start_calibration || []);
  renderBacktestDecisions(report.decisions || {});
  text($("#backtest-limitations"), (report.methodology?.limitations || []).join(" "));
}

function renderDecisionLedger(report) {
  state.ledger = report;
  const summary = report.summary || {};
  const scored = Number(summary.verified_transfers || 0);
  text($("#ledger-net"), scored ? signed(summary.verified_net_points, " pts") : "—");
  $("#ledger-net").classList.toggle("negative", scored > 0 && Number(summary.verified_net_points) < 0);
  text($("#ledger-verified"), scored);
  text($("#ledger-unverified"), Number(summary.unverified_decisions || 0) + Number(summary.pending || 0));
  if (report.methodology) text($("#ledger-methodology"), report.methodology);
  const root = $("#ledger-records");
  root.replaceChildren();
  if (report.status === "connect_team") {
    root.appendChild(backtestEmpty("Connect your FPL team first. Future pre-deadline advice will then be recorded here."));
    return;
  }
  const filter = $("#ledger-status-filter").value;
  const records = (report.records || []).filter((item) => {
    if (filter === "all") return true;
    if (filter === "verified") return ["verified_transfer", "verified_hold"].includes(item.status);
    if (filter === "unverified") return ["unverified", "followed_unscored"].includes(item.status);
    return item.status === filter;
  });
  if (!records.length) {
    const issues = report.tracking?.issues || [];
    root.appendChild(backtestEmpty((report.records || []).length
      ? "No decisions match this filter."
      : issues.length
        ? `No decision has been locked yet. ${issues.join(" ")}`
        : "No pre-deadline decisions have been saved for this team yet. Open the app before a future deadline to start the ledger."));
    if (!(report.records || []).length && issues.some((issue) => issue.includes("My Team snapshot"))) {
      const connect = document.createElement("button");
      connect.type = "button";
      connect.className = "ghost compact ledger-team-link";
      connect.textContent = "Open My team for one-step import";
      connect.addEventListener("click", () => {
        setView("team");
        $("#manager-verification")?.scrollIntoView({ block: "start" });
      });
      root.appendChild(connect);
    }
    return;
  }
  const labels = {
    verified_transfer: "Verified impact",
    verified_hold: "Hold confirmed",
    followed_unscored: "Matched · not scorable",
    not_followed: "Not matched",
    unverified: "Unverified",
    pending: "Awaiting results",
  };
  records.forEach((item) => {
    const card = document.createElement("article");
    card.className = `ledger-record ${item.status}`;
    if (item.realized && Number(item.realized.net_points) < 0) card.classList.add("negative");
    const header = document.createElement("div"); header.className = "ledger-record-header";
    const gw = document.createElement("strong"); gw.textContent = `GW${item.gameweek}`;
    const badge = document.createElement("span"); badge.className = "ledger-badge"; badge.textContent = labels[item.status] || "Unverified";
    header.append(gw, badge);
    const action = document.createElement("h4");
    action.textContent = item.action === "transfer" && item.sell && item.buy
      ? `${item.sell.name || "Player"} → ${item.buy.name || "Player"}`
      : item.action === "do_nothing" ? "Hold the transfer" : "Decision unavailable";
    const details = document.createElement("div"); details.className = "ledger-record-metrics";
    const expectation = document.createElement("div");
    const expectedLabel = document.createElement("span"); expectedLabel.textContent = "Pre-deadline GW1 estimate";
    const expectedValue = document.createElement("strong");
    expectedValue.textContent = item.predicted_gw1_direct == null ? "—" : signed(item.predicted_gw1_direct, " pts");
    expectation.append(expectedLabel, expectedValue);
    const outcome = document.createElement("div");
    const outcomeLabel = document.createElement("span"); outcomeLabel.textContent = "Verified net vs hold";
    const outcomeValue = document.createElement("strong");
    outcomeValue.textContent = item.realized ? signed(item.realized.net_points, " pts") : "—";
    outcome.append(outcomeLabel, outcomeValue);
    details.append(expectation, outcome);
    const explanation = document.createElement("p"); explanation.textContent = item.explanation || "";
    card.append(header, action, details, explanation);
    if (item.realized) {
      const calc = document.createElement("small"); calc.className = "ledger-calculation";
      calc.textContent = `Bought player ${item.realized.buy_points} pts − held player ${item.realized.hold_points} pts − transfer cost ${item.realized.hit_cost} pts = ${signed(item.realized.net_points, " pts")}.`;
      card.appendChild(calc);
    }
    const footer = document.createElement("small"); footer.className = "ledger-snapshot";
    footer.textContent = `Saved ${formatDate(item.snapshot_at)}${item.model_version ? ` · ${item.model_version}` : ""}${item.input_quality === "estimate_only" ? " · affordability was estimated" : ""}`;
    card.appendChild(footer);
    root.appendChild(card);
  });
}

async function loadDecisionLedger(force = false) {
  if (state.ledger && !force) return renderDecisionLedger(state.ledger);
  if (state.ledgerLoading) return state.ledgerLoading;
  const view = $("#view-ledger");
  const button = $("#refresh-ledger");
  view.setAttribute("aria-busy", "true");
  button.disabled = true;
  button.textContent = "Checking…";
  state.ledgerLoading = api(force ? "/api/decision-ledger?refresh=1" : "/api/decision-ledger")
    .then(renderDecisionLedger)
    .catch((error) => {
      $("#ledger-records").replaceChildren(backtestEmpty(`Decision history unavailable: ${error.message}`));
      notice(`Decision history unavailable: ${error.message}`, true);
    })
    .finally(() => {
      state.ledgerLoading = null;
      view.setAttribute("aria-busy", "false");
      button.disabled = false;
      button.textContent = "Check latest results";
    });
  return state.ledgerLoading;
}

async function loadBacktestReport(force = false) {
  if (state.backtestReport && !force) return renderBacktestReport(state.backtestReport);
  if (state.backtestLoading) return state.backtestLoading;
  const button = $("#run-backtest");
  const view = $("#view-backtesting");
  if (view) view.setAttribute("aria-busy", "true");
  if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); button.textContent = force ? "Scoring…" : "Loading…"; }
  state.backtestLoading = api(force ? "/api/backtest" : "/api/backtest-report", force ? { method: "POST", body: "{}" } : {})
    .then((result) => renderBacktestReport(result.report || result))
    .catch((error) => notice(`Backtest unavailable: ${error.message}`, true))
    .finally(() => {
      state.backtestLoading = null;
      if (view) view.setAttribute("aria-busy", "false");
      if (button) { button.disabled = false; button.removeAttribute("aria-busy"); button.textContent = "Run backtest now"; }
    });
  return state.backtestLoading;
}

function chipStatusLabel(status) {
  return { strong: "Strong opportunity", consider: "Consider", save: "Save", used: "Used" }[status] || "Prepare";
}

function chipMetric(label, value, note = "") {
  const node = document.createElement("div");
  const dt = document.createElement("span"); dt.textContent = label;
  const dd = document.createElement("strong"); dd.textContent = value;
  node.append(dt, dd);
  if (note) { const small = document.createElement("small"); small.textContent = note; node.appendChild(small); }
  return node;
}

function renderOpportunityBars(root, opportunities, { suffix = "pts", selectedEvent = null } = {}) {
  root.replaceChildren();
  const ordered = [...(opportunities || [])].sort((a, b) => Number(a.event) - Number(b.event));
  if (!ordered.length) { const p = document.createElement("p"); p.className = "empty"; p.textContent = "No projected opportunities are available."; root.appendChild(p); return; }
  const maximum = Math.max(...ordered.map((item) => Math.max(Number(item.gain || 0), 0)), 1);
  ordered.forEach((item) => {
    const row = document.createElement("div"); row.className = `chip-opportunity-row${Number(item.event) === Number(selectedEvent) ? " selected" : ""}`;
    const label = document.createElement("strong"); label.textContent = `GW${item.event}`;
    const track = document.createElement("div"); track.className = "chip-opportunity-track";
    const fill = document.createElement("i"); fill.style.width = `${Math.max(3, Number(item.gain || 0) / maximum * 100)}%`; track.appendChild(fill);
    const value = document.createElement("span"); value.textContent = `${signed(item.gain)} ${suffix}`;
    row.append(label, track, value); root.appendChild(row);
  });
}

function renderChipCards(plan) {
  const root = $("#chip-cards"); root.replaceChildren();
  (plan.cards || []).forEach((card) => {
    const article = document.createElement("article"); article.className = `panel chip-card ${card.status}`;
    const top = document.createElement("div"); top.className = "chip-card-top";
    const title = document.createElement("div");
    const label = document.createElement("span"); label.className = "eyebrow"; label.textContent = chipStatusLabel(card.status);
    const h = document.createElement("h3"); h.textContent = card.name; title.append(label, h);
    const badge = document.createElement("span"); badge.className = `chip-recommendation ${String(card.recommendation).toLowerCase()}`; badge.textContent = card.recommendation;
    top.append(title, badge);
    const metrics = document.createElement("div"); metrics.className = "chip-card-metrics";
    metrics.append(
      chipMetric("Best Gameweek", card.best_event ? `GW${card.best_event}` : "—"),
      chipMetric("Expected gain", signed(card.expected_gain, " pts")),
      chipMetric("Model signal", `${Math.round(Number(card.confidence || 0) * 100)}/100`),
    );
    const why = document.createElement("p"); why.className = "chip-card-why"; why.textContent = card.reason;
    const alternative = document.createElement("p"); alternative.className = "chip-card-alternative"; alternative.textContent = `Alternative · ${card.alternative}`;
    const button = document.createElement("button"); button.type = "button"; button.className = "ghost compact chip-card-open"; button.dataset.chipFocus = card.key; button.textContent = `View ${card.name} plan`;
    article.append(top, metrics, why, alternative, button); root.appendChild(article);
  });
}

function renderTripleCaptain(plan) {
  const detail = plan.triple_captain || {};
  const best = (detail.opportunities || []).find((item) => Number(item.event) === Number(detail.best_event));
  text($("#tc-title"), best ? `GW${best.event} Triple Captain rankings` : "Captain opportunity curve");
  const status = $("#tc-status"); status.textContent = best ? `${signed(best.gain)} pts` : "Unavailable"; status.className = "status-pill good";
  renderOpportunityBars($("#tc-opportunities"), detail.opportunities, { selectedEvent: detail.best_event });
  const root = $("#tc-candidates"); root.replaceChildren();
  if (!best) return;
  (best.rankings || []).slice(0, 5).forEach((player, index) => {
    const row = document.createElement("div"); row.className = "chip-candidate";
    const rank = document.createElement("span"); rank.className = "chip-rank"; rank.textContent = String(index + 1).padStart(2, "0");
    const identity = document.createElement("div");
    const name = document.createElement("strong"); name.textContent = player.name;
    const meta = document.createElement("small"); meta.textContent = `${player.team} · ${player.fixture}${player.penalty_taker ? " · Penalties" : ""}`; identity.append(name, meta);
    const points = document.createElement("strong"); points.className = "chip-candidate-points"; points.textContent = `${Number(player.expected_points).toFixed(1)} xPts`;
    const score = document.createElement("span"); score.className = "chip-score-mini"; score.textContent = `${player.scores.overall}/100`;
    row.append(rank, identity, points, score);
    if (index === 0) {
      const factors = document.createElement("div"); factors.className = "tc-factor-grid";
      [["Fixture", player.scores.fixture], ["Minutes", player.scores.minutes], ["Goal threat", player.scores.goal_threat], ["Assist threat", player.scores.assist_threat], ["Rotation risk", player.scores.rotation_risk]].forEach(([label, value]) => factors.append(chipMetric(label, `${value}/100`)));
      row.appendChild(factors);
    }
    root.appendChild(row);
  });
}

function renderBenchBoost(plan) {
  const detail = plan.bench_boost || {};
  const best = (detail.opportunities || []).find((item) => Number(item.event) === Number(detail.best_event));
  text($("#bb-title"), best ? `GW${best.event} bench projection` : "Bench readiness");
  text($("#bb-score"), best ? `${best.readiness}/100` : "—");
  const root = $("#bb-players"); root.replaceChildren();
  (best?.bench || []).forEach((player) => {
    const row = document.createElement("div"); row.className = "chip-player-row";
    const pos = document.createElement("span"); pos.className = "chip-position"; pos.textContent = player.position;
    const identity = document.createElement("div");
    const name = document.createElement("strong"); name.textContent = player.name;
    const meta = document.createElement("small"); meta.textContent = `${player.team} · ${player.fixture}`; identity.append(name, meta);
    const mins = document.createElement("span"); mins.textContent = `${player.expected_minutes} min · ${Math.round(player.start_probability * 100)}% start`;
    const points = document.createElement("strong"); points.textContent = `${Number(player.expected_points).toFixed(1)} pts`;
    row.append(pos, identity, mins, points); root.appendChild(row);
  });
  const prep = $("#bb-preparation"); prep.replaceChildren();
  if (detail.preparation) {
    const h = document.createElement("strong"); h.textContent = `Prepare: ${detail.preparation.sell.name} → ${detail.preparation.buy.name}`;
    const p = document.createElement("p"); p.textContent = `This raises the projected Bench Boost from ${Number(detail.best_gain).toFixed(1)} to ${Number(detail.preparation.prepared_value).toFixed(1)} points in GW${detail.best_event}.`;
    prep.append(h, p);
  } else {
    const p = document.createElement("p"); p.textContent = "No affordable single bench upgrade adds enough value yet."; prep.appendChild(p);
  }
}

function renderRebuildComparison(root, detail, label) {
  root.replaceChildren();
  const best = (detail.opportunities || []).find((item) => Number(item.event) === Number(detail.best_event));
  if (!best) { const p = document.createElement("p"); p.className = "empty"; p.textContent = "No legal optimized squad is available."; root.appendChild(p); return; }
  const score = document.createElement("div"); score.className = "chip-rebuild-score";
  score.append(
    chipMetric("Current best XI", `${Number(best.current_points).toFixed(1)} pts`),
    chipMetric(`Optimal ${label}`, `${Number(best.optimized_points).toFixed(1)} pts`),
    chipMetric("Projected gain", signed(best.gain, " pts"), `Best in GW${best.event}`),
  );
  const optimized = detail.recommended_squad || {};
  const changes = document.createElement("p"); changes.className = "chip-change-note";
  changes.textContent = `${optimized.formation || "Legal XI"} · £${Number(optimized.squad_cost || 0).toFixed(1)}m · Captain ${optimized.captain?.name || "—"} · ${(optimized.comparison || {}).changes || 0} squad changes`;
  const preview = document.createElement("div"); preview.className = "chip-team-preview hidden"; preview.dataset.teamPreview = label.toLowerCase().replaceAll(" ", "-");
  (optimized.squad || []).forEach((player) => {
    const tag = document.createElement("span"); tag.textContent = `${player.position} · ${player.name}`; preview.appendChild(tag);
  });
  root.append(score, changes, preview);
}

function renderWildcard(plan) {
  const detail = plan.wildcard || {};
  const root = $("#wildcard-comparison"); root.replaceChildren();
  const horizonGrid = document.createElement("div"); horizonGrid.className = "chip-horizon-grid";
  (detail.horizon_comparison || []).forEach((item) => horizonGrid.append(chipMetric(`${item.horizon} GWs`, signed(item.gain, " pts"), `${Number(item.current_points).toFixed(1)} → ${Number(item.optimized_points).toFixed(1)}`)));
  root.appendChild(horizonGrid);
  const best = (detail.opportunities || []).find((item) => Number(item.event) === Number(detail.best_event));
  if (best) {
    const note = document.createElement("p"); note.className = "chip-change-note"; note.textContent = `Best visible timing: GW${best.event}. The ${best.horizon}-GW rebuild improves the current squad by ${signed(best.gain, " points")} with ${best.changes} changes.`; root.appendChild(note);
    const preview = document.createElement("div"); preview.className = "chip-team-preview hidden"; preview.dataset.teamPreview = "wildcard";
    (detail.recommended_squad?.squad || []).forEach((player) => { const tag = document.createElement("span"); tag.textContent = `${player.position} · ${player.name}`; preview.appendChild(tag); }); root.appendChild(preview);
  }
  renderOpportunityBars($("#wildcard-opportunities"), detail.opportunities, { selectedEvent: detail.best_event });
}

function renderFreeHit(plan) {
  const detail = plan.free_hit || {};
  renderRebuildComparison($("#freehit-comparison"), detail, "Free Hit XI");
  renderOpportunityBars($("#freehit-opportunities"), detail.opportunities, { selectedEvent: detail.best_event });
}

function renderStrategies(plan) {
  const strategy = plan.season_strategy || {};
  text($("#strategy-window"), strategy.window ? `GW${strategy.window.start}–GW${strategy.window.end}` : "8-GW window");
  const root = $("#chip-strategies"); root.replaceChildren();
  (strategy.strategies || []).forEach((item, index) => {
    const safe = plan.decision_safety === "ready";
    const article = document.createElement("article"); article.className = `chip-strategy${index === 0 && safe ? " recommended" : ""}`;
    const heading = document.createElement("div");
    const label = document.createElement("span"); label.className = "eyebrow"; label.textContent = index === 0 ? safe ? "Recommended strategy" : "Illustrative strategy · confirm data" : `Alternative ${index}`;
    const h = document.createElement("h4"); h.textContent = item.name; heading.append(label, h);
    const gain = document.createElement("strong"); gain.className = "chip-strategy-gain"; gain.textContent = signed(item.projected_advantage, " pts");
    const timeline = document.createElement("div"); timeline.className = "chip-strategy-timeline";
    (item.sequence || []).forEach((step) => { const chip = document.createElement("span"); chip.textContent = `${step.chip.replaceAll("_", " ")} · GW${step.event}`; timeline.appendChild(chip); });
    article.append(heading, gain, timeline); root.appendChild(article);
  });
  const eventSelect = $("#chip-scenario-event"); eventSelect.replaceChildren();
  (plan.forecast_window?.events || []).forEach((event) => { const option = document.createElement("option"); option.value = event; option.textContent = `GW${event}`; eventSelect.appendChild(option); });
}

function renderChipPlan(plan) {
  state.chipPlan = plan;
  if (plan.status !== "ready") {
    text($("#chip-summary-title"), plan.message || "Chip planning is unavailable");
    text($("#chip-summary-copy"), "Connect a complete squad and generate projections to continue.");
    return;
  }
  const summary = plan.summary || {};
  text($("#chip-summary-title"), summary.headline);
  text($("#chip-summary-copy"), summary.explanation);
  text($("#chip-summary-opportunity"), summary.strongest_name ? `Best opportunity · ${summary.strongest_name} GW${summary.best_event}` : "Best opportunity · —");
  text($("#chip-summary-gain"), `Expected gain · ${signed(summary.expected_gain, " pts")}`);
  text($("#chip-summary-confidence"), `Model signal · ${Math.round(Number(summary.confidence || 0) * 100)}/100 heuristic`);
  renderChipCards(plan);
  renderTripleCaptain(plan);
  renderBenchBoost(plan);
  renderWildcard(plan);
  renderFreeHit(plan);
  renderStrategies(plan);
  const boundaries = $("#chip-boundary-list"); boundaries.replaceChildren();
  [...(plan.data_warnings || []), ...(plan.data_boundaries || [])].forEach((item) => { const p = document.createElement("p"); p.textContent = item; boundaries.appendChild(p); });
}

async function loadChipPlanner(force = false) {
  if (state.chipPlan && !force) return renderChipPlan(state.chipPlan);
  if (state.chipPlanLoading) return state.chipPlanLoading;
  const button = $("#refresh-chip-plan");
  if (button) { button.disabled = true; button.textContent = "Simulating…"; }
  state.chipPlanLoading = api("/api/chip-planner")
    .then(renderChipPlan)
    .catch((error) => notice(`Chip planner unavailable: ${error.message}`, true))
    .finally(() => {
      state.chipPlanLoading = null;
      if (button) { button.disabled = false; button.textContent = "Recalculate strategy"; }
    });
  return state.chipPlanLoading;
}

function toggleChipTeam(kind) {
  const preview = document.querySelector(`[data-team-preview="${kind}"]`);
  if (!preview) return;
  preview.classList.toggle("hidden");
  preview.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function simulateChipScenario() {
  const chip = $("#chip-scenario-chip").value;
  const event = Number($("#chip-scenario-event").value);
  const root = $("#chip-scenario-result"); root.classList.remove("hidden"); root.textContent = "Running counterfactual…";
  try {
    const result = await api("/api/chip-plan", { method: "POST", body: JSON.stringify({ chip, event }) });
    const scenario = result.scenario;
    root.textContent = scenario ? `${chip.replaceAll("_", " ")} in GW${event}: ${signed(scenario.gain, " pts")}. ${scenario.verdict}${scenario.available ? "" : " This chip is recorded as already used."}` : "That scenario is outside the current forecast window.";
  } catch (error) { root.textContent = error.message; }
}

async function loadAll() {
  try {
    await Promise.all([loadStatus(), loadTeams(), loadProfile(), loadQuality(), loadCalibration(), loadTransfers()]);
    if (state.currentSquad.length) renderSquad(state.currentSquad);
    await loadDashboard(true);
  }
  catch (error) { notice(error.message, true); }
}

function debounce(callback, wait = 250) {
  let timer;
  return (...args) => { window.clearTimeout(timer); timer = window.setTimeout(() => callback(...args), wait); };
}

document.addEventListener("DOMContentLoaded", () => {
  const activateSkipLink = (event) => {
    event.preventDefault();
    $("#main-content")?.focus({ preventScroll: true });
    $("#main-content")?.scrollIntoView({ block: "start" });
  };
  $(".skip-link")?.addEventListener("click", activateSkipLink);
  $(".skip-link")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") activateSkipLink(event);
  });
  $$(".nav-item").forEach((item) => item.addEventListener("click", () => setView(item.dataset.view)));
  $("#refresh-button").addEventListener("click", refreshData);
  $("#team-form").addEventListener("submit", submitTeam);
  $("#manager-verification-form")?.addEventListener("submit", submitManagerVerification);
  $("#my-team-snapshot-form")?.addEventListener("submit", submitMyTeamSnapshot);
  $("#manual-transfer-form")?.addEventListener("submit", submitManualTransfer);
  $("#manual-transfer-out")?.addEventListener("change", populateManualTransferTargets);
  $("#run-backtest")?.addEventListener("click", () => loadBacktestReport(true));
  $("#refresh-ledger")?.addEventListener("click", () => loadDecisionLedger(true));
  $("#ledger-status-filter")?.addEventListener("change", () => { if (state.ledger) renderDecisionLedger(state.ledger); });
  $("#ledger-model-performance")?.addEventListener("click", () => setView("backtesting"));
  $("#refresh-chip-plan")?.addEventListener("click", () => { state.chipPlan = null; loadChipPlanner(true); });
  $("#view-chip-strategy")?.addEventListener("click", () => $("#chip-season-strategy")?.scrollIntoView({ behavior: "smooth" }));
  $("#build-wildcard")?.addEventListener("click", () => toggleChipTeam("wildcard"));
  $("#build-free-hit")?.addEventListener("click", () => toggleChipTeam("free-hit-xi"));
  $("#chip-cards")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-chip-focus]");
    if (!button) return;
    const targets = { triple_captain: "#tc-title", bench_boost: "#bb-title", wildcard: "#wildcard-comparison", free_hit: "#freehit-comparison" };
    document.querySelector(targets[button.dataset.chipFocus])?.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $("#chip-scenario-form")?.addEventListener("submit", (event) => { event.preventDefault(); simulateChipScenario(); });
  $("#dashboard-filters")?.addEventListener("submit", (event) => {
    event.preventDefault();
    applyPlanningPreferences("dashboard");
  });
  $("#dashboard-open-optimizer")?.addEventListener("click", () => setView("optimizer"));
  $("#hero-analyse-team")?.addEventListener("click", () => setView("team"));
  $("#hero-see-evidence")?.addEventListener("click", () => setView("backtesting"));
  $$(".pricing-team-cta").forEach((button) => button.addEventListener("click", () => setView("team")));
  $("#assistant-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    askAssistant();
  });
  $("#assistant-clear-history")?.addEventListener("click", clearAssistantHistory);
  $("#assistant-scenario-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    submitAssistantScenario();
  });
  $("#assistant-scenario-type")?.addEventListener("change", updateAssistantScenarioForm);
  $("#assistant-scenario-sell")?.addEventListener("change", updateAssistantScenarioBuyOptions);
  $("#assistant-scenario-sell")?.addEventListener("input", updateAssistantScenarioBuyOptions);
  $$(".assistant-scenario-field input").forEach((input) => input.addEventListener("input", () => input.setCustomValidity("")));
  ["#strategy-horizon", "#strategy-risk"].forEach((selector) => $(selector)?.addEventListener("change", () => {
    applyPlanningPreferences("strategy");
  }));
  $("#run-squad-optimizer")?.addEventListener("click", () => loadSquadOptimizer(true));
  $("#formation-select")?.addEventListener("change", (event) => {
    const next = event.target.value;
    state.selectedFormation = next;
    localStorage.setItem(SAVED_FORMATION_KEY, next);
    renderSquad(state.currentSquad || []);
  });
  $("#pitch-view-button")?.addEventListener("click", () => setSquadDisplayMode("pitch"));
  $("#list-view-button")?.addEventListener("click", () => setSquadDisplayMode("list"));
  $("#close-team-player-detail")?.addEventListener("click", () => {
    $("#team-player-detail").classList.add("hidden");
  });
  $("#player-search").addEventListener("input", debounce(loadPlayers));
  ["#player-team", "#player-position", "#player-sort"].forEach((selector) => $(selector).addEventListener("change", loadPlayers));
  ["#fixture-start", "#fixture-horizon"].forEach((selector) => $(selector).addEventListener("change", loadFixtures));
  const initial = window.location.hash.slice(1);
  if (VIEWS.includes(initial)) setView(initial, { updateHash: false, scroll: false });
  else {
    window.history.replaceState(null, "", "#overview");
    setView("overview", { updateHash: false, scroll: false });
  }
  window.addEventListener("hashchange", () => setView(window.location.hash.slice(1), { updateHash: false }));
  $("#projection-search").addEventListener("input", debounce(loadProjections));
  ["#projection-team", "#projection-position", "#projection-horizon", "#projection-sort"].forEach((selector) => $(selector).addEventListener("change", loadProjections));
  $("#close-projection-detail").addEventListener("click", () => {
    $("#projection-detail").classList.add("hidden");
    state.lastProjectionTrigger?.focus();
  });
  const rerenderPitchOnResize = debounce(() => {
    const squad = state.currentSquad || [];
    if (squad.length) renderSquad(squad);
    if (state.squadOptimizer?.status === "ready") renderOptimizerPitch(state.squadOptimizer);
  }, 140);
  window.addEventListener("resize", rerenderPitchOnResize);
  loadAll();
});
