#!/usr/bin/env python3
"""Tiny static + relations + punch server for grabbot-flow."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
PENDING = ROOT / "pending-relations.json"
PUNCH_LOG = ROOT / "data" / "punch.jsonl"
HOST = "0.0.0.0"
PORT = 8765
TAIPEI = ZoneInfo("Asia/Taipei")

# Reject labels that look like dumped email / message bodies.
EMAILISH = re.compile(r"(?i)(@|mailto:|subject:|from:|to:|dear |hi |hello |附件|郵件內文|http[s]?://)")
NUM_HEAVY = re.compile(r"\d{4,}")
BALANCEISH = re.compile(r"(?i)(balance|餘額|金額|usd|twd|\$|NT\$)")


def _now_taipei() -> datetime:
    return datetime.now(TAIPEI)


def _parse_at(raw) -> datetime:
    if raw is None or raw == "":
        return _now_taipei()
    if not isinstance(raw, str):
        raise ValueError("at must be iso string")
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI)
    return dt.astimezone(TAIPEI)


def _sanitize_name(val, field: str) -> str:
    if not isinstance(val, str):
        raise ValueError("%s must be string" % field)
    name = " ".join(val.strip().split())
    if not name or len(name) > 32:
        raise ValueError("%s invalid" % field)
    if EMAILISH.search(name) or NUM_HEAVY.search(name):
        raise ValueError("%s looks like contact dump" % field)
    return name


def _sanitize_label(val) -> str:
    if not isinstance(val, str):
        raise ValueError("label must be string")
    label = " ".join(val.strip().split())
    if not label:
        raise ValueError("label required")
    if len(label) > 24:
        raise ValueError("label too long")
    if EMAILISH.search(label) or NUM_HEAVY.search(label) or BALANCEISH.search(label):
        raise ValueError("label looks like email/body dump")
    if any(ch in label for ch in "<>{}[]"):
        raise ValueError("label looks like dump")
    return label


def _record_to_public(rec: dict) -> dict:
    dt = _parse_at(rec.get("at"))
    return {
        "at": dt.isoformat(timespec="seconds"),
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M"),
        "from": rec.get("from"),
        "to": rec.get("to"),
        "label": rec.get("label"),
        "sample": bool(rec.get("sample")),
    }


def _load_logs(days: int) -> list:
    if days < 1:
        days = 1
    if days > 90:
        days = 90
    cutoff = _now_taipei() - timedelta(days=days)
    rows = []
    if not PUNCH_LOG.exists():
        return rows
    for line in PUNCH_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict):
                continue
            pub = _record_to_public(rec)
            dt = _parse_at(pub["at"])
            if dt >= cutoff:
                rows.append(pub)
        except (ValueError, json.JSONDecodeError, TypeError):
            continue
    rows.sort(key=lambda r: r["at"])
    return rows


def _append_log(rec: dict) -> None:
    PUNCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PUNCH_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _json_response(handler, status: int, obj) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/punch":
            self.path = "/punch.html"
            return super().do_GET()
        if path == "/api/punch":
            qs = parse_qs(parsed.query)
            try:
                days = int((qs.get("days") or ["90"])[0])
            except ValueError:
                days = 90
            logs = _load_logs(days)
            _json_response(self, 200, {"ok": True, "days": min(max(days, 1), 90), "logs": logs})
            return
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/save-relations":
            return self._save_relations()
        if path == "/api/punch":
            return self._post_punch()
        self.send_error(404, "Not Found")

    def _save_relations(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("expected object")
            edges = payload.get("edges", [])
            if not isinstance(edges, list):
                raise ValueError("edges must be a list")
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            _json_response(self, 400, {"ok": False, "error": "bad json"})
            return
        out = {"edges": edges}
        PENDING.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _json_response(self, 200, {"ok": True})

    def _post_punch(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") if raw else "{}")
            if not isinstance(payload, dict):
                raise ValueError("expected object")
            frm = _sanitize_name(payload.get("from"), "from")
            to = _sanitize_name(payload.get("to"), "to")
            label = _sanitize_label(payload.get("label"))
            dt = _parse_at(payload.get("at"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
            _json_response(self, 400, {"ok": False, "error": str(e) if isinstance(e, ValueError) else "bad json"})
            return
        rec = {
            "at": dt.isoformat(timespec="seconds"),
            "from": frm,
            "to": to,
            "label": label,
        }
        if payload.get("sample"):
            rec["sample"] = True
        _append_log(rec)
        _json_response(self, 200, {"ok": True, "log": _record_to_public(rec)})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("serving %s on http://%s:%s" % (ROOT, HOST, PORT))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
