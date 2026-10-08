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
os.environ.update(DATABASE_PATH=":memory:", DISABLE_SCHEDULER="1", DRY_RUN="1", ADMIN_PASSWORD="pw",
                  OWNER_PHONE="808-555-0999", PAYMENT_LINK_BASIC="https://buy.stripe.com/test_basic")

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


class Editing(unittest.TestCase):
    def test_update_changes_only_what_changed_and_logs_it(self):
        e, phone, clock, kid = make()
        k = dict(e.kupuna(kid))
        k.update(call_time="9:30", contact1_name="Kainoa K.", contact1_phone="808-555-0199")
        e.update_kupuna(kid, **k)
        k2 = e.kupuna(kid)
        self.assertEqual(k2["call_time"], "09:30")
        self.assertEqual(k2["contact1_phone"], "+18085550199")
        self.assertEqual(k2["consent_at"], e.kupuna(kid)["consent_at"])
        last = e.db.execute("SELECT detail FROM events ORDER BY id DESC LIMIT 1").fetchone()["detail"]
        self.assertIn("call time", last); self.assertIn("family contact", last)

    def test_update_validates(self):
        e, phone, clock, kid = make()
        k = dict(e.kupuna(kid)); k["call_time"] = "05:00"
        with self.assertRaises(ValueError):
            e.update_kupuna(kid, **k)
        k = dict(e.kupuna(kid)); k["contact2_name"] = " "
        with self.assertRaises(ValueError):
            e.update_kupuna(kid, **k)

    def test_new_call_time_used_next_morning(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick(); e.keypress(today(e, kid)["id"], "1")
        k = dict(e.kupuna(kid)); k["call_time"] = "10:00"
        e.update_kupuna(kid, **k)
        clock.go(24 * 60); e.tick()
        self.assertEqual(len(e.db.execute("SELECT * FROM checkins").fetchall()), 1, "not at 8 the next day")
        clock.go(120); e.tick()
        self.assertEqual(len(e.db.execute("SELECT * FROM checkins").fetchall()), 2)


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

    def test_edit_page_and_save(self):
        auth = {"Authorization": "Basic " + base64.b64encode(b"x:pw").decode()}
        r = self.client.get(f"/admin/edit/{self.kid}", headers=auth)
        body = r.get_data(as_text=True)
        self.assertIn("Edit Mr. Hiroshi Tanaka", body)
        self.assertIn("value='+18085550110'", body)
        self.assertIn("<option selected>Japanese</option>", body)
        data = {k: str(self.m.engine.kupuna(self.kid)[k]) for k in self.m.engine.FIELDS}
        data["contact2_name"] = "Pastor Ito"
        r = self.client.post(f"/admin/edit/{self.kid}", data=data, headers=auth)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.m.engine.kupuna(self.kid)["contact2_name"], "Pastor Ito")
        self.assertIn("/admin/edit/", self.client.get("/admin", headers=auth).get_data(as_text=True))

    def test_admin_needs_password(self):
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login", r.headers["Location"])
        auth = {"Authorization": "Basic " + base64.b64encode(b"x:pw").decode()}
        r = self.client.get("/admin", headers=auth)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Mr. Hiroshi Tanaka", r.get_data(as_text=True))
        self.assertIn("Calling", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()


GOOD_SIGNUP = dict(plan="basic", family_name="Kainoa Kekona", family_phone="425-555-0110", family_email="kainoa@example.com",
                   relationship="Son", kupuna_name="Auntie Leilani", kupuna_phone="808-555-0142", call_time="08:30",
                   language="Pidgin", backup_name="Mele", backup_phone="808-555-0133", notes="Hard of hearing",
                   agree_consent="on", agree_sms="on", agree_911="on")


class SignupFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        import signup
        cls.m, cls.signup = app_module, signup
        cls.client = app_module.app.test_client()
        cls.auth = {"Authorization": "Basic " + base64.b64encode(b"x:pw").decode()}

    def setUp(self):
        self.m.engine.db = db.connect(":memory:")
        self.m.engine.phone = ph.DryRunPhone(quiet=True)
        self.m.engine.clock = Clock(10, 0)
        self.signup._recent.clear()

    def post(self, **over):
        d = dict(GOOD_SIGNUP); d.update(over)
        return self.client.post("/signup", data={k: v for k, v in d.items() if v is not None})

    def test_home_is_public_signup_page(self):
        r = self.client.get("/", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("A friendly call every morning", body)
        self.assertIn("$12/month", body)

    def test_signup_saved_owner_texted_no_calls_yet(self):
        r = self.post()
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("You're signed up", body)
        self.assertIn("https://buy.stripe.com/test_basic", body)
        s = self.m.engine.db.execute("SELECT * FROM signups").fetchone()
        self.assertEqual((s["status"], s["kupuna_phone"], s["family_phone"]), ("new", "+18085550142", "+14255550110"))
        self.assertEqual(self.m.engine.phone.sent[0][:2], ("sms", "+18085550999"))
        self.assertEqual(self.m.engine.db.execute("SELECT COUNT(*) FROM kupuna").fetchone()[0], 0)
        self.m.engine.clock.go(60); self.m.engine.tick()
        self.assertEqual([k for k, *_ in self.m.engine.phone.sent], ["sms"], "nobody called before approval")

    def test_missing_info_shows_error_and_keeps_answers(self):
        r = self.post(backup_phone="")
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn("backup contact&#x27;s phone", body)
        self.assertIn("value=\"Kainoa Kekona\"", body)

    def test_boxes_must_be_ticked(self):
        self.assertEqual(self.post(agree_911=None).status_code, 400)

    def test_bad_time_and_email(self):
        self.assertIn("between 7 AM and 9 PM", self.post(call_time="06:00").get_data(as_text=True))
        self.assertIn("email address", self.post(family_email="nope").get_data(as_text=True))

    def test_bot_honeypot_saves_nothing(self):
        self.post(website="http://spam")
        self.assertEqual(self.m.engine.db.execute("SELECT COUNT(*) FROM signups").fetchone()[0], 0)

    def test_rate_limit(self):
        for _ in range(5):
            self.post()
        self.assertEqual(self.post().status_code, 429)

    def test_html_is_escaped(self):
        self.post(kupuna_name="<script>x</script>")
        r = self.client.get("/admin", headers=self.auth)
        self.assertNotIn("<script>x</script>", r.get_data(as_text=True))

    def test_review_approve_starts_calls(self):
        self.post()
        sid = self.m.engine.db.execute("SELECT id FROM signups").fetchone()[0]
        self.assertIn("New sign-ups (1)", self.client.get("/admin", headers=self.auth).get_data(as_text=True))
        page = self.client.get(f"/admin/signup/{sid}", headers=self.auth).get_data(as_text=True)
        self.assertIn("value='+18085550142'", page)
        self.assertIn("Approve and start calls", page)
        fields = {"name": "Auntie Leilani", "phone": "+18085550142", "call_time": "08:30", "language": "Pidgin",
                  "contact1_name": "Kainoa Kekona", "contact1_phone": "+14255550110", "contact2_name": "Mele",
                  "contact2_phone": "+18085550133", "consent_note": ""}
        r = self.client.post(f"/admin/signup/{sid}/approve", data=fields, headers=self.auth)
        self.assertIn("consent", r.headers["Location"].lower(), "consent note required")
        fields["consent_note"] = "Called Auntie 10/7, she agreed"
        self.client.post(f"/admin/signup/{sid}/approve", data=fields, headers=self.auth)
        s = self.m.engine.signup(sid)
        self.assertEqual(s["status"], "approved")
        self.assertEqual(self.m.engine.kupuna(s["kupuna_id"])["contact1_phone"], "+14255550110")
        self.assertNotIn("New sign-ups", self.client.get("/admin", headers=self.auth).get_data(as_text=True))

    def test_decline(self):
        self.post()
        sid = self.m.engine.db.execute("SELECT id FROM signups").fetchone()[0]
        self.client.post(f"/admin/signup/{sid}/decline", headers=self.auth)
        self.assertEqual(self.m.engine.signup(sid)["status"], "declined")
        self.assertEqual(self.m.engine.db.execute("SELECT COUNT(*) FROM kupuna").fetchone()[0], 0)

    def test_talk_story_hidden_by_default(self):
        body = self.client.get("/signup").get_data(as_text=True)
        self.assertNotIn("Talk Story", body)
        self.assertNotIn("$39", body)
        self.assertIn("Daily Check-In", body)
        r = self.post(plan="talk_story")
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn&#x27;t available yet", r.get_data(as_text=True))
        self.assertEqual(self.m.engine.db.execute("SELECT COUNT(*) FROM signups").fetchone()[0], 0)
        self.assertEqual(self.post().status_code, 200, "basic plan still works")

    def test_talk_story_shown_when_turned_on(self):
        cfg = self.m.engine.cfg
        old = cfg.OFFER_TALK_STORY
        cfg.OFFER_TALK_STORY = True
        try:
            body = self.client.get("/signup").get_data(as_text=True)
            self.assertIn("Talk Story", body)
            self.assertIn("$39/month", body)
            self.assertEqual(self.post(plan="talk_story").status_code, 200)
            self.assertEqual(self.m.engine.db.execute("SELECT plan FROM signups").fetchone()[0], "talk_story")
        finally:
            cfg.OFFER_TALK_STORY = old

    def test_review_page_needs_password(self):
        r = self.client.get("/admin/signup/1")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login", r.headers["Location"])


class Removing(unittest.TestCase):
    def test_remove_stops_retries_and_alerts_midway(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick(); miss(e)                      # first try missed, retry pending
        e.remove_kupuna(kid)
        before = len(phone.sent)
        for _ in range(60):
            clock.go(1); e.tick()
        self.assertEqual(len(phone.sent), before, "no calls or alerts after removal")
        self.assertEqual(today(e, kid)["status"], "skipped")
        clock.go(24 * 60); e.tick()
        self.assertEqual(len(phone.sent), before, "not called the next day either")

    def test_remove_while_family_alerted_stops_backup(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        for _ in range(2):
            miss(e); clock.go(5); e.tick()
        miss(e)
        e.remove_kupuna(kid)
        clock.go(30); e.tick()
        self.assertNotIn("+18085550133", [to for _, to, _ in phone.sent])

    def test_history_kept_and_restore(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick(); e.keypress(today(e, kid)["id"], "1")
        e.remove_kupuna(kid)
        self.assertIsNotNone(e.kupuna(kid)["removed_at"])
        self.assertEqual(len(e.db.execute("SELECT * FROM checkins WHERE kupuna_id=?", (kid,)).fetchall()), 1)
        e.restore_kupuna(kid)
        self.assertIsNone(e.kupuna(kid)["removed_at"])
        clock.go(24 * 60); e.tick()
        self.assertEqual(len(e.db.execute("SELECT * FROM checkins WHERE kupuna_id=?", (kid,)).fetchall()), 2)

    def test_old_database_upgrades_without_losing_people(self):
        import sqlite3, tempfile
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.executescript(db.SCHEMA.replace(",\n    removed_at      TEXT                    -- set when taken off the list; history is kept", ""))
        old.execute("INSERT INTO kupuna (name, phone, call_time, language, contact1_name, contact1_phone, contact2_name,"
                    " contact2_phone, consent_note, consent_at, created_at) VALUES ('Sarah','+18085550100','09:00',"
                    "'English','A','+18085550101','B','+18085550102','self','x','x')")
        old.commit()
        self.assertNotIn("removed_at", [r[1] for r in old.execute("PRAGMA table_info(kupuna)")])
        old.close()
        conn = db.connect(path)
        row = conn.execute("SELECT * FROM kupuna").fetchone()
        self.assertEqual(row["name"], "Sarah")
        self.assertIsNone(row["removed_at"])
        db.connect(path)  # running the upgrade twice is harmless

    def test_admin_remove_confirm_and_restore(self):
        import app as m
        m.engine.db = db.connect(":memory:"); m.engine.phone = ph.DryRunPhone(quiet=True); m.engine.clock = Clock(10, 0)
        kid = m.engine.add_kupuna(name="Test Person", phone="8085550100", call_time="09:00", language="English",
                                  contact1_name="A", contact1_phone="8085550101", contact2_name="B",
                                  contact2_phone="8085550102", consent_note="self")
        c = m.app.test_client(); auth = {"Authorization": "Basic " + base64.b64encode(b"x:pw").decode()}
        page = c.get("/admin", headers=auth).get_data(as_text=True)
        self.assertIn(f"/admin/remove/{kid}", page)
        self.assertIn("Yes, remove Test Person", c.get(f"/admin/remove/{kid}", headers=auth).get_data(as_text=True))
        self.assertIsNone(m.engine.kupuna(kid)["removed_at"], "viewing the confirm page removes nothing")
        c.post(f"/admin/remove/{kid}", headers=auth)
        page = c.get("/admin", headers=auth).get_data(as_text=True)
        self.assertNotIn(f"/admin/edit/{kid}", page)
        self.assertIn("Restore", page)
        c.post(f"/admin/restore/{kid}", headers=auth)
        self.assertIn(f"/admin/edit/{kid}", c.get("/admin", headers=auth).get_data(as_text=True))


class PickyPhone(ph.DryRunPhone):
    """Like Twilio: refuses some numbers."""
    def __init__(self, bad_sms=(), bad_calls=(), bad_alerts=()):
        super().__init__(quiet=True)
        self.bad_sms, self.bad_calls, self.bad_alerts = set(bad_sms), set(bad_calls), set(bad_alerts)

    def send_sms(self, to, body):
        if to in self.bad_sms:
            raise ph.TwilioError(f"Twilio refused the message to {to}: Invalid 'To' Phone Number")
        return super().send_sms(to, body)

    def alert_call(self, to, message):
        if to in self.bad_alerts:
            raise ph.TwilioError(f"Twilio refused the call to {to}")
        return super().alert_call(to, message)

    def place_checkin_call(self, to, checkin_id):
        if to in self.bad_calls:
            raise ph.TwilioError(f"Twilio refused the call to {to}")
        return super().place_checkin_call(to, checkin_id)


class FailureHandling(unittest.TestCase):
    def three_misses(self, e, clock):
        clock.go(1); e.tick()
        for _ in range(2):
            miss(e); clock.go(5); e.tick()
        miss(e)

    def test_impossible_numbers_rejected_at_signup(self):
        for bad in ("808-000-0000", "808-155-1234", "008-555-1234", "(108) 555-1234"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_phone(bad)
        self.assertEqual(normalize_phone("1 (808) 555-0142"), "+18085550142")

    def test_family_text_refused_still_calls_family_and_moves_on(self):
        e, phone, clock, kid = make()
        e.phone = PickyPhone(bad_sms={"+18085550110"})
        self.three_misses(e, clock)
        self.assertEqual(today(e, kid)["status"], "alerted")
        self.assertIn(("alert", "+18085550110"), [(k, t) for k, t, _ in e.phone.sent])
        errors = [r["detail"] for r in e.db.execute("SELECT detail FROM events WHERE kind='error'")]
        self.assertTrue(any("Couldn't text Kainoa" in d for d in errors), errors)
        clock.go(15); e.tick()
        self.assertEqual(today(e, kid)["status"], "backup_alerted", "backup still reached after 15 min")

    def test_family_unreachable_goes_straight_to_backup(self):
        e, phone, clock, kid = make()
        e.phone = PickyPhone(bad_sms={"+18085550110"}, bad_alerts={"+18085550110"})
        self.three_misses(e, clock)
        self.assertEqual(today(e, kid)["status"], "backup_alerted")
        self.assertIn(("sms", "+18085550133"), [(k, t) for k, t, _ in e.phone.sent])

    def test_refused_checkin_call_counts_as_miss_and_still_alerts_family(self):
        e, phone, clock, kid = make()
        e.phone = PickyPhone(bad_calls={"+18085550142"})
        clock.go(1); e.tick()
        self.assertEqual(today(e, kid)["status"], "waiting_retry")
        for _ in range(2):
            clock.go(5); e.tick()
        self.assertEqual(today(e, kid)["status"], "alerted")
        self.assertIn(("sms", "+18085550110"), [(k, t) for k, t, _ in e.phone.sent])

    def test_one_bad_record_does_not_block_others(self):
        e, phone, clock, kid = make()
        other = e.add_kupuna(name="Uncle Walter", phone="808-555-0177", call_time="08:00", language="English",
                             contact1_name="Dana", contact1_phone="808-555-0178", contact2_name="Bobby",
                             contact2_phone="808-555-0179", consent_note="ok")
        # simulate an old bad row saved before validation existed
        e.db.execute("UPDATE kupuna SET contact1_phone='+18080000000', contact2_phone='+18080000001' WHERE id=?", (kid,))
        e.phone = PickyPhone(bad_sms={"+18080000000", "+18080000001"}, bad_alerts={"+18080000000", "+18080000001"})
        self.three_misses_for(e, clock, kid)
        # the other person's morning went normally
        self.assertIn(("call", "+18085550177"), [(k, t) for k, t, _ in e.phone.sent])
        # and nothing raised out of tick
        for _ in range(5):
            clock.go(1); e.tick()

    def three_misses_for(self, e, clock, kid):
        clock.go(1); e.tick()
        for _ in range(3):
            c = today(e, kid)
            if c["status"] == "calling":
                e.call_finished(c["id"], c["call_sid"], "no-answer")
            clock.go(5); e.tick()

    def test_removing_after_failures_works(self):
        e, phone, clock, kid = make()
        e.phone = PickyPhone(bad_sms={"+18085550110"}, bad_alerts={"+18085550110", "+18085550133"})
        self.three_misses(e, clock)
        e.remove_kupuna(kid)
        self.assertIsNotNone(e.kupuna(kid)["removed_at"])

    def test_database_waits_instead_of_failing_fast(self):
        conn = db.connect(":memory:")
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)


class SignInPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as m
        cls.m = m

    def setUp(self):
        self.m._login_tries.clear()
        self.c = self.m.app.test_client()

    def test_signed_out_visit_shows_password_form(self):
        r = self.c.get("/admin", follow_redirects=True)
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('type="password"', body)
        self.assertIn("Owner sign-in", body)

    def test_right_password_signs_in_and_returns_to_page(self):
        r = self.c.post("/admin/login", data={"password": "pw", "next": "/admin/edit/5"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/admin/edit/5"))
        r = self.c.get("/admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sign out", r.get_data(as_text=True))

    def test_wrong_password(self):
        r = self.c.post("/admin/login", data={"password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("isn&#x27;t right", r.get_data(as_text=True))
        self.assertEqual(self.c.get("/admin").status_code, 302)

    def test_next_cannot_send_you_to_another_site(self):
        r = self.c.post("/admin/login", data={"password": "pw", "next": "https://evil.example/admin"})
        self.assertTrue(r.headers["Location"].endswith("/admin"))
        r = self.c.post("/admin/login", data={"password": "pw", "next": "//evil.example"})
        self.assertTrue(r.headers["Location"].endswith("/admin"))

    def test_sign_out(self):
        self.c.post("/admin/login", data={"password": "pw"})
        self.c.post("/admin/logout")
        self.assertEqual(self.c.get("/admin").status_code, 302)

    def test_too_many_wrong_tries_locks_for_a_while(self):
        for _ in range(10):
            self.c.post("/admin/login", data={"password": "nope"})
        r = self.c.post("/admin/login", data={"password": "pw"})
        self.assertIn("Too many tries", r.get_data(as_text=True))
        self.assertEqual(self.c.get("/admin").status_code, 302)

    def test_signed_out_post_does_nothing(self):
        r = self.c.post("/admin/remove/1")
        self.assertEqual(r.status_code, 401)


class UndeliveredTexts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as m
        cls.m = m

    def setUp(self):
        self.m.engine.db = db.connect(":memory:")
        self.m.engine.phone = ph.DryRunPhone(quiet=True)
        self.m.engine.clock = Clock(13, 0)
        self.kid = self.m.engine.add_kupuna(
            name="Auntie Leilani", phone="8085550142", call_time="09:00", language="Pidgin",
            contact1_name="Kainoa", contact1_phone="8085550110", contact2_name="Mele",
            contact2_phone="8085550133", consent_note="ok")
        self.c = self.m.app.test_client()
        self.c.post("/admin/login", data={"password": "pw"})

    def errors(self):
        return [r["detail"] for r in self.m.engine.db.execute("SELECT detail FROM events WHERE kind='error'")]

    def test_blocked_family_text_is_named_and_explained(self):
        r = self.c.post("/sms/status", data={"MessageStatus": "undelivered", "To": "+18085550110", "ErrorCode": "30034"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(len(self.errors()), 1)
        self.assertIn("Kainoa (+18085550110)", self.errors()[0])
        self.assertIn("business texting", self.errors()[0])

    def test_delivered_texts_are_ignored(self):
        self.c.post("/sms/status", data={"MessageStatus": "delivered", "To": "+18085550110"})
        self.c.post("/sms/status", data={"MessageStatus": "sent", "To": "+18085550110"})
        self.assertEqual(self.errors(), [])

    def test_banner_shows_while_texts_are_blocked(self):
        self.assertNotIn("Texts are being blocked", self.c.get("/admin").get_data(as_text=True))
        self.c.post("/sms/status", data={"MessageStatus": "undelivered", "To": "+18083922341", "ErrorCode": "30034"})
        self.assertIn("Texts are being blocked", self.c.get("/admin").get_data(as_text=True))
        self.m.engine.clock.go(8 * 24 * 60)
        self.assertNotIn("Texts are being blocked", self.c.get("/admin").get_data(as_text=True))

    def test_owner_alert_failure_is_logged_not_swallowed(self):
        self.m.engine.phone = PickyPhone(bad_sms={"+18085550999"})
        self.m.engine.add_signup(plan="basic", family_name="Kainoa", family_phone="4255550110",
                                 family_email="k@example.com", relationship="Son", kupuna_name="Auntie",
                                 kupuna_phone="8085550150", call_time="09:00", language="English",
                                 backup_name="Mele", backup_phone="8085550151", notes="")
        self.assertTrue(any("sign-up alert" in e for e in self.errors()), self.errors())
        self.assertEqual(self.m.engine.db.execute("SELECT COUNT(*) FROM signups").fetchone()[0], 1, "sign-up still saved")

    def test_twilio_is_asked_to_report_back(self):
        cfg = type("C", (), dict(TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="t", TWILIO_FROM_NUMBER="+18085550000",
                                 PUBLIC_BASE_URL="https://kupuna-checkin.onrender.com", RING_SECONDS=25))
        tp = ph.TwilioPhone(cfg)
        sent = {}
        tp._post = lambda what, fields: sent.update(fields) or "SM1"
        tp.send_sms("+18085550110", "hi")
        self.assertEqual(sent["StatusCallback"], "https://kupuna-checkin.onrender.com/sms/status")

    def test_status_callback_requires_twilio_signature_when_live(self):
        cfg = self.m.cfg
        old = (cfg.DRY_RUN, cfg.TWILIO_AUTH_TOKEN, cfg.PUBLIC_BASE_URL)
        cfg.DRY_RUN, cfg.TWILIO_AUTH_TOKEN, cfg.PUBLIC_BASE_URL = False, "secret", "https://x.example"
        try:
            r = self.c.post("/sms/status", data={"MessageStatus": "undelivered", "To": "+18085550110", "ErrorCode": "30034"})
            self.assertEqual(r.status_code, 403)
            self.assertEqual(self.errors(), [])
        finally:
            cfg.DRY_RUN, cfg.TWILIO_AUTH_TOKEN, cfg.PUBLIC_BASE_URL = old


class TextingCompliance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as m
        import signup
        cls.m, cls.signup = m, signup

    def setUp(self):
        self.m.engine.db = db.connect(":memory:")
        self.m.engine.phone = ph.DryRunPhone(quiet=True)
        self.m.engine.clock = Clock(13, 0)
        self.signup._recent.clear()
        self.c = self.m.app.test_client()

    def post(self, **over):
        d = dict(GOOD_SIGNUP); d.update(over)
        return self.c.post("/signup", data={k: v for k, v in d.items() if v is not None})

    def test_texting_consent_box_is_required(self):
        r = self.post(agree_sms=None)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.m.engine.db.execute("SELECT COUNT(*) FROM signups").fetchone()[0], 0)
        body = self.c.get("/signup").get_data(as_text=True)
        self.assertIn("Msg &amp; data rates may apply. Reply STOP to opt out, HELP for help.", body)
        self.assertIn('href="/privacy"', body)

    def test_privacy_page(self):
        r = self.c.get("/privacy")
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("No mobile information will be shared with third parties", body)
        self.assertIn("808-555-0999", body)  # OWNER_PHONE in tests

    def test_approval_sends_welcome_texts_to_both_contacts(self):
        self.post()
        sid = self.m.engine.db.execute("SELECT id FROM signups").fetchone()[0]
        self.m.engine.phone.sent.clear()
        self.m.engine.approve_signup(sid, name="Auntie Leilani", phone="8085550142", call_time="08:30",
                                     language="Pidgin", contact1_name="Kainoa", contact1_phone="4255550110",
                                     contact2_name="Mele", contact2_phone="8085550133", consent_note="phoned 10/7")
        texts = {to: body for k, to, body in self.m.engine.phone.sent if k == "sms"}
        self.assertIn("family contact for Auntie Leilani", texts["+14255550110"])
        self.assertIn("backup contact", texts["+18085550133"])
        for body in texts.values():
            self.assertIn("Reply STOP to opt out, HELP for help", body)
            self.assertTrue(body.startswith("Kupuna Check-In:"))

    def test_adding_by_hand_also_sends_welcome(self):
        self.c.post("/admin/login", data={"password": "pw"})
        self.c.post("/admin/add", data=dict(name="Uncle Walter", phone="8085550177", call_time="09:00",
                                             language="English", contact1_name="Dana", contact1_phone="8085550178",
                                             contact2_name="Bobby", contact2_phone="8085550179", consent_note="ok"))
        self.assertEqual(sorted(to for k, to, _ in self.m.engine.phone.sent if k == "sms"),
                         ["+18085550178", "+18085550179"])

    def test_help_and_stop_replies(self):
        e = self.m.engine
        help_reply = e.family_reply("+18085550110", "help")
        self.assertIn("808-555-0999", help_reply)
        self.assertIn("Reply STOP to opt out", help_reply)
        self.assertEqual(e.family_reply("+18085550110", "STOP"), "", "Twilio answers STOP itself")
        self.assertEqual(e.family_reply("+18085550110", "Start"), "")

    def test_alert_and_weekly_texts_include_opt_out(self):
        e, phone, clock, kid = make()
        clock.go(1); e.tick()
        for _ in range(2):
            miss(e); clock.go(5); e.tick()
        miss(e)
        alert = next(b for k, to, b in phone.sent if k == "sms")
        self.assertIn("Reply STOP to opt out.", alert)
        self.assertIn("Reply STOP to opt out.", e.weekly_text(e.kupuna(kid), clock().date(), clock().date()))


class TermsPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as m
        cls.c = m.app.test_client()

    def test_terms_page_has_sms_program_details(self):
        r = self.c.get("/terms")
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        for phrase in ("Message and data rates may apply", "STOP", "HELP", "Carriers are not liable",
                       "does not call 911", "808-555-0999", "Hawaiʻi"):
            self.assertIn(phrase, body)

    def test_terms_linked_from_signup_and_privacy(self):
        self.assertIn('href="/terms"', self.c.get("/signup").get_data(as_text=True))
        self.assertIn('href="/terms"', self.c.get("/privacy").get_data(as_text=True))


class Backups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as m
        cls.m = m

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "checkin.db")
        self.conn = db.connect(self.path)
        self.conn.execute(
            "INSERT INTO kupuna (name, phone, call_time, contact1_name, contact1_phone, contact2_name, contact2_phone,"
            " consent_note, consent_at, created_at) VALUES ('=Auntie Leilani','+18085550101','08:00','Kai','+18085550102',"
            "'Noe','+18085550103','test','2026-10-01','2026-10-01')")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_daily_copy_once_per_day_and_readable(self):
        import backup
        from datetime import date
        first = backup.daily_copy(self.conn, self.path, date(2026, 10, 8))
        self.assertTrue(first.exists())
        self.assertIsNone(backup.daily_copy(self.conn, self.path, date(2026, 10, 8)))
        import sqlite3
        copy = sqlite3.connect(str(first))
        self.assertEqual(copy.execute("SELECT name FROM kupuna").fetchone()[0], "=Auntie Leilani")
        copy.close()
        self.assertEqual(backup.latest_copy(self.path), "2026-10-08")

    def test_old_copies_are_dropped(self):
        import backup
        from datetime import date
        for d in range(1, 21):
            backup.daily_copy(self.conn, self.path, date(2026, 10, d))
        kept = sorted(p.name for p in backup.backup_dir(self.path).glob("checkin-*.db"))
        self.assertEqual(len(kept), backup.KEEP_DAYS)
        self.assertEqual(kept[0], "checkin-2026-10-07.db")
        self.assertEqual(kept[-1], "checkin-2026-10-20.db")

    def test_memory_database_is_skipped(self):
        import backup
        from datetime import date
        self.assertIsNone(backup.daily_copy(self.conn, ":memory:", date(2026, 10, 8)))

    def test_spreadsheet_is_safe_to_open(self):
        import backup
        text = backup.people_csv(self.conn)
        self.assertIn("Family contact", text.splitlines()[0])
        self.assertIn("'=Auntie Leilani", text)
        self.assertIn("Active", text)

    def test_download_needs_sign_in(self):
        c = self.m.app.test_client()
        self.assertEqual(c.get("/admin/backup.db").status_code, 302)
        self.assertEqual(c.get("/admin/people.csv").status_code, 302)

    def test_downloads_when_signed_in(self):
        c = self.m.app.test_client()
        self.m._login_tries.clear()
        c.post("/admin/login", data={"password": "pw"})
        r = c.get("/admin/backup.db")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"SQLite format 3"))
        self.assertIn("attachment", r.headers["Content-Disposition"])
        r = c.get("/admin/people.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Name,Phone", r.get_data(as_text=True))
        self.assertIn("Download full backup", c.get("/admin").get_data(as_text=True))
