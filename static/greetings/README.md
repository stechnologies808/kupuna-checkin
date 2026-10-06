# Recorded greetings

Put .mp3 files here to replace the computer voice.

- `kupuna-<id>.mp3`: a personal greeting for one kūpuna (find the id with `python manage.py list`)
- `pidgin.mp3`, `ilocano.mp3`, `japanese.mp3`, `english.mp3`: shared greeting for that language

A personal greeting is used first, then the language greeting, then the computer voice.

The recording must end by asking them to press 1 if they're okay or 2 if they need help.
Keep it under 20 seconds. The system plays it twice before hanging up.
