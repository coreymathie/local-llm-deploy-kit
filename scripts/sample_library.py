# Corey Mathie, 2026
# ruff: noqa: E501  (document text reads better unwrapped)
"""
Write the document library of Cypress Harbor Credit Union, a fictional credit union, for the console's
demo mode: demo/library/<collection>/*.md and the catalog demo/data/library.json.

Every document is invented for the demo. It is written to read like a mid-size credit union's internal
procedures (owners, review dates, concrete limits and deadlines) so the assistant has something real to
answer from, but none of it is legal, regulatory or HR guidance, and the console marks the whole
workspace as fictional. The thirteen documents that predate the library stay in demo/ (the tests and
the RAG eval fixtures use them); the catalog adds their titles and owners.

    python scripts/sample_library.py            # write the library and the catalog
    python scripts/sample_library.py --check    # exit 1 if anything committed differs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "demo" / "library"
CATALOG = ROOT / "demo" / "data" / "library.json"

COLLECTIONS = [
    {
        "name": "policies",
        "label": "Staff policies",
        "description": "HR, IT, security and conduct policies for every employee",
    },
    {
        "name": "member-services",
        "label": "Member services",
        "description": "Procedures for branch and contact-center staff",
    },
    {"name": "lending", "label": "Lending", "description": "Loan products, underwriting and servicing"},
    {"name": "compliance", "label": "Compliance", "description": "Regulatory procedures, privacy and fair lending"},
    {
        "name": "branch-operations",
        "label": "Branch operations",
        "description": "Cash, security and facilities for the 11 branches",
    },
]

# The documents in demo/ that the library started with: (file, title, owner, department, reviewed, version, type)
ORIGINALS = [
    (
        "sample-remote-work-policy.md",
        "Remote Work Policy",
        "Priya Shah",
        "Human resources",
        "2026-06-02",
        "3.1",
        "docx",
    ),
    (
        "sample-data-retention-policy.md",
        "Data Retention and Records Policy",
        "Elena Petrov",
        "Compliance",
        "2026-04-15",
        "5.0",
        "pdf",
    ),
    (
        "sample-travel-expense-policy.md",
        "Travel and Expense Policy",
        "Grace Liu",
        "Finance",
        "2026-01-20",
        "4.2",
        "pdf",
    ),
    (
        "sample-card-dispute-procedure.md",
        "Card Dispute Procedure",
        "Rosa Jimenez",
        "Member services",
        "2026-08-11",
        "2.4",
        "docx",
    ),
    (
        "sample-wire-transfer-verification.md",
        "Wire Transfer Verification",
        "Thomas Reed",
        "Payments operations",
        "2026-07-07",
        "3.0",
        "pdf",
    ),
    (
        "sample-consumer-lending-guidelines.md",
        "Consumer Lending Guidelines",
        "Andre Baptiste",
        "Lending",
        "2026-09-29",
        "6.1",
        "pdf",
    ),
    (
        "sample-member-identity-verification.md",
        "Member Identity Verification Standard",
        "Rosa Jimenez",
        "Member services",
        "2026-05-19",
        "2.0",
        "docx",
    ),
    (
        "sample-complaint-handling-policy.md",
        "Member Complaint Handling Policy",
        "Elena Petrov",
        "Compliance",
        "2026-03-30",
        "2.2",
        "pdf",
    ),
    (
        "sample-branch-security-procedures.md",
        "Branch Security Procedures",
        "Victor Ramos",
        "Facilities and security",
        "2026-02-10",
        "4.0",
        "pdf",
    ),
    (
        "sample-ai-acceptable-use-policy.md",
        "Acceptable Use of AI Assistants",
        "Kevin Tran",
        "IT and digital banking",
        "2026-08-25",
        "1.1",
        "docx",
    ),
    (
        "restricted-hr-compensation-bands.md",
        "Compensation Bands 2026",
        "Priya Shah",
        "Human resources",
        "2026-03-02",
        "2026.1",
        "xlsx",
    ),
    (
        "restricted-payments-oncall-runbook.md",
        "Payments On-Call Runbook",
        "Dana Ortiz",
        "IT and digital banking",
        "2026-09-14",
        "7.3",
        "md",
    ),
    (
        "restricted-bsa-aml-escalation.md",
        "BSA/AML Escalation Procedure",
        "Marcus Bell",
        "Compliance and BSA",
        "2026-07-21",
        "3.2",
        "pdf",
    ),
]
ORIGINAL_ACL = {
    "restricted-hr-compensation-bands.md": ["group:hr"],
    "restricted-payments-oncall-runbook.md": ["group:engineering"],
    "restricted-bsa-aml-escalation.md": ["group:compliance"],
}

# (collection, slug, title, owner, department, reviewed, version, type, acl, body)
DOCS = [
    # ---------------- staff policies ----------------
    (
        "policies",
        "benefits-overview",
        "Employee Benefits Overview",
        "Priya Shah",
        "Human resources",
        "2026-01-05",
        "2026.1",
        "pdf",
        [],
        """
