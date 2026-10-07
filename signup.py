"""Public sign-up page for families, and the owner's review step on /admin.

Nobody gets called until the owner approves a sign-up and records the kūpuna's own consent.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from html import escape

from flask import redirect, request, url_for

import db

LANGUAGES = ("English", "Pidgin", "Ilocano", "Japanese")
_recent: dict[str, deque] = defaultdict(deque)  # ip -> sign-up times, simple spam brake


def _too_many(ip: str, limit: int = 5, window: int = 3600) -> bool:
    q, now = _recent[ip], time.time()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


PUBLIC_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&display=swap">
<style>
:root{--bg:#F1F4F2;--s:#fff;--sunk:#E6ECE9;--line:#D3DCD8;--fg:#17221F;--mu:#5A6964;--ac:#B3322B;--acink:#fff;--ok:#1F7A4D;--okb:#DDF0E5;--bad:#B3322B;--badb:#F8DEDB;
--display:"Bricolage Grotesque","Segoe UI",system-ui,sans-serif;--body:"Atkinson Hyperlegible","Segoe UI",system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#111816;--s:#18211E;--sunk:#1F2A26;--line:#2C3934;--fg:#E7EEEB;--mu:#9AABA5;--ac:#F07A6F;--acink:#1A0E0C;--ok:#6FD39E;--okb:#173326;--bad:#F48A80;--badb:#3F1C19;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.55 var(--body);padding:28px 18px 56px}
main{max-width:860px;margin:auto;display:grid;gap:30px}
h1,h2,h3{font-family:var(--display);margin:0;letter-spacing:-.01em;text-wrap:balance}
h1{font-size:clamp(30px,5.2vw,46px);font-weight:800;line-height:1.08}
h2{font-size:23px;font-weight:600}h3{font-size:19px;font-weight:600}
p{margin:0;max-width:62ch}.mu{color:var(--mu)}
.eyebrow{font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--ac)}
.hero{display:grid;gap:12px}
.steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;list-style:none;padding:0;margin:0;counter-reset:s}
.steps li{background:var(--s);border:1px solid var(--line);border-radius:12px;padding:16px;counter-increment:s;display:grid;gap:4px}
.steps li::before{content:counter(s);font-family:var(--display);font-weight:800;font-size:22px;color:var(--ac)}
.plans{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.plan{position:relative;background:var(--s);border:2px solid var(--line);border-radius:12px;padding:16px 18px;cursor:pointer;display:grid;gap:6px;font-weight:400;font-size:16px}
.plan:has(input:checked){border-color:var(--ac)}
.plan input{position:absolute;opacity:0}
.plan .price{font-family:var(--display);font-size:26px;font-weight:800}
.plan:focus-within{outline:3px solid var(--ac);outline-offset:2px}
form{display:grid;gap:22px}
fieldset{border:1px solid var(--line);border-radius:12px;background:var(--s);padding:16px 18px 18px;margin:0;display:grid;gap:14px;grid-template-columns:repeat(2,minmax(0,1fr))}
legend{font-family:var(--display);font-weight:600;font-size:19px;padding:0 6px}
label{display:grid;gap:4px;font-weight:700;font-size:15px}
label .hint{font-weight:400;color:var(--mu);font-size:14px}
input,select,textarea{font:inherit;font-weight:400;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);width:100%}
textarea{min-height:80px}
.full{grid-column:1/-1}
.check{display:flex;gap:10px;align-items:flex-start;font-weight:400}
.check input{width:auto;margin-top:5px}
button,.btn{font:inherit;font-weight:700;background:var(--ac);color:var(--acink);border:0;border-radius:10px;padding:13px 22px;cursor:pointer;text-decoration:none;display:inline-block;justify-self:start}
:focus-visible{outline:3px solid var(--ac);outline-offset:2px}
.err{background:var(--badb);color:var(--bad);padding:12px 14px;border-radius:10px;font-weight:700}
.note{background:var(--okb);color:var(--ok);padding:12px 14px;border-radius:10px;font-weight:700}
.trap{position:absolute;left:-9999px}
.small{font-size:14px;color:var(--mu)}
@media (max-width:640px){.steps,.plans,fieldset{grid-template-columns:1fr}}
</style></head><body><main>"""


def _sel(name, options, chosen):
    return (f"<select name='{name}' id='{name}'>"
            + "".join(f"<option{' selected' if o == chosen else ''}>{o}</option>" for o in options) + "</select>")


def _v(form, k, default=""):
    return escape(form.get(k, default))


