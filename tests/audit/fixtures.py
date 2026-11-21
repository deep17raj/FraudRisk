"""Regression corpus: 15 contracts, each with the model output a good extraction would give.

Each case pairs a contract with a plausible extraction and the verdict the roll-up must
reach. The contracts are deliberately varied in shape - numbered and unnumbered, wrapped and
flat, UK and US dating, a 256-byte snippet of the kind an on-chain request carries - because
the citation grounding has to hold across all of them, not just on tidy input.

`expect` is the verdict; `must_cite` are substrings that have to appear somewhere in the
returned citations, which is how we check the audit cited the clause that actually decides it.
"""
from __future__ import annotations

from tests.audit.conftest import extraction

# --- 1-3: clearly compliant -------------------------------------------------------------

C_SIMPLE_SUPPLY = """SUPPLY AGREEMENT

1.1 The Supplier shall deliver 500 units of Product X to the Buyer's warehouse at 14 Mill
Road, Leeds, by 1 March 2026.
1.2 The Buyer shall pay GBP 10,000 within thirty (30) days of the date of invoice.
1.3 The Supplier shall maintain product liability insurance of not less than GBP 2,000,000
throughout the term.
"""

C_NDA = """MUTUAL NON-DISCLOSURE AGREEMENT

2.1 Each party shall keep the other's Confidential Information secret for five (5) years
from the Effective Date.
2.2 Each party shall return or destroy all Confidential Information within fourteen (14)
days of written request.
"""

C_PERFORMED = """SETTLEMENT AGREEMENT

3.1 The Respondent shall pay the Claimant GBP 25,000 on or before 30 April 2025.
3.2 The parties record that the sum of GBP 25,000 was received by the Claimant on
28 April 2025 and that the Claimant's claims are discharged.
"""

# --- 4-6: clear breach ------------------------------------------------------------------

C_LATE_REPORT = """CONSULTANCY AGREEMENT

4.1 The Consultant shall deliver the final report to the Client by 1 October 2025.
4.2 The parties record that the final report was delivered on 15 November 2025.
4.3 The Client shall pay the Fee of GBP 5,000 within fourteen (14) days of delivery.
"""

C_SHORT_PAYMENT = """LOAN AGREEMENT

5.1 The Borrower shall repay the principal sum of USD 100,000 in full on 1 June 2025.
5.2 It is acknowledged that the Borrower paid only USD 60,000 on 1 June 2025 and that no
further payment has been made.
"""

C_CONTRADICTION = """LICENCE AGREEMENT

6.1 The Licensee shall pay a royalty of fifteen per cent (15%) of Net Revenue each quarter.
6.2 The royalty payable under this Agreement shall be five per cent (5%) of Net Revenue,
payable annually.
6.3 No other royalty is payable.
"""

# --- 7-9: partially compliant -----------------------------------------------------------

C_MIXED_LEASE = """LEASE

7.1 The Tenant shall pay rent of GBP 1,200 per calendar month in advance on the first day
of each month.
7.2 The parties record that rent has been paid in full to 1 January 2026.
7.3 The Tenant shall keep the Premises in good and substantial repair.
7.4 The Landlord shall insure the Premises against fire.
"""

C_SLA = """SERVICE LEVEL AGREEMENT

8.1 The Provider shall maintain availability of at least 99.9% measured monthly.
8.2 The Provider shall credit the Customer 10% of monthly fees for each full hour of
unplanned downtime.
8.3 The Customer shall report incidents through the support portal within 24 hours.
8.4 Availability for January 2026 was 99.95%.
"""

C_UNNUMBERED = """AGREEMENT FOR SALE OF GOODS

The Seller agrees to transfer title in the Equipment to the Purchaser upon receipt of the
full purchase price. The Purchaser agrees to pay the sum of EUR 45,000 no later than
14 February 2026. The Seller shall deliver the Equipment within seven days of payment.
Risk in the Equipment passes to the Purchaser on delivery.
"""

# --- 10-12: unclear, the real terms are elsewhere ---------------------------------------

