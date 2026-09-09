/* Apex frontend.
 *
 * Two independent data paths, mirroring the backend:
 *   loadPredictions()  ->  /api/predictions/*   deterministic model output
 *   sendChat()         ->  /api/chat            the Claude agent
 * Nothing in the predictions path calls the chat path or vice versa.
 *
 * All text reaches the DOM through textContent. Feed summaries are already
 * tag-stripped server-side; this is the second of the two layers.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  mode: "upcoming",
  circuit: "marina_bay",
  raceId: null,
  circuits: [],
  races: [],
  status: null,
  chatBusy: false,
  newsLoaded: false,
  newsItems: [],
  newsSources: [],
  newsCategories: [],
  newsFilter: null,
  lastRetrainedAt: null,
  seasonRounds: [],
  seasonYear: null,
  // Set only by clicking a specific future round on the season strip --
  // everywhere else (a plain circuit chip, switching modes by hand) means
  // "predict the next race," which is what leaving this null does.
  upcomingRound: null,
};

const STATUS_POLL_MS = 60_000;

const FEATURE_LABELS = {
  driver_standing_before: "Championship pos.",
  constructor_standing_before: "Constructor pos.",
  driver_circuit_avg_finish: "Avg here",
  driver_recent_form: "Recent form",
  constructor_recent_form: "Team form",
  years_experience: "Seasons",
  is_rookie: "Rookie",
};

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};

const api = async (path, options) => {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_) { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return response.json();
};

/* ---------------------------------------------------------------- track art
 * Every circuit gets a distinct, stable piece of generated line art: a closed
 * spline whose shape is seeded from the circuit id. It is deliberately not a
 * survey-accurate track map -- it is a consistent visual identity that needs
 * no image licence and renders identically offline.
 */

