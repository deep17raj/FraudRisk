"""Compare organisation and person names across spellings, word orders and legal forms."""
from __future__ import annotations

import re
import unicodedata

_NONWORD = re.compile(r"[^0-9a-z]+")
#: Words that carry no identity ("Ltd", "The", "Aktiengesellschaft"): ignored when comparing, so "Siemens AG"
#: matches "Siemens Aktiengesellschaft" and "Acme Ltd" matches "ACME LIMITED".
NOISE = frozenset({
    "the", "of", "and", "ltd", "limited", "llc", "inc", "incorporated", "co", "corp", "corporation", "company",
    "sa", "ag", "aktiengesellschaft", "kg", "kgaa", "gmbh", "plc", "jsc", "ooo", "oao", "zao", "pjsc", "bv", "nv",
    "srl", "spa", "fze", "fzco", "llp", "lp", "se", "ab", "as", "oy", "aps", "sas", "sarl", "ltda", "pte", "pty",
    "bhd", "sdn", "societe", "anonyme",
})


def normalize(text: str) -> tuple[str, ...]:
    """Lower-case ASCII words of a name, accents and punctuation removed, sorted, noise words dropped."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    words = [w for w in _NONWORD.split(s) if w]
    kept = [w for w in words if w not in NOISE] or words
    return tuple(sorted(kept))
