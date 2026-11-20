"""Country-name normalisation, shared by the classifier and the card-BIN source.

Geography comparisons must happen on ISO codes, never on surface words: "German customer in
Germany" names one country twice, and treating those two words as two countries invents a
geography mismatch that nobody described.

The pronoun "us" is the reason `find_countries` is case-sensitive for ambiguous tokens: in
"asked us to pay a supplier in France", a case-insensitive match reads "us" as the United
States and fabricates a cross-border mismatch.
"""
from __future__ import annotations

import re

#: Surface form -> ISO 3166-1 alpha-2. Adjectives map to the same code as the noun.
COUNTRY_ISO: dict[str, str] = {
    "usa": "US", "u.s.a.": "US", "u.s.": "US", "america": "US", "american": "US",
    "united states": "US", "us": "US",
    "uk": "GB", "u.k.": "GB", "britain": "GB", "british": "GB", "england": "GB",
    "english": "GB", "scotland": "GB", "wales": "GB", "united kingdom": "GB",
    "nigeria": "NG", "nigerian": "NG", "ghana": "GH", "ghanaian": "GH",
    "kenya": "KE", "kenyan": "KE", "south africa": "ZA",
    "russia": "RU", "russian": "RU", "belarus": "BY", "ukraine": "UA", "ukrainian": "UA",
    "moldova": "MD", "romania": "RO", "romanian": "RO", "bulgaria": "BG",
    "kazakhstan": "KZ", "china": "CN", "chinese": "CN", "hong kong": "HK",
    "india": "IN", "indian": "IN", "pakistan": "PK", "bangladesh": "BD",
    "indonesia": "ID", "indonesian": "ID", "vietnam": "VN", "vietnamese": "VN",
    "thailand": "TH", "thai": "TH", "malaysia": "MY", "singapore": "SG",
    "philippines": "PH", "filipino": "PH", "japan": "JP", "japanese": "JP",
    "korea": "KR", "south korea": "KR",
    "netherlands": "NL", "dutch": "NL", "holland": "NL",
    "germany": "DE", "german": "DE", "france": "FR", "french": "FR",
    "spain": "ES", "spanish": "ES", "italy": "IT", "italian": "IT",
    "poland": "PL", "polish": "PL", "portugal": "PT", "portuguese": "PT",
    "greece": "GR", "greek": "GR", "ireland": "IE", "irish": "IE",
    "denmark": "DK", "danish": "DK", "sweden": "SE", "swedish": "SE",
    "norway": "NO", "norwegian": "NO", "finland": "FI", "finnish": "FI",
    "switzerland": "CH", "swiss": "CH", "austria": "AT", "austrian": "AT",
    "belgium": "BE", "belgian": "BE", "turkey": "TR", "turkish": "TR",
    "canada": "CA", "canadian": "CA", "mexico": "MX", "mexican": "MX",
    "brazil": "BR", "brazilian": "BR", "argentina": "AR", "chile": "CL",
    "colombia": "CO", "venezuela": "VE", "peru": "PE",
    "australia": "AU", "australian": "AU", "new zealand": "NZ",
    "iran": "IR", "north korea": "KP", "israel": "IL", "uae": "AE",
    "saudi arabia": "SA", "egypt": "EG", "morocco": "MA",
}

#: Tokens that are also ordinary English words; only count them when capitalised as a country.
_CASE_SENSITIVE = {"us", "uk"}

_PATTERN = re.compile(
    r"\b(" + "|".join(
        sorted((re.escape(k) for k in COUNTRY_ISO), key=len, reverse=True)
    ) + r")\b"
)

#: Countries with elevated card-not-present fraud rates. A WEAK contextual signal only -
#: geography is never guilt, and it can never carry a grade on its own.
ELEVATED_RISK_ISO = {"NG", "GH", "RU", "BY", "UA", "RO", "MD", "VN", "ID", "PK", "BD",
                     "VE", "KP", "IR"}


def iso_for(name: str) -> str:
    return COUNTRY_ISO.get(name.strip().lower(), "")


#: Phrases that contain a country word but name something else entirely. "American Express
#: card used in Germany" was being read as a United States / Germany mismatch.
_NOT_A_COUNTRY = re.compile(
    r"\b(?:american\s+express|americanexpress|american\s+airlines|bank\s+of\s+america|"
    r"america[nl]?\s+(?:express|airlines|eagle|apparel)|india\s+pale|turkey\s+(?:sandwich|dinner)|"
    r"chinese\s+(?:takeaway|restaurant|food)|french\s+(?:fries|press|toast)|"
    r"swiss\s+(?:cheese|army)|russian\s+(?:salad|roulette)|dutch\s+(?:oven|auction)|"
    r"english\s+(?:breakfast|muffin)|irish\s+coffee|greek\s+yog)", re.I)


def find_countries(text: str) -> list[str]:
    """Every country named in `text`, as a sorted list of distinct ISO alpha-2 codes."""
    found: set[str] = set()
    # blank out brand and idiom phrases first, so their country words cannot match
    text = _NOT_A_COUNTRY.sub(lambda m: " " * len(m.group(0)), text)
    for match in _PATTERN.finditer(text.lower()):
        token = match.group(1)
        if token in _CASE_SENSITIVE:
            # require the ORIGINAL text to be capitalised ("US", "U.S.", "UK"), so the
            # pronoun "us" is not read as the United States
            original = text[match.start(1):match.end(1)]
            if original != original.upper():
                continue
        code = COUNTRY_ISO.get(token)
        if code:
            found.add(code)
    return sorted(found)