function seedFrom(text) {
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function mulberry32(seed) {
  return function random() {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function trackPath(circuitId) {
  const random = mulberry32(seedFrom(circuitId));
  const count = 9 + Math.floor(random() * 4);
  const points = [];
  for (let i = 0; i < count; i += 1) {
    const angle = (i / count) * Math.PI * 2;
    const radius = 0.52 + random() * 0.46;
    points.push([
      50 + Math.cos(angle) * radius * 40,
      50 + Math.sin(angle) * radius * 30,
    ]);
  }
  // Catmull-Rom through the points, closed, converted to cubic beziers.
  let path = `M ${points[0][0].toFixed(2)} ${points[0][1].toFixed(2)}`;
  for (let i = 0; i < count; i += 1) {
    const p0 = points[(i - 1 + count) % count];
    const p1 = points[i];
    const p2 = points[(i + 1) % count];
    const p3 = points[(i + 2) % count];
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    path += ` C ${c1[0].toFixed(2)} ${c1[1].toFixed(2)}, ${c2[0].toFixed(2)} ${c2[1].toFixed(2)}, ${p2[0].toFixed(2)} ${p2[1].toFixed(2)}`;
  }
  return `${path} Z`;
}

function renderHeroArt(circuitId) {
  const art = $("#hero-art");
  art.innerHTML = "";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 100 100");
  // "meet" keeps the whole loop visible; the CSS anchors it as a motif on
  // the right rather than letting a wide viewport crop it away.
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  defs.innerHTML =
    '<linearGradient id="trackGrad" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0%" stop-color="#ff2d55" stop-opacity="0.85"/>' +
    '<stop offset="100%" stop-color="#5e5ce6" stop-opacity="0.35"/>' +
    "</linearGradient>";
  svg.appendChild(defs);

  const d = trackPath(circuitId);
  const glow = document.createElementNS("http://www.w3.org/2000/svg", "path");
  glow.setAttribute("d", d);
  glow.setAttribute("fill", "none");
  glow.setAttribute("stroke", "url(#trackGrad)");
  glow.setAttribute("stroke-width", "5");
  glow.setAttribute("stroke-linejoin", "round");
  glow.setAttribute("opacity", "0.16");
  glow.style.filter = "blur(3px)";  // CSS filter, not an SVG <filter> reference
  svg.appendChild(glow);

  const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
  line.setAttribute("d", d);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "url(#trackGrad)");
  line.setAttribute("stroke-width", "0.7");
  line.setAttribute("stroke-linejoin", "round");
  line.setAttribute("opacity", "0.9");
  svg.appendChild(line);

  art.appendChild(svg);
}

/* -------------------------------------------------------------------- tabs */

function showTab(name) {
  $$(".tab").forEach((tab) => tab.classList.toggle("is-active", tab.id === `tab-${name}`));
  $$("[data-tab]").forEach((btn) => btn.classList.toggle("is-active", btn.dataset.tab === name));
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (name === "news" && !state.newsLoaded) loadNews();
}

$$("[data-tab]").forEach((btn) => {
  btn.addEventListener("click", (event) => {
    event.preventDefault();
    showTab(btn.dataset.tab);
  });
});

/* ------------------------------------------------------------------ status */

// Re-polled periodically (see boot()) so a background retrain's progress
// -- and its eventual completion -- shows up without a page reload.
// Every branch below sets BOTH the "on" and the "off" state explicitly;
// a status that only ever adds a notice and never clears one would leave
// stale banners up once a retrain fixes what they were warning about.
async function loadStatus() {
  try {
    state.status = await api("/api/status");
  } catch (error) {
    return;
  }
  const { model, data, live, auto_update: autoUpdate } = state.status;
  const box = $("#nav-status");
  box.innerHTML = "";

  const dot = el("span", "dot");
  let label = data.cutoff.label ? `Data to ${data.cutoff.label}` : "No data";
  if (autoUpdate.state === "retraining") {
    dot.classList.add("dot--warn");
    label = "Retraining on latest race…";
  } else if (!model.ready) {
    dot.classList.add("dot--bad");
  } else if (live.online === false) {
    dot.classList.add("dot--warn");
  } else {
    dot.classList.add("dot--ok");
  }
  box.appendChild(dot);
  box.appendChild(el("span", null, label));

  const modelNotice = $("#predictions-notice");
  if (!model.ready) {
    modelNotice.className = "notice notice--bad";
    modelNotice.innerHTML = "";
    modelNotice.appendChild(el("strong", null, "The model needs rebuilding. "));
    modelNotice.appendChild(document.createTextNode(model.error || ""));
  } else {
    modelNotice.className = "notice is-hidden";
  }

  const assistantNotice = $("#assistant-notice");
  if (!state.status.assistant.available) {
    assistantNotice.className = "notice";
    assistantNotice.textContent = state.status.assistant.reason || "The assistant is unavailable.";
  } else {
    assistantNotice.className = "notice is-hidden";
  }

  // A completed retrain in the background changes the numbers under
  // whatever's already on screen -- worth an automatic refresh rather
  // than leaving pre-retrain predictions up until the next click.
  if (autoUpdate.last_retrained && autoUpdate.last_retrained !== state.lastRetrainedAt) {
    const isFirstStatus = state.lastRetrainedAt === null;
    state.lastRetrainedAt = autoUpdate.last_retrained;
    if (!isFirstStatus) {
      loadPredictions();
      loadSeason();
    }
  }
}

/* ------------------------------------------------------------- predictions */

// A pre-created <img data-wiki="..."> that hydratePhotos() fills in later --
// the same pattern for a driver headshot in the avatar circle and a team
// badge next to the team name, so one batched lookup covers both.
function wikiPhoto(title, className) {
  const img = el("img", className);
  img.alt = "";
  img.loading = "lazy";
  img.dataset.wiki = title;
  return img;
}

function avatarFor(driver, team) {
  const avatar = el("div", "avatar");
  avatar.style.background = `linear-gradient(150deg, ${team.color}, ${team.color}55)`;
  avatar.appendChild(el("span", null, driver.code));
  if (driver.name && driver.name !== driver.code) {
    avatar.appendChild(wikiPhoto(driver.name));
  }
  return avatar;
}

function featureBlock(key, value) {
  const box = el("div", "feature");
  box.appendChild(el("div", "feature__label", FEATURE_LABELS[key] || key));
  let text;
  if (value === null || value === undefined) text = "No data";
  else if (key === "is_rookie") text = value >= 0.5 ? "Yes" : "No";
  else if (key === "years_experience") text = String(Math.round(value));
  else if (key.endsWith("_before")) text = `P${Math.round(value)}`;
  else text = value.toFixed(1);
  const node = el("div", "feature__value", text);
  if (value === null || value === undefined) node.classList.add("feature__value--none");
  box.appendChild(node);
  return box;
}

function driverCard(row, index, showActual) {
  const card = el("article", "card");
  card.style.setProperty("--team", row.team.color);
  // predicted_top5 IS the top 5 rows by probability, not a raw percentage
  // cutoff -- a real top-5 finish always names exactly 5 drivers, so this
  // border is a visible, unambiguous marker of the 5 the model is actually
  // picking, distinct from "happens to be ranked 5th."
  if (row.predicted_top5) card.classList.add("card--picked");

  const main = el("div", "card__main");
  main.appendChild(el("div", "card__rank", String(index + 1)));
  main.appendChild(avatarFor(row.driver, row.team));

  const who = el("div", "card__who");
  who.appendChild(el("div", "card__name", row.driver.name));
  const teamRow = el("div", "card__team");
  teamRow.appendChild(wikiPhoto(row.team.name, "card__team-badge"));
  teamRow.appendChild(el("span", null, row.team.name));
  who.appendChild(teamRow);
  main.appendChild(who);

  const prob = el("div", "card__prob");
  prob.appendChild(el("div", "card__pct", `${Math.round(row.top5_probability * 100)}%`));
  const bar = el("div", "bar");
  const fill = el("div", "bar__fill");
  fill.style.width = `${Math.max(2, row.top5_probability * 100)}%`;
  bar.appendChild(fill);
  prob.appendChild(bar);
  if (showActual) prob.appendChild(el("div", "card__prob-label", "Predicted"));
  main.appendChild(prob);

  // On an elapsed race, what actually happened sits beside the prediction
  // rather than being a small badge under it -- the whole point of replay
  // is the comparison, so both halves get equal weight and a label saying
  // which is which.
  if (showActual) {
    main.classList.add("card__main--compare");
    const actual = el("div", "card__actual");
    actual.appendChild(el(
      "div", "card__actual-value",
      row.actual_position ? `P${row.actual_position}` : "DNF"
    ));
    if (row.actual_top5 !== null) {
      const hit = row.predicted_top5 === row.actual_top5;
      actual.appendChild(el(
        "div", `card__actual-verdict ${hit ? "is-hit" : "is-miss"}`,
        hit ? "Called right" : "Missed"
      ));
    }
    actual.appendChild(el("div", "card__prob-label", "Actual"));
    main.appendChild(actual);
  }

  card.appendChild(main);

  const detail = el("div", "card__detail");
  const inner = el("div");
  const features = el("div", "features");
  Object.entries(row.features).forEach(([key, value]) => {
    features.appendChild(featureBlock(key, value));
  });
  inner.appendChild(features);
  detail.appendChild(inner);
  card.appendChild(detail);

  main.addEventListener("click", () => card.classList.toggle("is-open"));
  return card;
}

function renderSkeletons() {
  const list = $("#driver-list");
  list.innerHTML = "";
  for (let i = 0; i < 8; i += 1) list.appendChild(el("div", "skeleton"));
}

function flagPill(circuit) {
  const pill = el("span", "pill");
  if (circuit.country) {
    const img = el("img");
    img.src = `https://flagcdn.com/w40/${circuit.country}.png`;
    img.alt = "";
    img.addEventListener("error", () => img.remove());
    pill.appendChild(img);
  }
  pill.appendChild(el("span", null, circuit.locality || circuit.name));
  return pill;
}

async function loadPredictions() {
  renderSkeletons();
  $("#scoreline").classList.add("is-hidden");

  let payload;
  try {
    if (state.mode === "upcoming") {
      const params = new URLSearchParams({ circuit: state.circuit });
      if (state.upcomingRound) {
        params.set("year", state.upcomingRound.year);
        params.set("round", state.upcomingRound.round);
      }
      payload = await api(`/api/predictions/upcoming?${params.toString()}`);
    } else {
      payload = await api(`/api/predictions/race/${encodeURIComponent(state.raceId)}`);
    }
  } catch (error) {
    $("#driver-list").innerHTML = "";
    const notice = $("#predictions-notice");
    notice.className = "notice notice--bad";
    notice.textContent = `Could not load predictions: ${error.message}`;
    return;
  }

  const isReplay = payload.mode === "replay";
  const circuit = isReplay ? payload.race.circuit : payload.circuit;

  renderHeroArt(circuit.id);
  $("#hero-eyebrow").textContent = isReplay
    ? `${payload.race.year} · Round ${payload.race.round} · result vs prediction`
    : `${payload.predicted_for.year} · Round ${payload.predicted_for.round} · prediction only`;
  $("#hero-title").textContent = circuit.name;

  const meta = $("#hero-meta");
  meta.innerHTML = "";
  meta.appendChild(flagPill(circuit));
  if (!isReplay && payload.form_as_of.label) {
    meta.appendChild(el("span", "pill", `Form as of ${payload.form_as_of.label}`));
  }
  if (state.status && state.status.model.ready) {
    meta.appendChild(el("span", "pill", state.status.model.name));
  }

  if (isReplay && payload.accuracy !== null) {
    const scoreline = $("#scoreline");
    scoreline.innerHTML = "";
    const hits = payload.drivers.filter(
      (row) => row.actual_top5 !== null && row.predicted_top5 === row.actual_top5
    ).length;
    const scored = payload.drivers.filter((row) => row.actual_top5 !== null).length;
    [
      [`${Math.round(payload.accuracy * 100)}%`, "Correct here (in-sample)"],
      [`${hits}/${scored}`, "Drivers called right"],
      [
        payload.drivers.filter((row) => row.actual_top5).map((r) => r.driver.code).join(" "),
        "Actual top 5",
      ],
    ].forEach(([value, label]) => {
      const stat = el("div", "stat");
      stat.appendChild(el("div", "stat__value", value));
      stat.appendChild(el("div", "stat__label", label));
      scoreline.appendChild(stat);
    });
    scoreline.classList.remove("is-hidden");
  }

  const list = $("#driver-list");
  list.innerHTML = "";
  payload.drivers.forEach((row, index) => list.appendChild(driverCard(row, index, isReplay)));

  $("#predictions-footnote").textContent = isReplay
    ? "Left column is what the model said beforehand; right column is the real classified result. Features are strictly pre-race: championship and constructor standing going into the race, seasons of experience, average finish at this circuit, and three-race form for driver and team — nothing from qualifying or from during the race. Read the score as a sanity check rather than a measure of accuracy, though: pipeline.py refits the exported model on every season before saving it, so a race shown here was part of that final fit. The honest number is the held-out score printed when you train."
    : `Prediction only — this race hasn't been run, so there are no actual results to compare against. Standings and form are as of ${payload.form_as_of.label || "the latest race in the data"}, which is the most recent information the model has; nothing between then and this race is knowable yet. Uses pre-qualifying information only — no grid position, no lap or pit-stop times. Tap a driver to see the inputs behind their number.`;

  hydratePhotos();
  renderSeasonStrip();
}

/* Photos are fetched only after the cards are on screen, and every one of
   them is optional -- driver monograms and the plain team-colour bar are
   the designed defaults, not placeholders waiting to be replaced. Covers
   both driver headshots (in the avatar circle) and team badges (next to
   the team name) in one batched lookup, since both are just <img
   data-wiki="..."> at this point. */
async function hydratePhotos() {
  const targets = $$("img[data-wiki]");
  const titles = Array.from(new Set(targets.map((node) => node.dataset.wiki)));
  if (!titles.length) return;

  let resolved;
  try {
    resolved = await api("/api/images", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ titles }),
    });
  } catch (error) {
    return;
  }

  targets.forEach((img) => {
    const found = resolved[img.dataset.wiki];
    if (!found || !found.image) return;
    img.addEventListener("load", () => img.classList.add("is-loaded"));
    img.addEventListener("error", () => img.remove());
    img.src = found.image;
  });
}