## Health coverage

Medical, dental and vision coverage starts on the first day of the month after the hire date. The credit union pays 85 percent of employee-only medical premiums and 70 percent for dependents.

## Retirement

The 401k retirement plan matches 100 percent of the first 4 percent of pay and 50 percent of the next 2 percent. Matching contributions vest after two years of service.

## Other benefits

Employees receive a $25 monthly wellness credit and free basic life insurance worth one times annual salary. Open enrollment runs from November 1 to November 15 each year.
""",
    ),
    (
        "policies",
        "paid-time-off-and-leave",
        "Paid Time Off and Leave",
        "Priya Shah",
        "Human resources",
        "2026-01-05",
        "3.4",
        "pdf",
        [],
        """
## Accrual

Full-time employees accrue 15 days of paid time off (PTO) in their first three years and 20 days from year four. Up to 5 unused days carry over into the next calendar year.

## Requesting time off

Request PTO in the HR system at least two weeks ahead for absences longer than two days. Branch staff coordinate with the branch manager so that every branch keeps two employees on site.

## Parental and medical leave

Eligible employees receive 8 weeks of paid parental leave within 12 months of a birth or adoption. Medical leave follows the Family and Medical Leave Act; HR confirms eligibility within 5 business days of a request.
""",
    ),
    (
        "policies",
        "holiday-calendar-2026",
        "Holiday Calendar 2026",
        "Priya Shah",
        "Human resources",
        "2025-11-20",
        "2026",
        "pdf",
        [],
        """
## Observed holidays

The credit union observes New Year's Day, Martin Luther King Jr. Day, Presidents Day, Memorial Day, Juneteenth, Independence Day, Labor Day, Columbus Day, Veterans Day, Thanksgiving Day and Christmas Day.

## Branch and contact-center hours

Branches and the member contact center are closed on observed holidays. The AI voice agent and online banking stay available, and holiday transfers to a person go to the next business day callback queue.

## Floating holiday

Each employee receives one floating holiday per year, which must be used by December 31 and does not carry over.
""",
    ),
    (
        "policies",
        "code-of-conduct",
        "Code of Conduct",
        "Elena Petrov",
        "Compliance",
        "2026-02-18",
        "4.0",
        "pdf",
        [],
        """
## Conflicts of interest

Employees may not process transactions, loans or account changes for themselves, relatives or anyone they live with. Hand those requests to a colleague and note the reason.

## Gifts

Employees may accept gifts from members or vendors worth up to $50 per year from any one source. Cash and gift cards are never acceptable.

## Reporting concerns

Report suspected misconduct to a manager, to compliance or to the anonymous ethics hotline. The credit union does not tolerate retaliation against anyone who reports in good faith.
""",
    ),
    (
        "policies",
        "password-and-mfa-standard",
        "Password and Multi-Factor Authentication Standard",
        "Kevin Tran",
        "IT and digital banking",
        "2026-06-30",
        "2.3",
        "pdf",
        [],
        """
## Passwords

Passwords must be at least 14 characters long. Use the company password manager; do not reuse a work password anywhere else.

## Multi-factor authentication

Multi-factor authentication is required for email, the core banking system, the VPN and every administrative console. Use the authenticator app; text-message codes are allowed only as a backup.

## Lockouts

Accounts lock after 5 failed sign-in attempts. The IT service desk unlocks accounts only after verifying the employee by video call or in person.
""",
    ),
    (
        "policies",
        "phishing-and-incident-reporting",
        "Phishing and Security Incident Reporting",
        "Kevin Tran",
        "IT and digital banking",
        "2026-07-12",
        "3.0",
        "pdf",
        [],
        """
## Suspicious email

Report suspicious email with the Report Phishing button in Outlook. Do not forward it to colleagues and do not click links or open attachments.

## Security incidents

Report a suspected security incident, including a lost or stolen laptop or phone, to the IT service desk within 24 hours by phone at extension 4357. After hours, the on-call security engineer is paged automatically.

## What happens next

The security team acknowledges an incident report within 1 hour during business hours. If member data may be involved, the privacy officer is notified the same day.
""",
    ),
    (
        "policies",
        "tuition-reimbursement",
        "Tuition Reimbursement Program",
        "Priya Shah",
        "Human resources",
        "2025-12-08",
        "1.4",
        "docx",
        [],
        """
