from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from intentkit.names import normalize
from intents.sanctions.app import SanctionsPipeline, SanctionsResponse, create_app
from intents.sanctions.config import Settings
from intents.sanctions.lists import BASE, CONS_LIST, SDN_LIST, SanctionsIndex, parse_aliases, parse_primary
from tests.intents.helpers import FakeHttp, StubPipeline, fast

SDN = '''306,"BANCO NACIONAL DE CUBA",-0- ,"CUBA",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,"a.k.a. 'BNC'."
35096,"PUTIN, Vladimir Vladimirovich","individual","RUSSIA-EO14024",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,"DOB 07 Oct 1952"
22790,"MADURO MOROS, Nicolas","individual","VENEZUELA",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0-
555,"ACME TRADING LIMITED",-0- ,"SDGT",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0-
'''
ALT = '''306,220,"aka","NATIONAL BANK OF CUBA",-0-
555,901,"fka","ZETA IMPORTS",-0-
'''
CONS = '''9640,"ABU TEIR, Mohammed","individual","NS-PLC",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,"DOB 1951."
'''
CONS_ALT = '''9640,1,"aka","ABU TAYR, Muhammad",-0-
'''


def lists_http(**over) -> FakeHttp:
    routes = {BASE + "SDN.CSV": SDN, BASE + "ALT.CSV": ALT, BASE + "CONS_PRIM.CSV": CONS, BASE + "CONS_ALT.CSV": CONS_ALT}
    routes.update(over)
    return FakeHttp(routes)


async def loaded(fake=None, **kw) -> SanctionsPipeline:
    fake = fake or lists_http()
    pl = SanctionsPipeline(fast(Settings, **kw), fake.client())
    await pl.index.load(pl.client)
    return pl


async def screen(pl, name):
    return await pl.answer({"name": name})


def test_names_are_compared_by_their_words():
    assert normalize("ABU TEIR, Mohammed") == normalize("Mohammed Abu Teir")
    assert normalize("Müller & Söhne GmbH") == ("muller", "sohne")
    assert normalize("The Acme Ltd.") == normalize("ACME LIMITED") == ("acme",)
    assert normalize("") == () and normalize("Ltd") == ("ltd",)       # a name made only of noise keeps its words


def test_the_csv_formats_parse():
    e = parse_primary(SDN, SDN_LIST)
    assert e["306"].name == "BANCO NACIONAL DE CUBA" and e["306"].kind == "" and e["306"].programs == "CUBA"
    assert e["35096"].kind == "individual"
    parse_aliases(ALT, e)
    assert e["306"].aliases == ["NATIONAL BANK OF CUBA"]
    assert "header,row" not in parse_primary("header,row\n" + SDN, SDN_LIST)


async def test_exact_reordered_and_noise_variants_match_fully():
    pl = await loaded()
    for name in ("Banco Nacional de Cuba", "BANCO NACIONAL DE CUBA", "Vladimir Vladimirovich Putin",
                 "PUTIN, Vladimir Vladimirovich", "Acme Trading Ltd", "Mohammed Abu Teir"):
        r = await screen(pl, name)
        assert r.matched and r.match_score == 1.0 and r.matches, name
    assert (await screen(pl, "Banco Nacional de Cuba")).matches[0].startswith("BANCO NACIONAL DE CUBA (OFAC SDN;")


async def test_aliases_are_matched_and_reported_under_their_listed_name():
    pl = await loaded()
    r = await screen(pl, "National Bank of Cuba")
    assert r.matched and r.matches[0].startswith("BANCO NACIONAL DE CUBA")
    r2 = await screen(pl, "Muhammad Abu Tayr")
    assert r2.matched and "OFAC Consolidated (non-SDN)" in r2.matches[0]


async def test_a_longer_listed_name_still_matches_a_shorter_query_of_two_words():
    pl = await loaded()
    r = await screen(pl, "Vladimir Putin")
    assert r.matched and r.match_score == 0.9


async def test_small_misspellings_still_match_and_unrelated_names_do_not():
    pl = await loaded()
    assert (await screen(pl, "Banco Nacional de Cubaa")).matched
    for name in ("Taylor Swift", "Apple Inc", "Banco Santander", "Nico"):
        r = await screen(pl, name)
        assert not r.matched and r.matches == [] and r.lists, name


async def test_a_single_common_word_is_not_a_match_for_a_longer_listed_name():
    assert not (await screen(await loaded(), "Putin")).matched          # one word: not a subset match


async def test_matches_are_only_listed_above_the_threshold():
    pl = await loaded(match_threshold=0.99)
    r = await screen(pl, "Vladimir Putin")
    assert not r.matched and r.matches == [] and r.match_score == 0.9


