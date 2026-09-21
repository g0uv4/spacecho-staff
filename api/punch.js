const EMAILISH = /(@|mailto:|subject:|from:|to:|dear |hi |hello |附件|郵件內文|https?:\/\/)/i;
const NUM_HEAVY = /\d{4,}/;
const BALANCEISH = /(balance|餘額|金額|usd|twd|\$|NT\$)/i;
const PREFIX = "punch/";
const LEGACY = "punch/logs.json";


function punchWriteToken() {
  return (process.env.SPACECHO_PUNCH_WRITE_TOKEN || "").trim();
}

function authorizeWrite(req, res) {
  const expected = punchWriteToken();
  if (!expected) {
    res.status(503).json({ ok: false, error: "write token not configured" });
    return false;
  }
  const raw = String(req.headers.authorization || "");
  const m = raw.match(/^Bearer\s+(.+)$/i);
  const got = m ? m[1].trim() : "";
  if (!got || got !== expected) {
    res.status(401).json({ ok: false, error: "unauthorized" });
    return false;
  }
  return true;
}

function cors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");
}

function taipeiNow() {
  return new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Taipei" }));
}

function parseAt(raw) {
  if (raw == null || raw === "") return taipeiNow();
  if (typeof raw !== "string") throw new Error("at must be iso string");
  let s = raw.trim();
  if (s.endsWith("Z")) s = s.slice(0, -1) + "+00:00";
  const dt = new Date(s);
  if (Number.isNaN(dt.getTime())) throw new Error("at invalid");
  return new Date(dt.toLocaleString("en-US", { timeZone: "Asia/Taipei" }));
}

function fmt(dt) {
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, "0");
  const d = String(dt.getDate()).padStart(2, "0");
  const hh = String(dt.getHours()).padStart(2, "0");
  const mm = String(dt.getMinutes()).padStart(2, "0");
  const ss = String(dt.getSeconds()).padStart(2, "0");
  return {
    at: `${y}-${m}-${d}T${hh}:${mm}:${ss}+08:00`,
    date: `${y}-${m}-${d}`,
    time: `${hh}:${mm}`,
  };
}

function sanitizeName(val, field) {
  if (typeof val !== "string") throw new Error(field + " must be string");
  const name = val.trim().replace(/\s+/g, " ");
  if (!name || name.length > 32) throw new Error(field + " invalid");
  if (EMAILISH.test(name) || NUM_HEAVY.test(name)) throw new Error(field + " looks like contact dump");
  return name;
}

function sanitizeLabel(val) {
  if (typeof val !== "string") throw new Error("label must be string");
  const label = val.trim().replace(/\s+/g, " ");
  if (!label) throw new Error("label required");
  if (label.length > 24) throw new Error("label too long");
  if (EMAILISH.test(label) || NUM_HEAVY.test(label) || BALANCEISH.test(label)) {
    throw new Error("label looks like email/body dump");
  }
  if (/[<>{}[\]]/.test(label)) throw new Error("label looks like dump");
  return label;
}

function toPublic(rec) {
  const dt = parseAt(rec.at);
  const t = fmt(dt);
  return {
    at: t.at,
    date: t.date,
    time: t.time,
    from: rec.from,
    to: rec.to,
    label: rec.label,
    sample: Boolean(rec.sample),
  };
}

function recKey(rec) {
  return [rec.at, rec.from, rec.to, rec.label].join("|");
}

async function fetchBlob(hit, token) {
  const url = hit.downloadUrl || hit.url;
  const r = await fetch(url, {
    cache: "no-store",
    headers: { Authorization: "Bearer " + token },
  });
  if (!r.ok) return [];
  const data = await r.json();
  if (Array.isArray(data.logs)) return data.logs;
  if (data && data.from && data.to && data.label) return [data];
  return [];
}

async function listAll(token) {
  const { list } = await import("@vercel/blob");
  const out = [];
  let cursor;
  do {
    const page = await list({ prefix: PREFIX, limit: 1000, cursor, token });
    out.push(...(page.blobs || []));
    cursor = page.hasMore ? page.cursor : null;
  } while (cursor);
  return out;
}

async function loadAll() {
  const token = process.env.BLOB_READ_WRITE_TOKEN;
  if (!token) return [];
  const blobs = await listAll(token);
  const rows = [];
  const seen = new Set();
  for (const hit of blobs) {
    try {
      const recs = await fetchBlob(hit, token);
      for (const rec of recs) {
        const k = recKey(toPublic(rec));
        if (seen.has(k)) continue;
        seen.add(k);
        rows.push(rec);
      }
    } catch {
      /* skip bad blob */
    }
  }
  return rows;
}

async function saveOne(rec) {
  const { put } = await import("@vercel/blob");
  const t = fmt(parseAt(rec.at));
  const stamp = t.date.replace(/-/g, "") + t.time.replace(":", "") + String(Date.now()).slice(-4);
  const path = `punch/item/${stamp}.json`;
  await put(path, JSON.stringify(rec), {
    access: "private",
    addRandomSuffix: true,
    allowOverwrite: false,
    contentType: "application/json",
    token: process.env.BLOB_READ_WRITE_TOKEN,
  });
}

export default async function handler(req, res) {
  cors(res);
  if (req.method === "OPTIONS") {
    res.status(204).end();
    return;
  }
  try {
    if (req.method === "GET") {
      let days = parseInt(String((req.query && req.query.days) || "90"), 10);
      if (!Number.isFinite(days)) days = 90;
      days = Math.min(90, Math.max(1, days));
      const cutoff = Date.now() - days * 86400000;
      const includeSample = String((req.query && req.query.includeSample) || "") === "1";
      const logs = (await loadAll())
        .map((rec) => {
          try { return toPublic(rec); } catch { return null; }
        })
        .filter((r) => r && new Date(r.at).getTime() >= cutoff)
        .filter((r) => includeSample || !r.sample)
        .sort((a, b) => a.at.localeCompare(b.at));
      res.status(200).json({ ok: true, days, logs });
      return;
    }
    if (req.method === "POST") {
      if (!authorizeWrite(req, res)) return;
      if (!process.env.BLOB_READ_WRITE_TOKEN) {
        res.status(503).json({ ok: false, error: "blob token missing" });
        return;
      }
      const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
      if (!body || typeof body !== "object") throw new Error("expected object");
      const rec = {
        at: fmt(parseAt(body.at)).at,
        from: sanitizeName(body.from, "from"),
        to: sanitizeName(body.to, "to"),
        label: sanitizeLabel(body.label),
      };
      if (body.sample) rec.sample = true;
      await saveOne(rec);
      res.status(200).json({ ok: true, log: toPublic(rec) });
      return;
    }
    res.status(405).json({ ok: false, error: "method" });
  } catch (e) {
    const msg = e && e.message ? e.message : "bad json";
    res.status(400).json({ ok: false, error: msg });
  }
}