## Eligibility

Employees with at least 6 months of service may apply for tuition reimbursement for job-related courses and degree programs.

## Amounts

The credit union reimburses up to $5,250 per calendar year for courses completed with a grade of B or better. Certification exams relevant to the employee's role are reimbursed in full.

## Applying

Submit the application before the course starts. Employees who leave within 12 months of a reimbursement repay a prorated share.
""",
    ),
    (
        "policies",
        "vendor-access-standard",
        "Vendor and Third-Party Access Standard",
        "Kevin Tran",
        "IT and digital banking",
        "2026-05-04",
        "2.1",
        "pdf",
        [],
        """
## Access requests

Vendor accounts are requested by the employee who owns the vendor relationship and approved by IT security. Every vendor account has an expiry date no more than 90 days out.

## Remote sessions

Vendors connect only through the monitored remote access gateway. Sessions into production systems are recorded and reviewed weekly.

## Reviews

Vendor management reviews critical vendors every year, including their latest SOC 2 report and their incident history.
""",
    ),
    (
        "policies",
        "change-management-policy",
        "Change Management Policy",
        "Dana Ortiz",
        "IT and digital banking",
        "2026-04-27",
        "2.6",
        "md",
        [],
        """
## Standard changes

Every production change has a ticket with a rollback plan and an approver who did not write the change.

## Change freeze

No production changes are made between December 20 and January 3, or in the two business days before and after month-end close, except emergency fixes approved by the CIO.

## Emergency changes

Emergency changes may be deployed first and documented within one business day. They are reviewed at the next change advisory board meeting.
""",
    ),
    (
        "policies",
        "business-continuity-plan-summary",
        "Business Continuity Plan Summary",
        "Victor Ramos",
        "Facilities and security",
        "2026-05-28",
        "5.1",
        "pdf",
        [],
        """
## Recovery targets

Core banking must be restored within 4 hours of a declared disaster, with no more than 15 minutes of lost data. Online banking and the member contact center must be restored within 8 hours.

## Alternate sites

If the Fort Lauderdale operations center is unavailable, operations move to the Weston branch training room. Employees with a company laptop can work remotely over the VPN.

## Testing

The plan is tested twice a year, including one full failover of the core system to the secondary data center.
""",
    ),
    # ---------------- member services ----------------
    (
        "member-services",
        "account-opening-checklist",
        "Account Opening Checklist",
        "Rosa Jimenez",
        "Member services",
        "2026-06-16",
        "3.2",
        "docx",
        [],
        """
## Required documents

Every new member provides one government-issued photo ID and a Social Security or Individual Taxpayer Identification Number. Non-resident applicants may use a passport with a visa.

## Membership eligibility

Anyone who lives, works, worships or attends school in Broward, Palm Beach or Miami-Dade County may join, along with family members of current members.

## Opening deposit

Membership requires a $5 deposit into a Share Savings account, which establishes the member's ownership share. Everyday Checking has no minimum opening deposit.
""",
    ),
    (
        "member-services",
        "fee-schedule",
        "Fee Schedule",
        "Rosa Jimenez",
        "Member services",
        "2026-07-01",
        "2026.2",
        "pdf",
        [],
        """
## Account fees

Everyday Checking has no monthly fee. Share Savings has no monthly fee when the balance stays at $5 or more.

## Service fees

An overdraft or returned item costs $25, with a limit of 3 fees per day. A stop payment costs $30, a domestic outgoing wire costs $25 and an international outgoing wire costs $45.

## Waivers

Member services representatives may waive one overdraft fee per member every 12 months. Further waivers need a supervisor's approval and a note in the CRM.
""",
    ),
    (
        "member-services",
        "overdraft-courtesy-pay",
        "Overdraft and Courtesy Pay",
        "Rosa Jimenez",
        "Member services",
        "2026-04-08",
        "2.0",
        "pdf",
        [],
        """
## Courtesy pay limits

Members in good standing for 90 days may be covered for overdrafts up to $500. Members who direct deposit their pay qualify for a $750 limit.

## Opting in

Courtesy pay applies to ATM and one-time debit card transactions only if the member opts in. Members can opt out at any time by phone, in online banking or at a branch.

## Repayment

An overdrawn account must be brought positive within 30 days. After 45 days negative, the account goes to the collections team.
""",
    ),
    (
        "member-services",
        "stop-payment-procedure",
        "Stop Payment Procedure",
        "Rosa Jimenez",
        "Member services",
        "2025-11-03",
        "1.6",
        "docx",
        [],
        """