def signup_page(cfg, form=None, error=""):
    form = form or {}
    plan = form.get("plan", "basic")
    trial = f"<p class='note'>{escape(cfg.FREE_TRIAL_NOTE)}</p>" if cfg.FREE_TRIAL_NOTE else ""
    return (PUBLIC_HEAD.replace("{title}", "Kupuna Check-In · Sign up") + f"""
<section class="hero">
  <span class="eyebrow">Oʻahu · daily check-in calls</span>
  <h1>A friendly call every morning for the kūpuna you love</h1>
  <p class="mu">Your kūpuna gets a short call at the time they choose and presses 1 if all good.
  If they don't answer, we try again, then text and call you so someone can check on them.</p>
</section>
<ol class="steps">
  <li><h3>Morning call</h3><span class="mu">In English, Pidgin, Ilocano or Japanese.</span></li>
  <li><h3>Three tries</h3><span class="mu">No answer? We call back 5 and 10 minutes later.</span></li>
  <li><h3>You get a text</h3><span class="mu">Plus a call. If you don't reply, we reach your backup person.</span></li>
</ol>
{trial}
{f"<p class='err' role='alert'>{escape(error)}</p>" if error else ""}
<form method="post" action="{url_for('signup_submit')}" novalidate>
  <div class="plans" role="radiogroup" aria-label="Plan">
    <label class="plan"><input type="radio" name="plan" value="basic" {'checked' if plan == 'basic' else ''}>
      <h3>Daily Check-In</h3><span class="price">{escape(cfg.PRICE_BASIC)}</span>
      <span class="mu">Automated morning call, family alerts, and a weekly summary text.</span></label>
    <label class="plan"><input type="radio" name="plan" value="talk_story" {'checked' if plan == 'talk_story' else ''}>
      <h3>Talk Story</h3><span class="price">{escape(cfg.PRICE_TALK_STORY)}</span>
      <span class="mu">Everything in Daily Check-In, plus a weekly 15-minute call with a real person.</span></label>
  </div>

  <fieldset><legend>About you</legend>
    <label>Your name<input id="family_name" name="family_name" autocomplete="name" value="{_v(form,'family_name')}"></label>
    <label>Relationship to your kūpuna<input id="relationship" name="relationship" placeholder="Son, daughter, niece…" value="{_v(form,'relationship')}"></label>
    <label>Your mobile phone<span class="hint">Alerts are texted here</span><input id="family_phone" name="family_phone" type="tel" autocomplete="tel" value="{_v(form,'family_phone')}"></label>
    <label>Your email<input id="family_email" name="family_email" type="email" autocomplete="email" value="{_v(form,'family_email')}"></label>
  </fieldset>

  <fieldset><legend>About your kūpuna</legend>
    <label>Their name<span class="hint">How the call should greet them, e.g. Auntie Leilani</span><input id="kupuna_name" name="kupuna_name" value="{_v(form,'kupuna_name')}"></label>
    <label>Their phone<span class="hint">The phone they'll answer each morning</span><input id="kupuna_phone" name="kupuna_phone" type="tel" value="{_v(form,'kupuna_phone')}"></label>
    <label>Call time<span class="hint">Hawaiʻi time, between 7 AM and 9 PM</span><input id="call_time" name="call_time" type="time" value="{_v(form,'call_time','09:00')}"></label>
    <label>Language for the call{_sel('language', LANGUAGES, form.get('language', 'English'))}</label>
  </fieldset>

  <fieldset><legend>Backup contact</legend>
    <p class="full mu small">Someone nearby we can reach if you don't reply within 15 minutes: a sibling, neighbor, or church friend.</p>
    <label>Backup's name<input id="backup_name" name="backup_name" value="{_v(form,'backup_name')}"></label>
    <label>Backup's phone<input id="backup_phone" name="backup_phone" type="tel" value="{_v(form,'backup_phone')}"></label>
    <label class="full">Anything we should know? <span class="hint">Hearing, best way to reach them, etc. (optional)</span>
      <textarea id="notes" name="notes">{_v(form,'notes')}</textarea></label>
  </fieldset>

  <label class="trap" aria-hidden="true">Leave this empty<input name="website" tabindex="-1" autocomplete="off"></label>
  <label class="check"><input type="checkbox" id="agree_consent" name="agree_consent" {'checked' if form.get('agree_consent') else ''}>
    I understand you'll call my kūpuna to introduce yourselves and get their OK before daily calls begin.</label>
  <label class="check"><input type="checkbox" id="agree_911" name="agree_911" {'checked' if form.get('agree_911') else ''}>
    I understand this service texts and calls family. It does not call 911 and is not a medical alert system.</label>
  <button type="submit">Sign up</button>
  <p class="small">We use these phone numbers only for check-in calls and alerts. Reply STOP to any text to stop texts.</p>
</form>
</main></body></html>""")


def thanks_page(cfg, s):
    link = cfg.PAYMENT_LINK_TALK_STORY if s["plan"] == "talk_story" else cfg.PAYMENT_LINK_BASIC
    pay = (f"<p>To set up payment now, use this secure link:</p><a class='btn' href='{escape(link)}'>Set up payment</a>"
           if link else "<p>We'll text you a payment link once your kūpuna has said yes.</p>")
    return (PUBLIC_HEAD.replace("{title}", "Kupuna Check-In · Thank you") + f"""
<section class="hero"><span class="eyebrow">Mahalo, {escape(s['family_name'].split()[0])}</span>
<h1>You're signed up. Here's what happens next.</h1></section>
<ol class="steps">
  <li><h3>We call {escape(s['kupuna_name'])}</h3><span class="mu">Usually within 1–2 days, to introduce ourselves and get their OK.</span></li>
  <li><h3>You get a text</h3><span class="mu">Confirming the start date and call time.</span></li>
  <li><h3>Calls begin</h3><span class="mu">Every day at the time you picked.</span></li>
</ol>
{pay}
<p class="small">Questions? Reply to the text we send you.</p>
</main></body></html>""")


