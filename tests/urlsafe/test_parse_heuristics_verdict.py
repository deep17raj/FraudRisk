"""The pure parts: finding the URL, structural red flags, and the evidence -> verdict mapping."""
from __future__ import annotations

import pytest

from urlsafe.heuristics import Flag, analyse, split_host
from urlsafe.parse import parse_target
from urlsafe.verdict import Evidence, Listing, Verdict, decide


class TestParse:
    @pytest.mark.parametrize("text,url,host", [
        ("https://example.com/a?b=1", "https://example.com/a?b=1", "example.com"),
        ("example.com", "http://example.com", "example.com"),
        ("example.com/login", "http://example.com/login", "example.com"),
        ("  HTTP://Example.COM:8080/x  ", "HTTP://Example.COM:8080/x", "example.com"),
        ("Check https://login-secure-update.example.com against threat feeds, is it listed?",
         "https://login-secure-update.example.com", "login-secure-update.example.com"),
        ("is (https://example.com/path). safe?", "https://example.com/path", "example.com"),
        ("is www.example.com safe", "http://www.example.com", "www.example.com"),
        ("http://203.0.113.9/login", "http://203.0.113.9/login", "203.0.113.9"),
        ("http://[2001:db8::1]/x", "http://[2001:db8::1]/x", "2001:db8::1"),
        ("bücher.example/x", "http://bücher.example/x", "xn--bcher-kva.example"),
    ])
    def test_finds_the_target(self, text, url, host):
        t = parse_target(text)
        assert t is not None and t.host == host
        assert t.url == url

    @pytest.mark.parametrize("text", ["", "   ", "hello world", "localhost", "just words, no dots", "http://", "a..b"])
    def test_none_when_there_is_no_url(self, text):
        assert parse_target(text) is None

    def test_raw_is_the_url_found_not_the_whole_sentence(self):
        assert parse_target("Is https://example.com/x. safe to click?").raw == "https://example.com/x"

    def test_ip_and_userinfo_flags(self):
        assert parse_target("http://203.0.113.9/x").is_ip and not parse_target("http://example.com").is_ip
        assert parse_target("https://paypal.com@evil.example/login").userinfo
        assert not parse_target("https://example.com/@user").userinfo


class TestHeuristics:
    def ids(self, url):
        return {f.id for f in analyse(parse_target(url))}

    @pytest.mark.parametrize("url", [
        "https://www.paypal.com/signin", "https://login.microsoft.com", "https://accounts.google.com",
        "https://amazon.de", "https://google.co.uk", "https://appleinsider.com", "https://microsoftonline.com",
        "https://paypalobjects.com", "https://secure.example.com", "https://example.xyz", "https://github.io",
        "https://en.wikipedia.org/wiki/PayPal", "https://developer.apple.com",
    ])
    def test_legitimate_addresses_raise_no_strong_flag(self, url):
        weight = sum(f.weight for f in analyse(parse_target(url)))
        assert weight < 0.6, (url, [f.id for f in analyse(parse_target(url))])

    @pytest.mark.parametrize("url,expected", [
        ("https://paypal.com.account-verify.example", "brand_lure"),
        ("https://secure-paypal.login-update.top", "brand_lure"),
        ("http://apple-id-support.xyz", "brand_term"),
        ("https://xn--pypal-4ve.com", "punycode"),
        ("https://paypal.com@evil.example/login", "userinfo"),
        ("http://203.0.113.9/paypal", "ip_host"),
        ("http://a-b-c-d.example.com", "many_hyphens"),
        ("https://one.two.three.four.five.example.com", "deep_subdomains"),
        ("https://secure-login-verify.tk", "lure_on_tld"),
    ])
    def test_lure_shapes_are_flagged(self, url, expected):
        assert expected in self.ids(url)

    def test_brand_lure_alone_is_enough_for_suspicious_but_a_free_tld_alone_is_not(self):
        assert sum(f.weight for f in analyse(parse_target("https://paypal.com.account-verify.example"))) >= 0.6
        assert sum(f.weight for f in analyse(parse_target("https://example.tk"))) < 0.6

    def test_multi_label_suffixes_are_understood(self):
        assert split_host("login.google.co.uk") == ("google.co.uk", "google", "co.uk")
        assert split_host("a.b.example.com") == ("example.com", "example", "com")


def ev(listings=(), flags=(), checked=("OpenPhish", "Cloudflare Security DNS")) -> Evidence:
    return Evidence(listings=list(listings), flags=list(flags), checked=list(checked))


URL_HIT = Listing("OpenPhish", "url", "this exact URL is on its live phishing feed")
HOST_HIT = Listing("OpenPhish", "host", "this site is on its live phishing feed")
DNS_HIT = Listing("Cloudflare Security DNS", "host", "it blocks this host as malware or phishing")


class TestVerdict:
    def test_an_exact_url_listing_is_malicious_on_its_own(self):
        o = decide(ev([URL_HIT]))
        assert o.verdict is Verdict.MALICIOUS and o.detections == 1 and "OpenPhish" in o.summary

    def test_two_sources_are_malicious(self):
        o = decide(ev([HOST_HIT, DNS_HIT]))
        assert o.verdict is Verdict.MALICIOUS and o.detections == 2
        assert "OpenPhish" in o.summary and "Cloudflare Security DNS" in o.summary

    def test_one_host_level_source_is_only_suspicious(self):
        for hit in (HOST_HIT, DNS_HIT):
            o = decide(ev([hit]))
            assert o.verdict is Verdict.SUSPICIOUS and o.detections == 1 and "single source" in o.summary

    def test_strong_structure_without_any_listing_is_suspicious_with_zero_detections(self):
        o = decide(ev(flags=[Flag("brand_lure", 0.6, "it carries the brand name")]))
        assert o.verdict is Verdict.SUSPICIOUS and o.detections == 0 and "brand name" in o.summary

    def test_weak_flags_do_not_make_it_suspicious_but_are_mentioned(self):
        o = decide(ev(flags=[Flag("abused_tld", 0.3, "it sits on .xyz")]))
        assert o.verdict is Verdict.SAFE and "it sits on .xyz" in o.summary

    def test_checked_and_clean_is_safe_and_names_what_was_checked(self):
        o = decide(ev())
        assert o.verdict is Verdict.SAFE and o.detections == 0
        assert "OpenPhish and Cloudflare Security DNS" in o.summary and "not a guarantee" in o.summary

    def test_one_source_checked_is_still_safe_and_names_only_it(self):
        o = decide(ev(checked=("Cloudflare Security DNS",)))
        assert o.verdict is Verdict.SAFE and "OpenPhish" not in o.summary

    def test_nothing_checked_is_unknown_never_safe(self):
        o = decide(ev(checked=()))
        assert o.verdict is Verdict.UNKNOWN and o.detections == 0

    def test_the_threshold_is_configurable(self):
        flags = [Flag("abused_tld", 0.3, "x")]
        assert decide(ev(flags=flags), suspicious_weight=0.25).verdict is Verdict.SUSPICIOUS
        assert decide(ev(flags=flags), suspicious_weight=0.6).verdict is Verdict.SAFE

    def test_detections_count_listings_only_never_flags(self):
        assert decide(ev(flags=[Flag("a", 0.9, "x"), Flag("b", 0.9, "y")])).detections == 0
