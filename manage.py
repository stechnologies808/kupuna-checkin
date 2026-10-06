"""Command-line helpers.

    python manage.py demo                 play all four scenarios offline with a fake clock (no Twilio needed)
    python manage.py add                  add a kūpuna (asks questions)
    python manage.py list                 show everyone on the list
    python manage.py call-now <id>        start a check-in right now (use your own number to test)
    python manage.py pause <id> / resume <id>
    python manage.py log [n]              show the last n events (default 30)
"""
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DISABLE_SCHEDULER", "1")

import db  # noqa: E402
import phone as ph  # noqa: E402
from config import Config  # noqa: E402
from engine import Engine  # noqa: E402


def real_engine():
    return Engine(db.connect(Config.DATABASE_PATH), ph.make_phone(Config), Config)


def ask(prompt, default=""):
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def cmd_add():
    e = real_engine()
    print("Add a kūpuna. Automated calls need written consent from them (or their legal representative).")
    fields = dict(
        name=ask("Name (e.g. Auntie Leilani Kekona)"),
        phone=ask("Their phone"),
        call_time=ask("Call time, 24-hour HH:MM", "09:00"),
        language=ask("Language: English, Pidgin, Ilocano or Japanese", "English"),
        contact1_name=ask("Family contact name"),
        contact1_phone=ask("Family contact phone"),
        contact2_name=ask("Backup contact name"),
        contact2_phone=ask("Backup contact phone"),
        consent_note=ask("Consent: who agreed, how, and when"),
    )
    try:
        kid = e.add_kupuna(**fields)
    except ValueError as err:
        sys.exit(f"Not added: {err}")
    print(f"Added with id {kid}. Test it with: python manage.py call-now {kid}")


def cmd_list():
    e = real_engine()
    for k in e.db.execute("SELECT * FROM kupuna ORDER BY id"):
        state = "active" if k["active"] else "PAUSED"
        print(f"{k['id']:>3}  {k['name']:<28} {k['phone']:<14} {k['call_time']}  {k['language']:<9} "
              f"family: {k['contact1_name']}  backup: {k['contact2_name']}  ({state})")


def cmd_call_now(kid):
    e = real_engine()
    cid = e.call_now(int(kid))
    print(f"Started check-in #{cid}. Keep app.py running so the retries and alerts happen.")


def cmd_log(n=30):
    e = real_engine()
    rows = e.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(n),)).fetchall()
    for r in reversed(rows):
        print(f"{e.local(db.parse(r['at'])).strftime('%a %I:%M %p')}  {r['detail']}")


class FakeClock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)


def cmd_demo():
    """Runs a fake Tuesday morning: everyone's phone 'behaves' per their scenario."""
    tz = Config.TIMEZONE
    clock = FakeClock(datetime(2026, 10, 6, 7, 55, tzinfo=tz).astimezone(timezone.utc))
    phone = ph.DryRunPhone(quiet=True)
    e = Engine(db.connect(":memory:"), phone, Config, clock)
    people = [
        ("Auntie Leilani Kekona", "08:00", "Pidgin", "Kainoa", "Mele", "answers"),
        ("Hiroshi Tanaka", "08:00", "Japanese", "Amy", "Rev. Ito", "second"),
        ("Lola Erlinda Bautista", "08:30", "Ilocano", "Joel", "Tita Nora", "family_ok"),
        ("Uncle Walter Ah Sing", "09:00", "English", "Dana", "Bobby", "backup"),
    ]
    scen, n = {}, 0
    for name, t, lang, c1, c2, s in people:
        n += 1
        kid = e.add_kupuna(name=name, phone=f"808-555-01{n:02d}", call_time=t, language=lang,
                           contact1_name=c1, contact1_phone=f"808-555-02{n:02d}",
                           contact2_name=c2, contact2_phone=f"808-555-03{n:02d}", consent_note="demo")
        scen[kid] = s
    for _ in range(100):  # 100 simulated minutes
        e.tick()
        for c in e.db.execute("SELECT * FROM checkins WHERE status='calling'").fetchall():
            s = scen[c["kupuna_id"]]
            if s == "answers" or (s == "second" and c["attempts"] == 2):
                e.keypress(c["id"], "1")
            else:
                e.call_finished(c["id"], c["call_sid"], "no-answer")
        for c in e.db.execute("SELECT c.*, k.contact1_phone FROM checkins c JOIN kupuna k ON k.id=c.kupuna_id "
                              "WHERE status='alerted'").fetchall():
            if scen[c["kupuna_id"]] == "family_ok" and e.clock() >= db.parse(c["alerted_at"]) + timedelta(minutes=8):
                print(f"  (family text) {e.family_reply(c['contact1_phone'], 'OK')}")
        clock.advance(1)
    print()
    for r in e.db.execute("SELECT * FROM events WHERE kind!='signup' ORDER BY id"):
        print(f"{e.fmt(r['at']):>8}  {r['detail']}")
    print(f"\n{len(phone.sent)} calls/texts would have gone out. Example family alert text:\n")
    print(next(c for k, _, c in phone.sent if k == "sms"))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
    elif args[0] == "demo":
        cmd_demo()
    elif args[0] == "add":
        cmd_add()
    elif args[0] == "list":
        cmd_list()
    elif args[0] == "call-now" and len(args) == 2:
        cmd_call_now(args[1])
    elif args[0] in ("pause", "resume") and len(args) == 2:
        real_engine().set_active(int(args[1]), args[0] == "resume")
        print(f"{args[0].capitalize()}d {args[1]}.")
    elif args[0] == "log":
        cmd_log(*args[1:2])
    else:
        print(__doc__)
