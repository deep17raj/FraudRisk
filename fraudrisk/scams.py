"""Named scam archetypes, recognised from their own wording.

The generic cues in `classify.py` look for amounts, payee novelty, velocity and geography.
The classic consumer and business scams describe themselves in language none of that touches:
a safe-account call, an invoice redirection, a SIM swap, a romance scam, an overpayment
refund, mule recruitment, a gift-card demand. Without these, a richly detailed scenario -
the most valuable and most common kind of subject - produced no signal at all.

Design rule, learned the hard way
---------------------------------
Every alternative must carry TWO anchors in one clause: the actor or trigger, AND the
scam-specific action. A single evocative phrase is never enough, because each of these cues
scores MEDIUM or HIGH on its own and names a fraud pattern - so a misfire is the most
damaging kind of fabrication this service can commit.

The first version of this file broke that rule and fired on thirteen ordinary sentences:

    "Customer logged in to their secure account and paid a bill"     -> impersonation
    "The bank called the customer to confirm the transfer was genuine" -> impersonation
    "Payment received and the customer reset their password"         -> mule pass-through
    "The marketplace will take a commission on each sale"            -> mule recruitment
    "Her employer processed the monthly salary payment"              -> mule recruitment
    "Customer paid $9.99 for a dating app subscription"              -> romance fraud
    "Customer paid an unexpected medical bill at the hospital"       -> romance fraud
    "The customer asked for a gift card for their nephew"            -> gift-card demand
    "The customer's account details were updated after they moved"   -> invoice redirection
    "The customer no longer has the order number"                    -> SIM swap
    "The bank advised the customer how to respond to phishing"       -> coaching

Those exact sentences are now negatives in `tests/test_no_fabrication.py`. Lone phrases such
as `secure account`, `keep a commission`, `dating app`, `emergency medical bill` and a bare
`number` were deleted outright.

Each entry is `(hint_field, positive_pattern, negative_pattern)` and is resolved by the same
tri-state helper as every other cue: stated, explicitly denied, or not mentioned.
"""
from __future__ import annotations

#: Verbs by which a victim is directed to pay - the "instructed" half of a social-engineering
#: clause. Used so "the bank called to confirm the transfer" cannot read as a safe-account scam.
_DIRECTED = (r"(?:told|asked|instruct\w+|persuad\w+|convinc\w+|pressur\w+|urged|directed|"
             r"demand\w+|coach\w+|talked\s+\w+\s+into|got\s+\w+\s+to)")
#: Money being moved out, as opposed to any use of the word "transfer".
_MOVE_OUT = r"(?:move|transfer|send|wire|withdraw|pay|remit)\w*"
_MONEY = r"(?:money|funds?|balance|savings|cash|payment|deposit|proceeds|amount)"