## Placing a stop payment

A stop payment can be placed on a check that hasn't cleared by phone, in online banking or at a branch. Record the check number, the amount and the payee.

## Duration

An oral stop payment request lasts 14 days unless the member confirms it in writing. A written stop payment lasts 6 months and can be renewed. A stop payment costs $30.

## Electronic payments

To stop a recurring electronic payment, the member also tells the company to stop charging. Stop payments on electronic payments must be received at least 3 business days before the payment date.
""",
    ),
    (
        "member-services",
        "mobile-deposit-holds",
        "Mobile Deposit Limits and Holds",
        "Kevin Tran",
        "IT and digital banking",
        "2026-03-23",
        "2.2",
        "pdf",
        [],
        """
## Limits

Members can deposit up to $5,000 per day and $10,000 per month with mobile deposit. Members with 12 months of good standing may request higher limits.

## Availability

The first $225 of a mobile deposit is available the next business day. The rest is available on the second business day.

## Holds

Deposits from accounts open less than 30 days, or larger than $5,000, may be held for up to 7 business days. Write "For mobile deposit only at Cypress Harbor CU" on the back of the check.
""",
    ),
    (
        "member-services",
        "atm-and-debit-card-disputes",
        "ATM and Debit Card Error Resolution",
        "Rosa Jimenez",
        "Member services",
        "2026-08-11",
        "2.1",
        "pdf",
        [],
        """
## Reporting window

Members report electronic fund transfer errors within 60 days of the statement that showed the error.

## Investigation

Investigate within 10 business days. If more time is needed, give provisional credit within those 10 business days and finish within 45 days, or 90 days for point-of-sale and foreign transactions.

## ATM cash shortages

When an ATM dispenses less cash than requested, balance the ATM the same day and credit the member if the balance confirms the shortage.
""",
    ),
    (
        "member-services",
        "deceased-member-accounts",
        "Deceased Member Accounts",
        "Rosa Jimenez",
        "Member services",
        "2026-02-02",
        "1.8",
        "docx",
        [],
        """
## First notice

When a death is reported, place a deceased flag on every account, stop automatic payments out, and keep accepting incoming deposits until the estate is settled.

## Releasing funds

Accounts with a payable-on-death beneficiary are released to the beneficiary with a certified death certificate and the beneficiary's ID. Without a beneficiary, funds go to the personal representative named in letters of administration.

## Sensitivity

Offer condolences, avoid jargon and give the family a single point of contact. Send a summary of next steps in writing within 2 business days.
""",
    ),
    (
        "member-services",
        "power-of-attorney",
        "Power of Attorney Requests",
        "Rosa Jimenez",
        "Member services",
        "2025-10-14",
        "1.5",
        "pdf",
        [],
        """
## Accepting a power of attorney

A power of attorney must be signed by the member, notarized and signed by two witnesses as Florida law requires. Send every new power of attorney to the legal review queue before acting on it.

## Review time

Legal review completes within 5 business days. Until then, the agent may not transact on the member's accounts.

## Red flags

Escalate to the elder financial abuse team if the agent was recently added, is not a family member and requests large withdrawals, or if the member appears confused or pressured.
""",
    ),
    (
        "member-services",
        "dormant-accounts-and-escheatment",
        "Dormant Accounts and Unclaimed Property",
        "Elena Petrov",
        "Compliance",
        "2026-01-27",
        "2.0",
        "pdf",
        [],
        """
## Dormancy

An account with no member-initiated activity for 12 months is marked dormant. A dormant account fee is not charged.

## Contacting the member

Send a letter and an email at 12 months and again at 30 months of inactivity.

## Unclaimed property

After 5 years without member contact, Florida unclaimed property law requires the balance to be reported and sent to the state. Reports are filed by May 1 each year.
""",
    ),
    (
        "member-services",
        "contact-center-call-handling",
        "Contact Center Call Handling Standard",
        "Rosa Jimenez",
        "Member services",
        "2026-09-08",
        "4.3",
        "docx",
        [],
        """
## Service levels

The member contact center answers 80 percent of calls within 30 seconds once the AI agent transfers a caller. Callbacks requested after hours are returned by 11:00 the next business day.

## Transfers from the AI agent

Calls transferred by the AI agent arrive with a summary and the reason for the transfer. Do not ask the member to repeat information the summary already contains.

## Quality reviews

