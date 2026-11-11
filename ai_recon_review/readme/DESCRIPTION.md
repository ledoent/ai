Stages proposed bank reconciliations so a human can review them in a daily
batch, and turns the ones nobody could guess into candidate matching rules.

Each statement line the agent looks at lands in one of three buckets:

- **Confident** — applied immediately and flagged `To Review` in Odoo's own
  queue. Reconciliation is reversible, so a wrong confident match costs
  cleanup, not harm, and review becomes a sampling exercise rather than a
  bottleneck.
- **Needs a human** — nothing is written. The proposal is held as data and the
  entry is flagged `anomaly`, which puts it in the same native "To Review"
  filter without touching a single value on the journal entry.
- **No idea** — recorded as an observation only, then clustered. A group of
  lines sharing a vendor prefix in one journal is one missing
  `account.reconcile.model` away from never needing an agent again, so the
  agent's own success metric is the share of work it stops having to do.

Two design points worth defending.

**The proposed action is data, not a half-written entry.** Nothing has
happened, so there is nothing to block — *"unapproved cannot take effect"* is
true by construction. The alternative, freezing fields on a live record and
maintaining a list of the ones that must stay writable, is where approval
frameworks usually become brittle.

**The agent queues work it cannot sign off.** Writing `todo` or `anomaly`
needs no accounting rights, while `reviewed` requires `account.group_account_user`.
An agent below that group can therefore fill the review queue and is
structurally incapable of clearing it. There is a test asserting exactly that,
because the separation of duties rests on it.

This separation requires Accounting Firms mode to be enabled for both the
subject's company and the current company. Routing and approval refuse to
apply proposals when that precondition no longer holds.

Repeated batches keep one live proposal per statement line, enforced in the
database. Unknown observations remain available for clustering across batches,
counted once per distinct unresolved line. Applied and rejected proposals remain
as history. A line made unreconciled again can be considered in a fresh batch.