/* ------------------------------------------------------------------ season */

async function loadSeason() {
  let payload;
  try {
    payload = await api("/api/schedule");
  } catch (error) {
    return; // an enhancement, not a requirement -- leave the strip hidden
  }
  state.seasonRounds = payload.rounds;
  state.seasonYear = payload.year;
  const done = payload.rounds.filter((round) => round.completed).length;
  $("#season-label").textContent =
    `${payload.year} Season — ${done} of ${payload.rounds.length} races run`;
  $("#season").classList.toggle("is-hidden", payload.rounds.length === 0);
  renderSeasonStrip();
  updatePickerVisibility();
}

function renderSeasonStrip() {
  const track = $("#season-track");
  if (!track || !state.seasonRounds.length) return;
  track.innerHTML = "";

  state.seasonRounds.forEach((round) => {
    const item = el("button", "season__round");
    if (round.completed) item.classList.add("is-done");
    if (round.is_next) item.classList.add("is-next");

    const isSelected =
      state.mode === "replay"
        ? Boolean(round.race_id) && state.raceId === round.race_id
        : !round.completed &&
          state.circuit === round.circuit.id &&
          (state.upcomingRound ? state.upcomingRound.round === round.round : round.is_next);
    if (isSelected) item.classList.add("is-selected");

    item.appendChild(el("div", "season__round-num", `R${round.round}`));
    if (round.circuit.country) {
      const flag = el("img", "season__round-flag");
      flag.src = `https://flagcdn.com/w40/${round.circuit.country}.png`;
      flag.alt = "";
      flag.addEventListener("error", () => flag.remove());
      item.appendChild(flag);
    }
    item.appendChild(el("div", "season__round-name", round.circuit.locality || round.circuit.name));
    item.appendChild(
      el("div", "season__round-status", round.is_next ? "Next" : round.completed ? "Done" : "")
    );

    item.addEventListener("click", () => {
      if (round.completed && round.race_id) {
        setMode("replay");
        state.raceId = round.race_id;
        const select = $("#race-select");
        if (select) select.value = round.race_id;
      } else {
        setMode("upcoming");
        state.circuit = round.circuit.id;
        state.upcomingRound = { year: state.seasonYear, round: round.round };
        $$("#circuit-chips .chip").forEach((chip) => chip.classList.remove("is-active"));
      }
      loadPredictions();
    });

    track.appendChild(item);
  });
}

