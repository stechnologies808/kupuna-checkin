"""The check-in rules. No web or Twilio code here, so every rule is testable.

A morning for one kūpuna moves through these statuses:

    scheduled ──call──> calling ──pressed 1──> ok
                           │
                     missed│ (no answer, busy, voicemail, hung up without pressing)
                           v
                     waiting_retry ──5 min──> calling   (up to MAX_TRIES)
                           │
                  3rd miss │
                           v
                       alerted  (family contact texted + called)
                     │        │
        family replies OK   15 min, no reply
                     v        v
                   safe    backup_alerted (backup contact texted + called)
                              │
                     either replies OK ──> safe

Pressing 2 at any point jumps to `help`: both contacts are texted and called at once.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

import greetings
from db import iso, parse

OPEN = ("scheduled", "calling", "waiting_retry")
NEEDS_FAMILY = ("alerted", "backup_alerted", "help")
DONE = ("ok", "safe", "skipped")


class Phone(Protocol):
    def place_checkin_call(self, to: str, checkin_id: int) -> str: ...
    def alert_call(self, to: str, message: str) -> str: ...
    def send_sms(self, to: str, body: str) -> str: ...


def normalize_phone(raw: str) -> str:
    """Return +1XXXXXXXXXX for US numbers; keep other +country numbers as typed."""
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    raise ValueError(f"Can't read phone number {raw!r}. Use a 10-digit number like 808-555-0142.")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Engine:
    def __init__(self, conn: sqlite3.Connection, phone: Phone, cfg, clock: Callable[[], datetime] = utcnow):
        self.db = conn
        self.phone = phone
        self.cfg = cfg
        self.clock = clock

    # ---------- helpers ----------
    def local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.cfg.TIMEZONE)

    def fmt(self, dt: datetime | str | None) -> str:
        if isinstance(dt, str):
            dt = parse(dt)
        return self.local(dt).strftime("%-I:%M %p") if dt else ""

    def log(self, checkin: sqlite3.Row | None, kupuna_id: int, kind: str, detail: str) -> None:
        self.db.execute(
            "INSERT INTO events (checkin_id, kupuna_id, at, kind, detail) VALUES (?,?,?,?,?)",
            (checkin["id"] if checkin else None, kupuna_id, iso(self.clock()), kind, detail),
        )

    def kupuna(self, kupuna_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM kupuna WHERE id=?", (kupuna_id,)).fetchone()

    def checkin(self, checkin_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM checkins WHERE id=?", (checkin_id,)).fetchone()

    def update(self, checkin_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE checkins SET {cols} WHERE id=?", (*fields.values(), checkin_id))

    def in_quiet_hours(self, now: datetime) -> bool:
        h = self.local(now).hour
        return h < self.cfg.QUIET_BEFORE_HOUR or h >= self.cfg.QUIET_AFTER_HOUR

    # ---------- sign-up ----------
    FIELDS = ("name", "phone", "call_time", "language", "contact1_name", "contact1_phone",
              "contact2_name", "contact2_phone", "consent_note")

    def _clean(self, f: dict) -> dict:
        f = {k: (f.get(k) or "").strip() for k in self.FIELDS}
        for k, label in (("name", "their name"), ("contact1_name", "the family contact's name"),
                         ("contact2_name", "the backup contact's name")):
            if not f[k]:
                raise ValueError(f"Add {label}.")
        if not f["consent_note"]:
            raise ValueError("Record who agreed to automated calls, how, and when (consent_note).")
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", f["call_time"]):
            raise ValueError("call_time must look like 08:30")
        hour = int(f["call_time"].split(":")[0])
        if hour < self.cfg.QUIET_BEFORE_HOUR or hour >= self.cfg.QUIET_AFTER_HOUR:
            raise ValueError(f"Pick a call time between {self.cfg.QUIET_BEFORE_HOUR}:00 and {self.cfg.QUIET_AFTER_HOUR}:00.")
        if f["language"] not in greetings.GREETING:
            raise ValueError(f"language must be one of {', '.join(greetings.GREETING)}")
        for k in ("phone", "contact1_phone", "contact2_phone"):
            f[k] = normalize_phone(f[k])
        f["call_time"] = "%02d:%s" % (hour, f["call_time"].split(":")[1])
        return f

    def add_kupuna(self, **fields) -> int:
        f = self._clean(fields)
        now = iso(self.clock())
        cur = self.db.execute(
            f"INSERT INTO kupuna ({', '.join(self.FIELDS)}, consent_at, created_at) "
            f"VALUES ({', '.join('?' * (len(self.FIELDS) + 2))})",
            (*(f[k] for k in self.FIELDS), now, now),
        )
        self.log(None, cur.lastrowid, "signup", f"Added {f['name']} · calls at {f['call_time']} in {f['language']}")
        return cur.lastrowid

    def update_kupuna(self, kupuna_id: int, **fields) -> None:
        old = self.kupuna(kupuna_id)
        if not old:
            raise ValueError(f"No kūpuna with id {kupuna_id}")
        f = self._clean(fields)
        changed = [k for k in self.FIELDS if f[k] != old[k]]
        if not changed:
            return
        sets = ", ".join(f"{k}=?" for k in changed)
        vals = [f[k] for k in changed]
        if "consent_note" in changed:
            sets += ", consent_at=?"
            vals.append(iso(self.clock()))
        self.db.execute(f"UPDATE kupuna SET {sets} WHERE id=?", (*vals, kupuna_id))
        labels = {"name": "name", "phone": "phone", "call_time": "call time", "language": "language",
                  "contact1_name": "family contact", "contact1_phone": "family phone",
                  "contact2_name": "backup contact", "contact2_phone": "backup phone", "consent_note": "consent note"}
        self.log(None, kupuna_id, "signup", f"Updated {f['name']}: {', '.join(labels[k] for k in changed)}")

    # ---------- public sign-ups (need the owner's approval before any calls) ----------
    PLANS = {"basic": "Daily Check-In", "talk_story": "Talk Story"}
    SIGNUP_FIELDS = ("plan", "family_name", "family_phone", "family_email", "relationship", "kupuna_name",
                     "kupuna_phone", "call_time", "language", "backup_name", "backup_phone", "notes")

    def add_signup(self, **fields) -> int:
        f = {k: (fields.get(k) or "").strip() for k in self.SIGNUP_FIELDS}
        missing = [label for k, label in (
            ("family_name", "your name"), ("family_phone", "your phone"), ("family_email", "your email"),
            ("kupuna_name", "your kūpuna's name"), ("kupuna_phone", "their phone"),
            ("backup_name", "a backup contact's name"), ("backup_phone", "the backup contact's phone"))
            if not f[k]]
        if missing:
            raise ValueError("Please add " + ", ".join(missing) + ".")
        if f["plan"] not in self.PLANS:
            raise ValueError("Please choose a plan.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", f["family_email"]):
            raise ValueError("That email address doesn't look right.")
        if len(f["notes"]) > 1000 or any(len(f[k]) > 120 for k in self.SIGNUP_FIELDS if k != "notes"):
            raise ValueError("One of the answers is too long.")
        for k in ("family_phone", "kupuna_phone", "backup_phone"):
            f[k] = normalize_phone(f[k])
        if f["kupuna_phone"] in (f["family_phone"], f["backup_phone"]):
            raise ValueError("Your kūpuna's phone needs to be different from the contact phones.")
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", f["call_time"]):
            raise ValueError("Please pick a call time.")
        hour = int(f["call_time"].split(":")[0])
        if hour < self.cfg.QUIET_BEFORE_HOUR or hour >= self.cfg.QUIET_AFTER_HOUR:
            raise ValueError(f"Please pick a call time between {self.cfg.QUIET_BEFORE_HOUR} AM and "
                             f"{self.cfg.QUIET_AFTER_HOUR - 12} PM.")
        if f["language"] not in greetings.GREETING:
            f["language"] = "English"
        cur = self.db.execute(
            f"INSERT INTO signups (created_at, {', '.join(self.SIGNUP_FIELDS)}) "
            f"VALUES ({', '.join('?' * (len(self.SIGNUP_FIELDS) + 1))})",
            (iso(self.clock()), *(f[k] for k in self.SIGNUP_FIELDS)),
        )
        self.log(None, None, "signup", f"New sign-up: {f['family_name']} for {f['kupuna_name']} ({self.PLANS[f['plan']]})")
        if self.cfg.OWNER_PHONE:
            try:
                self.phone.send_sms(normalize_phone(self.cfg.OWNER_PHONE),
                                    f"{self.cfg.SERVICE_NAME}: new sign-up from {f['family_name']} "
                                    f"({f['family_phone']}) for {f['kupuna_name']}, {self.PLANS[f['plan']]} plan. "
                                    f"Review it on /admin.")
            except Exception:  # the sign-up is saved either way; a failed text shouldn't lose it
                pass
        return cur.lastrowid

    def signup(self, signup_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM signups WHERE id=?", (signup_id,)).fetchone()

    def approve_signup(self, signup_id: int, **kupuna_fields) -> int:
        s = self.signup(signup_id)
        if not s or s["status"] != "new":
            raise ValueError("That sign-up was already handled.")
        kid = self.add_kupuna(**kupuna_fields)
        self.db.execute("UPDATE signups SET status='approved', kupuna_id=?, decided_at=? WHERE id=?",
                        (kid, iso(self.clock()), signup_id))
        return kid

    def decline_signup(self, signup_id: int) -> None:
        s = self.signup(signup_id)
        if s and s["status"] == "new":
            self.db.execute("UPDATE signups SET status='declined', decided_at=? WHERE id=?",
                            (iso(self.clock()), signup_id))
            self.log(None, None, "signup", f"Declined sign-up from {s['family_name']} for {s['kupuna_name']}")

    def call_now(self, kupuna_id: int) -> int:
        """Start (or restart) today's check-in right away. For testing with your own phone."""
        k = self.kupuna(kupuna_id)
        if not k:
            raise ValueError(f"No kūpuna with id {kupuna_id}")
        now = self.clock()
        day = self.local(now).date().isoformat()
        row = self.db.execute("SELECT id FROM checkins WHERE kupuna_id=? AND day=?", (kupuna_id, day)).fetchone()
        if row:
            cid = row["id"]
            self.update(cid, status="scheduled", attempts=0, call_sid=None, last_call_at=None, next_at=iso(now),
                        alerted_at=None, backup_at=None, resolved_by=None, resolved_at=None)
        else:
            cid = self.db.execute("INSERT INTO checkins (kupuna_id, day, status, next_at) VALUES (?,?,?,?)",
                                  (kupuna_id, day, "scheduled", iso(now))).lastrowid
        self.log(self.checkin(cid), kupuna_id, "manual", f"Started a check-in for {k['name']} by hand")
        self._place_call(self.checkin(cid), now, force=True)
        return cid

    def remove_kupuna(self, kupuna_id: int) -> None:
        """Take someone off the list: no more calls or alerts, history kept."""
        k = self.kupuna(kupuna_id)
        if not k or k["removed_at"]:
            return
        now = iso(self.clock())
        self.db.execute("UPDATE kupuna SET active=0, removed_at=? WHERE id=?", (now, kupuna_id))
        # stop anything still in progress today (retries, family alerts, backup alerts)
        self.db.execute(
            "UPDATE checkins SET next_at=NULL, status=CASE WHEN status IN ('scheduled','calling','waiting_retry') "
            "THEN 'skipped' ELSE status END WHERE kupuna_id=? AND next_at IS NOT NULL", (kupuna_id,))
        self.log(None, kupuna_id, "signup", f"Removed {k['name']} from the list")

    def restore_kupuna(self, kupuna_id: int) -> None:
        k = self.kupuna(kupuna_id)
        if not k or not k["removed_at"]:
            return
        self.db.execute("UPDATE kupuna SET active=1, removed_at=NULL WHERE id=?", (kupuna_id,))
        self.log(None, kupuna_id, "signup", f"Restored {k['name']} to the list")

    def set_active(self, kupuna_id: int, active: bool) -> None:
        self.db.execute("UPDATE kupuna SET active=? WHERE id=?", (1 if active else 0, kupuna_id))
        self.log(None, kupuna_id, "signup", "Calls resumed" if active else "Calls paused")

    # ---------- the scheduler tick ----------
    def tick(self) -> None:
        """Run every ~20 seconds. Starts today's calls and moves overdue steps along."""
        now = self.clock()
        self._start_due_mornings(now)
        due = self.db.execute(
            "SELECT * FROM checkins WHERE next_at IS NOT NULL AND next_at <= ? ORDER BY next_at", (iso(now),)
        ).fetchall()
        for c in due:
            if c["status"] in ("scheduled", "waiting_retry"):
                self._place_call(c, now)
            elif c["status"] == "calling":
                self._miss(c, now, "no result from the phone company")
            elif c["status"] == "alerted":
                self._alert_backup(c, now)
        self._weekly_summaries(now)

    def _start_due_mornings(self, now: datetime) -> None:
        local_now = self.local(now)
        day = local_now.date().isoformat()
        for k in self.db.execute("SELECT * FROM kupuna WHERE active=1").fetchall():
            hh, mm = map(int, k["call_time"].split(":"))
            call_at = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if local_now < call_at:
                continue
            exists = self.db.execute("SELECT 1 FROM checkins WHERE kupuna_id=? AND day=?", (k["id"], day)).fetchone()
            if exists:
                continue
            late = local_now - call_at > timedelta(minutes=self.cfg.LATE_START_WINDOW_MIN)
            cur = self.db.execute(
                "INSERT INTO checkins (kupuna_id, day, status, next_at) VALUES (?,?,?,?)",
                (k["id"], day, "skipped" if late else "scheduled", None if late else iso(now)),
            )
            if late:
                self.log(self.checkin(cur.lastrowid), k["id"], "skipped",
                         f"Service was offline at {k['call_time']}; skipped today's call for {k['name']}")

    def _place_call(self, c: sqlite3.Row, now: datetime, force: bool = False) -> None:
        k = self.kupuna(c["kupuna_id"])
        # Quiet hours only block starting a new morning; retries of one already under way still go out.
        if self.in_quiet_hours(now) and not force and c["attempts"] == 0:
            self.update(c["id"], status="skipped", next_at=None)
            self.log(c, k["id"], "skipped", "Quiet hours; not calling")
            return
        attempt = c["attempts"] + 1
        sid = self.phone.place_checkin_call(k["phone"], c["id"])
        self.update(c["id"], status="calling", attempts=attempt, call_sid=sid, last_call_at=iso(now),
                    next_at=iso(now + timedelta(minutes=self.cfg.NO_RESULT_TIMEOUT_MIN)))
        self.log(c, k["id"], "call", f"Calling {k['name']} ({k['phone']}) · try {attempt} of {self.cfg.MAX_TRIES}")

    def _miss(self, c: sqlite3.Row, now: datetime, why: str) -> None:
        k = self.kupuna(c["kupuna_id"])
        self.log(c, k["id"], "miss", f"No check-in from {k['name']} on try {c['attempts']} ({why})")
        if c["attempts"] < self.cfg.MAX_TRIES:
            next_at = parse(c["last_call_at"]) + timedelta(minutes=self.cfg.RETRY_GAP_MIN)
            self.update(c["id"], status="waiting_retry", next_at=iso(max(next_at, now)))
        else:
            self._alert_family(self.checkin(c["id"]), now)

    def _call_times(self, c: sqlite3.Row) -> str:
        rows = self.db.execute(
            "SELECT at FROM events WHERE checkin_id=? AND kind='call' ORDER BY at", (c["id"],)
        ).fetchall()
        times = [self.fmt(r["at"]) for r in rows]
        return ", ".join(times[:-1]) + (" and " if len(times) > 1 else "") + times[-1] if times else ""

    def _alert_family(self, c: sqlite3.Row, now: datetime) -> None:
        k = self.kupuna(c["kupuna_id"])
        n = greetings.short_name(k["name"])
        deadline = now + timedelta(minutes=self.cfg.FAMILY_REPLY_WINDOW_MIN)
        sms = (f"{self.cfg.SERVICE_NAME}: {n} didn't answer this morning's calls at {self._call_times(c)}. "
               f"Please check on them. Reply OK once you've reached them. If we don't hear back by "
               f"{self.fmt(deadline)}, we'll contact {k['contact2_name']}. This service does not call 911.")
        voice = (f"This is {self.cfg.SERVICE_NAME}. {n} did not answer {c['attempts']} check-in calls this morning. "
                 f"Please check on them, then reply OK to our text message.")
        self.phone.send_sms(k["contact1_phone"], sms)
        self.phone.alert_call(k["contact1_phone"], voice)
        self.update(c["id"], status="alerted", alerted_at=iso(now), next_at=iso(deadline))
        self.log(c, k["id"], "alert", f"{c['attempts']} missed calls. Texted and called {k['contact1_name']}.")

    def _alert_backup(self, c: sqlite3.Row, now: datetime) -> None:
        k = self.kupuna(c["kupuna_id"])
        n = greetings.short_name(k["name"])
        sms = (f"{self.cfg.SERVICE_NAME}: {n} didn't answer this morning's check-in calls and we couldn't reach "
               f"{k['contact1_name']}. Please check on them if you can. Reply OK once you've reached them. "
               f"This service does not call 911.")
        voice = (f"This is {self.cfg.SERVICE_NAME}. {n} missed this morning's check-in calls and we could not reach "
                 f"{k['contact1_name']}. Please check on them, then reply OK to our text message.")
        self.phone.send_sms(k["contact2_phone"], sms)
        self.phone.alert_call(k["contact2_phone"], voice)
        self.update(c["id"], status="backup_alerted", backup_at=iso(now), next_at=None)
        self.log(c, k["id"], "alert",
                 f"No reply from {k['contact1_name']} in {self.cfg.FAMILY_REPLY_WINDOW_MIN} min. "
                 f"Texted and called backup {k['contact2_name']}.")

    # ---------- things Twilio tells us ----------
    def keypress(self, checkin_id: int, digit: str) -> str:
        """Returns 'ok', 'help' or 'unknown' so the call can say the right thing."""
        c = self.checkin(checkin_id)
        if not c:
            return "unknown"
        k = self.kupuna(c["kupuna_id"])
        now = self.clock()
        if digit == "1":
            if c["status"] in OPEN:
                self.update(c["id"], status="ok", next_at=None, resolved_by="pressed 1", resolved_at=iso(now))
                self.log(c, k["id"], "ok", f"{k['name']} pressed 1 on try {c['attempts']}. Checked in.")
            elif c["status"] in ("alerted", "backup_alerted"):
                # They called back / picked up a late retry after family was already alerted
                self.update(c["id"], status="safe", next_at=None, resolved_by="pressed 1 (late)", resolved_at=iso(now))
                self.log(c, k["id"], "ok", f"{k['name']} pressed 1 after family was alerted. Marked safe.")
                n = greetings.short_name(k["name"])
                self.phone.send_sms(k["contact1_phone"], f"{self.cfg.SERVICE_NAME} update: {n} just pressed 1. They're checked in.")
            return "ok"
        if digit == "2":
            n = greetings.short_name(k["name"])
            msg = (f"{self.cfg.SERVICE_NAME}: {n} pressed 2 on their check-in call, which means they asked for help. "
                   f"Please contact them now. Reply OK once you've reached them. If it's an emergency, call 911.")
            voice = (f"This is {self.cfg.SERVICE_NAME}. {n} asked for help on their check-in call. "
                     f"Please contact them right away, then reply OK to our text message.")
            for who in ("contact1", "contact2"):
                self.phone.send_sms(k[f"{who}_phone"], msg)
                self.phone.alert_call(k[f"{who}_phone"], voice)
            self.update(c["id"], status="help", next_at=None, alerted_at=iso(now))
            self.log(c, k["id"], "alert", f"{k['name']} pressed 2 (asked for help). Texted and called both contacts.")
            return "help"
        return "unknown"

    def call_finished(self, checkin_id: int, call_sid: str, call_status: str) -> None:
        """Twilio's status callback. Any finished call that didn't end in a 1 or 2 is a miss."""
        c = self.checkin(checkin_id)
        if not c or c["status"] != "calling" or c["call_sid"] != call_sid:
            return  # already handled (they pressed a key) or an old call
        reasons = {"no-answer": "no answer", "busy": "line busy", "failed": "call failed",
                   "canceled": "call canceled", "completed": "answered but no key pressed (maybe voicemail)"}
        self._miss(c, self.clock(), reasons.get(call_status, call_status))

    def family_reply(self, from_phone: str, body: str) -> str:
        """Handle a text from a family contact. Returns the reply to send back."""
        try:
            sender = normalize_phone(from_phone)
        except ValueError:
            return ""
        word = body.strip().split()[0].upper().strip(".!") if body.strip() else ""
        rows = self.db.execute(
            f"""SELECT c.*, k.name AS kname, k.contact1_name, k.contact1_phone, k.contact2_name, k.contact2_phone
                FROM checkins c JOIN kupuna k ON k.id=c.kupuna_id
                WHERE c.status IN ({",".join("?" * len(NEEDS_FAMILY))})
                  AND (k.contact1_phone=? OR k.contact2_phone=?)
                ORDER BY c.id DESC""",
            (*NEEDS_FAMILY, sender, sender),
        ).fetchall()
        if word not in ("OK", "OKAY", "YES", "SAFE"):
            if rows:
                return f"{self.cfg.SERVICE_NAME}: reply OK once you've reached {greetings.short_name(rows[0]['kname'])}."
            return f"{self.cfg.SERVICE_NAME}: nothing needs your reply right now. Mahalo!"
        if not rows:
            return f"{self.cfg.SERVICE_NAME}: thanks. Nothing is open right now."
        now = self.clock()
        names = []
        for r in rows:
            who = r["contact1_name"] if r["contact1_phone"] == sender else r["contact2_name"]
            self.update(r["id"], status="safe", next_at=None, resolved_by=who, resolved_at=iso(now))
            self.log(r, r["kupuna_id"], "ok", f"{who} replied OK. {r['kname']} marked safe.")
            names.append(greetings.short_name(r["kname"]))
        return f"{self.cfg.SERVICE_NAME}: mahalo. {', '.join(names)} marked safe for today."

    # ---------- weekly text ----------
    def _weekly_summaries(self, now: datetime) -> None:
        local_now = self.local(now)
        if local_now.weekday() != self.cfg.WEEKLY_SUMMARY_WEEKDAY or local_now.hour < self.cfg.WEEKLY_SUMMARY_HOUR:
            return
        week_end = local_now.date()
        week_start = week_end - timedelta(days=6)
        for k in self.db.execute("SELECT * FROM kupuna WHERE active=1").fetchall():
            sent = self.db.execute("SELECT 1 FROM summaries WHERE kupuna_id=? AND week_ending=?",
                                   (k["id"], week_end.isoformat())).fetchone()
            if sent:
                continue
            body = self.weekly_text(k, week_start, week_end)
            if body:
                self.phone.send_sms(k["contact1_phone"], body)
                self.log(None, k["id"], "summary", f"Sent weekly summary to {k['contact1_name']}")
            self.db.execute("INSERT INTO summaries VALUES (?,?,?)", (k["id"], week_end.isoformat(), iso(now)))

    def weekly_text(self, k: sqlite3.Row, start, end) -> str | None:
        rows = self.db.execute(
            "SELECT * FROM checkins WHERE kupuna_id=? AND day BETWEEN ? AND ? ORDER BY day",
            (k["id"], start.isoformat(), end.isoformat()),
        ).fetchall()
        rows = [r for r in rows if r["status"] != "skipped"]
        if not rows:
            return None
        n = greetings.short_name(k["name"])
        on_own = sum(r["status"] == "ok" for r in rows)
        parts = [f"{self.cfg.SERVICE_NAME} weekly: {n} checked in on their own {on_own} of {len(rows)} days."]
        for r in rows:
            if r["status"] in ("safe", "help", "alerted", "backup_alerted"):
                day = datetime.fromisoformat(r["day"]).strftime("%A")
                if r["status"] == "help":
                    parts.append(f"{day} they pressed 2 to ask for help.")
                elif r["status"] == "safe":
                    parts.append(f"{day} they missed the calls; {r['resolved_by']} confirmed they were okay.")
                else:
                    parts.append(f"{day} they missed the calls and nobody replied OK.")
        return " ".join(parts)
