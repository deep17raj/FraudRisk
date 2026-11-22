"""Provider payloads for 8.8.8.8, recorded from the live services on 2026-11-22.

The five disagree, which is the point: ipinfo, ipapi.co, freeipapi and ipwho put the address in the
San Francisco Bay Area, while geojs places it in the middle of the United States.
"""
IPINFO = {"ip": "8.8.8.8", "hostname": "dns.google", "city": "Mountain View", "region": "California",
          "country": "US", "loc": "38.0088,-122.1175", "org": "AS15169 Google LLC", "postal": "94043",
          "timezone": "America/Los_Angeles", "readme": "https://ipinfo.io/missingauth", "anycast": True}

IPWHO = {"ip": "8.8.8.8", "success": True, "type": "IPv4", "continent": "North America",
         "country": "United States", "country_code": "US", "region": "California", "city": "San Jose",
         "latitude": 37.3393939, "longitude": -121.8949553,
         "connection": {"asn": 15169, "org": "Google LLC", "isp": "Google LLC", "domain": "google.com"}}

IPAPI_CO = {"ip": "8.8.8.8", "city": "Mountain View", "region": "California", "country_name": "United States",
            "country_code": "US", "latitude": 37.42301, "longitude": -122.083352, "org": "Google LLC",
            "asn": "AS15169"}

FREEIPAPI = {"ipVersion": 4, "ipAddress": "8.8.8.8", "latitude": 37.422, "longitude": -122.085,
             "countryName": "United States", "countryCode": "US", "cityName": "Mountain View",
             "regionName": "California", "asn": "15169", "asnOrganization": "Google LLC", "isProxy": False}

GEOJS = {"accuracy": 1000, "asn": 15169, "country": "United States", "country_code": "US", "ip": "8.8.8.8",
         "latitude": "37.751", "longitude": "-97.822", "organization": "AS15169 Google LLC",
         "organization_name": "Google LLC"}

#: hostname -> payload, for the mock transport
ALL_BY_HOST = {
    "ipinfo.io": IPINFO,
    "ipwho.is": IPWHO,
    "ipapi.co": IPAPI_CO,
    "free.freeipapi.com": FREEIPAPI,
    "get.geojs.io": GEOJS,
}