/* ----------------------------------------------------------------- pickers */

async function loadPickers() {
  const [circuits, races] = await Promise.all([api("/api/circuits"), api("/api/races")]);
  state.circuits = circuits;
  state.races = races;

  const chips = $("#circuit-chips");
  chips.innerHTML = "";
  circuits.forEach((circuit) => {
    const chip = el("button", "chip");
    if (circuit.country) {
      const img = el("img");
      img.src = `https://flagcdn.com/w40/${circuit.country}.png`;
      img.alt = "";
      img.addEventListener("error", () => img.remove());
      chip.appendChild(img);
    }
    chip.appendChild(el("span", null, circuit.locality || circuit.name));
    chip.classList.toggle("is-active", circuit.id === state.circuit);
    chip.addEventListener("click", () => {
      state.circuit = circuit.id;
      state.upcomingRound = null; // a plain circuit pick always means "next race"
      $$("#circuit-chips .chip").forEach((other) => other.classList.remove("is-active"));
      chip.classList.add("is-active");
      loadPredictions();
    });
    chips.appendChild(chip);
  });

  // The API returns newest-first; the dropdown reads chronologically so it
  // matches the season strip's left-to-right running order. Most recent is
  // still what's selected by default -- it's just last in the list now.
  const select = $("#race-select");
  select.innerHTML = "";
  const chronological = [...races].sort(
    (a, b) => a.year - b.year || a.round - b.round
  );
  chronological.forEach((race) => {
    const option = el("option", null, `${race.label} · R${race.round}`);
    option.value = race.race_id;
    select.appendChild(option);
  });
  state.raceId = chronological.length ? chronological[chronological.length - 1].race_id : null;
  if (state.raceId) select.value = state.raceId;
  select.addEventListener("change", () => {
    state.raceId = select.value;
    loadPredictions();
  });
}

