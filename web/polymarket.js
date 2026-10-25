// Polymarket client and seat projection, run in the browser. Both Polymarket APIs allow any origin,
// so the site needs no server. Mirrors polymarket.py, which the command-line export still uses.

const GAMMA = "https://gamma-api.polymarket.com";
const CLOB = "https://clob.polymarket.com";
const TAG = "israel-election";
const PAGE_SIZE = 100;

// interval -> fidelity in minutes, so each range returns a few hundred points
const INTERVALS = { "1d": 5, "1w": 60, "1m": 240, max: 720 };

const BUCKET_RE = /^\s*[<>≤≥]?\s*\d+(\.\d+)?\s*%?\s*([-–+]\s*(\d+(\.\d+)?)?\s*%?)?\s*$/;

async function getJson(url, params) {
  const res = await fetch(`${url}?${new URLSearchParams(params)}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function num(value, fallback = null) {
  const n = parseFloat(value);
  return Number.isFinite(n) ? n : fallback;
}

function jsonList(value) {
  if (Array.isArray(value)) return value;
  try { return JSON.parse(value || "[]"); } catch { return []; }
}

// the probability Polymarket itself shows: the midpoint, or the last trade when the spread is wide
function marketPrice(m) {
  const bid = num(m.bestBid), ask = num(m.bestAsk), last = num(m.lastTradePrice);
  if (bid != null && ask != null && ask > 0 && ask - bid <= 0.10) return Math.round(((bid + ask) / 2) * 1e4) / 1e4;
  if (last != null && last > 0) return last;
  const prices = jsonList(m.outcomePrices);
  return prices.length ? num(prices[0], 0) : 0;
}

function normalizeMarket(m) {
  const tokens = jsonList(m.clobTokenIds), prices = jsonList(m.outcomePrices), closed = !!m.closed;
  return {
    id: m.id,
    label: (m.groupItemTitle || m.question || "").trim(),
    question: m.question,
    slug: m.slug,
    // a resolved market's outcomePrices are the settlement (1/0)
    price: closed && prices.length ? num(prices[0], 0) : marketPrice(m),
    bestBid: num(m.bestBid),
    bestAsk: num(m.bestAsk),
    lastTrade: num(m.lastTradePrice),
    change1d: num(m.oneDayPriceChange, 0),
    change1w: num(m.oneWeekPriceChange, 0),
    volume: num(m.volume, 0),
    volume24h: num(m.volume24hr, 0),
    liquidity: num(m.liquidity, 0),
    threshold: num(m.groupItemThreshold),
    outcomes: jsonList(m.outcomes),
    tokenYes: tokens[0] || null,
    active: !!m.active,
    closed,
    endDate: m.endDate,
  };
}

function normalizeEvent(e) {
  // drop never-traded placeholders Polymarket pre-creates for future candidates
  let markets = (e.markets || []).map(normalizeMarket).filter((m) => m.tokenYes && (m.volume > 0 || m.closed));
  if (!e.closed) {
    const live = markets.filter((m) => !m.closed);
    if (live.length) markets = live;
  }
  const isBuckets = markets.length > 1 && markets.every((m) => BUCKET_RE.test(m.label));
  if (isBuckets) markets.sort((a, b) => (a.threshold ?? Infinity) - (b.threshold ?? Infinity));
  else markets.sort((a, b) => b.price - a.price);
  return {
    id: e.id,
    slug: e.slug,
    title: (e.title || "").trim(),
    url: `https://polymarket.com/event/${e.slug}`,
    endDate: e.endDate,
    closed: !!e.closed,
    kind: isBuckets ? "buckets" : markets.length === 1 ? "binary" : "multi",
    volume: num(e.volume, 0),
    volume24h: num(e.volume24hr, 0),
    liquidity: num(e.liquidity, 0),
    markets,
  };
}