Supervisors review 5 calls per representative per month, including at least one transferred from the AI agent, and score them against the quality form.
""",
    ),
    (
        "member-services",
        "spanish-language-service",
        "Spanish-Language Service Standard",
        "Rosa Jimenez",
        "Member services",
        "2026-08-03",
        "1.2",
        "docx",
        [],
        """
## Availability

Spanish-language service is available in every branch during business hours and in the contact center from 8:00 to 19:00, Monday through Saturday. The AI agent answers in Spanish at any hour.

## Documents

Account agreements, the fee schedule and dispute forms are available in Spanish. If a document is not available in Spanish, tell the member and offer a bilingual representative to explain it.

## Interpreters

For Haitian Creole and other languages, use the phone interpreter service; never ask a member's child to interpret account details.
""",
    ),
    (
        "member-services",
        "address-change-procedure",
        "Address and Contact Change Procedure",
        "Rosa Jimenez",
        "Member services",
        "2026-05-19",
        "2.3",
        "docx",
        [],
        """
## Verification

Contact changes requested by phone require a one-time code to the mobile number already on file. If the mobile number itself is changing, verify in a branch or by video call.

## After a change

Send a confirmation letter to the old mailing address and an email to the old email address. Place a 7-day hold on new payees and outgoing wires.

## Account takeover signs

Escalate to the fraud team if a contact change is followed by a payment, a payee change or a wire request within the same call or day.
""",
    ),
    # ---------------- lending ----------------
    (
        "lending",
        "auto-loan-rate-sheet",
        "Auto Loan Rate Sheet",
        "Andre Baptiste",
        "Lending",
        "2026-10-01",
        "2026.10",
        "pdf",
        [],
        """
## New and used vehicles

New vehicle loans start at 5.24 percent APR for terms up to 60 months and 5.74 percent for 61 to 84 months. Used vehicles from model year 2020 or newer start at 5.89 percent APR.

## Discounts

Members with automatic payment from a Cypress Harbor checking account receive a 0.25 percent rate discount.

## Rate locks

Approved rates are locked for 30 days. Rates are updated on the first business day of each month.
""",
    ),
    (
        "lending",
        "heloc-product-guide",
        "Home Equity Line of Credit Product Guide",
        "Andre Baptiste",
        "Lending",
        "2026-08-18",
        "3.0",
        "pdf",
        [],
        """
## Limits

Home equity lines are available up to 85 percent combined loan-to-value, with a minimum line of $10,000 and a maximum of $400,000.

## Draw and repayment

The draw period is 10 years with interest-only payments, followed by a 20-year repayment period. The rate is the prime rate plus a margin based on credit score.

## Closing costs

The credit union pays closing costs on lines up to $250,000 if the line stays open for at least 36 months.
""",
    ),
    (
        "lending",
        "mortgage-referral-process",
        "Mortgage Referral Process",
        "Andre Baptiste",
        "Lending",
        "2026-03-09",
        "1.9",
        "docx",
        [],
        """
## Referrals

Member services and branch staff refer purchase and refinance mortgage questions to a mortgage loan officer. Do not quote mortgage rates.

## Response time

A mortgage loan officer contacts the member within 1 business day of the referral.

## First-time buyers

First-time home buyers are offered the Harbor Home program with down payments as low as 3 percent and a free homebuyer education class.
""",
    ),
    (
        "lending",
        "hardship-and-payment-deferral",
        "Hardship and Payment Deferral Program",
        "Andre Baptiste",
        "Lending",
        "2026-09-30",
        "2.5",
        "pdf",
        [],
        """
## Who qualifies

Members facing a temporary hardship, such as job loss, illness or a declared disaster, may request a deferral of consumer loan payments.

## Terms

A deferral moves up to 2 monthly payments to the end of the loan. Members may receive no more than 2 deferrals in any 12-month period. Interest continues to accrue during the deferral.

## Disaster relief

After a federally declared disaster in the credit union's counties, the lending manager may approve deferrals of up to 3 payments and waive the deferral fee.
""",
    ),
    (
        "lending",
        "skip-a-pay",
        "Skip-a-Pay Program",
        "Andre Baptiste",
        "Lending",
        "2026-10-02",
        "1.3",
        "pdf",
        [],
        """
## How it works

Members may skip one monthly payment on an eligible consumer loan in November or December for a $35 fee.

## Eligibility

The loan must be at least 6 months old and no payment may have been more than 30 days late in the past 12 months. Mortgages, home equity lines and credit cards are not eligible.

## Requests

Requests are submitted in online banking by November 15 for a November skip and by December 10 for a December skip.
""",
    ),
    (
        "lending",
        "early-stage-collections",
        "Early-Stage Collections Procedure",
        "Andre Baptiste",
        "Lending",
        "2026-06-23",
        "3.1",
        "docx",
        [],
        """
