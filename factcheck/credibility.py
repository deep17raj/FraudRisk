"""Stage 3 - source credibility scoring.

A maintained allow/deny list plus a few cheap heuristics. Deliberately not ML. Extend the lists in
place, or point CREDIBILITY_OVERRIDES at a JSON file:
    {"high": ["example.org"], "medium": [...], "low": [...], "reject": [...]}
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from .models import Tier

log = logging.getLogger("factcheck.credibility")

# Weight each tier contributes to evidence (multiplied by relevance * stance confidence).
TIER_WEIGHT = {Tier.REJECTED: 0.0, Tier.LOW: 0.25, Tier.MEDIUM: 0.6, Tier.HIGH: 1.0}

FACT_CHECKERS = {
    "snopes.com", "politifact.com", "factcheck.org", "fullfact.org", "leadstories.com",
    "africacheck.org", "checkyourfact.com", "factcheck.afp.com", "reuters.com/fact-check",
    "apnews.com/hub/ap-fact-check", "truthorfiction.com", "factcheckni.org", "boomlive.in",
    "factly.in", "maldita.es", "newtral.es", "correctiv.org", "demagog.org.pl", "check4spam.com",
    "washingtonpost.com/politics/fact-checker", "mediabiasfactcheck.com", "healthfeedback.org",
    "sciencefeedback.co", "climatefeedback.org", "logically.ai", "verafiles.org", "factcheck.kz",
}

HIGH = {
    # wire services & fact-checkers
    "reuters.com", "apnews.com", "afp.com", "snopes.com", "politifact.com", "factcheck.org",
    "fullfact.org", "leadstories.com", "africacheck.org", "checkyourfact.com", "healthfeedback.org",
    "sciencefeedback.co", "climatefeedback.org", "boomlive.in", "factly.in", "maldita.es",
    "newtral.es", "correctiv.org", "truthorfiction.com", "factcheck.afp.com",
    # reference works
    "wikipedia.org", "wikidata.org", "britannica.com",
    # intergovernmental / science bodies
    "who.int", "un.org", "unesco.org", "worldbank.org", "imf.org", "oecd.org", "europa.eu",
    "nasa.gov", "noaa.gov", "nih.gov", "cdc.gov", "fda.gov", "nature.com", "science.org",
    "thelancet.com", "nejm.org", "pnas.org", "cell.com", "bmj.com", "royalsociety.org",
    "nationalacademies.org", "ipcc.ch", "cern.ch", "esa.int", "iea.org", "jstor.org",
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "arxiv.org",
}

MEDIUM = {
    "nytimes.com", "washingtonpost.com", "theguardian.com", "bbc.com", "bbc.co.uk", "npr.org",
    "pbs.org", "cnn.com", "wsj.com", "ft.com", "economist.com", "bloomberg.com", "aljazeera.com",
    "usatoday.com", "time.com", "smithsonianmag.com", "nationalgeographic.com", "scientificamerican.com",
    "newscientist.com", "abc.net.au", "cbc.ca", "dw.com", "france24.com", "politico.com", "axios.com",
    "theatlantic.com", "newyorker.com", "latimes.com", "forbes.com", "cnbc.com", "nbcnews.com",
    "cbsnews.com", "abcnews.go.com", "sciencenews.org", "livescience.com", "space.com", "history.com",
    "vox.com", "propublica.org", "pewresearch.org", "brookings.edu", "statista.com", "ourworldindata.org",
    "theconversation.com", "independent.co.uk", "telegraph.co.uk", "thehindu.com", "japantimes.co.jp",
    "straitstimes.com", "scmp.com", "irishtimes.com", "lemonde.fr", "spiegel.de", "mayoclinic.org",
    "clevelandclinic.org", "webmd.com", "healthline.com", "investopedia.com", "coindesk.com",
}

LOW = {
    "reddit.com", "quora.com", "facebook.com", "x.com", "twitter.com", "tiktok.com", "instagram.com",
    "youtube.com", "medium.com", "substack.com", "blogspot.com", "wordpress.com", "tumblr.com",
    "pinterest.com", "answers.com", "ask.com", "yahoo.com", "linkedin.com", "telegram.org",
}

REJECT = {
    # fabricated-news / conspiracy mills
    "infowars.com", "naturalnews.com", "beforeitsnews.com", "yournewswire.com", "newspunch.com",
    "worldnewsdailyreport.com", "empirenews.net", "neonnettle.com", "thegatewaypundit.com",
    "dcclothesline.com", "usasupreme.com", "realnewsrightnow.com", "whatdoesitmean.com",
    "veteranstoday.com", "globalresearch.ca", "gnews.org", "bigleaguepolitics.com",
    "madworldnews.com", "freewestmedia.com", "thedailysheeple.com", "activistpost.com",
    "healthnutnews.com", "realfarmacy.com", "collective-evolution.com", "davidicke.com",
    # satire (a joke is not evidence)
    "theonion.com", "babylonbee.com", "clickhole.com", "thebeaverton.com", "waterfordwhispersnews.com",
    "newsthump.com", "dailymash.co.uk", "borowitz-report.com", "reductress.com", "thespoof.com",
    # state propaganda outlets
    "rt.com", "sputniknews.com", "sputnikglobe.com",
}

_HIGH_SUFFIXES = (".gov", ".mil", ".edu", ".ac.uk", ".gov.uk", ".gov.au", ".gov.in", ".gc.ca", ".gouv.fr",
                  ".edu.au", ".ac.jp", ".go.jp", ".gov.sg", ".int")
_GOV_RE = re.compile(r"\.(gov|gob|gouv|govt)(\.[a-z]{2})?$|\.(edu|ac)\.[a-z]{2}$")
_SUSPICIOUS = re.compile(r"(?:^|[.\-])(truth|patriot|exposed|wakeup|prepper|conspiracy|illuminati|nwo)", re.I)


class CredibilityScorer:
    def __init__(self, overrides_path: str = "") -> None:
        self.high = set(HIGH)
        self.medium = set(MEDIUM)
        self.low = set(LOW)
        self.reject = set(REJECT)
        if overrides_path:
            self._load(overrides_path)

    def _load(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for key, target in (("high", self.high), ("medium", self.medium), ("low", self.low), ("reject", self.reject)):
                target.update(d.lower() for d in data.get(key, []))
        except Exception as exc:  # never let a bad override file stop the service
            log.warning("could not load credibility overrides %s: %s", path, exc)

    @staticmethod
    def domain_of(url: str) -> str:
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return ""
        return host[4:] if host.startswith("www.") else host

    @staticmethod
    def _match(domain: str, table: set[str]) -> bool:
        parts = domain.split(".")
        return any(".".join(parts[i:]) in table for i in range(len(parts) - 1))

    def tier_for(self, url: str) -> Tier:
        domain = self.domain_of(url)
        if not domain:
            return Tier.LOW
        if self._match(domain, self.reject):
            return Tier.REJECTED  # reject always wins, even over a suffix match
        if self._match(domain, self.high) or domain.endswith(_HIGH_SUFFIXES) or _GOV_RE.search(domain):
            return Tier.HIGH
        if self._match(domain, self.medium):
            return Tier.MEDIUM
        if self._match(domain, self.low):
            return Tier.LOW
        if _SUSPICIOUS.search(domain):
            return Tier.LOW
        return Tier.LOW  # unknown domains are low-trust by default

    def is_fact_checker(self, url: str) -> bool:
        domain = self.domain_of(url)
        return any(self._match(domain, {f.split("/")[0]}) for f in FACT_CHECKERS)

    @staticmethod
    def weight(tier: Tier) -> float:
        return TIER_WEIGHT[tier]
