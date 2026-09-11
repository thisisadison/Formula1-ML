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
  analyticsLoaded: false,
  analytics: null,
  analyticsYear: null,
  analyticsFeature: null,
  lastRetrainedAt: null,
  seasonRounds: [],
  seasonYear: null,
  // Set only by clicking a specific future round on the season strip --
  // everywhere else (a plain circuit chip, switching modes by hand) means
  // "predict the next race," which is what leaving this null does.
  upcomingRound: null,
};

const STATUS_POLL_MS = 60_000;

// Leading entries whose breakdown is expanded on arrival, for the most
// recent race only (see driverCard's `open`).
const OPEN_BY_DEFAULT = 5;

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
  // Charts are sized in real pixels off their container, so they can only be
  // laid out once the tab is actually displayed -- hence the load-on-show
  // here rather than at boot.
  if (name === "analytics" && !state.analyticsLoaded) loadAnalytics();
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

// Photo URLs are resolved server-side (webapp/photos.py) and arrive on the
// driver/team objects, rather than being built here by naming convention.
// The frontend can't know whether a bundled file is VER.jpg,
// max_verstappen.JPG or neither, so it doesn't guess: a null photo means
// no file matched, and the monogram / bare team name stands in. The
// onerror path below still exists as a backstop for a file that exists but
// won't decode.
function localPhoto(src, className) {
  const img = el("img", className);
  img.alt = "";
  img.loading = "lazy";
  img.addEventListener("load", () => img.classList.add("is-loaded"));
  img.addEventListener("error", () => img.remove());
  img.src = src;
  return img;
}

