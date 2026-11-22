"""EMAIL_SECURITY_GENERAL: did a DNS lookup for a mail domain succeed (DNS status 0, NOERROR)?

Telegraph's published response format for this intent has a single field, `lookup_ok`, defined as
"DNS Status 0 (NOERROR) means yes, any other status means no". The intent's prose also mentions SPF,
DKIM and DMARC analysis, but no field exists to carry that, so none is returned.
"""