// every event tagged israel-election, paging until the API runs dry
async function fetchEvents(includeClosed = false) {
  const byId = new Map();
  for (const closed of includeClosed ? ["false", "true"] : ["false"]) {
    for (let offset = 0; ; offset += PAGE_SIZE) {
      const page = await getJson(`${GAMMA}/events`, { tag_slug: TAG, closed, limit: PAGE_SIZE, offset });
      page.forEach((e) => byId.set(e.id, e));
      if (page.length < PAGE_SIZE) break;
    }
  }
  const events = [...byId.values()].map(normalizeEvent).filter((e) => e.markets.length);
  events.sort((a, b) => b.volume - a.volume);
  return { fetchedAt: new Date().toISOString(), events, projection: projectSeats(events.filter((e) => !e.closed)) };
}

async function fetchHistory(token, interval = "1w") {
  const data = await getJson(`${CLOB}/prices-history`, { market: token, interval, fidelity: INTERVALS[interval] });
  return (data.history || []).map((p) => ({ t: Number(p.t), p: Number(p.p) }));
}

const KNESSET = 120;
const MAJORITY = 61;
const THRESHOLD_PCT = 3.25;
const THRESHOLD_SEATS = 4;
const SEATS_RE = /^Israel Election:\s*(.+?)\s*# of seats\?$/i;
const SHARE_RE = /^Israel Election:\s*(.+?)\s*vote share\?$/i;

// market titles use several spellings for one party
const ALIASES = {
  "Dems": "The Democrats",
  "Otzma": "Otzma Yehudit",
  "UTJ": "United Torah Judaism",
  "United Arab List (Ra'am)": "Ra'am",
  "Religious Zionist Party–Zehut": "RZP-Zehut",
  "The Joint List": "Joint List",
  "Reservists and New Economic Party": "Reservists–New Economic Party",
  "Blue & White": "Blue and White",
};

// Polymarket's own definition, from the rules of "Which Bloc Wins More Seats?" and "Any Coalition Wins a Majority?"
const BLOCS = {
  "Netanyahu bloc": ["Likud", "Otzma Yehudit", "RZP-Zehut", "Amcha Yisrael", "United Torah Judaism", "Shas"],
  "Opposition bloc": ["The Democrats", "Yashar", "Together", "Yisrael Beiteinu"],
};
const OTHERS = "Not in either bloc";

// '<20' -> [null, 20], '35+' -> [35, null], '20-24' -> [20, 24], '6' -> [6, 6]
function bounds(label) {
  const s = label.replace(/%/g, "").replace(/–/g, "-").trim();
  if (s.startsWith("<")) return [null, parseFloat(s.slice(1))];
  if (s.endsWith("+")) return [parseFloat(s.slice(0, -1)), null];
  if (s.includes("-")) { const [lo, hi] = s.split("-"); return [parseFloat(lo), parseFloat(hi)]; }
  return [parseFloat(s), parseFloat(s)];
}

// a representative value per bracket; open-ended brackets borrow their neighbour's width, and
// anything entirely below the electoral threshold counts as 0: the party gets no seats at all
function bracketValues(labels, integer, threshold) {
  const bs = labels.map(bounds);
  const step = integer ? 1 : 0;
  return bs.map(([lo, hi], i) => {
    if (lo == null) {
      if (hi <= threshold) return 0;
      const [nlo, nhi] = bs[i + 1] || [hi, hi];
      const width = nlo != null && nhi != null ? nhi - nlo + step : 1;
      return hi - (width + step) / 2;
    }
    if (hi == null) {
      const [plo, phi] = i ? bs[i - 1] : [lo, lo];
      const width = plo != null && phi != null ? phi - plo + step : 1;
      return lo + (width - step) / 2;
    }
    if (integer ? hi < threshold : hi <= threshold) return 0;
    return (lo + hi) / 2;
  });
}

// market_price fell back to the last trade, which may be days old, because the book is too wide
const onLastTrade = (m) => m.bestBid == null || m.bestAsk == null || m.bestAsk - m.bestBid > 0.10;

function expectation(event, integer, threshold) {
  const ms = event.markets;
  const total = ms.reduce((s, m) => s + m.price, 0) || 1;
  const values = bracketValues(ms.map((m) => m.label), integer, threshold);
  const probs = ms.map((m) => m.price / total);
  const top = probs.indexOf(Math.max(...probs));
  return {
    mean: values.reduce((s, v, i) => s + v * probs[i], 0),
    likely: { label: ms[top].label, p: probs[top] },
    pIn: values.reduce((s, v, i) => s + (v > 0 ? probs[i] : 0), 0),
    bookSum: total,
    stale: ms.reduce((s, m, i) => s + (onLastTrade(m) ? probs[i] : 0), 0),
    liquidity: event.liquidity,
  };
}

