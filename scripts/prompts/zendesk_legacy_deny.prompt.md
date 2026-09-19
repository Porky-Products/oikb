# Zendesk legacy-ticket deny classifier prompt
#
# WHAT THIS FILE IS
# The scanner script (scripts/zendesk_llm_denylist_scan.py) sends the text
# below as the classification instructions for ONE ticket at a time, then
# appends its own strict-JSON response-format block. Do NOT add response
# format instructions here — the script owns the return contract
# (ticket_id + verdict: deny|unsure|allow).
#
# VERDICT SEMANTICS (enforced by the script)
#   deny   -> ticket ID appended to the denylist file; the next oikb sync
#             purges it from the knowledge base.
#   unsure -> ticket ID appended to the review file for human triage.
#   allow  -> ignored.
# Never guess. When in doubt between deny and allow, answer unsure.
#
# SCOPE BOUND: the scanner only classifies ticket IDs <= the boundary ID
# where Zendesk auto-tagging (ach_request / sensitive) was introduced.
# Set that boundary via LLM_SCAN_STOP_TICKET_ID from your Zendesk history.

## What to deny

A ticket contains, or concerns the handling of, sensitive customer
financial / credit information. Examples of matching content:

- Credit applications (incoming or outgoing), credit references, or
  correspondence about opening or reviewing a customer's credit account.
- ACH setup or change requests: bank names, routing numbers, account
  numbers, voided checks, direct-deposit or payment-bank-detail forms.
- Credit-limit requests, credit holds, or collections/accounts-receivable
  correspondence that references customer financial standing.
- Documents or attachments with names suggesting the above (e.g.
  "credit_application.pdf", "ach_form.jpg", "voided_check.png",
  "bank_details.xlsx").

## Supporting signals (not sufficient alone)

- Requester is a member of the credit department or accounts receivable
  (e.g. mark.mclaughlin@porky.com) — supporting signal only; credit staff
  also file ordinary support tickets.
- Subject/body phrases like "credit application", "new account", "ACH",
  "banking information", "payment terms" — supporting signal only;
  ordinary orders or account-creation questions may use similar wording
  without containing sensitive information.

## What NOT to deny

- Ordinary sales orders, order changes, and shipping questions, even when
  financial terms like "payment terms" or "invoice" appear in passing and
  no bank/credit data is present.
- Password resets, login problems, app/support questions from
  credit-dept staff that contain no credit or banking content.
- Tickets that merely reference that a credit application "was received
  previously" without quoting or attaching its contents.

## Examples

ticket: subject "Fwd: New account for 3406", requester from credit dept,
attachment "credit_app.pdf" → deny

ticket: subject "Pricing update for Q3", requester sales rep, no
attachments, mentions "payment terms" once → allow

ticket: subject "Bank details", no description body (empty), no
attachments, requester unknown → unsure (name suggests sensitivity but
there is no content to judge)

ticket: credit-dept requester asks how to change their own app password
→ allow

## Rule of thumb

Deny when the ticket itself carries or requests the sensitive financial
material. Allow when the ticket only discusses business in general terms.
If you cannot tell — truncated, ambiguous, missing pieces — answer
unsure. A wrongly-denied ordinary ticket is recoverable by a human
reviewing the review-file pipeline; sensitive data leaked into the KB is
not.