function setMode(mode) {
  state.mode = mode;
  if (mode === "upcoming") state.upcomingRound = null; // resolved via the season strip, not here
  $$(".segmented__opt").forEach((opt) => opt.classList.toggle("is-active", opt.dataset.mode === mode));
  updatePickerVisibility();
}

// The season strip already lists every circuit on the calendar, in order,
// so showing the free-form circuit chips underneath it as well is just the
// same choice offered twice. The chips stay as the fallback for when no
// schedule could be loaded (offline, or a season with no calendar yet).
function updatePickerVisibility() {
  const hasSeasonStrip = state.seasonRounds.length > 0;
  $("#picker-upcoming").classList.toggle(
    "is-hidden", state.mode !== "upcoming" || hasSeasonStrip
  );
  $("#picker-replay").classList.toggle("is-hidden", state.mode !== "replay");
}

$$(".segmented__opt").forEach((opt) => {
  opt.addEventListener("click", () => {
    setMode(opt.dataset.mode);
    loadPredictions();
  });
});

/* -------------------------------------------------------------------- chat */

function addMessage(role, text) {
  const wrapper = el("div", `msg msg--${role}`);
  const bubble = el("div", "bubble", text);
  wrapper.appendChild(bubble);
  $("#chat").appendChild(wrapper);
  wrapper.scrollIntoView({ behavior: "smooth", block: "end" });
  return { wrapper, bubble };
}

