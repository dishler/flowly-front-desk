from __future__ import annotations

MIN_STEM_LEN = 3

# Inflectional endings only (verb conjugation, noun case, adjective agreement) --
# deliberately NOT derivational suffixes like "-олог"/"-ація", so e.g.
# "імплантація" and "імплант" stay distinct stems. Longest-first so a single
# greedy pass picks the most specific ending.
_SUFFIXES: tuple[str, ...] = tuple(
    sorted(
        {
            "ившись", "вшись",
            "увати", "ювати",
            "ується", "юється",
            # deverbal "action" nouns -- treated as equivalent to their verb
            # root in this domain, since patients freely switch between
            # "видалити зуб" / "видалення зуба" style phrasing for the same
            # procedure.
            "ування", "ювання", "ення", "ання", "іння",
            "ити", "ати", "яти", "іти",
            "ую", "юю", "аю", "яю", "ує", "ює",
            "ать", "ять", "уть", "ють", "имо", "емо", "єте", "ите",
            "ими", "іми", "ого", "ому", "его", "єму",
            "ями", "ами", "ах", "ях", "ом", "ем", "єм",
            "ів", "ей",
            # o-/i-/a-thematic reflexive/plain past tense (e.g. "відколовся"/
            # "відколола", "видалила", "записала")
            "овся", "олася", "олись", "олось", "ола", "оло", "оли",
            "ився", "илася", "илась", "илось", "ила", "ило", "или",
            "алася", "алась", "алось", "ала", "ало", "али",
            "вся", "лася", "лись", "лось",
            "ла", "ло", "ли",
            "ся", "сь",
            "ий", "ім", "ій",
            "ою", "ею", "єю",
            "у", "ю", "а", "я", "и", "і", "ї", "е", "є", "о", "ь",
        },
        key=len,
        reverse=True,
    )
)


def stem(token: str) -> str:
    token = token.lower()
    if len(token) <= MIN_STEM_LEN:
        return token

    for suffix in _SUFFIXES:
        if len(suffix) >= len(token):
            continue
        if token.endswith(suffix) and len(token) - len(suffix) >= MIN_STEM_LEN:
            token = token[: -len(suffix)]
            break

    return token