SCAM_CUES: list[tuple[str, str, str]] = [
    (
        "impersonation_of_authority",
        # someone CLAIMING to be an authority (the claim is the anchor, not the authority)
        r"\b(?:claim(?:ing|ed|s)?\s+to\s+be\s+(?:from|a)|pretend\w*\s+to\s+be|"
        r"purport\w*\s+to\s+be|posing\s+as|impersonat\w+|spoof\w+|fake\s+call\s+from)\b"
        r"[^.;]{0,70}\b(?:bank|fraud\s+team|security\s+team|police|hmrc|irs|tax|government|"
        r"revenue|court|utility|microsoft|amazon|apple|courier|delivery|investigator)\b"
        # ... or a caller DIRECTING the customer to move money to a "safe" account
        rf"|\b(?:safe|secure|holding|protected|safeguard\w*)\s+account\b[^.;]{{0,40}}"
        rf"\b{_MOVE_OUT}\b"
        rf"|\b{_MOVE_OUT}\b[^.;]{{0,60}}\b(?:safe|secure|holding|protected)\s+account\b"
        # an unsolicited CONTACT from "the bank" that then directs a payment. The contact
        # verb is required: without it, "coached on what to tell the bank about the transfer"
        # matched bank + coached + transfer and was called impersonation.
        rf"|\b(?:bank|fraud\s+team|police|security\s+team)\b[^.;]{{0,30}}"
        rf"\b(?:called|phoned|rang|contacted|messaged|texted|emailed)\b[^.;]{{0,45}}"
        rf"\b{_DIRECTED}\b[^.;]{{0,45}}\b{_MOVE_OUT}\b",
        r"\bverified\s+(?:the\s+)?call(?:er|)\b|\bcalled\s+the\s+bank\s+back\b"
        r"|\bconfirm\w*\s+(?:the\s+)?(?:transfer|payment)\s+was\s+genuine\b",
    ),
    (
        "payee_details_changed",
        # the details must be PAYMENT-ROUTING details: a customer's address or contact
        # details being updated is ordinary account maintenance
        r"\b(?:new|updated|changed|amended|different|revised|alternative)\s+"
        r"(?:bank|payee|beneficiary|remittance|sort\s*code|iban|routing|payment)\s*"
        r"(?:account\s*)?(?:details|information|instructions|number|numbers)\b"
        r"|\b(?:bank|payee|beneficiary|remittance|payment)\s*(?:account\s*)?"
        r"(?:details|instructions)\b[^.;]{0,35}"
        r"\b(?:changed|updated|amended|altered|switched|redirected|revised)\b"
        r"|\bchanged\s+the\s+(?:payroll|supplier|invoice|beneficiary|vendor)\s+"
        r"(?:bank\s+)?(?:details|account)\b",
        r"\bdetails\s+(?:were\s+)?(?:verified|confirmed)\s+by\s+(?:phone|callback|a\s+call)\b"
        r"|\bafter\s+(?:they\s+)?moved\s+house\b",
    ),
    (
        "otp_or_number_lost",
        r"\bsim[\s-]?swap\w*\b"
        # loss of the PHONE NUMBER or the codes specifically - never a bare "number"
        r"|\b(?:lost\s+access\s+to|no\s+longer\s+(?:has|have|receiv\w+|controls?)|hijack\w+|"
        r"ported\s+away|ported\s+out|taken\s+over)\b[^.;]{0,45}"
        r"\b(?:phone\s+number|mobile\s+number|sim\s*card|sim\b|email\s+account)\b"
        r"|\b(?:someone\s+else|a\s+third\s+party|an\s+attacker|somebody\s+else|"
        r"an\s+unknown\s+(?:person|party))\b[^.;]{0,50}"
        r"\b(?:receiv\w+|getting|intercept\w+|now\s+has)\b[^.;]{0,40}"
        r"\b(?:one[\s-]time\s+(?:passcode|password|code)s?|otps?|2fa\s+codes?|sms\s+codes?|"
        r"verification\s+codes?|security\s+codes?)\b",
        "",
    ),
    (
        "romance_or_grooming",
        # the relationship must be tied to SENDING MONEY or to never having met
        rf"\b(?:met\s+(?:them\s+)?online|met\s+on\s+(?:a\s+)?(?:dating|social)|"
        rf"dating\s+(?:contact|profile)|online\s+(?:partner|boyfriend|girlfriend|romance))\b"
        rf"[^.;]{{0,90}}\b(?:{_MOVE_OUT}|sending|sent|gave|gifted|lent)\b"
        rf"|\b(?:{_MOVE_OUT}|sending|sent|gave|lent)\b[^.;]{{0,90}}"
        rf"\b(?:met\s+(?:them\s+)?online|met\s+on\s+(?:a\s+)?(?:dating|social)|"
        rf"dating\s+(?:contact|profile)|online\s+(?:partner|boyfriend|girlfriend|romance))\b"
        rf"|\bnever\s+(?:video\s*-?\s*call\w*|met\s+(?:them\s+)?(?:in\s+person|face\s+to\s+face))\b"
        # an "emergency bill abroad" only counts when someone ELSE asked for the money
        rf"|\b(?:asked|requested|needed)\b[^.;]{{0,40}}"
        rf"\b(?:emergency|urgent|unexpected)\s+(?:medical|hospital|travel|customs|legal|visa)\s+"
        rf"(?:bill|fee|cost|payment|expense)s?\b",
        "",
    ),
    (
        "refund_to_other_instrument",
        # the refund must go somewhere OTHER than the instrument that paid
        r"\b(?:overpaid|overpayment|paid\s+too\s+much|excess\s+payment|sent\s+too\s+much)\b"
        r"[^.;]{0,90}\b(?:refund|return|send\s+back|repay)\w*\b[^.;]{0,40}"
        r"\b(?:different|another|new|alternative|separate|third[\s-]party|not\s+the\s+same)\s+"
        r"(?:card|account|bank|payee|method|wallet)\b"
        r"|\brefund\w*\b[^.;]{0,60}\bto\s+(?:a\s+)?(?:different|another|new|alternative|"
        r"separate|third[\s-]party)\s+(?:card|account|bank|payee|method|wallet)\b",
        r"\brefund\w*\b[^.;]{0,40}\b(?:original|same)\s+(?:card|account|method)\b"
        r"|\bto\s+the\s+card\s+they\s+paid\s+with\b",
    ),
    (
        "mule_recruitment",
        # recruitment to RECEIVE OTHERS' FUNDS into one's own account, not any commission
        r"\b(?:recruiter|job\s+(?:offer|ad|advert|advertisement)|work\s+from\s+home|"
        r"easy\s+money|money\s+transfer\s+agent)\b[^.;]{0,90}"
        r"\b(?:receiv\w+|accept\w+|process\w+|handle)\b[^.;]{0,50}"
        r"\b(?:payments?|funds?|money|transfers?)\b[^.;]{0,50}"
        r"\b(?:own|personal|their|his|her|your)\s+(?:bank\s+)?account\b"
        r"|\breceiv\w+\s+(?:client|customer|company|other\s+people'?s?)\s+(?:payments?|funds?|"
        r"money)\b[^.;]{0,50}\b(?:own|personal|their|his|her|your)\s+(?:bank\s+)?account\b"
        r"|\b(?:keep|retain|take)\s+(?:a\s+)?(?:commission|cut|percentage|fee|\d+\s?%)\b"
        r"[^.;]{0,60}\b(?:forward|send\s+on|pass\s+on|transfer\s+the\s+rest|remit)\w*\b"
        r"|\b(?:forward|send\s+on|pass\s+on|remit)\w*\b[^.;]{0,60}"
        r"\b(?:keep|retain|take)\s+(?:a\s+)?(?:commission|cut|percentage|fee|\d+\s?%)\b",
        "",
    ),
    (
        "untraceable_payment_demand",
        # the untraceable instrument must be demanded AS PAYMENT of something owed
        r"\b(?:pay(?:ment|ing)?|settle|fine|debt|tax|fee|bill|ransom|release\s+fee)\b"
        r"[^.;]{0,40}\b(?:in|with|via|using|by)\s+(?:gift\s+cards?|vouchers?|itunes|"
        r"steam\s+cards?|bitcoin|crypto(?:currency)?|usdt|monero|western\s+union|moneygram)\b"
        r"|\b(?:demand\w*|insist\w*|only\s+accept\w*|required|instructed)\b[^.;]{0,50}"
        r"\b(?:gift\s+cards?|vouchers?|itunes|steam\s+cards?|bitcoin|crypto(?:currency)?|"
        r"western\s+union|moneygram)\b"
        r"|\b(?:gift\s+cards?|vouchers?|bitcoin|crypto(?:currency)?|western\s+union|"
        r"moneygram)\b[^.;]{0,40}"
        r"\b(?:demand\w*|insist\w*|only\s+accept\w*|required|to\s+settle|to\s+pay\s+the)\b",
        r"\bgift\s+card\s+for\s+(?:their|his|her|my|a)\b",
    ),
    (
        "coached_against_bank",
        # the coaching must be about what to tell THE BANK
        r"\b(?:coach\w+|told|instruct\w+|advis\w+|script\w*|warned|prepped)\b[^.;]{0,55}"
        r"\b(?:what\s+to\s+(?:say|tell)|how\s+to\s+(?:answer|respond)|a\s+story)\b"
        r"[^.;]{0,45}\b(?:bank|branch|cashier|staff|fraud\s+team)\b"
        r"|\bif\s+(?:the\s+)?bank\s+(?:asks|asked|calls|called|phones|phoned|contacts|queries)\b"
        r"|\b(?:deny|lie\s+to|mislead|not\s+to\s+tell)\b[^.;]{0,35}"
        r"\b(?:bank|branch|staff|cashier)\b",
        r"\bhow\s+to\s+(?:respond|react)\s+to\s+phishing\b"
        r"|\bbank\s+advis\w+\b[^.;]{0,40}\bphishing\b",
    ),
    (
        "disputed_but_consistent",
        r"\b(?:did\s*n[o']?t|denies|denied|disput\w+|claims?\s+not\s+to\s+have)\b[^.;]{0,45}"
        r"\b(?:authoris\w+|authoriz\w+|make|made|recognis\w+|recogniz\w+)\b[^.;]{0,90}"
        r"\b(?:device|location|ip|pattern|behaviour|behavior)\b[^.;]{0,45}"
        r"\b(?:match\w*|usual|same|consistent|normal)\b"
        r"|\bshipped\b[^.;]{0,60}\b(?:then|afterwards|later)\b[^.;]{0,60}"
        r"\b(?:claimed|disputed|chargeback|stolen)\b",
        "",
    ),
    (
        "stolen_or_synthetic_identity",
        r"\b(?:stolen|synthetic|fabricated|fake|false|borrowed|made[\s-]up|invented)\s+"
        r"(?:identity|identities|documents?|id\b|name\s+and\s+address)\b"
        r"|\bidentity\s+(?:theft|fraud)\b"
        r"|\breal\s+name\b[^.;]{0,45}\b(?:fabricated|fake|false|invalid|made[\s-]up)\b",
        "",
    ),
    (
        "inbound_then_onward",
        # funds in, then OUT AGAIN somewhere else - never merely "sent to the customer",
        # and never the word "password" matching a `pass\w*` alternative
        rf"\b(?:funds?|money|payment|deposit)\s+(?:arrived|came\s+in|was\s+received|received)\b"
        rf"[^.;]{{0,80}}\b(?:{_MOVE_OUT}|forward\w+|passed\s+(?:it\s+)?on|sent\s+on)\b"
        rf"[^.;]{{0,45}}\b(?:on\b|onward|elsewhere|abroad|overseas|out\s+again|third\s+party|"
        rf"another\s+account|someone\s+else|most\s+of\s+it|crypto|cash)\b"
        rf"|\bunknown\s+sender\b[^.;]{{0,80}}\b(?:{_MOVE_OUT}|forward\w+)\b"
        rf"|\basked\s+to\s+(?:{_MOVE_OUT}|forward)\w*\s+most\s+of\s+it\b",
        "",
    ),
]
