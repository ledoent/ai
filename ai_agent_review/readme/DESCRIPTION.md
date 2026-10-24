The reusable half of agent-assisted review: an agent proposes an action, the
proposal is held as **data** until a human agrees, and only then does anything
happen.

That is the one idea worth carrying between verticals. Because the proposed
action never exists as a half-written record, *"unapproved cannot take effect"*
is true by construction — there is nothing to block, no fields to freeze on a
live record, and no list of exceptions that must stay writable. Maintaining
that list is where approval frameworks usually become brittle.

Two abstract models:

- **`ai.proposal.mixin`** — confidence, the state machine, `approved_by` /
  `approved_date`, approve / reject / undo, and the `_execute()` gate.
- **`ai.batch.mixin`** — the scheduled run, bucket counts, clustering of the
  "no idea" pile into candidate rules, and one email per batch.

A vertical inherits both and supplies its own foreign key to the subject
record, its payload fields, and a handful of hooks.

## Two defaults worth knowing

**`high` flags; it does not apply.** Auto-applying a confident match is
defensible only where the action is reversible — true for bank reconciliation,
false for a sent dunning email, a confirmed purchase order, or a price on a
live pricelist. A vertical that wants auto-apply opts in and says why.

**`_assert_preconditions()` fails closed.** It exists because one vertical's
safety guarantee silently did not hold: it depended on a company setting the
module did not own, and with that setting off the guarantee evaporated. A
vertical resting on an external precondition checks it there rather than
documenting it and hoping.

## Where this does not belong

Some work already has a native gate, and a parallel queue would be worse than
what Odoo ships. `stock.warehouse.orderpoint` proposes a quantity, a human
confirms, and a draft RFQ needs a second confirm — with lead-time-aware
forecasting and excess-stock warnings nobody should rewrite. `project.task`
already has `changes_requested` / `approved` and an attention decoration.

For those, an agent adds value by improving the *inputs* to machinery that
exists. The tell is whether the "no idea" pile has a deterministic rule to
cluster toward; if it does not, this framework is the wrong tool.
