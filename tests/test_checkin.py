"""Run with:  python -m unittest discover tests -v
No Twilio account or network needed: calls and texts go to a fake phone.
"""
import base64
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update(DATABASE_PATH=":memory:", DISABLE_SCHEDULER="1", DRY_RUN="1", ADMIN_PASSWORD="pw")

import db  # noqa: E402
import phone as ph  # noqa: E402
from config import Config  # noqa: E402
from engine import Engine, normalize_phone  # noqa: E402

HST = Config.TIMEZONE


class Clock:
    def __init__(self, h, m, day=6):  # Tue 6 Oct 2026, Hawaii time
        self.now = datetime(2026, 10, day, h, m, tzinfo=HST).astimezone(timezone.utc)

    def __call__(self):
        return self.now

    def go(self, minutes):
        self.now += timedelta(minutes=minutes)


def make(h=7, m=59, call_time="08:00", language="English", day=6):
    clock = Clock(h, m, day)
    phone = ph.DryRunPhone(quiet=True)
    e = Engine(db.connect(":memory:"), phone, Config, clock)
    kid = e.add_kupuna(name="Auntie Leilani Kekona", phone="808-555-0142", call_time=call_time, language=language,
                       contact1_name="Kainoa", contact1_phone="(808) 555-0110",
                       contact2_name="Mele", contact2_phone="808.555.0133",
                       consent_note="Signed form, 10/1/2026")
    return e, phone, clock, kid


def today(e, kid):
    return e.db.execute("SELECT * FROM checkins WHERE kupuna_id=?", (kid,)).fetchone()


def miss(e):
    c = e.db.execute("SELECT * FROM checkins").fetchone()
    e.call_finished(c["id"], c["call_sid"], "no-answer")


