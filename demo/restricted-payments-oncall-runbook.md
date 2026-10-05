# Payments On-Call Runbook (Engineering only, fictional sample)

This is a fictional sample document for the demo. It is visible only to callers in the engineering group.

## Rotation

The payments on-call rotation changes every Monday at 10:00. The primary on-call engineer acknowledges pages within 15 minutes; if not, the page escalates to the secondary.

## Incident steps

Declare an incident in the incident channel, then freeze deploys to the payments service. Roll back the last deploy if error rates exceed 2 percent for five minutes.

## Credentials

Production database credentials are issued just in time through the access broker and expire after 4 hours. Never paste credentials into tickets.