// shift each party's raw estimate so the total is 120, taking the gap mostly from thin markets:
// a party's share of the correction is seats / liquidity, and nobody is pushed below 0
function fitToKnesset(parties) {
  parties.forEach((p) => { p.seats = p.seatsRaw; });
  for (let round = 0; round < parties.length; round++) {
    const gap = parties.reduce((s, p) => s + p.seats, 0) - KNESSET;
    const movable = parties.filter((p) => p.seats > 0 || gap < 0);
    if (Math.abs(gap) < 1e-9 || !movable.length) break;
    const weights = movable.map((p) => p.seatsRaw / Math.max(p.liquidity, 1));
    const total = weights.reduce((a, b) => a + b, 0) || 1;
    movable.forEach((p, i) => { p.seats = Math.max(0, p.seats - (gap * weights[i]) / total); });
  }
}

// expected Knesset seats per party, implied by the seat-bracket and vote-share-bracket markets;
// where a party has both, the two estimates are averaged by liquidity
function projectSeats(events) {
  const canon = (name) => ALIASES[name.trim()] || name.trim();
  const seats = {}, shares = {};
  let below = 5.0, majority = null, bigger = null, pInDirect = {};
  for (const e of events) {
    let m;
    if ((m = SEATS_RE.exec(e.title)) && e.kind === "buckets") seats[canon(m[1])] = [expectation(e, true, THRESHOLD_SEATS), e];
    else if ((m = SHARE_RE.exec(e.title)) && e.kind === "buckets") shares[canon(m[1])] = [expectation(e, false, THRESHOLD_PCT), e];
    else if (e.title.toLowerCase().includes("below threshold") && e.kind === "buckets") below = expectation(e, false, 0).mean;
    else if (e.slug.startsWith("which-parties-will-win-a-seat")) pInDirect = Object.fromEntries(e.markets.map((x) => [canon(x.label), x.price]));
    else if (e.slug === "israel-election-any-coalition-wins-a-majority") majority = e.markets[0].price;
    else if (e.slug === "israel-election-which-bloc-wins-more-seats") bigger = e.markets[0].price; // first outcome is "Bibi"
  }
  const names = [...new Set([...Object.keys(seats), ...Object.keys(shares)])].sort();
  if (!names.length) return null;

  const perPct = KNESSET / (100 - below);
  const blocOf = {};
  Object.entries(BLOCS).forEach(([bloc, members]) => members.forEach((p) => { blocOf[p] = bloc; }));
  let parties = names.map((name) => {
    const bySeats = seats[name], byShare = shares[name];
    const [main, event] = bySeats || byShare;
    const sources = [];
    if (bySeats) sources.push(["seats", bySeats[0].mean, bySeats[0]]);
    if (byShare) sources.push(["share", byShare[0].mean * perPct, byShare[0]]);
    const liquidity = sources.reduce((s, [, , x]) => s + x.liquidity, 0);
    const weights = Object.fromEntries(sources.map(([kind, , x]) => [kind, liquidity ? x.liquidity / liquidity : 1 / sources.length]));
    return {
      name,
      bloc: blocOf[name] || OTHERS,
      seatsRaw: sources.reduce((s, [kind, value]) => s + weights[kind] * value, 0),
      seatsFromSeats: bySeats ? bySeats[0].mean : null,
      seatsFromShare: byShare ? byShare[0].mean * perPct : null,
      weights,
      liquidity,
      stale: sources.reduce((s, [kind, , x]) => s + weights[kind] * x.stale, 0),
      source: bySeats ? "seats" : "share",
      likely: main.likely,
      pIn: pInDirect[name] ?? main.pIn,
      bookSum: main.bookSum,
      slug: event.slug,
      volume: event.volume,
    };
  });

  const rawTotal = parties.reduce((s, p) => s + p.seatsRaw, 0);
  fitToKnesset(parties);
  // a party the market all but rules out of the Knesset is noise, not a row
  parties = parties.filter((p) => p.seats >= 0.5 || p.pIn >= 0.05).sort((a, b) => b.seats - a.seats);
  const blocs = [...Object.keys(BLOCS), OTHERS].map((name) => ({
    name, seats: parties.filter((p) => p.bloc === name).reduce((s, p) => s + p.seats, 0),
  }));
  return {
    knesset: KNESSET,
    majority: MAJORITY,
    rawTotal,
    belowThresholdPct: below,
    parties,
    blocs,
    markets: { netanyahuBlocBigger: bigger, anyBlocMajority: majority },
  };
}

