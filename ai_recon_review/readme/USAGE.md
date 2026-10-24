Turn on **Accounting Firms mode** first — Settings → Accounting →
`set_to_review_documents`. Without it the review-state access checks return
immediately and the separation of duties is inert.

Give the agent its own user with `base.group_user` +
`account.group_account_invoice` and **not** `account.group_account_user`. Above
that group, work it applies is marked `no_review` and never enters the queue.

```python
batch = env["ai.recon.batch"].create({"lines_seen": len(lines)})
env["ai.recon.proposal"].create({
    "batch_id": batch.id,
    "statement_line_id": line.id,
    "confidence": "high",          # or "medium" / "none"
    "account_id": expense.id,
    "partner_id": vendor.id,
    "rationale": "label matches the GitHub subscription pattern",
})
env["ai.recon.proposal"]._route(batch.proposal_ids)
```

Then `batch.action_draft_rules()` creates one **inactive**
`account.reconcile.model` per cluster of unmatched lines. They change nothing
until somebody activates them, and the account is deliberately left empty —
choosing it is the judgement being asked for.

## A warning about catch-all rules

A reconcile model with no `match_label`, `match_amount` or `match_partner_ids`
becomes proposable purely by being set to `auto_reconcile`
(`_compute_can_be_proposed`), and then matches every line in its journals.
Measured on a real database, one such rule swept 714 of 754 lines into an
"Uncategorized" account — technically reconciled, substantively unclassified,
and with the rule-discovery signal destroyed. Keep catch-alls off while you are
trying to learn what rules you are missing.