async function sendChat(question) {
  if (state.chatBusy || !question.trim()) return;
  state.chatBusy = true;
  $("#composer-send").disabled = true;
  $("#suggestions").classList.add("is-hidden");

  addMessage("user", question);
  const pending = addMessage("bot", "");
  pending.bubble.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';

  try {
    const result = await api("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    });
    pending.bubble.innerHTML = "";
    if (result.tool_calls && result.tool_calls.length) {
      const strip = el("div", "toolstrip");
      result.tool_calls.forEach((call) => {
        strip.appendChild(el("span", "toolstrip__item", call.name));
      });
      pending.bubble.appendChild(strip);
    }
    if (result.error) {
      pending.wrapper.className = "msg msg--error";
      pending.bubble.appendChild(document.createTextNode(result.error));
    } else {
      pending.bubble.appendChild(document.createTextNode(result.answer || "No answer."));
    }
  } catch (error) {
    pending.wrapper.className = "msg msg--error";
    pending.bubble.textContent = error.message;
  } finally {
    state.chatBusy = false;
    $("#composer-send").disabled = false;
  }
}

$("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("#composer-input");
  const question = input.value;
  input.value = "";
  sendChat(question);
});

$$("#suggestions .chip").forEach((chip) => {
  chip.addEventListener("click", () => sendChat(chip.textContent));
});

/* -------------------------------------------------------------------- news */

function timeAgo(seconds) {
  if (!seconds) return "";
  const delta = Date.now() / 1000 - seconds;
  if (delta < 3600) return `${Math.max(1, Math.round(delta / 60))} min ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)} hr ago`;
  return `${Math.round(delta / 86400)} d ago`;
}

