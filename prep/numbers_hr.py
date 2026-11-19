"""Convert Croatian cardinal number words to digits.

  "trideset godina"            -> "30 godina"
  "dvadeset i pet miligrama"   -> "25 miligrama"
  "dvije tisuće dvadeset pet"  -> "2025"
  "dva zarez pet"              -> "2,5"
Left alone on purpose:
  - a lone "jedan/jedna/jedno/..." ("jedna stvar", "jedan od") is usually not a count
  - ordinals ("prvi", "drugi"), approximations ("desetak"), collectives ("petero")
"""
import re

UNITS = {
    "nula": 0,
    "jedan": 1, "jedna": 1, "jedno": 1, "jednu": 1, "jednog": 1, "jednoga": 1,
    "jednom": 1, "jednoj": 1, "jednim": 1, "jedne": 1,
    "dva": 2, "dvije": 2, "dvaju": 2, "dvama": 2, "dvjema": 2,
    "tri": 3, "triju": 3, "trima": 3,
    "četiri": 4, "četiriju": 4, "četirima": 4,
    "pet": 5, "šest": 6, "sedam": 7, "osam": 8, "devet": 9,
}
TEENS = {
    "deset": 10, "jedanaest": 11, "dvanaest": 12, "trinaest": 13, "četrnaest": 14,
    "petnaest": 15, "šesnaest": 16, "sedamnaest": 17, "osamnaest": 18, "devetnaest": 19,
}
TENS = {
    "dvadeset": 20, "trideset": 30, "četrdeset": 40, "pedeset": 50,
    "šezdeset": 60, "sedamdeset": 70, "osamdeset": 80, "devedeset": 90,
}
HUNDREDS = {
    "sto": 100, "stotinu": 100, "dvjesto": 200, "dvjesta": 200, "dvijesto": 200, "dvjest": 200,
    "dvijest": 200, "tristo": 300, "trista": 300, "trist": 300,
    "četiristo": 400, "petsto": 500, "šeststo": 600, "sedamsto": 700, "osamsto": 800, "devetsto": 900,
}
MULTIPLIERS = {
    "tisuću": 1000, "tisuća": 1000, "tisuće": 1000, "hiljadu": 1000, "hiljada": 1000,
    "milijun": 1_000_000, "milijuna": 1_000_000, "milijuni": 1_000_000,
}
ADDITIVE = {**UNITS, **TEENS, **TENS, **HUNDREDS}
TOKEN = re.compile(r"^([\"'(]*)(.*?)([.,!?;:\"')]*)$")


def _split(token: str) -> tuple[str, str, str]:
    lead, core, trail = TOKEN.match(token).groups()
    return lead, core, trail


def _limit_after(value: int) -> int:
    """Largest value that may follow an additive word (dvadeset -> pet, sto -> dvadeset)."""
    if value >= 100:
        return 100
    if value >= 20 and value % 10 == 0:
        return 10
    return 0


def _parse(tokens: list[str], start: int):
    """Read one number starting at tokens[start]. Returns (value, end, word_count, trail) or None."""
    total, current, limit, i, words, trail = 0, 0, 10**9, start, 0, ""
    while i < len(tokens):
        lead, core, punct = _split(tokens[i])
        word = core.lower()
        if lead and i != start:
            break
        if word == "i" and words and limit > 0 and i + 1 < len(tokens):
            _, nxt, _ = _split(tokens[i + 1])
            if nxt.lower() in ADDITIVE and ADDITIVE[nxt.lower()] < limit and not punct:
                i += 1
                continue
            break
        if word in ADDITIVE and ADDITIVE[word] < limit:
            value = ADDITIVE[word]
            current += value
            limit = _limit_after(value)
        elif word in MULTIPLIERS and words and current < 1000 and MULTIPLIERS[word] > total:
            total += max(current, 1) * MULTIPLIERS[word]
            current, limit = 0, 1000
        elif word in MULTIPLIERS and not words:
            total, current, limit = MULTIPLIERS[word], 0, 1000
        else:
            break
        words += 1
        i += 1
        trail = punct
        if punct:
            break
    if not words:
        return None
    return total + current, i, words, trail


def words_to_digits(text: str) -> str:
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        parsed = _parse(tokens, i)
        if parsed is None:
            out.append(tokens[i])
            i += 1
            continue
        value, end, words, trail = parsed
        lead, core, _ = _split(tokens[i])
        if words == 1 and UNITS.get(core.lower()) == 1:
            out.append(tokens[i])  # lone "jedan/jedna" stays a word
            i += 1
            continue
        number = str(value)
        # "dva zarez pet" -> "2,5"
        if not trail and end + 1 < len(tokens) and _split(tokens[end])[1].lower() == "zarez":
            decimal = _parse(tokens, end + 1)
            if decimal:
                d_value, d_end, _, d_trail = decimal
                number, end, trail = f"{number},{d_value}", d_end, d_trail
        out.append(lead + number + trail)
        i = end
    return " ".join(out)


if __name__ == "__main__":
    samples = [
        "Trideset godina. Međutim, one dođu",
        "dvadeset i pet miligrama dva puta dnevno",
        "jedna stvar s kojom uvijek, jedan od primarnih",
        "u dvije tisuće dvadeset i pet godini",
        "tri do četiri sata, pet i šest",
        "doza od dva zarez pet miligrama",
        "sto dvadeset na osamdeset",
        "prvi put, desetak pacijenata, jednom tjedno",
        "petnaest posto, četrdeset i dvije godine",
    ]
    for s in samples:
        print(f"{s!r:50} -> {words_to_digits(s)!r}")