// run fn over items with at most `limit` requests in flight
async function mapLimit(items, limit, fn) {
  const out = [];
  let next = 0;
  await Promise.all(Array.from({ length: limit }, async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i]);
    }
  }));
  return out;
}

const HISTORY_STEP_MIN = 240;

// The projection recomputed every stepMin minutes (4 hours by default) from each bracket's price history,
// with projectSeats itself; `since` (unix seconds) limits it to a recent window, e.g. hourly for a day.
// Polymarket publishes no history of liquidity or spreads, so the blend and the fit to 120 use today's
// liquidity throughout. A market counts from when all its brackets have a price; the series starts once
// every party has at least one, so no party drops out and hands its seats to the rest.
async function projectionHistory(events, { stepMin = HISTORY_STEP_MIN, since = null } = {}) {
  const used = events.filter((e) => e.kind === "buckets" &&
    (SEATS_RE.test(e.title) || SHARE_RE.test(e.title) || e.title.toLowerCase().includes("below threshold")));
  const tokens = used.flatMap((e) => e.markets.map((m) => m.tokenYes));
  const series = new Map(await mapLimit(tokens, 8, async (token) => {
    // a window reaches back two extra days, so a bracket that did not trade inside it still has a price
    const range = since ? { startTs: Math.floor(since - 2 * 86400), endTs: Math.ceil(Date.now() / 1000) } : { interval: "max" };
    const data = await getJson(`${CLOB}/prices-history`, { market: token, ...range, fidelity: stepMin })
      .catch(() => ({ history: [] }));
    return [token, (data.history || []).map((p) => ({ t: Number(p.t), p: Number(p.p) })).sort((a, b) => a.t - b.t)];
  }));

  const opens = new Map(used.map((e) => [e, Math.max(...e.markets.map((m) => series.get(m.tokenYes)[0]?.t ?? Infinity))]));
  const canon = (name) => ALIASES[name.trim()] || name.trim();
  const partyOpens = {};
  for (const e of used) {
    const m = SEATS_RE.exec(e.title) || SHARE_RE.exec(e.title);
    if (m) partyOpens[canon(m[1])] = Math.min(partyOpens[canon(m[1])] ?? Infinity, opens.get(e));
  }
  const start = Math.max(...Object.values(partyOpens));
  if (!Number.isFinite(start)) return { start: null, points: [] };

  const step = stepMin * 60, now = Date.now() / 1000;
  const cursor = new Map(tokens.map((t) => [t, { i: 0, p: null }]));
  const priceAt = (token, t) => {
    const h = series.get(token), c = cursor.get(token);
    while (c.i < h.length && h[c.i].t <= t) c.p = h[c.i++].p;
    return c.p;
  };
  const points = [];
  for (let t = Math.ceil(Math.max(start, since ?? -Infinity) / step) * step; t <= now; t += step) {
    const snapshot = used.filter((e) => opens.get(e) <= t).map((e) => ({
      ...e, markets: e.markets.map((m) => ({ ...m, price: priceAt(m.tokenYes, t) })),
    }));
    // advance every cursor, so markets that open later are positioned when they join
    tokens.forEach((token) => priceAt(token, t));
    const pr = projectSeats(snapshot);
    if (!pr) continue;
    points.push({
      t,
      blocs: Object.fromEntries(pr.blocs.map((b) => [b.name, b.seats])),
      parties: Object.fromEntries(pr.parties.map((p) => [p.name, p.seats])),
      belowKnown: snapshot.some((e) => e.title.toLowerCase().includes("below threshold")),
    });
  }
  return { start, points };
}
