AI Reconciliation Agent
=======================

This addon adds attributed matching records alongside ``ai_recon_review``.
The existing categorization proposals remain unattributed and do not become
capability evidence.

The service exposes three curated agent tools on
``ai.recon.match.service``:

* ``candidate_packet`` is read-only and returns a bounded same-company,
  same-counterparty list with document references, open amounts, currencies,
  and dates. It omits bank account numbers and attachments. A truncated or
  unsupported candidate set cannot proceed to proposal creation.
* ``match_items`` validates the selected documents and stores an immutable
  proposal with the exact accounting facts and payload hash. It does not
  reconcile or post anything. A single document may be matched partially up to
  its open balance. Groups are limited to exact totals with every selected
  document fully allocated; mixed or partial groups are refused for manual
  work. The initial bridge only supports the company's currency.
* ``post_match`` requires a reviewer-approved proposal and a runtime call whose
  exact snapshot was approved. Configure the tool with ``requires_approval``.
  It re-locks and rechecks the bank and document facts before calling Odoo's
  native reconciliation plan.

Configure the company opt-in, provider, disclosure version, candidate limits,
and reviewers as an administrator. LLM runs are refused unless the company has
explicitly selected the provider used by the run. Rules-only runs remain
available. Reviewers need the AI reviewer group, agent assignment, company
assignment, and accounting review rights. Agent execution accounts cannot
review.
Assessments, matches, and allocations are readable only by administrators, the
agent execution identity, or reviewers assigned to that agent.

A reviewer decision on the match content is separate from approving the
runtime invocation that creates the proposal. Only the match-content decision
counts as reconciliation quality evidence; approving the tool invocation
alone does not. A reviewer acceptance does not execute reconciliation. Posting
requires a separate tool configured with ``requires_approval`` and an approval
of its exact frozen argument snapshot.

An assigned reviewer can withdraw an approved but unposted proposal with a
reason. This records a failed reversal attempt on the originating call, removes
automatic apply authority, marks the proposal stale, and lets a new assessment
be created. A posted reconciliation cannot be withdrawn this way.

Safety status and limitations
-----------------------------

This is an installable implementation foundation, not a staged pilot. The
proposal-creation tool has a trusted database-local adapter that can withdraw
an unreviewed proposal. It refuses withdrawal after a reviewer acts. The
posting tool has no reversal adapter, so it cannot earn automatic ``apply``
authority. An undo request after posting requires manual accounting repair.
Do not grant manual apply authority to posting tools. Exact approval is
enforced at runtime for ``post_match``; posting uses Odoo's native
reconciliation and invoice-paid hooks.

The current bridge does not implement tolerance/write-off accounts, foreign
currency, FX differences, cash-basis tax, arbitrary partial groups, or provider
usage/disclosure audit records. These paths must remain with a human accountant
until separately implemented and tested. Local end-to-end fixtures cover
invoice matching, reviewer judgment, exact-snapshot posting, undo refusal after
review, provider selection, and single-document partial payment. The addon has
not been enabled on staging or production.
