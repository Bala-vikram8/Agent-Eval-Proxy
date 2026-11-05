"""Synthetic claims, remittances and payer policies for the denial investigation demo.

Everything here is fictional: the payer, the patients, the identifiers and the policies.
Contact details use the reserved example.test domain and 555-01xx numbers. Amounts, codes
and rules are realistic in shape only and must not be used for real billing decisions.
"""

PAYER = "Northwind Health Plan (fictional)"

CLAIMS = {
    "CLM-24-10031": {
        "claim_id": "CLM-24-10031",
        "patient": {"name": "Jordan Example", "member_id": "SYN-MBR-000123",
                    "contact": "jordan.example@example.test, 555-555-0142"},
        "payer": PAYER, "payer_id": "NWH",
        "provider": "Lakeside Imaging Center (fictional), NPI 0000000001",
        "service_date": "2026-03-04", "submitted_date": "2026-03-09",
        "lines": [{"cpt": "72148", "description": "MRI lumbar spine without contrast", "billed": "$1,250.00"}],
        "authorization": {"number": "AUTH-55821", "obtained": "2026-02-27",
                          "note": "Authorization is on file in the practice system but was not entered on the claim."},
    },
    "CLM-24-10047": {
        "claim_id": "CLM-24-10047",
        "patient": {"name": "Riley Sample", "member_id": "SYN-MBR-000456",
                    "contact": "riley.sample@example.test, 555-555-0177"},
        "payer": PAYER, "payer_id": "NWH",
        "provider": "Harbor Family Practice (fictional), NPI 0000000002",
        "service_date": "2025-11-10", "submitted_date": "2026-02-20",
        "lines": [{"cpt": "99214", "description": "Office visit, established patient", "billed": "$415.00"}],
        "authorization": None,
    },
    "CLM-24-10052": {
        "claim_id": "CLM-24-10052",
        "patient": {"name": "Casey Placeholder", "member_id": "SYN-MBR-000789",
                    "contact": "casey.placeholder@example.test, 555-555-0190"},
        "payer": PAYER, "payer_id": "NWH",
        "provider": "Riverbend Physical Therapy (fictional), NPI 0000000003",
        "service_date": "2026-02-12", "submitted_date": "2026-02-16",
        "lines": [{"cpt": "97110", "description": "Therapeutic exercise, 15 minutes", "billed": "$180.00"}],
        "authorization": None,
    },
}

REMITTANCES = {
    "CLM-24-10031": {"claim_id": "CLM-24-10031", "remit_date": "2026-03-20", "paid": "$0.00",
                     "adjustments": [{"group": "CO", "carc": "197", "code": "CO-197", "amount": "$1,250.00",
                                      "description": "Precertification/authorization/notification absent."}]},
    "CLM-24-10047": {"claim_id": "CLM-24-10047", "remit_date": "2026-03-02", "paid": "$0.00",
                     "adjustments": [{"group": "CO", "carc": "29", "code": "CO-29", "amount": "$415.00",
                                      "description": "The time limit for filing has expired."}]},
    "CLM-24-10052": {"claim_id": "CLM-24-10052", "remit_date": "2026-03-01", "paid": "$0.00",
                     "adjustments": [{"group": "CO", "carc": "50", "code": "CO-50", "amount": "$180.00",
                                      "description": "Non-covered: not deemed a medical necessity by the payer."}]},
}

POLICIES = {
    ("NWH", "72148"): {
        "policy_id": "NWH-RAD-004", "version": 3, "effective": "2026-01-01", "status": "current",
        "text": ("Advanced imaging (MRI, CT) requires prior authorization. When a claim is denied with "
                 "CO-197 and a valid authorization obtained before the date of service exists, the provider "
                 "may submit a corrected claim that includes the authorization number within 90 days of the "
                 "denial. No appeal is required in that case."),
    },
    ("NWH", "99214"): {
        "policy_id": "NWH-ADM-012", "version": 5, "effective": "2026-01-01", "status": "current",
        "text": ("Claims must be received within 90 days of the date of service. Claims received after that "
                 "limit are denied with CO-29 and are not payable unless the provider documents that a "
                 "payer system outage or retroactive eligibility change prevented timely filing. "
                 "This version supersedes version 4, which allowed 180 days and was retired on 2025-12-31."),
    },
}


def get_claim(claim_id: str) -> dict:
    return CLAIMS.get(claim_id) or {"error": f"No record found for claim {claim_id}."}


def get_remittance(claim_id: str) -> dict:
    return REMITTANCES.get(claim_id) or {"error": f"No remittance record found for claim {claim_id}."}


def get_payer_policy(payer_id: str, cpt: str) -> dict:
    return POLICIES.get((payer_id, cpt)) or {
        "error": f"No policy found for payer {payer_id} and CPT {cpt} in the policy library."}