## Contact schedule

Members are contacted by text and email at 5 days past due and by phone at 15 days past due.

## Limits on contact

Collectors call between 8:00 and 21:00 in the member's time zone and make no more than 7 call attempts in 7 days for any one debt.

## Payment arrangements

Collectors may offer a payment arrangement of up to 3 months without manager approval. Longer arrangements, and any arrangement for a member who mentions a hardship, go to the hardship program.
""",
    ),
    (
        "lending",
        "credit-card-limit-guidelines",
        "Credit Card Limit Guidelines",
        "Andre Baptiste",
        "Lending",
        "2026-04-20",
        "2.0",
        "pdf",
        [],
        """
## Starting limits

Starting limits range from $1,000 to $25,000 based on credit score and income. Members with scores below 640 may be offered the secured card.

## Increases

Members may request an increase every 6 months. Increases of up to $5,000 can be approved automatically when the account has no late payments in the past 12 months.

## Secured card graduation

Secured card accounts are reviewed after 12 months of on-time payments for graduation to an unsecured card.
""",
    ),
    (
        "lending",
        "indirect-lending-dealer-standards",
        "Indirect Lending Dealer Standards",
        "Andre Baptiste",
        "Lending",
        "2025-12-15",
        "2.2",
        "pdf",
        [],
        """
## Approved dealers

Indirect auto loans are accepted only from dealers on the approved dealer list, reviewed every year.

## Dealer reserve

Dealer reserve is capped at 2 percentage points above the buy rate, and at 1.5 points for terms over 72 months.

## Monitoring

Lending reviews each dealer's loans every quarter for early payment defaults and for pricing differences that could indicate a fair lending risk.
""",
    ),
    (
        "lending",
        "small-business-lending-overview",
        "Small Business Lending Overview",
        "Andre Baptiste",
        "Lending",
        "2026-05-12",
        "1.7",
        "docx",
        [],
        """
## Products

Business members may apply for term loans, equipment loans and lines of credit from $10,000 to $500,000.

## Requirements

Applicants provide two years of business tax returns, year-to-date financial statements and a personal guarantee from every owner of 20 percent or more.

## Decisions

Applications up to $100,000 are decided by the business lending officer within 5 business days. Larger requests go to the credit committee, which meets every Tuesday.
""",
    ),
    # ---------------- compliance ----------------
    (
        "compliance",
        "privacy-and-glba-notice-procedure",
        "Privacy Notice and Information Sharing Procedure",
        "Elena Petrov",
        "Compliance",
        "2026-02-24",
        "2.4",
        "pdf",
        [],
        """
## Privacy notice

New members receive the privacy notice at account opening. The credit union shares member information only as the notice describes and does not sell member data.

## Opt-out requests

Members may opt out of information sharing with affiliates and third parties for marketing. Process opt-outs within 30 days of the request.

## Information requests

Requests for member information from third parties, including law enforcement, go to the compliance department. Never confirm whether someone is a member over the phone without verifying the caller.
""",
    ),
    (
        "compliance",
        "fair-lending-policy",
        "Fair Lending Policy",
        "Elena Petrov",
        "Compliance",
        "2026-06-09",
        "3.0",
        "pdf",
        [],
        """
## Commitment

Credit decisions are based only on creditworthiness. Never consider race, color, religion, national origin, sex, marital status, age, receipt of public assistance or other prohibited bases.

## Second review

Every declined consumer loan application is reviewed by a second underwriter before the adverse action notice is sent. Adverse action notices are sent within 30 days of a completed application.

## Monitoring

Compliance tests pricing and decisions for disparities every quarter and reports the results to the board's audit committee.
""",
    ),
    (
        "compliance",
        "elder-financial-abuse-reporting",
        "Elder Financial Abuse Reporting",
        "Elena Petrov",
        "Compliance",
        "2026-07-28",
        "2.1",
        "docx",
        [],
        """
## Warning signs

Warning signs include sudden large withdrawals, a new person who speaks for the member, changes to beneficiaries, and a member who seems confused or fearful.

## What to do

Delay a suspicious transaction for up to 15 business days while the case is reviewed, as Florida law allows for members aged 65 or older. Notify the compliance department the same day.

## Reporting

Compliance reports suspected abuse to the Florida Abuse Hotline and to adult protective services when the review supports it.
""",
    ),
    (
        "compliance",
        "udaap-and-marketing-review",
        "UDAAP and Marketing Review",
        "Elena Petrov",
        "Compliance",
        "2026-03-16",
        "1.8",
        "docx",
        [],
        """