C_SCHEDULE_A = """SERVICES AGREEMENT

9.1 The Supplier shall provide the Services as set out in Schedule A.
9.2 The Client shall pay the Fees in accordance with Schedule B.
9.3 This Agreement is governed by the law of England and Wales.
"""

C_SOW = """MASTER SERVICES AGREEMENT

10.1 The Contractor shall perform the work more particularly described in the Statement of
Work agreed between the parties from time to time.
10.2 Payment terms shall be as set forth in Exhibit 1.
"""

C_NOT_ATTACHED = """PURCHASE ORDER TERMS

11.1 The Vendor shall supply the items listed in Appendix C (not attached).
11.2 Delivery dates shall be as per Schedule 2, to be agreed.
"""

# --- 13-15: unclear, edge shapes --------------------------------------------------------

C_NOT_A_CONTRACT = """Hi there - following up on our call. I think the project went well and
everyone seemed pleased with the outcome. Let me know if you want to grab coffee next week.
"""

C_TITLE_PAGE = """DATED 14 JANUARY 2026

ACME HOLDINGS LIMITED
and
BETA TRADING LLP

________________________________

SHARE PURCHASE AGREEMENT

________________________________

Freshfield & Co, 1 London Wall
"""

#: The 256-byte shape an on-chain Telegraph request actually carries.
C_SNIPPET = ("Clause 4: Supplier shall deliver by 2026-03-01. Clause 5: Buyer shall pay "
             "within 30 days of invoice. Delivery occurred 2026-03-14.")


