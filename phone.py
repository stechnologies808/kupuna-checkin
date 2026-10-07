"""Talking to Twilio: placing calls, sending texts, building call scripts (TwiML),
and checking that incoming webhooks really came from Twilio.

Uses Twilio's REST API directly, so no Twilio package is needed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from itertools import count
from xml.sax.saxutils import escape, quoteattr

import greetings

API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/{what}.json"


class TwilioError(RuntimeError):
    pass


class TwilioPhone:
    def __init__(self, cfg):
        missing = [n for n in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PUBLIC_BASE_URL")
                   if not getattr(cfg, n)]
        if missing:
            raise TwilioError(f"Set {', '.join(missing)} in .env (or set DRY_RUN=1).")
        if not cfg.PUBLIC_BASE_URL.startswith("https://"):
            raise TwilioError("PUBLIC_BASE_URL must be the https address Twilio can reach (your ngrok or host URL).")
        self.cfg = cfg

    def _post(self, what: str, fields: dict) -> str:
        url = API.format(sid=self.cfg.TWILIO_ACCOUNT_SID, what=what)
        data = urllib.parse.urlencode(fields).encode()
        auth = base64.b64encode(f"{self.cfg.TWILIO_ACCOUNT_SID}:{self.cfg.TWILIO_AUTH_TOKEN}".encode()).decode()
        req = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)["sid"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("message", detail)
            except ValueError:
                pass
            raise TwilioError(f"Twilio refused the {what[:-1].lower()} to {fields.get('To')}: {detail}") from None

    def place_checkin_call(self, to: str, checkin_id: int) -> str:
        base = self.cfg.PUBLIC_BASE_URL
        return self._post("Calls", {
            "To": to, "From": self.cfg.TWILIO_FROM_NUMBER,
            "Url": f"{base}/voice/checkin/{checkin_id}",
            "StatusCallback": f"{base}/voice/status/{checkin_id}",
            "StatusCallbackEvent": "completed",
            "Timeout": str(self.cfg.RING_SECONDS),
        })

    def alert_call(self, to: str, message: str) -> str:
        return self._post("Calls", {
            "To": to, "From": self.cfg.TWILIO_FROM_NUMBER,
            "Twiml": twiml_say_twice(message),
            "Timeout": str(self.cfg.RING_SECONDS),
        })

    def send_sms(self, to: str, body: str) -> str:
        return self._post("Messages", {"To": to, "From": self.cfg.TWILIO_FROM_NUMBER, "Body": body,
                                       "StatusCallback": f"{self.cfg.PUBLIC_BASE_URL}/sms/status"})


class DryRunPhone:
    """Prints what would happen and records it. Used for local testing and in the test suite."""

    def __init__(self, quiet: bool = False):
        self.sent: list[tuple[str, str, str]] = []  # (kind, to, content)
        self._ids = count(1)
        self.quiet = quiet

    def _record(self, kind: str, to: str, content: str) -> str:
        self.sent.append((kind, to, content))
        if not self.quiet:
            print(f"[dry run] {kind:>6} → {to}: {content}")
        return f"DRY{next(self._ids):06d}"

    def place_checkin_call(self, to: str, checkin_id: int) -> str:
        return self._record("call", to, f"check-in call #{checkin_id}")

    def alert_call(self, to: str, message: str) -> str:
        return self._record("alert", to, message)

    def send_sms(self, to: str, body: str) -> str:
        return self._record("sms", to, body)


def make_phone(cfg):
    return DryRunPhone() if cfg.DRY_RUN else TwilioPhone(cfg)


# ---------- TwiML (the call scripts) ----------
def _say(text: str, language: str) -> str:
    v, lang = greetings.voice(language)
    return f"<Say voice={quoteattr(v)} language={quoteattr(lang)}>{escape(text)}</Say>"


def twiml_checkin(base_url: str, checkin_id: int, kupuna_id: int, name: str, language: str) -> str:
    rec = greetings.recording_for(kupuna_id, language)
    prompt = (f"<Play>{escape(base_url)}/{rec}</Play>" if rec
              else _say(greetings.text(greetings.GREETING, language, name=greetings.short_name(name)), language))
    action = quoteattr(f"{base_url}/voice/gather/{checkin_id}")
    gather = f'<Gather numDigits="1" timeout="8" action={action} method="POST">{prompt}</Gather>'
    # Say the greeting twice before giving up; hanging up with no key counts as a miss.
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f"{gather}{gather}{_say(greetings.text(greetings.GOODBYE, language), language)}<Hangup/></Response>")


def twiml_after_key(result: str, base_url: str, checkin_id: int, language: str) -> str:
    if result == "ok":
        body = _say(greetings.text(greetings.THANKS, language), language) + "<Hangup/>"
    elif result == "help":
        body = _say(greetings.text(greetings.HELP, language), language) + "<Hangup/>"
    else:  # pressed some other key: ask again
        body = f"<Redirect method=\"POST\">{escape(base_url)}/voice/checkin/{checkin_id}</Redirect>"
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


def twiml_say_twice(message: str) -> str:
    s = _say(message, "English")
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{s}<Pause length="1"/>{s}</Response>'


def twiml_sms_reply(text: str) -> str:
    inner = f"<Message>{escape(text)}</Message>" if text else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>'


# ---------- webhook signature check ----------
def twilio_signature(auth_token: str, url: str, params: dict) -> str:
    """Twilio signs: full URL + each POST param name and value, sorted by name, HMAC-SHA1, base64."""
    payload = url + "".join(k + v for k, v in sorted(params.items()))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def signature_ok(auth_token: str, url: str, params: dict, signature: str) -> bool:
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature)
