# Kupuna Check-In

A daily automated phone call for kūpuna who live alone. They press 1 if they're okay.
If they miss three calls, their family contact gets a text and a phone call. If family
doesn't reply OK within 15 minutes, a backup contact gets the same.

```
call ──1──> checked in
  │ no answer / voicemail / hung up
  └─> retry in 5 min (3 tries total) ──> text + call family ──OK──> marked safe
                                              │ 15 min, no reply
                                              └─> text + call backup contact
Press 2 at any time ──> text + call BOTH contacts right away
Sunday 6 PM ──> weekly summary text to family
```

This service notifies family. It does not call 911 and it is not a medical alert system.

---

## 1. Try it with no accounts (5 minutes)

You need Python 3.11 or newer.

```bash
pip install -r requirements.txt
python manage.py demo                      # plays a fake morning with four kūpuna
python -m unittest discover tests -v       # 31 tests covering every path
```

Then run the app in dry-run mode (nothing is really called or texted):

```bash
cp .env.example .env                       # then set ADMIN_PASSWORD in .env
python app.py
```

Open http://localhost:5000/admin (any username, your ADMIN_PASSWORD), add a kūpuna,
and press **Call now**. The terminal prints what would have been sent.

## 2. Make a real call to your own phone

1. **Twilio account.** Sign up at twilio.com. A trial account is fine for testing: it can
   only call and text numbers you verify (add your own cell), and calls start with a short
   trial notice.
2. **Phone number.** Buy a number in the Twilio console. A local 808 number looks
   familiar to kūpuna and is more likely to be picked up.
3. **Make your computer reachable.** Twilio needs a public https address to reach this
   app. Install ngrok (ngrok.com), then in a second terminal run `ngrok http 5000`.
   Copy the `https://…ngrok…` address it shows.
4. **Fill in `.env`:**
   ```
   TWILIO_ACCOUNT_SID=AC...        (console home page)
   TWILIO_AUTH_TOKEN=...           (console home page)
   TWILIO_FROM_NUMBER=+1808...     (the number you bought)
   PUBLIC_BASE_URL=https://xxxx.ngrok-free.app
   DRY_RUN=0
   ADMIN_PASSWORD=pick-something-long
   ```
5. **Texts back from family.** In the Twilio console open your number → Messaging →
   "A message comes in" → Webhook → `https://xxxx.ngrok-free.app/sms` (HTTP POST).
6. Run `python app.py`, open `/admin`, add **yourself** as the kūpuna and a second phone
   you own as the family contact, then press **Call now**.
   - Press 1 → you hear "Thank you", status turns green.
   - Let it ring out three times → the family phone gets a text and a call.
   - Text back `OK` → marked safe.
   - Press 2 → both contacts are alerted.

Every step shows in the activity list on `/admin` and in `python manage.py log`.

## 3. Running it for real customers

- **It has to be always on.** If the server is asleep at 8:00, nobody gets called. Free
  hosting tiers that sleep are not okay. Use an always-on host (Render, Railway, Fly.io, a
  small VPS) with a persistent disk for `checkin.db`, and point an uptime monitor (such as
  UptimeRobot) at `/health` so you get alerted if it goes down.
- **Render setup is included.** `render.yaml` sets up an always-on Starter service with a
  1 GB disk for the database. On Render choose New → Blueprint and pick your repo. Render
  fills in `PUBLIC_BASE_URL` itself, so you don't need ngrok there.
- **Run one worker only.** The scheduler runs inside the app:
  `gunicorn -w 1 --threads 4 -b 0.0.0.0:$PORT app:app` (see `Procfile`).
- **Register for texting.** US carriers block texts from unregistered business numbers.
  Register your number for A2P 10DLC (local numbers) or toll-free verification in the
  Twilio console before signing up families. Allow time for approval.
- **Costs.** Twilio charges per minute of calling, per text, and a monthly fee per
  number. Check twilio.com/en-us/pricing for current US rates. A normal morning is one
  short call per kūpuna; alerts add a few texts and calls.

## Family sign-ups

Your home page (`/`) is a public sign-up form. When a family signs up:

1. You get a text (set `OWNER_PHONE`) and the sign-up appears under **New sign-ups** on `/admin`.
2. Nobody is called yet. Click **Review**, call the kūpuna yourself, and get their OK.
3. Write how and when they agreed in the consent box, then click **Approve and start calls**.

To take payment, create a Stripe Payment Link for each plan and set `PAYMENT_LINK_BASIC` and
`PAYMENT_LINK_TALK_STORY`. The thank-you page then shows a **Set up payment** button. Prices and
the free-trial line on the page come from `PRICE_BASIC`, `PRICE_TALK_STORY` and `FREE_TRIAL_NOTE`.

The Talk Story plan is hidden until you set `OFFER_TALK_STORY=1`, since its weekly live calls are
made by a person. While hidden, the page offers only Daily Check-In.

## 4. Before you take money

- **Written consent.** Automated calls fall under the federal robocall law (TCPA). Get
  signed consent from each kūpuna or their legal representative. The app won't add
  anyone without a consent note.
- **Plain disclaimer** in your sign-up form and terms: you notify family contacts; you do
  not call 911 or provide medical monitoring.
- **Talk to a lawyer** about liability if a check-in is missed, and about business
  insurance. This is the main risk in this business.
- **Record the greetings.** Twilio has no Pidgin or Ilocano voice, so those use an
  English voice reading the words, which sounds wrong. Put recordings in
  `static/greetings/` (see the README there). A grandkid recording a personal greeting
  is a nice selling point.
- **Check the translations.** The Ilocano and Japanese wording in `greetings.py` is a
  draft. Have native speakers review it.

## Files

| File | What it does |
|---|---|
| `engine.py` | All the rules: when to call, retries, alerts, family replies, weekly text |
| `phone.py` | Twilio calls and texts, call scripts, webhook signature check, dry-run phone |
| `app.py` | Web app: Twilio webhooks, background scheduler, `/admin` status page |
| `greetings.py` | What kūpuna hear, per language |
| `signup.py` | Public sign-up page and the approve step on `/admin` |
| `manage.py` | Command line: `demo`, `add`, `list`, `call-now`, `pause`, `resume`, `log` |
| `config.py` | Settings from `.env` (timing rules, quiet hours, time zone) |
| `tests/` | Automated tests, no Twilio needed |

Timing rules (tries, minutes between tries, family reply window, quiet hours, weekly
summary day and hour) are all settings in `.env`; see `config.py` for the full list.
Calls are never started before 7 AM or after 9 PM Hawaii time.