async def test_a_question_in_the_name_field_screens_the_party_it_names():
    pl = await loaded()
    for question in ("Is Banco Nacional de Cuba on a sanctions list?", "Does Banco Nacional de Cuba appear on the OFAC SDN list?"):
        r = await screen(pl, question)
        assert r.matched is True and r.name == "Banco Nacional de Cuba", question
    assert (await screen(pl, "Is Zzyzx Qwerty on a sanctions list?")).matched is False


async def test_the_lists_checked_are_reported():
    r = await screen(await loaded(), "Anyone")
    assert r.lists == [SDN_LIST, CONS_LIST] and r.match_score is not None


async def test_when_a_list_could_not_be_downloaded_only_the_other_one_is_reported():
    pl = await loaded(lists_http(**{BASE + "CONS_PRIM.CSV": None, BASE + "CONS_ALT.CSV": None}))
    r = await screen(pl, "Banco Nacional de Cuba")
    assert r.matched and r.lists == [SDN_LIST]


async def test_with_no_lists_loaded_nothing_is_claimed_clear():
    pl = await loaded(lists_http(**{k: None for k in (BASE + "SDN.CSV", BASE + "ALT.CSV", BASE + "CONS_PRIM.CSV", BASE + "CONS_ALT.CSV")}))
    r = await screen(pl, "Vladimir Putin")
    assert (r.matched, r.matches, r.lists, r.match_score, r.name) == (False, [], [], None, "Vladimir Putin")


async def test_a_failed_refresh_keeps_the_previous_lists():
    fake = lists_http()
    pl = await loaded(fake)
    for k in list(fake.routes):
        fake.routes[k] = None
    await pl.index.load(pl.client)
    assert (await screen(pl, "Banco Nacional de Cuba")).matched


async def test_a_list_that_failed_to_download_is_retried_within_seconds_not_at_the_ttl():
    sdn_down = {BASE + "SDN.CSV": None, BASE + "ALT.CSV": None}
    fake = lists_http(**sdn_down)
    pl = await loaded(fake)
    assert pl.index.missing == (SDN_LIST,) and pl.index.lists == (CONS_LIST,)
    fake.routes[BASE + "SDN.CSV"], fake.routes[BASE + "ALT.CSV"] = SDN, ALT       # the source recovers
    assert not pl.index.stale                                                   # too soon to try again
    pl.index.attempted_at -= 60
    assert pl.index.stale
    r = await screen(pl, "Banco Nacional de Cuba")                              # this request triggers the retry and waits for it
    assert r.matched and r.lists == [SDN_LIST, CONS_LIST] and pl.index.missing == ()


async def test_a_failed_refresh_of_one_list_keeps_that_lists_earlier_entries():
    fake = lists_http()
    pl = await loaded(fake)
    fake.routes[BASE + "SDN.CSV"] = None
    fake.routes[BASE + "ALT.CSV"] = None
    fake.routes[BASE + "CONS_PRIM.CSV"] = CONS.replace("ABU TEIR, Mohammed", "NEW PERSON, Added")
    await pl.index.load(pl.client)
    assert pl.index.lists == (SDN_LIST, CONS_LIST) and pl.index.missing == ()
    assert (await screen(pl, "Banco Nacional de Cuba")).matched
    assert (await screen(pl, "Added New Person")).matched


async def test_each_download_is_tried_twice_before_giving_up():
    calls = []

    def flaky(request):
        calls.append(request.url.path)
        return None if len(calls) == 1 else SDN
    fake = lists_http(**{BASE + "SDN.CSV": flaky})
    pl = await loaded(fake)
    assert pl.index.lists == (SDN_LIST, CONS_LIST) and calls.count("/api/PublicationPreview/exports/SDN.CSV") == 2
    dead = lists_http(**{BASE + "SDN.CSV": None})
    await loaded(dead)
    assert dead.urls().count(BASE + "SDN.CSV") == 2


async def test_an_empty_name_is_answered_without_a_search():
    r = await screen(await loaded(), "")
    assert r == SanctionsResponse()


async def test_repeat_queries_are_served_from_the_cache(monkeypatch):
    pl = await loaded()
    calls = []
    orig = pl.index.search
    monkeypatch.setattr(pl.index, "search", lambda *a: calls.append(1) or orig(*a))
    await screen(pl, "Banco Nacional de Cuba")
    await screen(pl, "banco nacional de cuba")
    assert len(calls) == 1


def test_scoring_primitives():
    q = normalize("Vladimir Putin")
    assert SanctionsIndex.score(q, normalize("PUTIN, Vladimir")) == 1.0
    assert SanctionsIndex.score(q, normalize("Zhang Wei")) < 0.5


def test_http_contract():
    stub = StubPipeline(SanctionsResponse, name="x", matched=True, matches=["a"], lists=[SDN_LIST], match_score=1.0)
    with TestClient(create_app(fast(Settings), pipeline=stub)) as c:
        r = c.post("/sanctions-screening", json={"name": "x"})
        assert set(r.json()) == {"name", "matched", "matches", "lists", "match_score"}
        assert c.post("/sanctions-screening", content=b"x").status_code == 200