CASES: list[dict] = [
    {
        "name": "simple_supply_all_specified",
        "contract": C_SIMPLE_SUPPLY,
        "expect": "Compliant",
        "must_cite": ["deliver 500 units of Product X"],
        "payload": extraction([
            {"party": "The Supplier", "duty": "deliver 500 units of Product X to the Buyer's "
             "warehouse at 14 Mill Road, Leeds", "deadline": "1 March 2026", "status": "met",
             "clause_ref": "1.1",
             "quote": "The Supplier shall deliver 500 units of Product X to the Buyer's "
                      "warehouse at 14 Mill Road, Leeds, by 1 March 2026"},
            {"party": "The Buyer", "duty": "pay GBP 10,000", "status": "met",
             "deadline": "within thirty (30) days of the date of invoice", "clause_ref": "1.2",
             "quote": "The Buyer shall pay GBP 10,000 within thirty (30) days of the date of "
                      "invoice"},
            {"party": "The Supplier", "duty": "maintain product liability insurance of not "
             "less than GBP 2,000,000", "status": "met", "clause_ref": "1.3",
             "quote": "The Supplier shall maintain product liability insurance of not less "
                      "than GBP 2,000,000 throughout the term"},
        ]),
    },
    {
        "name": "mutual_nda_all_specified",
        "contract": C_NDA,
        "expect": "Compliant",
        "must_cite": ["keep the other"],
        "payload": extraction([
            {"party": "Each party", "duty": "keep the other's Confidential Information secret",
             "deadline": "five (5) years from the Effective Date", "status": "met",
             "clause_ref": "2.1",
             "quote": "Each party shall keep the other's Confidential Information secret for "
                      "five (5) years from the Effective Date"},
            {"party": "Each party", "duty": "return or destroy all Confidential Information",
             "deadline": "within fourteen (14) days of written request", "status": "met",
             "clause_ref": "2.2",
             "quote": "Each party shall return or destroy all Confidential Information within "
                      "fourteen (14) days of written request"},
        ]),
    },
    {
        "name": "settlement_performance_recorded_in_text",
        "contract": C_PERFORMED,
        "expect": "Compliant",
        "must_cite": ["28 April 2025"],
        "payload": extraction([
            {"party": "The Respondent", "duty": "pay the Claimant GBP 25,000",
             "deadline": "on or before 30 April 2025", "status": "met", "clause_ref": "3.1",
             "reason": "the text records receipt on 28 April 2025, inside the deadline",
             "quote": "The parties record that the sum of GBP 25,000 was received by the "
                      "Claimant on 28 April 2025"},
        ]),
    },
    {
        "name": "report_delivered_late",
        "contract": C_LATE_REPORT,
        "expect": "Non-compliant",
        "must_cite": ["15 November 2025"],
        "payload": extraction([
            {"party": "The Consultant", "duty": "deliver the final report to the Client",
             "deadline": "1 October 2025", "status": "breached", "clause_ref": "4.1",
             "reason": "the text records delivery on 15 November 2025, 45 days late",
             "quote": "The parties record that the final report was delivered on "
                      "15 November 2025"},
            {"party": "The Client", "duty": "pay the Fee of GBP 5,000",
             "deadline": "within fourteen (14) days of delivery", "status": "unverifiable",
             "clause_ref": "4.3", "reason": "the text does not record payment",
             "quote": "The Client shall pay the Fee of GBP 5,000 within fourteen (14) days of "
                      "delivery"},
        ]),
    },
    {
        "name": "loan_repaid_short",
        "contract": C_SHORT_PAYMENT,
        "expect": "Non-compliant",
        "must_cite": ["USD 60,000"],
        "payload": extraction([
            {"party": "The Borrower", "duty": "repay the principal sum of USD 100,000 in full",
             "deadline": "1 June 2025", "status": "breached", "clause_ref": "5.1",
             "reason": "the text records USD 60,000 paid and no further payment",
             "quote": "the Borrower paid only USD 60,000 on 1 June 2025 and that no further "
                      "payment has been made"},
        ]),
    },
    {
        "name": "royalty_rate_contradiction",
        "contract": C_CONTRADICTION,
        "expect": "Non-compliant",
        "must_cite": ["fifteen per cent"],
        "payload": extraction([
            {"party": "The Licensee", "duty": "pay a royalty of fifteen per cent (15%) of Net "
             "Revenue", "deadline": "each quarter", "status": "breached", "clause_ref": "6.1",
             "reason": "clause 6.2 sets the same royalty at 5% payable annually",
             "quote": "The Licensee shall pay a royalty of fifteen per cent (15%) of Net "
                      "Revenue each quarter"},
        ], contradiction=True,
            note="clauses 6.1 and 6.2 state different royalty rates and payment frequencies"),
    },
    {
        "name": "lease_rent_paid_repair_unverified",
        "contract": C_MIXED_LEASE,
        "expect": "Partially compliant",
        "must_cite": ["paid in full to 1 January 2026"],
        "payload": extraction([
            {"party": "The Tenant", "duty": "pay rent of GBP 1,200 per calendar month in "
             "advance", "deadline": "the first day of each month", "status": "met",
             "clause_ref": "7.1", "reason": "the text records rent paid in full to 1 Jan 2026",
             "quote": "The parties record that rent has been paid in full to 1 January 2026"},
            {"party": "The Tenant", "duty": "keep the Premises in good and substantial repair",
             "status": "unverifiable", "clause_ref": "7.3",
             "reason": "the condition of the Premises is not addressed in the text",
             "quote": "The Tenant shall keep the Premises in good and substantial repair"},
            {"party": "The Landlord", "duty": "insure the Premises against fire",
             "status": "unverifiable", "clause_ref": "7.4",
             "reason": "no policy or evidence of insurance appears in the text",
             "quote": "The Landlord shall insure the Premises against fire"},
        ]),
    },
    {
        "name": "sla_availability_met_reporting_unverified",
        "contract": C_SLA,
        "expect": "Partially compliant",
        "must_cite": ["99.95%"],
        "payload": extraction([
            {"party": "The Provider", "duty": "maintain availability of at least 99.9% measured "
             "monthly", "status": "met", "clause_ref": "8.1",
             "reason": "the text records 99.95% for January 2026, above the threshold",
             "quote": "Availability for January 2026 was 99.95%"},
            {"party": "The Customer", "duty": "report incidents through the support portal",
             "deadline": "within 24 hours", "status": "unverifiable", "clause_ref": "8.3",
             "reason": "no incident reports appear in the text",
             "quote": "The Customer shall report incidents through the support portal within "
                      "24 hours"},
        ]),
    },
    {
        "name": "unnumbered_sale_of_goods",
        "contract": C_UNNUMBERED,
        "expect": "Partially compliant",
        "must_cite": ["EUR 45,000"],
        "payload": extraction([
            {"party": "The Purchaser", "duty": "pay the sum of EUR 45,000",
             "deadline": "no later than 14 February 2026", "status": "met",
             "quote": "The Purchaser agrees to pay the sum of EUR 45,000 no later than "
                      "14 February 2026"},
            {"party": "The Seller", "duty": "deliver the Equipment",
             "deadline": "within seven days of payment", "status": "unverifiable",
             "reason": "delivery is conditional on payment, which the text does not record",
             "quote": "The Seller shall deliver the Equipment within seven days of payment"},
        ]),
    },
    {
        "name": "services_deferred_to_schedule_a_and_b",
        "contract": C_SCHEDULE_A,
        "expect": "Unclear",
        "must_cite": ["Schedule A"],
        "payload": extraction([
            {"party": "The Supplier", "duty": "provide the Services", "status": "unverifiable",
             "clause_ref": "9.1", "reason": "the Services are defined only in Schedule A",
             "quote": "The Supplier shall provide the Services as set out in Schedule A"},
            {"party": "The Client", "duty": "pay the Fees", "status": "unverifiable",
             "clause_ref": "9.2", "reason": "the Fees are defined only in Schedule B",
             "quote": "The Client shall pay the Fees in accordance with Schedule B"},
        ], missing_dependency=True, note="the Services and Fees are both defined elsewhere"),
    },
    {
        "name": "msa_deferred_to_statement_of_work",
        "contract": C_SOW,
        "expect": "Unclear",
        "must_cite": ["Statement of"],
        "payload": extraction([
            {"party": "The Contractor", "duty": "perform the work", "status": "unverifiable",
             "clause_ref": "10.1", "reason": "the work is described only in the Statement of Work",
             "quote": "The Contractor shall perform the work more particularly described in the "
                      "Statement of Work"},
        ], missing_dependency=True),
    },
    {
        "name": "purchase_order_appendix_not_attached",
        "contract": C_NOT_ATTACHED,
        "expect": "Unclear",
        "must_cite": ["Appendix C"],
        "payload": extraction([
            {"party": "The Vendor", "duty": "supply the items listed in Appendix C",
             "status": "unverifiable", "clause_ref": "11.1",
             "reason": "Appendix C is expressly not attached",
             "quote": "The Vendor shall supply the items listed in Appendix C (not attached)"},
        ], missing_dependency=True),
    },
    {
        "name": "not_a_contract_at_all",
        "contract": C_NOT_A_CONTRACT,
        "expect": "Unclear",
        "must_cite": [],
        "payload": extraction([], missing_key_terms=True,
                              note="this is correspondence, not a contract"),
    },
    {
        "name": "title_page_only",
        "contract": C_TITLE_PAGE,
        "expect": "Unclear",
        "must_cite": [],
        "payload": extraction([], missing_key_terms=True,
                              note="a cover page with no operative terms"),
    },
    {
        "name": "onchain_sized_snippet_late_delivery",
        "contract": C_SNIPPET,
        "expect": "Non-compliant",
        "must_cite": ["Delivery occurred 2026-03-14"],
        "payload": extraction([
            {"party": "Supplier", "duty": "deliver", "deadline": "2026-03-01",
             "status": "breached", "clause_ref": "Clause 4",
             "reason": "delivery occurred on 2026-03-14, after the 2026-03-01 deadline",
             "quote": "Delivery occurred 2026-03-14"},
            {"party": "Buyer", "duty": "pay", "deadline": "within 30 days of invoice",
             "status": "unverifiable", "clause_ref": "Clause 5",
             "reason": "no payment is recorded in the text",
             "quote": "Buyer shall pay within 30 days of invoice"},
        ]),
    },
]