def register(app, engine, lock, admin_only, admin_head):
    @app.get("/signup")
    def signup_form():
        return signup_page(engine.cfg)

    @app.post("/signup")
    def signup_submit():
        f = request.form
        if f.get("website"):  # bots fill hidden fields; pretend it worked
            return redirect(url_for("signup_form"))
        ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()
        if _too_many(ip):
            return signup_page(engine.cfg, f, "Too many sign-ups from this connection. Please try again in an hour."), 429
        if not (f.get("agree_consent") and f.get("agree_911")):
            return signup_page(engine.cfg, f, "Please tick both boxes at the bottom to continue."), 400
        try:
            with lock:
                sid = engine.add_signup(**{k: f.get(k, "") for k in engine.SIGNUP_FIELDS})
                s = engine.signup(sid)
        except ValueError as e:
            return signup_page(engine.cfg, f, str(e)), 400
        return thanks_page(engine.cfg, s)

    @app.get("/admin/signup/<int:signup_id>")
    @admin_only
    def admin_signup(signup_id):
        from app import form_fields  # shared with the edit page
        with lock:
            s = engine.signup(signup_id)
        if not s:
            return redirect(url_for("admin", msg="That sign-up doesn't exist."))
        flash = request.args.get("msg", "")
        prefill = {"name": s["kupuna_name"], "phone": s["kupuna_phone"], "call_time": s["call_time"],
                   "language": s["language"], "contact1_name": s["family_name"], "contact1_phone": s["family_phone"],
                   "contact2_name": s["backup_name"], "contact2_phone": s["backup_phone"], "consent_note": ""}
        details = (f"<p><b>{escape(s['family_name'])}</b> ({escape(s['relationship'] or 'family')}) · "
                   f"<span class='mono'>{escape(s['family_phone'])}</span> · {escape(s['family_email'])}</p>"
                   f"<p>Plan: <b>{escape(engine.PLANS.get(s['plan'], s['plan']))}</b> · signed up "
                   f"{escape(engine.local(db.parse(s['created_at'])).strftime('%a %b %-d, %-I:%M %p'))}</p>"
                   + (f"<p>Notes: {escape(s['notes'])}</p>" if s["notes"] else ""))
        if s["status"] != "new":
            body = f"<p class='mu'>This sign-up was {escape(s['status'])}.</p>"
        else:
            body = (f"<p class='mu'>Call {escape(s['kupuna_name'])} at <span class='mono'>{escape(s['kupuna_phone'])}</span> first. "
                    "Only approve once they've agreed themselves. Write down how and when in the consent box.</p>"
                    f"<div class='box'><form class='add' method='post' action='{url_for('admin_signup_approve', signup_id=signup_id)}'>"
                    + form_fields(prefill)
                    + "<div><button type='submit'>Approve and start calls</button></div></form></div>"
                    f"<form method='post' action='{url_for('admin_signup_decline', signup_id=signup_id)}' style='margin-top:12px'>"
                    "<button>Decline this sign-up</button></form>")
        return (admin_head + f"<header><h1>Sign-up for {escape(s['kupuna_name'])}</h1>"
                f"<p class='mu'><a href='{url_for('admin')}'>Back to today</a></p></header>"
                + (f"<p class='err'>{escape(flash)}</p>" if flash else "")
                + f"<section class='box' style='padding:12px 14px'>{details}</section><section>{body}</section></main></body></html>")

    @app.post("/admin/signup/<int:signup_id>/approve")
    @admin_only
    def admin_signup_approve(signup_id):
        try:
            with lock:
                engine.approve_signup(signup_id, **{k: request.form.get(k, "") for k in engine.FIELDS})
        except ValueError as e:
            return redirect(url_for("admin_signup", signup_id=signup_id, msg=str(e)))
        return redirect(url_for("admin"))

    @app.post("/admin/signup/<int:signup_id>/decline")
    @admin_only
    def admin_signup_decline(signup_id):
        with lock:
            engine.decline_signup(signup_id)
        return redirect(url_for("admin"))


def pending_section(engine, url_for) -> str:
    rows = engine.db.execute("SELECT * FROM signups WHERE status='new' ORDER BY id").fetchall()
    if not rows:
        return ""
    items = "".join(
        f"<tr><td><b>{escape(r['kupuna_name'])}</b><br><span class='mu'>from {escape(r['family_name'])} · "
        f"{escape(engine.PLANS.get(r['plan'], r['plan']))}</span></td>"
        f"<td><a class='btn' href='{url_for('admin_signup', signup_id=r['id'])}'>Review</a></td></tr>"
        for r in rows)
    return (f"<section><h2>New sign-ups ({len(rows)})</h2><div class='box'><table>"
            f"<tr><th>Kūpuna</th><th></th></tr>{items}</table></div></section>")
