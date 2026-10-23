# Runbook 6: duplicate keys

**Signal.** `KEY.DUPLICATE:<col>` on every copy of a key that must be `unique` (all copies
are rejected: the platform cannot know which one is right). Alert **RowRejectionRateHigh**
if enough rows fail.

**See it:** the committed fixture `tests/fixtures/finance/bad/invoices_breakages.xlsx`
has a duplicated invoice number (the E2E's `finance.vendor_invoice` cases expect
`KEY.DUPLICATE:invoice_number` on both copies).

## Evidence

1. `get_quarantine_sample(group)`: both rows, for a declared non-PHI key.
2. `get_feed_history`: was the same key loaded from an earlier file? (Duplicates are
   checked within a file; gold keeps the newest per key across files.)

## Decide

| What you see | Action |
|---|---|
| The same record twice (identical rows) | `request_resend` (the export doubled it), or `discard` if the record also exists in a good file |
| Two different records with one key | `request_resend`: the vendor must fix the key; never pick one |

## Check

The corrected file `loaded` with the expected count; gold has one row per key
(`gold.vendor_invoice`'s `unique` test passes in `get_dq_results`).
