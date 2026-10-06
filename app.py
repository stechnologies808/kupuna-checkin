"""Web app: Twilio webhooks, the background scheduler, and the status page.

Run locally:   python app.py
Run in production with ONE worker (the scheduler lives in this process):
               gunicorn -w 1 --threads 4 -b 0.0.0.0:$PORT app:app
"""
from __future__ import annotations

import os
import threading
import time
from functools import wraps
from html import escape

from flask import Flask, Response, abort, redirect, request, url_for

import db
import phone as ph
from config import Config
from engine import Engine

app = Flask(__name__, static_folder="static", static_url_path="/static")
cfg = Config
lock = threading.RLock()  # one SQLite connection, used by the web threads and the scheduler in turn
engine = Engine(db.connect(cfg.DATABASE_PATH), ph.make_phone(cfg), cfg)


def xml(body: str) -> Response:
    return Response(body, mimetype="text/xml")


def from_twilio(view):
    """Reject webhook requests that weren't signed by Twilio (skipped in dry run)."""
    @wraps(view)
    def wrapper(*a, **kw):
        if not cfg.DRY_RUN:
            url = cfg.PUBLIC_BASE_URL + request.full_path.rstrip("?")
            if not ph.signature_ok(cfg.TWILIO_AUTH_TOKEN, url, request.form.to_dict(),
                                   request.headers.get("X-Twilio-Signature", "")):
                abort(403)
        return view(*a, **kw)
    return wrapper


