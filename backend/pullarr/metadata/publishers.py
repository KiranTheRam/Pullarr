"""Telling comics apart from manga by publisher.

ComicVine indexes Japanese tankobon alongside western comics, so a plain
"what shipped this week" query comes back dominated by manga volumes that
belong in mangarr, not here. ComicVine has no language or format field, so
the publisher is the only usable signal.

Two groups are excluded: the Japanese publishers themselves, and the western
houses whose catalogue is manga/light novels in translation (Viz, Seven Seas,
Yen Press). Everything else — including publishers we have never heard of —
is kept, because wrongly hiding a small-press comic is worse than letting an
occasional manga volume through.
"""

import re

# Matched as substrings of the normalized publisher, so imprint variations
# ("Kodansha Comics", "Kodansha USA", "Shogakukan Asia") are covered.
MANGA_PUBLISHER_MARKERS = frozenset({
    "akitashoten",
    "asciimediaworks",
    "bunkasha",
    "coamix",
    "corocoro",
    "dengeki",
    "enterbrain",
    "futabasha",
    "gentosha",
    "hakusensha",
    "houbunsha",
    "hobunsha",
    "ichijinsha",
    "jive",
    "kadokawa",
    "kaiousha",
    "kodansha",
    "leedsha",
    "libre",
    "maggarden",
    "mediafactory",
    "nihonbungeisha",
    "ohzora",
    "shinchosha",
    "shogakukan",
    "shonengahosha",
    "shueisha",
    "shufunotomo",
    "squareenix",
    "takeshobo",
    "tokumashoten",
    "wanibooks",
    "wanimagazine",
    # western publishers of translated manga and light novels
    "sevenseas",
    "yenpress",
    "denpa",
    "kaiten",
    "onepeacebooks",
    "vertical",
})

# Names too short or too generic to match as substrings safely.
MANGA_PUBLISHER_EXACT = frozenset({
    "viz",
    "vizmedia",
    "vizcommunications",
    "vizllc",
    "jmanga",
    "tokyopop",
})


def normalize_publisher(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def is_manga_publisher(name: str) -> bool:
    """True when the publisher is a manga house. An unknown or empty
    publisher is never treated as manga — the filter fails open."""
    normalized = normalize_publisher(name)
    if not normalized:
        return False
    if normalized in MANGA_PUBLISHER_EXACT:
        return True
    return any(marker in normalized for marker in MANGA_PUBLISHER_MARKERS)