// Filters by topic (Drivers / Teams / Regulations / Race Weekend), not by
// outlet -- readers want "show me driver news," not "show me one site's
// coverage." Each headline is tagged server-side by keyword match against
// its title+summary and can carry more than one category.
function renderNewsFilters() {
  const box = $("#news-filter");
  box.innerHTML = "";
  const options = [["All", null], ...state.newsCategories.map((name) => [name, name])];
  options.forEach(([label, value]) => {
    const chip = el("button", "chip", label);
    chip.classList.toggle("is-active", state.newsFilter === value);
    chip.addEventListener("click", () => {
      state.newsFilter = value;
      $$("#news-filter .chip").forEach((other) => other.classList.remove("is-active"));
      chip.classList.add("is-active");
      renderNewsList();
    });
    box.appendChild(chip);
  });
}

function renderNewsList() {
  const list = $("#news-list");
  list.innerHTML = "";
  const items = state.newsFilter
    ? state.newsItems.filter((item) => item.categories.includes(state.newsFilter))
    : state.newsItems;

  if (!items.length) {
    if (state.newsItems.length) {
      list.appendChild(el("p", "footnote", `No recent headlines tagged "${state.newsFilter}".`));
    }
    return;
  }

  items.forEach((item) => {
    const story = el("a", "story");
    story.href = item.link;
    story.target = "_blank";
    story.rel = "noopener noreferrer";

    const media = el("div", "story__media");
    if (item.image) {
      const img = el("img");
      img.alt = "";
      img.loading = "lazy";
      img.addEventListener("load", () => img.classList.add("is-loaded"));
      img.addEventListener("error", () => img.remove());
      img.src = item.image;
      media.appendChild(img);
    }
    story.appendChild(media);

    const body = el("div", "story__body");
    body.appendChild(el("div", "story__source", item.source));
    body.appendChild(el("div", "story__title", item.title));
    if (item.summary) body.appendChild(el("div", "story__summary", item.summary));
    body.appendChild(el("div", "story__time", timeAgo(item.published)));
    story.appendChild(body);

    list.appendChild(story);
  });
}

async function loadNews() {
  const list = $("#news-list");
  list.innerHTML = "";
  for (let i = 0; i < 6; i += 1) {
    const skeleton = el("div", "skeleton");
    skeleton.style.height = "260px";
    list.appendChild(skeleton);
  }

  let payload;
  try {
    payload = await api("/api/news");
  } catch (error) {
    list.innerHTML = "";
    const notice = $("#news-notice");
    notice.className = "notice notice--bad";
    notice.textContent = `Could not load headlines: ${error.message}`;
    return;
  }

  state.newsLoaded = true;
  state.newsItems = payload.items;
  state.newsSources = payload.sources;
  state.newsCategories = payload.categories;
  $("#news-sources").textContent =
    `Headlines from ${payload.sources.join(", ")}, refreshed every few days.`;

  const notice = $("#news-notice");
  if (!payload.items.length) {
    notice.className = "notice";
    notice.textContent =
      "No headlines could be fetched. The feeds are read directly from each outlet, so this usually means no outbound network access.";
  } else if (payload.errors && payload.errors.length) {
    notice.className = "notice";
    notice.textContent = `Some feeds did not respond: ${payload.errors.join(", ")}.`;
  } else {
    notice.className = "notice is-hidden";
  }

  renderNewsFilters();
  renderNewsList();
}

/* -------------------------------------------------------------------- boot */

(async function boot() {
  renderHeroArt(state.circuit);
  await loadStatus();
  try {
    await loadPickers();
  } catch (error) {
    /* pickers are best-effort; predictions still work with the defaults */
  }
  await loadPredictions();
  loadSeason(); // independent of the predictions path; doesn't block first paint
  setInterval(loadStatus, STATUS_POLL_MS);
})();
