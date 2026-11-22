"""Small helpers shared by the keyless network-check intents (ipgeo, emailsec, urlsafe, malware).

The older packages (fraudrisk, factcheck, audit) each carry their own copy of the cache, logging and
circuit breaker. The four newer intents have identical needs, so they share one implementation here
instead of four more copies. Nothing in the older packages imports this module.
"""