// Two separate image bundles doing two separate jobs:
//
//   driver.headshot  -> this circular grid icon, shown for any race in the
//                       CURRENT season (the drivers are all still current,
//                       so the face is right for every round of it)
//   driver.photo     -> the large image in the expanded panel, shown only
//                       for the single most recent race
//
// Both are gated because driver CODES get reused across eras (VER is both
// Verstappen today and Vergne in 2012-2014; see reference.py's
// CURRENT_GRID), so an image keyed by code would put the wrong face on an
// older replay. Team ids have no such collision, so badges show anywhere.
function avatarFor(driver, team, showHeadshot) {
  const avatar = el("div", "avatar");
  avatar.style.background = `linear-gradient(150deg, ${team.color}, ${team.color}55)`;
  avatar.appendChild(el("span", null, driver.code));
  if (showHeadshot && driver.headshot) {
    avatar.appendChild(localPhoto(driver.headshot));
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

function driverCard(row, index, showActual, options) {
  const { showHeadshot = false, showRacePhoto = false, open = false } = options || {};
  const card = el("article", "card");
  card.style.setProperty("--team", row.team.color);
  // predicted_points IS the top 10 rows by probability, not a raw
  // percentage cutoff -- a real points finish always names exactly 10
  // drivers, so this border is a visible, unambiguous marker of the 10 the
  // model is actually picking, distinct from "happens to be ranked 10th."
  if (row.predicted_points) card.classList.add("card--picked");

  const main = el("div", "card__main");
  main.appendChild(el("div", "card__rank", String(index + 1)));
  main.appendChild(avatarFor(row.driver, row.team, showHeadshot));

  const who = el("div", "card__who");
  who.appendChild(el("div", "card__name", row.driver.name));
  const teamRow = el("div", "card__team");
  if (row.team.photo) {
    teamRow.appendChild(localPhoto(row.team.photo, "card__team-badge"));
  }
  teamRow.appendChild(el("span", null, row.team.name));
  who.appendChild(teamRow);
  main.appendChild(who);

  const prob = el("div", "card__prob");
  prob.appendChild(el("div", "card__pct", `${Math.round(row.points_probability * 100)}%`));
  const bar = el("div", "bar");
  const fill = el("div", "bar__fill");
  fill.style.width = `${Math.max(2, row.points_probability * 100)}%`;
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
    if (row.actual_points !== null) {
      const hit = row.predicted_points === row.actual_points;
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
  const body = el("div", "detailgrid");

  // The photo sits BESIDE the stats, not above them: the stats are only a
  // two-row block, so a full-width banner pushed them most of a screen
  // down for no extra information. Only where showRacePhoto allows one
  // (see avatarFor's comment). The outer .card__detail already slides the
  // whole panel open (grid-template-rows), so the photo only needs its own
  // opacity fade, timed to land just after that, rather than popping in.
  if (showRacePhoto && row.driver.photo) {
    const photo = el("div", "card__photo");
    const img = localPhoto(row.driver.photo);
    // A missing file has to collapse the whole column, not just the img --
    // otherwise the stats stay squashed into the right-hand track beside a
    // 220px gradient placeholder.
    img.addEventListener("error", () => {
      photo.remove();
      body.classList.add("detailgrid--nophoto");
    });
    photo.appendChild(img);
    body.appendChild(photo);
  } else {
    body.classList.add("detailgrid--nophoto");
  }

  const features = el("div", "features");
  Object.entries(row.features).forEach(([key, value]) => {
    features.appendChild(featureBlock(key, value));
  });
  body.appendChild(features);
  inner.appendChild(body);
  detail.appendChild(inner);
  card.appendChild(detail);

  main.addEventListener("click", () => card.classList.toggle("is-open"));
  // Opened on arrival for the leading entries of the most recent race, so
  // the landing view shows the reasoning rather than a list of closed rows
  // the reader has to know to tap. Still a plain class, so the first click
  // collapses it exactly as if they'd opened it themselves.
  if (open) card.classList.add("is-open");
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
      (row) => row.actual_points !== null && row.predicted_points === row.actual_points
    ).length;
    const scored = payload.drivers.filter((row) => row.actual_points !== null).length;
    [
      [`${Math.round(payload.accuracy * 100)}%`, "Correct here (in-sample)"],
      [`${hits}/${scored}`, "Drivers called right"],
      [
        payload.drivers.filter((row) => row.actual_points).map((r) => r.driver.code).join(" "),
        "Actual points finishers",
      ],
    ].forEach(([value, label]) => {
      const stat = el("div", "stat");
      const valueNode = el("div", "stat__value", value);
      // Up to 10 driver codes ("RUS VER NOR LEC PIA ANT ALO HAM TSU SAI")
      // is a lot more text than the percentages this size was tuned for.
      if (value.length > 14) valueNode.classList.add("stat__value--wide");
      stat.appendChild(valueNode);
      stat.appendChild(el("div", "stat__label", label));
      scoreline.appendChild(stat);
    });
    scoreline.classList.remove("is-hidden");
  }

  // state.races is newest-first, so [0] is the most recent completed race
  // and its year is the season currently being run.
  const mostRecent = state.races.length ? state.races[0] : null;
  const isMostRecentReplay =
    isReplay && mostRecent !== null && payload.race.race_id === mostRecent.race_id;

  // Headshots cover the whole current season: every round of it was raced
  // by the drivers on today's grid, so the face is right for all of them.
  // The big panel photo is deliberately narrower -- most recent race only.
  const raceYear = isReplay ? payload.race.year : payload.predicted_for.year;
  const currentSeason = mostRecent ? mostRecent.year
    : (state.status && state.status.data.next_slot.year) || null;
  const showHeadshot = currentSeason !== null && raceYear === currentSeason;
  const showRacePhoto = isMostRecentReplay;

  const list = $("#driver-list");
  list.innerHTML = "";
  payload.drivers.forEach((row, index) =>
    list.appendChild(driverCard(row, index, isReplay, {
      showHeadshot,
      showRacePhoto,
      open: isMostRecentReplay && index < OPEN_BY_DEFAULT,
    }))
  );

  $("#predictions-footnote").textContent = isReplay
    ? "Left column is what the model said beforehand; right column is the real classified result. Features are strictly pre-race: championship and constructor standing going into the race, seasons of experience, average finish at this circuit, and three-race form for driver and team — nothing from qualifying or from during the race. Read the score as a sanity check rather than a measure of accuracy, though: pipeline.py refits the exported model on every season before saving it, so a race shown here was part of that final fit. The honest number is the held-out score printed when you train."
    : `Prediction only — this race hasn't been run, so there are no actual results to compare against. Standings and form are as of ${payload.form_as_of.label || "the latest race in the data"}, which is the most recent information the model has; nothing between then and this race is knowable yet. Uses pre-qualifying information only — no grid position, no lap or pit-stop times. Tap a driver to see the inputs behind their number.`;

  renderSeasonStrip();
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

/* --------------------------------------------------------------- analytics
 *
 * Charts are hand-rolled inline SVG -- same stance as the rest of this
 * frontend: no build step, no charting dependency, nothing to load before
 * the first paint.
 *
 * They are drawn in REAL pixel coordinates measured off the container, not
 * in a scaled viewBox. A viewBox that stretches to fit would shrink the tick
 * labels along with the bars, and 9px type at 55% on a phone is unreadable;
 * re-rendering on resize costs a few milliseconds and keeps text at its
 * intended size at every width.
 *
 * Colour follows the job, not the entity: one hue for every single-series
 * chart (the categories are already named on the axis, so a second hue would
 * encode nothing), and a two-hue diverging scale ONLY on the correlation
 * chart, where the sign genuinely is the message.
 */

const SVG_NS = "http://www.w3.org/2000/svg";

const svgEl = (tag, attrs) => {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
};

const pct = (value, digits = 1) =>
  value === null || value === undefined || Number.isNaN(value)
    ? "—"
    : `${(value * 100).toFixed(digits)}%`;

/* A bar with rounded corners on the DATA end only. Rounding the baseline end
   too (which a plain rect with rx would do) detaches the bar from its axis
   and makes short bars read as floating pills. */
function barPath(x, y, width, height, radius, side) {
  const r = Math.max(0, Math.min(radius, side === "top" ? width / 2 : height / 2,
                                 side === "top" ? height : width));
  if (side === "top") {
    return `M${x},${y + height} V${y + r} Q${x},${y} ${x + r},${y} ` +
           `H${x + width - r} Q${x + width},${y} ${x + width},${y + r} V${y + height} Z`;
  }
  if (side === "left") {
    return `M${x + width},${y} H${x + r} Q${x},${y} ${x},${y + r} ` +
           `V${y + height - r} Q${x},${y + height} ${x + r},${y + height} H${x + width} Z`;
  }
  return `M${x},${y} H${x + width - r} Q${x + width},${y} ${x + width},${y + r} ` +
         `V${y + height - r} Q${x + width},${y + height} ${x + width - r},${y + height} H${x} Z`;
}

/* One tooltip element reused by every chart -- attaching a listener per bar is
   fine, but a node per bar is not. */
let chartTip = null;

function tipFor(node, lines) {
  node.addEventListener("mouseenter", (event) => {
    if (!chartTip) {
      chartTip = el("div", "charttip");
      document.body.appendChild(chartTip);
    }
    chartTip.innerHTML = "";
    lines.forEach((line, index) => {
      chartTip.appendChild(el("div", index === 0 ? "charttip__head" : "charttip__row", line));
    });
    chartTip.classList.add("is-on");
    moveTip(event);
  });
  node.addEventListener("mousemove", moveTip);
  node.addEventListener("mouseleave", () => chartTip && chartTip.classList.remove("is-on"));
}

function moveTip(event) {
  if (!chartTip) return;
  const pad = 14;
  const box = chartTip.getBoundingClientRect();
  const x = Math.min(Math.max(pad, event.clientX + pad), window.innerWidth - box.width - pad);
  const y = Math.max(pad, event.clientY - box.height - pad);
  chartTip.style.transform = `translate(${x}px, ${y}px)`;
}

function chartWidth(container, fallback = 640) {
  const width = container.clientWidth;
  return width > 40 ? width : fallback;
}

/* ---- correlation: the one genuinely diverging chart on the page ---------- */

function renderCorrelationChart(container, rows) {
  container.innerHTML = "";
  const usable = rows.filter((row) => row.correlation !== null);
  if (!usable.length) return;

  const width = chartWidth(container);
  const gutter = Math.min(150, Math.max(96, width * 0.3));
  // On a phone the gutter is too narrow for "Championship position", and an
  // SVG text node does not wrap or clip -- it just draws past the edge. The
  // short names the prediction cards already use fit; the tooltip still
  // carries the full one.
  const shortLabels = width < 560;
  const valueGap = 46;
  // Taller than a plain bar row -- there's a second, smaller line of text
  // under the category label stating in words which way the bar points, so
  // the chart never depends on a reader remembering a caption.
  const rowHeight = 38;
  const barHeight = 15;
  const top = 20;
  const height = top + usable.length * rowHeight + 8;
  const plotLeft = gutter + 8;
  const plotRight = width - valueGap;
  const centre = (plotLeft + plotRight) / 2;
  const half = centre - plotLeft;
  const peak = Math.max(0.5, ...usable.map((row) => Math.abs(row.correlation)));

  const svg = svgEl("svg", { class: "chart", width, height, viewBox: `0 0 ${width} ${height}` });
  svg.setAttribute("role", "img");

  ["−", "0", "+"].forEach((label, index) => {
    const x = plotLeft + (half * index);
    svg.appendChild(svgEl("text", {
      class: "chart__tick", x, y: 11, "text-anchor": "middle",
    })).textContent = index === 1 ? "0" : `${label}${peak.toFixed(2)}`;
  });

  usable.forEach((row, index) => {
    const y = top + index * rowHeight;
    const length = (Math.abs(row.correlation) / peak) * half;
    const negative = row.correlation < 0;
    // Stated directly from the sign, not from lower_is_better -- that field
    // says whether the direction matches racing intuition (P1 beats P10),
    // which is a separate, secondary fact from what the data actually shows.
    // Reading the meaning off "left means lower helps" required remembering
    // a caption; this puts the same sentence on every single bar.
    const meaning = negative ? "Lower value, more points" : "Higher value, more points";
    // The gutter is an SVG viewBox edge, not an overflow:hidden box -- text
    // anchored past it doesn't get cropped, it's pushed off past x=0 and
    // disappears entirely. On a narrow screen the on-chart label is
    // shortened; the tooltip always gets the full sentence.
    const meaningShort = negative ? "Lower helps" : "Higher helps";

    const label = svgEl("text", {
      class: "chart__cat", x: gutter, y: y + 10, "text-anchor": "end",
    });
    label.textContent = shortLabels ? (FEATURE_LABELS[row.feature] || row.label) : row.label;
    svg.appendChild(label);

    const meta = svgEl("text", {
      class: "chart__tick", x: gutter, y: y + 22, "text-anchor": "end",
    });
    meta.textContent = shortLabels ? meaningShort : meaning;
    svg.appendChild(meta);

    const bar = svgEl("path", {
      class: "chart__bar",
      d: barPath(negative ? centre - length : centre, y + 8, length, barHeight, 4,
                 negative ? "left" : "right"),
      fill: negative ? "var(--diverge-neg)" : "var(--diverge-pos)",
    });
    const matchesExpectation = row.lower_is_better ? negative : !negative;
    tipFor(bar, [
      row.label,
      `Correlation with scoring  ${row.correlation.toFixed(3)}`,
      `${meaning} this season`,
      matchesExpectation
        ? "Matches the usual racing logic for this input"
        : "Runs against the usual racing logic for this input -- worth a second look",
      `${row.coverage} entries with a value`,
    ]);
    svg.appendChild(bar);

    const value = svgEl("text", {
      class: "chart__value", x: width - 4, y: y + 20, "text-anchor": "end",
    });
    value.textContent = row.correlation.toFixed(2);
    svg.appendChild(value);
  });

  svg.appendChild(svgEl("line", {
    class: "chart__axis", x1: centre, y1: top - 6, x2: centre, y2: height - 6,
  }));

  container.appendChild(svg);

  const legend = el("div", "legend");
  [["var(--diverge-neg)", "Negative — a lower value goes with scoring"],
   ["var(--diverge-pos)", "Positive — a higher value goes with scoring"]].forEach(([color, text]) => {
    const item = el("div", "legend__item");
    const swatch = el("span", "legend__swatch");
    swatch.style.background = color;
    item.appendChild(swatch);
    item.appendChild(el("span", null, text));
    legend.appendChild(item);
  });
  container.appendChild(legend);
}

/* ---- points-finish rate across one feature's range ---------------------- */

function renderBucketChart(container, entry) {
  container.innerHTML = "";
  if (!entry || !entry.labels.length) {
    container.appendChild(el("p", "footnote", "Not enough variation in this feature to split the season into groups."));
    return;
  }

  const width = chartWidth(container);
  const height = 236;
  const padTop = 16;
  const padBottom = 44;
  const padLeft = 38;
  const plotWidth = width - padLeft - 8;
  const plotHeight = height - padTop - padBottom;
  const count = entry.labels.length;
  const band = plotWidth / count;
  const barWidth = Math.max(10, Math.min(78, band - 14));

  const svg = svgEl("svg", { class: "chart", width, height, viewBox: `0 0 ${width} ${height}` });

  [0, 0.25, 0.5, 0.75, 1].forEach((fraction) => {
    const y = padTop + plotHeight * (1 - fraction);
    svg.appendChild(svgEl("line", {
      class: "chart__grid", x1: padLeft, y1: y, x2: width - 8, y2: y,
    }));
    const tick = svgEl("text", { class: "chart__tick", x: padLeft - 8, y: y + 3, "text-anchor": "end" });
    tick.textContent = `${Math.round(fraction * 100)}%`;
    svg.appendChild(tick);
  });

  entry.labels.forEach((label, index) => {
    const rate = entry.rates[index];
    const x = padLeft + band * index + (band - barWidth) / 2;
    const barHeight = Math.max(2, plotHeight * rate);
    const y = padTop + plotHeight - barHeight;

    const bar = svgEl("path", {
      class: "chart__bar",
      d: barPath(x, y, barWidth, barHeight, 4, "top"),
      fill: "var(--series-1)",
    });
    tipFor(bar, [
      `${entry.label}: ${label}`,
      `Scored in ${pct(rate)} of entries`,
      `${entry.counts[index]} entries in this group`,
    ]);
    svg.appendChild(bar);

    const value = svgEl("text", {
      class: "chart__value", x: x + barWidth / 2, y: y - 6, "text-anchor": "middle",
    });
    value.textContent = pct(rate, 0);
    svg.appendChild(value);

    const cat = svgEl("text", {
      class: "chart__cat", x: padLeft + band * index + band / 2,
      y: height - padBottom + 18, "text-anchor": "middle",
    });
    cat.textContent = label;
    svg.appendChild(cat);

    const n = svgEl("text", {
      class: "chart__tick", x: padLeft + band * index + band / 2,
      y: height - padBottom + 32, "text-anchor": "middle",
    });
    n.textContent = `n=${entry.counts[index]}`;
    svg.appendChild(n);
  });

  svg.appendChild(svgEl("line", {
    class: "chart__axis", x1: padLeft, y1: padTop + plotHeight, x2: width - 8, y2: padTop + plotHeight,
  }));
  container.appendChild(svg);

  const direction = entry.lower_is_better
    ? "Groups run from the best (lowest) values on the left to the worst on the right."
    : "Groups run from the lowest values on the left to the highest on the right.";
  container.appendChild(el("p", "footnote", direction));
}

/* ---- distributions, as small multiples ---------------------------------- */

function renderDistributions(container, distributions, missingness) {
  container.innerHTML = "";
  const missingByFeature = Object.fromEntries(missingness.map((row) => [row.feature, row]));

  // Two passes on purpose. An auto-fit grid gives its first child the whole
  // row until siblings arrive, so measuring a cell as it is appended reads a
  // width the cell will not keep -- and an SVG sized to a stale width gets
  // letterboxed down by preserveAspectRatio, shrinking that one chart and its
  // labels. Build every cell, then measure.
  const cells = distributions
    .filter((entry) => entry.counts.length)
    .map((entry) => {
      const cell = el("div", "smallmultiple");
      cell.appendChild(el("div", "smallmultiple__title", entry.label));

      const gap = missingByFeature[entry.feature];
      const total = entry.counts.reduce((sum, value) => sum + value, 0);
      cell.appendChild(el("div", "smallmultiple__meta",
        gap && gap.missing
          ? `${total} entries · ${pct(gap.missing_pct, 0)} missing`
          : `${total} entries`));

      const plot = el("div");
      cell.appendChild(plot);
      container.appendChild(cell);
      return { entry, plot };
    });

  cells.forEach(({ entry, plot }) => {
    const width = chartWidth(plot, 240);
    const height = 112;
    const padTop = 8;
    const padBottom = 26;
    const plotHeight = height - padTop - padBottom;
    const band = width / entry.counts.length;
    // 2px surface gap between bars; capped so a two-state flag draws as a pair
    // of bars rather than two half-page slabs.
    const barWidth = Math.max(3, Math.min(64, band - 2));
    const peak = Math.max(...entry.counts) || 1;

    const svg = svgEl("svg", { class: "chart", width, height, viewBox: `0 0 ${width} ${height}` });
    entry.counts.forEach((count, index) => {
      const barHeight = Math.max(1.5, plotHeight * (count / peak));
      const x = band * index + (band - barWidth) / 2;
      const y = padTop + plotHeight - barHeight;
      const bar = svgEl("path", {
        class: "chart__bar",
        d: barPath(x, y, barWidth, barHeight, 3, "top"),
        fill: "var(--series-1)",
      });
      tipFor(bar, [entry.label, `${entry.bins[index]}`, `${count} entries`]);
      svg.appendChild(bar);
    });
    svg.appendChild(svgEl("line", {
      class: "chart__axis", x1: 0, y1: padTop + plotHeight, x2: width, y2: padTop + plotHeight,
    }));

    // Only the two ends are labelled: a tick under every bin collides at this
    // width, and the tooltip carries the exact range anyway.
    const ends = [[entry.bins[0], 0, "start"], [entry.bins[entry.bins.length - 1], width, "end"]];
    ends.forEach(([label, x, anchor]) => {
      const tick = svgEl("text", {
        class: "chart__tick", x, y: height - padBottom + 16, "text-anchor": anchor,
      });
      tick.textContent = label;
      svg.appendChild(tick);
    });
    plot.appendChild(svg);
  });
}

/* ---- points-finish rate by team ----------------------------------------- */

function renderTeamChart(container, rows) {
  container.innerHTML = "";
  if (!rows.length) return;

  const width = chartWidth(container);
  const gutter = Math.min(150, Math.max(92, width * 0.28));
  const valueGap = 52;
  const rowHeight = 26;
  const barHeight = 13;
  const height = rows.length * rowHeight + 10;
  const plotLeft = gutter + 8;
  const plotWidth = width - plotLeft - valueGap;

  const svg = svgEl("svg", { class: "chart", width, height, viewBox: `0 0 ${width} ${height}` });

  rows.forEach((row, index) => {
    const y = index * rowHeight + 5;
    const label = svgEl("text", {
      class: "chart__cat", x: gutter, y: y + barHeight - 2, "text-anchor": "end",
    });
    label.textContent = row.name;
    svg.appendChild(label);

    const length = Math.max(2, plotWidth * row.rate);
    // One series, and the team is already named on the axis -- painting each
    // bar in its team colour would be decoration that encodes nothing.
    const bar = svgEl("path", {
      class: "chart__bar",
      d: barPath(plotLeft, y, length, barHeight, 4, "right"),
      fill: "var(--series-1)",
    });
    tipFor(bar, [
      row.name,
      `Scored in ${pct(row.rate)} of entries`,
      `${row.scores} of ${row.entries} classified finishes`,
    ]);
    svg.appendChild(bar);

    const value = svgEl("text", {
      class: "chart__value", x: width - 4, y: y + barHeight - 2, "text-anchor": "end",
    });
    value.textContent = `${pct(row.rate, 0)} (${row.scores}/${row.entries})`;
    svg.appendChild(value);
  });

  svg.appendChild(svgEl("line", {
    class: "chart__axis", x1: plotLeft, y1: 0, x2: plotLeft, y2: height,
  }));
  container.appendChild(svg);
}

/* ---- share of the season's points finishes, by team (donut) ------------- */

// Ten teams on a donut is a wheel of near-identical slivers with no room for
// a legend to breathe. Past this many, the tail folds into one "Other" slice
// -- the reader loses nothing, because a slice that small was never legible
// on its own anyway.
const DONUT_MAX_SLICES = 6;

// Share is a magnitude (how big a piece of one whole), not a set of
// unrelated categories, so this is a sequential ramp -- one hue, light to
// dark -- rather than a categorical palette. Identity comes from the text
// label beside each slice, not from the color needing to be unique.
function donutColor(index, count) {
  const lightness = count <= 1 ? 45 : 34 + (index * (78 - 34)) / (count - 1);
  return `hsl(213 62% ${Math.round(lightness)}%)`;
}

function renderTeamDonut(container, rows) {
  container.innerHTML = "";
  if (!rows.length) return;

  const sorted = [...rows].sort((a, b) => b.share - a.share);
  const top = sorted.slice(0, DONUT_MAX_SLICES);
  const rest = sorted.slice(DONUT_MAX_SLICES);
  const slices = rest.length
    ? [...top, {
        name: `${rest.length} other team${rest.length === 1 ? "" : "s"}`,
        share: rest.reduce((sum, row) => sum + row.share, 0),
        scores: rest.reduce((sum, row) => sum + row.scores, 0),
        entries: rest.reduce((sum, row) => sum + row.entries, 0),
      }]
    : top;
  const totalScores = rows.reduce((sum, row) => sum + row.scores, 0);

  const size = 172;
  const stroke = 30;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const cx = size / 2;
  const cy = size / 2;

  const wrap = el("div", "donut");
  const svg = svgEl("svg", { width: size, height: size, viewBox: `0 0 ${size} ${size}` });
  // Rotated so the sweep starts at 12 o'clock instead of stroke-dasharray's
  // default 3 o'clock -- the reading direction a pie/donut is expected to use.
  const group = svgEl("g", { transform: `rotate(-90 ${cx} ${cy})` });
  svg.appendChild(group);

  let cumulative = 0;
  const legend = el("div", "donut__legend");
  slices.forEach((row, index) => {
    const color = donutColor(index, slices.length);
    const dash = row.share * circumference;
    const circle = svgEl("circle", {
      cx, cy, r: radius,
      fill: "none",
      stroke: color,
      "stroke-width": stroke,
      "stroke-dasharray": `${dash} ${circumference - dash}`,
      "stroke-dashoffset": -cumulative,
    });
    tipFor(circle, [
      row.name,
      `${pct(row.share)} of this season's points finishes`,
      `${row.scores} of ${totalScores} points finishes`,
    ]);
    group.appendChild(circle);
    cumulative += dash;

    const line = el("div", "donut__row");
    const swatch = el("span", "legend__swatch");
    swatch.style.background = color;
    line.appendChild(swatch);
    line.appendChild(el("span", null, row.name));
    line.appendChild(el("b", null, pct(row.share, 0)));
    legend.appendChild(line);
  });

  wrap.appendChild(svg);
  wrap.appendChild(legend);
  container.appendChild(wrap);
}

/* ---- rookies vs. the field, cumulative rate through the season (line) --- */

function linePath(points) {
  return points.map((point, index) => `${index === 0 ? "M" : "L"}${point[0].toFixed(1)},${point[1].toFixed(1)}`).join(" ");
}

function renderTrendChart(container, trend) {
  container.innerHTML = "";
  // Defensive against a response that hasn't caught up yet (a server still
  // running the previous analytics.py, or a browser holding a cached
  // response from before this field existed) -- trend can be undefined, or
  // present with too few rounds to plot a line against. Either way, fail
  // into a clear message rather than throwing: an uncaught exception here
  // would abort renderAnalytics() partway through and leave every panel
  // AFTER this one (coverage, the footnote) blank too, which is a much
  // more confusing failure than this one chart saying why it's empty.
  const rounds = trend && Array.isArray(trend.rounds) ? trend.rounds : [];
  if (rounds.length < 2) {
    container.appendChild(el("p", "footnote",
      rounds.length === 0
        ? "Not enough completed rounds yet this season to show a trend."
        : "Only one completed round so far this season -- check back after the next race."));
    return;
  }

  const width = chartWidth(container);
  const height = 236;
  const padTop = 16;
  const padBottom = 28;
  const padLeft = 38;
  const padRight = 10;
  const plotWidth = width - padLeft - padRight;
  const plotHeight = height - padTop - padBottom;
  const n = rounds.length;
  const xFor = (index) => padLeft + (n <= 1 ? 0 : (plotWidth * index) / (n - 1));
  // Fixed 0-100% domain, matching the bucket chart above -- autoscaling to
  // the data's own tight range would visually exaggerate a ~15-point gap
  // into something that looks like a canyon.
  const yFor = (value) => padTop + plotHeight * (1 - value);

  const svg = svgEl("svg", { class: "chart", width, height, viewBox: `0 0 ${width} ${height}` });

  [0, 0.25, 0.5, 0.75, 1].forEach((fraction) => {
    const y = yFor(fraction);
    svg.appendChild(svgEl("line", { class: "chart__grid", x1: padLeft, y1: y, x2: width - padRight, y2: y }));
    const tick = svgEl("text", { class: "chart__tick", x: padLeft - 8, y: y + 3, "text-anchor": "end" });
    tick.textContent = `${Math.round(fraction * 100)}%`;
    svg.appendChild(tick);
  });

  const series = [
    { key: "veteran_rate", color: "var(--series-1)", name: "Non-rookies" },
    { key: "rookie_rate", color: "var(--diverge-neg)", name: "Rookies" },
  ];

  series.forEach(({ key, color }) => {
    const points = rounds.map((round, index) => [xFor(index), yFor(trend[key][index])])
      .filter((point, index) => trend[key][index] !== null);
    if (points.length < 2) return;
    svg.appendChild(svgEl("path", {
      d: linePath(points), fill: "none", stroke: color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    rounds.forEach((round, index) => {
      const rate = trend[key][index];
      if (rate === null) return;
      const dot = svgEl("circle", { cx: xFor(index), cy: yFor(rate), r: 7, fill: "transparent" });
      const visible = svgEl("circle", { cx: xFor(index), cy: yFor(rate), r: 2.5, fill: color });
      const sample = key === "rookie_rate" ? `${trend.rookie_entries[index]} rookie entries so far` : null;
      tipFor(dot, [`Round ${round}`, `${pct(rate)} cumulative points-finish rate`, sample].filter(Boolean));
      svg.appendChild(visible);
      svg.appendChild(dot);
    });
  });

  [1, Math.round(n / 2), n].forEach((roundIndex) => {
    const index = roundIndex - 1;
    if (index < 0 || index >= n) return;
    const tick = svgEl("text", {
      class: "chart__tick", x: xFor(index), y: height - padBottom + 18,
      "text-anchor": index === 0 ? "start" : index === n - 1 ? "end" : "middle",
    });
    tick.textContent = `R${rounds[index]}`;
    svg.appendChild(tick);
  });

  svg.appendChild(svgEl("line", {
    class: "chart__axis", x1: padLeft, y1: padTop + plotHeight, x2: width - padRight, y2: padTop + plotHeight,
  }));
  container.appendChild(svg);

  const legend = el("div", "legend");
  series.forEach(({ color, name }) => {
    const item = el("div", "legend__item");
    const swatch = el("span", "legend__swatch");
    swatch.style.background = color;
    item.appendChild(swatch);
    item.appendChild(el("span", null, name));
    legend.appendChild(item);
  });
  container.appendChild(legend);

  const sub = $("#trend-sub");
  if (sub) {
    const gap = trend.veteran_rate[n - 1] - trend.rookie_rate[n - 1];
    const lead = gap >= 0 ? "non-rookies are scoring" : "rookies are scoring";
    sub.textContent =
      `Cumulative points-finish rate through each round — rookies vs. everyone else. By round ` +
      `${rounds[n - 1]}, ${lead} ${pct(Math.abs(gap), 0)} more often than the other group, and the ` +
      `gap between the two lines shows whether that's closing as the season goes on or holding steady.`;
  }
}

/* ---- coverage, as plain DOM (a bar per row needs no SVG) ---------------- */

// Only a feature with an actual gap earns a bar -- most seasons run every
// position/standing/experience input at 100%, and seven rows that all read
// "100%" bury the two that don't in noise instead of drawing the eye to them.
const COVERAGE_MIN_MISSING_PCT = 0.01;

function renderCoverage(container, missingness, total) {
  container.innerHTML = "";
  const gapped = missingness.filter((row) => row.missing_pct >= COVERAGE_MIN_MISSING_PCT);
  const complete = missingness.filter((row) => row.missing_pct < COVERAGE_MIN_MISSING_PCT);

  const sub = $("#coverage-sub");
  if (sub) {
    sub.textContent = complete.length
      ? `${complete.map((row) => row.label).join(", ")} ${complete.length === 1 ? "is" : "are"} ` +
        `populated for every entry this season. The gaps below are the only ones worth reading: ` +
        `a rookie has no history at a circuit and no prior three races to average, so the model ` +
        `imputes those rather than leaving them blank.`
      : "How much of the season each input is actually populated for.";
  }

  if (!gapped.length) {
    container.innerHTML = "";
    container.appendChild(el("p", "footnote", "Every input is fully populated this season -- nothing here is imputed."));
    return;
  }

  const box = el("div", "coverage");
  gapped.forEach((row) => {
    const line = el("div", "coverage__row");
    line.appendChild(el("div", "coverage__label", row.label));
    const track = el("div", "coverage__track");
    const fill = el("div", "coverage__fill");
    fill.style.width = `${(1 - row.missing_pct) * 100}%`;
    track.appendChild(fill);
    line.appendChild(track);
    line.appendChild(el("div", "coverage__value", pct(1 - row.missing_pct, 0)));
    line.title = `${total - row.missing} of ${total} entries have a value (${row.missing} imputed)`;
    box.appendChild(line);
  });
  container.appendChild(box);
}

/* ---- cards, chips, and the page as a whole ------------------------------ */

function renderStatCards(report) {
  const box = $("#analytics-cards");
  box.innerHTML = "";
  const head = report.headline;
  const lead = report.correlations.find((row) => row.correlation !== null);
  const cards = [
    ["Races run", head.races, `${report.year} season`, true],
    ["Points rate", pct(head.points_rate), `${head.points_finishes} of ${head.classified}`, true],
    ["Entries", head.entries, "driver–race rows"],
    ["Drivers", head.drivers, `${head.rookies} rookies`],
    ["Teams", head.teams, "on the grid"],
    ["Points finishes", head.points_finishes, "top-10 results"],
    ["Strongest signal", lead ? Math.abs(lead.correlation).toFixed(2) : "—",
     lead ? lead.label : "no usable feature"],
    ["Model inputs", report.correlations.length, "per entry"],
  ];
  // Only worth a card when there is something in it: the live feed classifies
  // every entrant, retirements included, so recent seasons have none at all.
  // It replaces the input count rather than adding a ninth card -- the grid is
  // laid out for exactly eight.
  if (head.dnf_or_unclassified) {
    cards[cards.length - 1] = ["Unclassified", head.dnf_or_unclassified, "excluded from rates"];
  }
  cards.forEach(([label, value, foot, lead]) => {
    const card = el("div", `statcard${lead ? " statcard--lead" : ""}`);
    card.appendChild(el("div", "statcard__label", label));
    card.appendChild(el("div", "statcard__value", String(value)));
    card.appendChild(el("div", "statcard__foot", foot));
    box.appendChild(card);
  });
}

function renderAnalyticsChips(report) {
  const years = $("#analytics-years");
  years.innerHTML = "";
  report.available_years.slice(0, 12).forEach((year) => {
    const chip = el("button", "chip", String(year));
    chip.classList.toggle("is-active", year === report.year);
    chip.addEventListener("click", () => loadAnalytics(year));
    years.appendChild(chip);
  });

  const features = $("#analytics-feature-chips");
  features.innerHTML = "";
  report.rate_by_bucket.forEach((entry) => {
    const chip = el("button", "chip", entry.label);
    chip.classList.toggle("is-active", entry.feature === state.analyticsFeature);
    chip.addEventListener("click", () => {
      state.analyticsFeature = entry.feature;
      $$("#analytics-feature-chips .chip").forEach((other) => other.classList.remove("is-active"));
      chip.classList.add("is-active");
      renderBucketChart($("#chart-bucket"), entry);
    });
    features.appendChild(chip);
  });
}

// One bad panel throwing should never blank out every panel drawn after it
// in source order -- that turned a single missing field (see
// renderTrendChart) into what looked like the whole page being broken. Each
// renderer runs in its own try/catch; a failure is logged and left visible
// as a short message in that panel only, and every other panel still draws.
function renderPanel(label, fn) {
  try {
    fn();
  } catch (error) {
    console.error(`Analytics panel failed: ${label}`, error);
  }
}

function renderAnalytics() {
  const report = state.analytics;
  if (!report) return;

  $("#analytics-sub").textContent =
    `Exploratory analysis of the ${report.correlations.length} inputs the model reads, ` +
    `over the ${report.year} season only. Older seasons ran different cars, rules and grids, ` +
    `so they say little about the next race.`;

  renderPanel("stat cards", () => renderStatCards(report));
  renderPanel("chips", () => renderAnalyticsChips(report));
  renderPanel("correlation", () => renderCorrelationChart($("#chart-correlation"), report.correlations));
  renderPanel("bucket", () => renderBucketChart(
    $("#chart-bucket"),
    report.rate_by_bucket.find((entry) => entry.feature === state.analyticsFeature),
  ));
  renderPanel("distributions", () => renderDistributions($("#chart-distributions"), report.distributions, report.missingness));
  renderPanel("team rate", () => renderTeamChart($("#chart-teams"), report.by_team));
  renderPanel("team share", () => renderTeamDonut($("#chart-team-share"), report.by_team));
  renderPanel("rookie trend", () => renderTrendChart($("#chart-trend"), report.experience_trend));
  renderPanel("coverage", () => renderCoverage($("#analytics-coverage"), report.missingness, report.headline.entries));

  $("#analytics-footnote").textContent =
    "Every value here is read from the same feature table the model is trained and scored on — " +
    "computed by pipeline.py, aggregated by analytics.py, which the project notebook imports too. " +
    "Each feature is built from races BEFORE the one it describes (expanding means and rolling " +
    "windows are shifted by one race), so nothing on this page can see a result it is meant to predict. " +
    "Rates exclude DNFs and unclassified finishes.";
}

async function loadAnalytics(year) {
  const notice = $("#analytics-notice");
  const cards = $("#analytics-cards");
  cards.innerHTML = "";
  for (let i = 0; i < 4; i += 1) {
    const skeleton = el("div", "skeleton");
    skeleton.style.height = "92px";
    cards.appendChild(skeleton);
  }

  let report;
  try {
    report = await api(`/api/analytics${year ? `?year=${year}` : ""}`);
  } catch (error) {
    cards.innerHTML = "";
    notice.className = "notice notice--bad";
    notice.textContent = `Could not load analytics: ${error.message}`;
    return;
  }

  notice.className = "notice is-hidden";
  state.analyticsLoaded = true;
  state.analytics = report;
  state.analyticsYear = report.year;
  // Default to the feature that separates scorers most strongly this season,
  // so the panel opens on the chart worth looking at rather than on whichever
  // feature happens to be first in the list.
  const strongest = report.correlations.find((row) => row.correlation !== null);
  if (!report.rate_by_bucket.some((entry) => entry.feature === state.analyticsFeature)) {
    state.analyticsFeature = strongest ? strongest.feature : report.rate_by_bucket[0].feature;
  }
  renderAnalytics();
}

// Pixel-sized charts have to be redrawn when the box they were measured
// against changes; debounced so a drag-resize doesn't rebuild them per frame.
let analyticsResizeTimer = null;
window.addEventListener("resize", () => {
  if (!state.analytics || !$("#tab-analytics").classList.contains("is-active")) return;
  clearTimeout(analyticsResizeTimer);
  analyticsResizeTimer = setTimeout(renderAnalytics, 180);
});

/* -------------------------------------------------------------------- boot */

(async function boot() {
  renderHeroArt(state.circuit);
  await loadStatus();
  try {
    await loadPickers();
    // Land on the most recent race rather than the next one. It's the only
    // view where every number can be checked against a result that actually
    // happened, which is a far better first impression than a prediction
    // nobody can evaluate yet -- and loadPickers has already pointed
    // state.raceId at it. The "Next race" tab is one tap away.
    if (state.raceId) setMode("replay");
  } catch (error) {
    /* pickers are best-effort; predictions still work with the defaults */
  }
  await loadPredictions();
  loadSeason(); // independent of the predictions path; doesn't block first paint
  setInterval(loadStatus, STATUS_POLL_MS);
})();