## Review before publishing

Every advertisement, email campaign, social media post and website change that mentions rates, fees or terms is reviewed by compliance before publishing.

## Review time

Compliance reviews marketing within 3 business days of submission. Rate advertisements must show the APR and the date the rate was effective.

## Records

Keep a copy of every approved advertisement for 2 years.
""",
    ),
    (
        "compliance",
        "ofac-screening-procedure",
        "OFAC Screening Procedure",
        "Marcus Bell",
        "Compliance and BSA",
        "2026-07-21",
        "2.0",
        "pdf",
        ["group:compliance"],
        """
## When to screen

Screen every new member, every new signer and every outgoing wire against the OFAC sanctions lists before opening the account or sending the wire.

## Potential matches

Place a hold on a potential match and send it to the BSA officer within 1 hour. Do not tell the member why the transaction is on hold.

## Confirmed matches

Confirmed matches are blocked or rejected and reported to OFAC within 10 business days.
""",
    ),
    (
        "compliance",
        "regulatory-exam-preparation",
        "Regulatory Examination Preparation",
        "Elena Petrov",
        "Compliance",
        "2026-08-31",
        "1.4",
        "docx",
        [],
        """
## Exam schedule

The NCUA examines the credit union every 12 to 18 months, and the Florida Office of Financial Regulation joins on alternate cycles.

## Document requests

Department heads return the examiner's document request list within 10 business days. Compliance assembles and indexes the responses.

## AI systems

For the internal AI assistant, provide the model inventory and pins, the access review records, the audit log verification report and the AI acceptable use policy.
""",
    ),
    # ---------------- branch operations ----------------
    (
        "branch-operations",
        "cash-handling-limits",
        "Cash Handling Limits",
        "Victor Ramos",
        "Facilities and security",
        "2026-02-10",
        "3.3",
        "pdf",
        [],
        """
## Teller limits

Teller drawers are limited to $15,000. Excess cash goes to the vault within the hour.

## Large transactions

Cash withdrawals over $10,000 need 2 business days' notice so the branch can order cash. Cash transactions over $10,000 in a day are reported on a currency transaction report.

## Counterfeit notes

Suspected counterfeit notes are kept, not returned to the member, and sent to the Secret Service through the cash vendor.
""",
    ),
    (
        "branch-operations",
        "robbery-response-procedure",
        "Robbery Response Procedure",
        "Victor Ramos",
        "Facilities and security",
        "2026-02-10",
        "4.0",
        "pdf",
        [],
        """
## During a robbery

Comply with the robber's demands and give bait money if it is safe to do so. Do not chase or follow the robber.

## After the robbery

Lock the doors, call 911, and ask witnesses to stay and write down what they saw. Do not touch anything the robber touched.

## Support

Every employee present is offered counseling through the employee assistance program. Refer all media questions to the marketing director.
""",
    ),
    (
        "branch-operations",
        "atm-replenishment",
        "ATM Replenishment and Balancing",
        "Victor Ramos",
        "Facilities and security",
        "2026-04-13",
        "2.0",
        "pdf",
        [],
        """
## Dual control

ATMs are loaded and balanced by two employees together. The cassettes are never left unattended.

## Schedule

Branch ATMs are replenished every Monday and Thursday, and the day before a holiday weekend.

## Out-of-balance

An ATM out of balance by more than $100 is reported to the branch manager and the operations center the same day.
""",
    ),
    (
        "branch-operations",
        "branch-hours-and-closures",
        "Branch Hours and Emergency Closures",
        "Victor Ramos",
        "Facilities and security",
        "2026-06-01",
        "2.7",
        "pdf",
        [],
        """
## Standard hours

Branches are open 9:00 to 17:00 Monday through Friday and 9:00 to 12:00 on Saturday. The Las Olas and Weston branches stay open until 18:00 on weekdays.

## Emergency closures

The COO decides on emergency closures. Closures are announced on the website, in the mobile app and in the AI voice agent's greeting within 30 minutes of the decision.

## Hurricanes

Branches close when a hurricane warning is issued for the county and reopen after facilities confirms power, water and safe access.
""",
    ),
    (
        "branch-operations",
        "hurricane-preparedness",
        "Hurricane Preparedness Plan",
        "Victor Ramos",
        "Facilities and security",
        "2026-05-28",
        "3.5",
        "pdf",
        [],
        """
## Before the season

Each branch checks its shutters, generator fuel and emergency supplies by May 31.

## Watch and warning

When a hurricane watch is issued, branches secure records and move cash above the vault's flood line. When a warning is issued, branches close and staff follow the evacuation orders for their area.