def admin_only(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not cfg.ADMIN_PASSWORD:
            return Response("Set ADMIN_PASSWORD in .env to use this page.", 403)
        auth = request.authorization
        if not auth or auth.password != cfg.ADMIN_PASSWORD:
            return Response("Sign in", 401, {"WWW-Authenticate": 'Basic realm="Kupuna Check-In"'})
        if request.method == "POST":  # block forms posted from other sites
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/") and origin.rstrip("/") != cfg.PUBLIC_BASE_URL:
                abort(403)
        return view(*a, **kw)
    return wrapper


# ---------- Twilio webhooks ----------
@app.post("/voice/checkin/<int:checkin_id>")
@from_twilio
def voice_checkin(checkin_id):
    with lock:
        c = engine.checkin(checkin_id)
        if not c:
            return xml('<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>')
        k = engine.kupuna(c["kupuna_id"])
        return xml(ph.twiml_checkin(cfg.PUBLIC_BASE_URL, checkin_id, k["id"], k["name"], k["language"]))


@app.post("/voice/gather/<int:checkin_id>")
@from_twilio
def voice_gather(checkin_id):
    digit = request.form.get("Digits", "")
    with lock:
        result = engine.keypress(checkin_id, digit)
        c = engine.checkin(checkin_id)
        lang = engine.kupuna(c["kupuna_id"])["language"] if c else "English"
        return xml(ph.twiml_after_key(result, cfg.PUBLIC_BASE_URL, checkin_id, lang))


@app.post("/voice/status/<int:checkin_id>")
@from_twilio
def voice_status(checkin_id):
    with lock:
        engine.call_finished(checkin_id, request.form.get("CallSid", ""), request.form.get("CallStatus", ""))
    return ("", 204)


@app.post("/sms")
@from_twilio
def sms_in():
    with lock:
        reply = engine.family_reply(request.form.get("From", ""), request.form.get("Body", ""))
    return xml(ph.twiml_sms_reply(reply))


# ---------- status page ----------
STATUS_LABEL = {
    "scheduled": ("Starting", "warn"), "calling": ("Calling", "warn"), "waiting_retry": ("Will retry", "warn"),
    "ok": ("Checked in", "ok"), "safe": ("Safe (family confirmed)", "ok"), "alerted": ("Family alerted", "bad"),
    "backup_alerted": ("Backup alerted", "bad"), "help": ("Asked for help", "bad"), "skipped": ("Skipped", "idle"),
}

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30">
<title>Kupuna Check-In · Today</title><style>
:root{--bg:#F1F4F2;--s:#fff;--line:#D3DCD8;--fg:#17221F;--mu:#5A6964;--ok:#1F7A4D;--okb:#DDF0E5;--warn:#9A6200;--warnb:#FBEBCB;--bad:#B3322B;--badb:#F8DEDB;--idb:#E6ECE9}
@media (prefers-color-scheme:dark){:root{--bg:#111816;--s:#18211E;--line:#2C3934;--fg:#E7EEEB;--mu:#9AABA5;--ok:#6FD39E;--okb:#173326;--warn:#F2BE5C;--warnb:#3A2C10;--bad:#F48A80;--badb:#3F1C19;--idb:#1F2A26;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;padding:20px 16px 40px}
main{max-width:1000px;margin:auto;display:grid;gap:20px}h1,h2{margin:0}h1{font-size:26px}h2{font-size:18px}
.box{background:var(--s);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
table{width:100%;border-collapse:collapse}td,th{padding:8px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:14px}
th{color:var(--mu);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap}
.ok{background:var(--okb);color:var(--ok)}.warn{background:var(--warnb);color:var(--warn)}.bad{background:var(--badb);color:var(--bad)}.idle{background:var(--idb);color:var(--mu)}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:13px;color:var(--mu);white-space:nowrap}
.dry{background:var(--warnb);color:var(--warn);padding:8px 12px;border-radius:8px;font-weight:600}
form.inline{display:inline}button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
.add{display:grid;gap:10px;padding:14px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.add label{display:grid;gap:3px;font-size:13px;font-weight:600}.add input,.add select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.err{color:var(--bad);font-weight:600}.mu{color:var(--mu)}
</style></head><body><main>
<header><h1>Kupuna Check-In</h1><p class="mu">{today} · refreshes every 30 seconds</p></header>
{dry}{flash}
<section><h2>Today</h2><div class="box"><table><tr><th>Kūpuna</th><th>Call time</th><th>Status</th><th>Tries</th><th></th></tr>{rows}</table></div></section>
<section><h2>Recent activity</h2><div class="box"><table><tr><th>Time</th><th>What happened</th></tr>{events}</table></div></section>
<section><h2>Add a kūpuna</h2><div class="box"><form class="add" method="post" action="{add_url}">
<label>Name<input name="name" required placeholder="Auntie Leilani Kekona"></label>
<label>Phone<input name="phone" required placeholder="808-555-0142"></label>
<label>Call time<input name="call_time" type="time" value="09:00" required></label>
<label>Language<select name="language"><option>English</option><option>Pidgin</option><option>Ilocano</option><option>Japanese</option></select></label>
<label>Family contact name<input name="contact1_name" required></label>
<label>Family contact phone<input name="contact1_phone" required></label>
<label>Backup contact name<input name="contact2_name" required></label>
<label>Backup contact phone<input name="contact2_phone" required></label>
<label style="grid-column:1/-1">Consent (who agreed in writing, and when)<input name="consent_note" required placeholder="Signed form from Leilani Kekona, 10/6/2026, kept in binder"></label>
<div><button type="submit">Add to the list</button></div></form></div></section>
</main></body></html>"""


@app.get("/")
def home():
    return redirect(url_for("admin"))


@app.get("/admin")
@admin_only
def admin():
    with lock:
        now = engine.clock()
        day = engine.local(now).date().isoformat()
        kup = engine.db.execute("SELECT * FROM kupuna ORDER BY call_time, name").fetchall()
        today = {r["kupuna_id"]: r for r in engine.db.execute("SELECT * FROM checkins WHERE day=?", (day,))}
        events = engine.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 60").fetchall()
    rows = []
    for k in kup:
        c = today.get(k["id"])
        label, tone = STATUS_LABEL.get(c["status"], (c["status"], "idle")) if c else (
            ("Waiting", "idle") if k["active"] else ("Paused", "idle"))
        toggle = "Pause" if k["active"] else "Resume"
        rows.append(
            f"<tr><td><b>{escape(k['name'])}</b><br><span class='mono'>{escape(k['phone'])} · {escape(k['language'])}</span>"
            f"<br><span class='mu'>Family: {escape(k['contact1_name'])} · Backup: {escape(k['contact2_name'])}</span></td>"
            f"<td class='mono'>{escape(k['call_time'])}</td><td><span class='pill {tone}'>{escape(label)}</span></td>"
            f"<td class='mono'>{c['attempts'] if c else 0}</td><td>"
            f"<form class='inline' method='post' action='{url_for('admin_call_now', kupuna_id=k['id'])}'><button>Call now</button></form> "
            f"<form class='inline' method='post' action='{url_for('admin_toggle', kupuna_id=k['id'])}'><button>{toggle}</button></form></td></tr>")
    ev = "".join(f"<tr><td class='mono'>{escape(engine.local(db.parse(e['at'])).strftime('%a %-I:%M %p'))}</td>"
                 f"<td>{escape(e['detail'])}</td></tr>" for e in events)
    flash = request.args.get("msg", "")
    html = (PAGE.replace("{today}", engine.local(now).strftime("%A, %B %-d"))
            .replace("{dry}", "<p class='dry'>Dry run: nothing is really being called or texted. Set DRY_RUN=0 to go live.</p>" if cfg.DRY_RUN else "")
            .replace("{flash}", f"<p class='err'>{escape(flash)}</p>" if flash else "")
            .replace("{rows}", "".join(rows) or "<tr><td colspan='5' class='mu'>No kūpuna yet. Add one below.</td></tr>")
            .replace("{events}", ev or "<tr><td colspan='2' class='mu'>Nothing yet.</td></tr>")
            .replace("{add_url}", url_for("admin_add")))
    return html


@app.post("/admin/add")
@admin_only
def admin_add():
    f = request.form
    try:
        with lock:
            engine.add_kupuna(**{k: f.get(k, "") for k in (
                "name", "phone", "call_time", "language", "contact1_name", "contact1_phone",
                "contact2_name", "contact2_phone", "consent_note")})
    except ValueError as e:
        return redirect(url_for("admin", msg=str(e)))
    return redirect(url_for("admin"))


@app.post("/admin/call-now/<int:kupuna_id>")
@admin_only
def admin_call_now(kupuna_id):
    try:
        with lock:
            engine.call_now(kupuna_id)
    except (ValueError, ph.TwilioError) as e:
        return redirect(url_for("admin", msg=str(e)))
    return redirect(url_for("admin"))


@app.post("/admin/toggle/<int:kupuna_id>")
@admin_only
def admin_toggle(kupuna_id):
    with lock:
        k = engine.kupuna(kupuna_id)
        if k:
            engine.set_active(kupuna_id, not k["active"])
    return redirect(url_for("admin"))


@app.get("/health")
def health():
    return {"ok": True, "dry_run": cfg.DRY_RUN}


# ---------- scheduler ----------
def scheduler_loop(every_seconds: int = 20):
    while True:
        try:
            with lock:
                engine.tick()
        except Exception as e:  # keep going; a Twilio hiccup shouldn't stop tomorrow's calls
            app.logger.exception("Scheduler tick failed: %s", e)
        time.sleep(every_seconds)


def start_scheduler():
    if os.environ.get("DISABLE_SCHEDULER") == "1":
        return
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()


start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