class Scheduling(unittest.TestCase):
    def test_no_call_before_call_time(self):
        e, phone, clock, kid = make()
        e.tick()
        self.assertIsNone(today(e, kid))
        self.assertEqual(phone.sent, [])

    def test_first_call_at_call_time(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        self.assertEqual(today(e, kid)["status"], "calling")
        self.assertEqual(phone.sent, [("call", "+18085550142", f"check-in call #{today(e, kid)['id']}")])

    def test_only_one_morning_per_day(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick(); e.keypress(today(e, kid)["id"], "1")
        for _ in range(30):
            clock.go(1); e.tick()
        self.assertEqual(len(phone.sent), 1)

    def test_next_day_calls_again(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick(); e.keypress(today(e, kid)["id"], "1")
        clock.go(24 * 60); e.tick()
        self.assertEqual(len(e.db.execute("SELECT * FROM checkins").fetchall()), 2)

    def test_server_started_hours_late_skips_instead_of_calling_at_noon(self):
        e, phone, clock, kid = make(h=12, m=0)
        e.tick()
        self.assertEqual(today(e, kid)["status"], "skipped")
        self.assertEqual(phone.sent, [])

    def test_paused_kupuna_not_called(self):
        e, phone, clock, kid = make()
        e.set_active(kid, False)
        clock.go(1); e.tick()
        self.assertEqual(phone.sent, [])


class Mornings(unittest.TestCase):
    def test_answers_first_call(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        self.assertEqual(e.keypress(today(e, kid)["id"], "1"), "ok")
        self.assertEqual(today(e, kid)["status"], "ok")
        e.call_finished(today(e, kid)["id"], today(e, kid)["call_sid"], "completed")  # hang-up after pressing 1
        self.assertEqual(today(e, kid)["status"], "ok")

    def test_answers_second_try_after_five_minutes(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick(); miss(e)
        self.assertEqual(today(e, kid)["status"], "waiting_retry")
        clock.go(4); e.tick()
        self.assertEqual(len(phone.sent), 1, "no retry before 5 minutes")
        clock.go(1); e.tick()
        self.assertEqual(today(e, kid)["attempts"], 2)
        e.keypress(today(e, kid)["id"], "1")
        self.assertEqual(today(e, kid)["status"], "ok")

    def test_voicemail_counts_as_miss(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        c = today(e, kid)
        e.call_finished(c["id"], c["call_sid"], "completed")  # answered by machine, no key
        self.assertEqual(today(e, kid)["status"], "waiting_retry")

    def test_twilio_never_reports_back_still_counts_as_miss(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        clock.go(Config.NO_RESULT_TIMEOUT_MIN); e.tick()
        self.assertEqual(today(e, kid)["status"], "waiting_retry")

    def test_three_misses_alert_family_by_text_and_call(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        for _ in range(2):
            miss(e); clock.go(5); e.tick()
        miss(e)
        self.assertEqual(today(e, kid)["status"], "alerted")
        kinds = [(k, to) for k, to, _ in phone.sent]
        self.assertEqual(kinds.count(("call", "+18085550142")), 3)
        self.assertIn(("sms", "+18085550110"), kinds)
        self.assertIn(("alert", "+18085550110"), kinds)
        text = next(c for k, to, c in phone.sent if k == "sms")
        self.assertIn("8:00 AM, 8:05 AM and 8:10 AM", text)
        self.assertIn("Reply OK", text)
        self.assertIn("does not call 911", text)

    def test_family_ok_reply_marks_safe_and_stops_backup(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        for _ in range(2):
            miss(e); clock.go(5); e.tick()
        miss(e)
        reply = e.family_reply("+1 808 555 0110", "ok she was in the garden")
        self.assertIn("marked safe", reply)
        self.assertEqual(today(e, kid)["status"], "safe")
        self.assertEqual(today(e, kid)["resolved_by"], "Kainoa")
        clock.go(30); e.tick()
        self.assertNotIn("+18085550133", [to for _, to, _ in phone.sent])

    def test_no_family_reply_escalates_to_backup_after_15_min(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        for _ in range(2):
            miss(e); clock.go(5); e.tick()
        miss(e)
        clock.go(14); e.tick()
        self.assertEqual(today(e, kid)["status"], "alerted")
        clock.go(1); e.tick()
        self.assertEqual(today(e, kid)["status"], "backup_alerted")
        self.assertIn(("sms", "+18085550133"), [(k, to) for k, to, _ in phone.sent])
        self.assertIn("marked safe", e.family_reply("8085550133", "OK"))
        self.assertEqual(today(e, kid)["resolved_by"], "Mele")

    def test_press_2_alerts_both_contacts_at_once(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        self.assertEqual(e.keypress(today(e, kid)["id"], "2"), "help")
        self.assertEqual(today(e, kid)["status"], "help")
        to = [(k, t) for k, t, _ in phone.sent]
        for n in ("+18085550110", "+18085550133"):
            self.assertIn(("sms", n), to)
            self.assertIn(("alert", n), to)
        clock.go(60); e.tick()
        self.assertEqual(today(e, kid)["attempts"], 1, "no more check-in calls after asking for help")

    def test_other_key_is_ignored(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        self.assertEqual(e.keypress(today(e, kid)["id"], "7"), "unknown")
        self.assertEqual(today(e, kid)["status"], "calling")

    def test_stale_status_callback_from_old_call_is_ignored(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick(); old = today(e, kid)["call_sid"]
        miss(e); clock.go(5); e.tick()
        e.call_finished(today(e, kid)["id"], old, "no-answer")
        self.assertEqual(today(e, kid)["status"], "calling")

    def test_random_text_gets_a_helpful_reply_and_changes_nothing(self):
        e, phone, clock, kid = make()
        self.assertIn("nothing needs your reply", e.family_reply("8085550110", "hello?"))
        self.assertIn("nothing is open", e.family_reply("8085550110", "OK").lower())

    def test_stranger_cannot_mark_someone_safe(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        for _ in range(2):
            miss(e); clock.go(5); e.tick()
        miss(e)
        e.family_reply("8085559999", "OK")
        self.assertEqual(today(e, kid)["status"], "alerted")


class QuietHoursAndTesting(unittest.TestCase):
    def test_call_time_in_quiet_hours_rejected(self):
        e, phone, clock, kid = make()
        with self.assertRaises(ValueError):
            e.add_kupuna(name="X", phone="8085550000", call_time="06:30", language="English",
                         contact1_name="a", contact1_phone="8085550001", contact2_name="b",
                         contact2_phone="8085550002", consent_note="yes")

    def test_consent_required(self):
        e, phone, clock, kid = make()
        with self.assertRaises(ValueError):
            e.add_kupuna(name="X", phone="8085550000", call_time="09:00", language="English",
                         contact1_name="a", contact1_phone="8085550001", contact2_name="b",
                         contact2_phone="8085550002", consent_note=" ")

    def test_call_now_works_at_night_and_retries_continue(self):
        e, phone, clock, kid = make(h=22, m=0)
        e.call_now(kid)
        miss(e); clock.go(5); e.tick()
        self.assertEqual(today(e, kid)["attempts"], 2)

    def test_call_now_restarts_a_finished_morning(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick(); e.keypress(today(e, kid)["id"], "1")
        e.call_now(kid)
        self.assertEqual(today(e, kid)["status"], "calling")
        self.assertEqual(today(e, kid)["attempts"], 1)


class WeeklySummary(unittest.TestCase):
    def test_sunday_evening_summary_sent_once(self):
        e, phone, clock, kid = make(day=5)  # Monday 5 Oct
        at = lambda d, h, m: datetime(2026, 10, d, h, m, tzinfo=HST).astimezone(timezone.utc)
        for d in range(5, 12):  # Mon 5 .. Sun 11
            clock.now = at(d, 8, 0); e.tick()
            c = today_for(e, kid, clock)
            if d == 8:  # Thursday: misses all three, family confirms
                for _ in range(2):
                    e.call_finished(c["id"], today_for(e, kid, clock)["call_sid"], "no-answer"); clock.go(5); e.tick()
                e.call_finished(c["id"], today_for(e, kid, clock)["call_sid"], "no-answer")
                e.family_reply("8085550110", "OK")
            else:
                e.keypress(c["id"], "1")
        clock.now = at(11, 17, 59); e.tick()
        self.assertFalse([c for k, _, c in phone.sent if "weekly" in c], "not before 6 PM")
        clock.now = at(11, 18, 0); e.tick(); e.tick()
        summaries = [c for k, to, c in phone.sent if k == "sms" and "weekly" in c]
        self.assertEqual(len(summaries), 1)
        self.assertIn("6 of 7 days", summaries[0])
        self.assertIn("Thursday", summaries[0])
        self.assertIn("Kainoa confirmed", summaries[0])


def today_for(e, kid, clock):
    day = e.local(clock()).date().isoformat()
    return e.db.execute("SELECT * FROM checkins WHERE kupuna_id=? AND day=?", (kid, day)).fetchone()


class Phones(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_phone("(808) 555-0142"), "+18085550142")
        self.assertEqual(normalize_phone("1-808-555-0142"), "+18085550142")
        self.assertEqual(normalize_phone("+81 90 1234 5678"), "+819012345678")
        with self.assertRaises(ValueError):
            normalize_phone("555-0142")


class Webhooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.m = app_module
        cls.client = app_module.app.test_client()

    def setUp(self):
        self.m.engine.db = db.connect(":memory:")
        self.m.engine.phone = ph.DryRunPhone(quiet=True)
        self.clock = Clock(7, 59)
        self.m.engine.clock = self.clock
        self.kid = self.m.engine.add_kupuna(
            name="Mr. Hiroshi Tanaka", phone="8085550187", call_time="08:00", language="Japanese",
            contact1_name="Amy", contact1_phone="8085550110", contact2_name="Rev. Ito",
            contact2_phone="8085550133", consent_note="Signed form")
        self.clock.go(1); self.m.engine.tick()
        self.cid = today(self.m.engine, self.kid)["id"]

    def test_call_script_is_japanese_and_gathers_one_digit(self):
        r = self.client.post(f"/voice/checkin/{self.cid}")
        body = r.get_data(as_text=True)
        self.assertEqual(r.mimetype, "text/xml")
        self.assertIn('numDigits="1"', body)
        self.assertIn("Polly.Mizuki", body)
        self.assertIn("Mr. Hiroshi、おはようございます", body)
        self.assertIn(f"/voice/gather/{self.cid}", body)

    def test_pressing_1_over_the_phone(self):
        r = self.client.post(f"/voice/gather/{self.cid}", data={"Digits": "1"})
        self.assertIn("ありがとうございます", r.get_data(as_text=True))
        self.assertEqual(today(self.m.engine, self.kid)["status"], "ok")

    def test_status_callback_registers_miss(self):
        sid = today(self.m.engine, self.kid)["call_sid"]
        r = self.client.post(f"/voice/status/{self.cid}", data={"CallSid": sid, "CallStatus": "busy"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(today(self.m.engine, self.kid)["status"], "waiting_retry")

    def test_sms_reply(self):
        e = self.m.engine
        for _ in range(2):
            miss(e); self.clock.go(5); e.tick()
        miss(e)
        r = self.client.post("/sms", data={"From": "+18085550110", "Body": "OK"})
        self.assertIn("<Message>", r.get_data(as_text=True))
        self.assertEqual(today(e, self.kid)["status"], "safe")

    def test_unsigned_webhook_rejected_when_live(self):
        cfg = self.m.cfg
        old = (cfg.DRY_RUN, cfg.TWILIO_AUTH_TOKEN, cfg.PUBLIC_BASE_URL)
        cfg.DRY_RUN, cfg.TWILIO_AUTH_TOKEN, cfg.PUBLIC_BASE_URL = False, "secret", "https://example.ngrok.app"
        try:
            r = self.client.post(f"/voice/gather/{self.cid}", data={"Digits": "1"})
            self.assertEqual(r.status_code, 403)
            self.assertEqual(today(self.m.engine, self.kid)["status"], "calling")
            url = f"https://example.ngrok.app/voice/gather/{self.cid}"
            sig = ph.twilio_signature("secret", url, {"Digits": "1"})
            r = self.client.post(f"/voice/gather/{self.cid}", data={"Digits": "1"}, headers={"X-Twilio-Signature": sig})
            self.assertEqual(r.status_code, 200)
        finally:
            cfg.DRY_RUN, cfg.TWILIO_AUTH_TOKEN, cfg.PUBLIC_BASE_URL = old

    def test_signature_matches_twilio_documented_example(self):
        # Example from Twilio's webhook security docs
        params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234",
                  "From": "+14158675310", "To": "+18005551212"}
        sig = ph.twilio_signature("12345", "https://example.com/myapp.php?foo=1&bar=2", params)
        self.assertEqual(sig, "L/OH5YylLD5NRKLltdqwSvS0BnU=")

    def test_admin_needs_password(self):
        self.assertEqual(self.client.get("/admin").status_code, 401)
        auth = {"Authorization": "Basic " + base64.b64encode(b"x:pw").decode()}
        r = self.client.get("/admin", headers=auth)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Mr. Hiroshi Tanaka", r.get_data(as_text=True))
        self.assertIn("Calling", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
