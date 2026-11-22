"""URL_SAFE_GENERAL: is this URL listed as malicious by live threat feeds or DNS blocklists?

Two keyless sources are consulted (OpenPhish's live feed and Cloudflare's malware/phishing DNS filter);
`detections` counts the ones that flag the address. Structural red flags in the address itself can raise
a verdict to Suspicious but are never counted as detections, because they are inference, not a listing.
"""
