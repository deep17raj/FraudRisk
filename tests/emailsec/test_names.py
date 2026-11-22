from __future__ import annotations

import pytest

from emailsec.names import normalize_domain, normalize_record_type


class TestDomain:
    @pytest.mark.parametrize("raw,expected", [
        ("example.com", "example.com"),
        ("  Example.COM  ", "example.com"),
        ("example.com.", "example.com"),
        ("user@example.com", "example.com"),
        ("Jane Doe <jane@Example.org>", "example.org"),
        ("https://mail.example.com/inbox?x=1", "mail.example.com"),
        ("example.com:25", "example.com"),
        ("_dmarc.example.com", "_dmarc.example.com"),
        ("selector1._domainkey.example.com", "selector1._domainkey.example.com"),
        ("Does example.com publish valid SPF?", "example.com"),
        ("check postmaster@example.net please", "example.net"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("com", "com"),
    ])
    def test_valid_names(self, raw, expected):
        assert normalize_domain(raw) == expected

    @pytest.mark.parametrize("raw", [
        "", "   ", "not a domain!", "-bad.example.com", "bad-.example.com", "a..b",
        "x" * 64 + ".com", ("a" * 60 + ".") * 5 + "com", "Does it work", "!!!", "ex$ample.com",
    ])
    def test_invalid_names(self, raw):
        assert normalize_domain(raw) is None

    def test_a_plain_word_in_a_sentence_is_not_a_domain(self):
        assert normalize_domain("Does this domain accept mail") is None


class TestRecordType:
    @pytest.mark.parametrize("raw,expected", [
        ("MX", "MX"), ("mx", "MX"), (" txt ", "TXT"), ("AAAA", "AAAA"), ("SPF", "SPF"), ("15", "15"), ("16", "16"),
    ])
    def test_accepted(self, raw, expected):
        assert normalize_record_type(raw) == expected

    @pytest.mark.parametrize("raw", ["", "NOPE", "DMARC", "0", "70000", "MX; DROP", "-1"])
    def test_rejected(self, raw):
        assert normalize_record_type(raw) is None
