"""Plain-language questions in a parameter field: the extractors find the parameters and never touch clean values."""
from __future__ import annotations

import pytest

from intentkit.ask import (dns_type_of, filings_parts, hours_of, is_sentence, place_of, registry_company, sanctions_name,
                           sla_parts)

CLEAN = ["London", "Rio de Janeiro, Brazil", "Bank of America Corporation", "John Smith", "google.com", "Apple Inc.",
         "USDC", "New York City", "Saint Petersburg, Russia", "GitHub"]


@pytest.mark.parametrize("value", CLEAN)
def test_a_clean_value_is_never_altered(value):
    assert not is_sentence(value)
    assert sanctions_name(value) == value and place_of(value) == value and registry_company(value) == value
    assert sla_parts(value, "") == (value, "") and filings_parts(value, "") == (value, "")


@pytest.mark.parametrize("text,expected", [
    ("Is Viktor Bout on a sanctions list?", "Viktor Bout"),
    ("Is Huawei sanctioned?", "Huawei"),
    ("Does Rosneft appear on the OFAC SDN list?", "Rosneft"),
    ("Screen Bank Melli Iran against OFAC lists", "Bank Melli Iran"),
    ("Check whether Mahan Air is sanctioned", "Mahan Air"),
    ("Is the company Wagner Group listed on OFAC?", "Wagner Group"),
    ("Is Bank Melli Iran on the SDN list?", "Bank Melli Iran"),
])
def test_the_sanctioned_party_is_found_in_a_question(text, expected):
    assert sanctions_name(text) == expected


@pytest.mark.parametrize("text,place,hours", [
    ("What is the weather forecast for London over the next 48 hours?", "London", 48),
    ("Will Riyadh exceed 40 degrees in the next 24 hours?", "Riyadh", 24),
    ("Forecast for New York City tomorrow?", "New York City", 24),
    ("What's the weather like in Paris, France this week?", "Paris, France", 168),
    ("How hot will it get in Karachi over the next 3 days?", "Karachi", 72),
    ("Is it going to rain in Seattle tonight?", "Seattle", 24),
    ("What is the current temperature in Tokyo?", "Tokyo", None),
])
def test_the_place_and_window_are_found_in_a_weather_question(text, place, hours):
    assert place_of(text) == place and hours_of(text) == hours


@pytest.mark.parametrize("text,expected", [
    ("Does google.com have an MX record?", "MX"), ("Does github.com have a TXT record?", "TXT"),
    ("Does example.com have an A record?", "A"), ("Does cloudflare.com publish an AAAA record?", "AAAA"),
    ("What are the NS records for mit.edu?", "NS"), ("Does gmail.com have SPF configured?", "TXT"),
    ("Does example.com have any mail server?", "MX"), ("Is there a reverse DNS entry for 8.8.8.8?", "PTR"),
    ("google.com", None), ("Does example.com exist?", None)])
def test_the_record_type_is_found_in_a_dns_question(text, expected):
    assert dns_type_of(text) == expected


@pytest.mark.parametrize("text,service,period", [
    ("Did GitHub meet 99.9% uptime over the last 30 days?", "GitHub", "over the last 30 days"),
    ("What was Cloudflare's uptime in the past 7 days?", "Cloudflare", "in the past 7 days"),
    ("Is Discord meeting its 99.95% SLA this month?", "Discord", "this month"),
    ("Did the Wikipedia service meet 99.9% availability over the last year?", "Wikipedia", "over the last year"),
])
def test_the_service_and_period_are_found_in_an_sla_question(text, service, period):
    assert sla_parts(text, "") == (service, period)


def test_a_period_given_separately_is_not_overridden():
    assert sla_parts("Did GitHub meet 99.9% uptime over the last 30 days?", "last 7 days")[1] == "last 7 days"


@pytest.mark.parametrize("text,entity,period", [
    ("How many SEC filings did Tesla make in the last 12 months?", "Tesla", "in the last 12 months"),
    ("How many 8-K filings did Apple file in 2024?", "Apple", "in 2024"),
    ("List the SEC filings that mention artificial intelligence in the last 6 months", "artificial intelligence",
     "in the last 6 months"),
    ("Has Microsoft filed any 10-K since January 2025?", "Microsoft", "since January 2025"),
])
def test_the_entity_and_period_are_found_in_a_filings_question(text, entity, period):
    assert filings_parts(text, "") == (entity, period)


@pytest.mark.parametrize("text,expected", [
    ("Look up Microsoft Corporation in the corporate registry", "Microsoft Corporation"),
    ("What is the LEI of Apple Inc.?", "Apple Inc."), ("Find Siemens AG in the GLEIF registry", "Siemens AG"),
    ("Is Stripe, Inc. an active registered company?", "Stripe, Inc."), ("Tell me the registered address of Shell plc", "Shell plc"),
    ("Look up Microsoft Corporation.", "Microsoft Corporation"),
])
def test_the_company_is_found_in_a_registry_question(text, expected):
    assert registry_company(text) == expected