## After the storm

Branch managers report damage within 24 hours. Members affected by a declared disaster may apply for the hardship and payment deferral program.
""",
    ),
    (
        "branch-operations",
        "lobby-kiosk-standard",
        "Lobby Kiosk Standard",
        "Kevin Tran",
        "IT and digital banking",
        "2026-07-15",
        "1.1",
        "docx",
        [],
        """
## What the kiosk does

The branch lobby kiosk answers general questions about products, hours and fees. It does not access member accounts and never asks for account numbers or card numbers.

## Content

The kiosk answers only from the member-facing collections. Internal procedures, HR policies and compliance documents are not available to it.

## Maintenance

Branch staff wipe the kiosk screen daily and report a kiosk that is offline to the IT service desk.
""",
    ),
    (
        "branch-operations",
        "safe-deposit-boxes",
        "Safe Deposit Box Procedures",
        "Victor Ramos",
        "Facilities and security",
        "2025-09-22",
        "1.9",
        "docx",
        [],
        """
## Access

Box holders sign the access card and show photo ID at every visit. Employees never handle the contents of a box.

## Fees

Annual rental is $40 for a small box, $75 for a medium box and $120 for a large box.

## Drilling

A box is drilled only after 60 days of unpaid rent and two letters, with two employees and a locksmith present. The contents are inventoried and sealed.
""",
    ),
]


def _md(title: str, body: str) -> str:
    return f"# {title}\n\n{body.strip()}\n"


def _passages(text: str) -> int:
    """Passages the gateway's own chunker makes of a document (what retrieval ranks)."""
    sys.path.insert(0, str(ROOT))
    from gateway.rag import chunk_text

    return len(chunk_text(text))


def _text(doc: dict) -> str:
    if doc["path"].startswith("library/"):
        col, slug = doc["collection"], doc["file"][:-3]
        body = next(d[-1] for d in DOCS if d[0] == col and d[1] == slug)
        return _md(doc["title"], body)
    return (ROOT / "demo" / doc["path"]).read_text(encoding="utf-8")


def catalog() -> dict:
    docs = []
    for file, title, owner, dept, reviewed, version, kind in ORIGINALS:
        docs.append(
            {
                "file": file,
                "path": file,
                "collection": "policies",
                "title": title,
                "owner": owner,
                "department": dept,
                "reviewed": reviewed,
                "version": version,
                "type": kind,
                "acl": ORIGINAL_ACL.get(file, []),
            }
        )
    for col, slug, title, owner, dept, reviewed, version, kind, acl, _body in DOCS:
        docs.append(
            {
                "file": f"{slug}.md",
                "path": f"library/{col}/{slug}.md",
                "collection": col,
                "title": title,
                "owner": owner,
                "department": dept,
                "reviewed": reviewed,
                "version": version,
                "type": kind,
                "acl": acl,
            }
        )
    for d in docs:
        d["passages"] = _passages(_text(d))
    counts = {c["name"]: sum(d["collection"] == c["name"] for d in docs) for c in COLLECTIONS}
    passages = {c["name"]: sum(d["passages"] for d in docs if d["collection"] == c["name"]) for c in COLLECTIONS}
    return {
        "generated_by": "scripts/sample_library.py",
        "disclaimer": "Fictional documents for a fictional credit union. Not legal, regulatory or HR guidance.",
        "collections": [{**c, "documents": counts[c["name"]], "passages": passages[c["name"]]} for c in COLLECTIONS],
        "documents": docs,
    }  # fmt: skip


def outputs() -> list[tuple[Path, str]]:
    out = [(LIB / col / f"{slug}.md", _md(title, body)) for col, slug, title, *_rest, body in DOCS]
    out.append((CATALOG, json.dumps(catalog(), indent=1) + "\n"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed library is stale")
    args = ap.parse_args(argv)
    files = outputs()
    if args.check:
        stale = [p for p, text in files if not p.exists() or p.read_text(encoding="utf-8") != text]
        expected = {p for p, _ in files if p.suffix == ".md"}
        extra = [p for p in LIB.rglob("*.md") if p not in expected]
        for p in stale + extra:
            print(f"{p.relative_to(ROOT)} is stale: run python scripts/sample_library.py")
        if not stale and not extra:
            print(f"demo/library ({len(expected)} documents) and demo/data/library.json are current")
        return 1 if stale or extra else 0
    for p, text in files:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    cat = catalog()
    print(f"wrote {len(cat['documents'])} documents in {len(cat['collections'])} collections")
    return 0


if __name__ == "__main__":
    sys.exit(main())
