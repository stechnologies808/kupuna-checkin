"""What kūpuna hear, per language.

Twilio's text-to-speech covers English and Japanese well. It has no Pidgin or
Ilocano voice, so for those (and honestly for everyone) a recorded greeting is
better. Drop an .mp3 in static/greetings/ and it is used instead of the voice:

    static/greetings/kupuna-<id>.mp3   personal greeting for one kūpuna (e.g. a grandchild's voice)
    static/greetings/<language>.mp3    shared greeting for a language, e.g. pidgin.mp3, ilocano.mp3

Non-English wording below is a DRAFT. Have a native speaker check it before use.
"""
from pathlib import Path

GREETING_DIR = Path(__file__).resolve().parent / "static" / "greetings"

# voice + language code for Twilio <Say>
VOICES = {
    "English": ("Polly.Joanna", "en-US"),
    "Pidgin": ("Polly.Joanna", "en-US"),     # no Pidgin voice exists; record one
    "Ilocano": ("Polly.Joanna", "en-US"),    # no Ilocano voice exists; record one
    "Japanese": ("Polly.Mizuki", "ja-JP"),
}

GREETING = {
    "English": "Good morning, {name}. This is your daily check-in call. If you're doing okay today, press 1. If you need help, press 2 and we'll call your family right away.",
    "Pidgin": "Eh, good morning {name}! Dis da daily check-in call. If you stay okay today, press 1. If you like help, press 2 an' we go call your family right now.",
    "Ilocano": "Naimbag a bigat, {name}! Daytoy ti inaldaw a panangkumusta. No nasayaat ti kasasaadmo ita nga aldaw, pinduten ti 1. No masapulmo ti tulong, pinduten ti 2.",
    "Japanese": "{name}、おはようございます。毎日の確認のお電話です。お元気でしたら1を、助けが必要でしたら2を押してください。",
}

THANKS = {
    "English": "Thank you. Have a good day.",
    "Pidgin": "Mahalo! Have one good day.",
    "Ilocano": "Agyamanak! Naimbag nga aldaw.",
    "Japanese": "ありがとうございます。良い一日を。",
}

HELP = {
    "English": "Okay. We are contacting your family now. If this is an emergency, hang up and dial 9 1 1.",
    "Pidgin": "Okay. We calling your family right now. If dis one emergency, hang up an' call 9 1 1.",
    "Ilocano": "Sige. Tawagan mi ita ti pamiliam. No emerhensia daytoy, ibabam ti telepono ket tawagan ti 9 1 1.",
    "Japanese": "わかりました。今からご家族に連絡します。緊急の場合は、電話を切って911に電話してください。",
}

GOODBYE = {
    "English": "We didn't hear a reply. We'll try again in a few minutes.",
    "Pidgin": "We never hear you. We go call back in a few minutes.",
    "Ilocano": "Saanmi a nangngeg ti sungbatmo. Tumawag kami manen kalpasan ti sumagmamano a minuto.",
    "Japanese": "お返事が確認できませんでした。数分後にもう一度お電話します。",
}


def _lang(language: str) -> str:
    return language if language in GREETING else "English"


def recording_for(kupuna_id: int, language: str) -> str | None:
    """Relative static path of a recorded greeting, if one exists."""
    for name in (f"kupuna-{kupuna_id}.mp3", f"{language.lower()}.mp3"):
        if (GREETING_DIR / name).exists():
            return f"static/greetings/{name}"
    return None


def short_name(full_name: str) -> str:
    """'Auntie Leilani Kekona' -> 'Auntie Leilani'. Titles like Auntie/Uncle stay."""
    parts = full_name.split()
    return " ".join(parts[:2]) if len(parts) > 2 else full_name


def text(table: dict, language: str, **kw) -> str:
    return table[_lang(language)].format(**kw)


def voice(language: str) -> tuple[str, str]:
    return VOICES[_lang(language)]
