import logging
import types

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AiReconProposal(models.Model):
    """A proposed reconciliation, held as data until someone agrees to it.

    The design decision worth defending: the proposed action lives **here**, not
    as a half-written journal entry. Nothing has happened, so there is nothing
    to block -- *"unapproved cannot take effect"* is true by construction rather
    than by freezing fields on a live record and maintaining an exception list
    of the ones that must stay writable. That exception list is where OCA's
    ``base_tier_validation`` gets brittle, and it is the part worth not copying.
    """

    _name = "ai.recon.proposal"
    _description = "AI Reconciliation Proposal"
    _inherit = "ai.proposal.mixin"

    # Bank opts into auto-applying its confident matches, and this is the only
    # place that is defensible: a reconciliation is reversible via
    # action_undo_reconciliation, so a wrong confident match costs cleanup
    # rather than harm. A vertical whose action cannot be undone must not copy
    # this line.
    _confidence_actions = types.MappingProxyType(
        {"high": "apply", "medium": "flag", "none": "note"}
    )

    batch_id = fields.Many2one("ai.recon.batch", ondelete="cascade", index=True)
    statement_line_id = fields.Many2one(
        "account.bank.statement.line", required=True, ondelete="cascade", index=True
    )
    move_id = fields.Many2one(related="statement_line_id.move_id", store=True)
    company_id = fields.Many2one(related="statement_line_id.company_id", store=True)
    journal_id = fields.Many2one(related="statement_line_id.journal_id", store=True)
    payment_ref = fields.Char(related="statement_line_id.payment_ref", string="Label")
    amount = fields.Monetary(related="statement_line_id.amount")
    currency_id = fields.Many2one(related="statement_line_id.currency_id")

    partner_id = fields.Many2one("res.partner", string="Proposed contact")
    account_id = fields.Many2one("account.account", string="Proposed account")
    reconcile_model_id = fields.Many2one(
        "account.reconcile.model",
        string="Matched rule",
        help="The existing rule that produced this, if any. Empty means the "
        "proposal came from the agent's own judgement -- which is exactly the "
        "case a new rule should eventually cover.",
    )
    _one_open_per_line = models.Constraint(
        "UNIQUE(statement_line_id, batch_id)",
        "That statement line already has a proposal in this batch.",
    )
    _one_live_per_line = models.UniqueIndex(
        "(statement_line_id) WHERE state IN "
        "('observed', 'proposed', 'approved', 'noted')",
        "That statement line already has a live review proposal.",
    )

    def init(self):
        """Repair old notes before the upgrade retries the unique index.

        ``_auto_init`` first attempts the index; an upgrade queues a failed
        attempt until ``Registry.finalize_constraints``. ``init`` repairs the
        old notes before that retry. Only redundant notes are disposable;
        conflicting human proposals require an explicit decision.
        """
        result = super().init()
        self._gc_duplicate_observations()
        self.env.cr.execute(
            """
            SELECT statement_line_id
              FROM ai_recon_proposal
             WHERE state IN ('observed', 'proposed', 'approved')
             GROUP BY statement_line_id
            HAVING COUNT(*) > 1
             LIMIT 1
            """
        )
        if collision := self.env.cr.fetchone():
            raise UserError(
                self.env._(
                    "Statement line %(line)s has multiple live review proposals. "
                    "Resolve them before upgrading; no human proposal was deleted.",
                    line=collision[0],
                )
            )
        return result

    @api.autovacuum
    def _gc_duplicate_observations(self):
        """Repair repeated observations without discarding the unknown corpus.

        Keep the oldest note for each subject, unless a live human proposal
        already covers it. Human proposals and applied/rejected history are
        never removed. This also repairs the old nightly builder's notes.
        """
        seen = set(
            self.search(
                [("state", "in", ["observed", "proposed", "approved"])]
            ).statement_line_id.ids
        )
        duplicates = []
        for proposal in self.search([("state", "=", "noted")], order="id"):
            subject = proposal.statement_line_id.id
            if subject in seen:
                duplicates.append(proposal.id)
            else:
                seen.add(subject)
        self.browse(duplicates).unlink()

    # -- applying ------------------------------------------------------------

    def _suspense_account(self):
        """Resolve suspense through the journal, never through an account code.

        On this database there are five different suspense accounts across five
        companies and two distinct codes, and code ``101402`` is Bank Suspense
        in one company and *Citibank*, a liquidity account, in another. Keying
        on a code would cover a handful of lines and corrupt the rest.
        """
        self.ensure_one()
        return self.statement_line_id.journal_id.suspense_account_id

    def _apply(self):
        """Move the counterpart off suspense and onto the proposed account.

        Rewrites the existing suspense line in place rather than deleting and
        recreating the move's lines. That is deliberate: a statement line's
        move is already posted, so a delete needs ``force_delete=True`` and a
        ``_check_balanced`` wrapper (the shape OCA's ``_reconcile_bank_line_edit``
        uses). Changing the account on the line we already have needs neither,
        touches one field, and is exactly what the audit trail records —
        *"101402 Bank Suspense Account → 617000 Computer and Internet Expenses
        (Account)"*.

        ``check_move_validity=False`` because the move stays balanced by
        construction: only the account changes, never an amount.
        """
        self.ensure_one()
        if not self.account_id:
            raise UserError(
                self.env._("Cannot apply a proposal with no account chosen.")
            )
        line = self.statement_line_id
        _liquidity, suspense, _other = line._seek_for_lines()
        if not suspense:
            raise UserError(
                self.env._(
                    "Statement line %(id)s has no suspense line left to reconcile.",
                    id=line.id,
                )
            )
        vals = {"account_id": self.account_id.id}
        if self.partner_id:
            vals["partner_id"] = self.partner_id.id
        suspense.with_context(
            check_move_validity=False,
            skip_readonly_check=True,
            skip_account_move_synchronization=True,
        ).write(vals)
        # Push it into the native review queue. Writing 'todo' needs no
        # accounting rights (account_move.py:3937), while 'reviewed' requires
        # group_account_user -- so an agent below that group can queue work and
        # is structurally unable to sign it off.
        line.move_id.review_state = "todo"
        return True

    def _flag_for_attention(self):
        """Mark the move so an uncertain line surfaces in the native queue.

        ``anomaly`` is in core's selection but is never written by any
        Community code -- an empty slot that already has a search filter, a
        dashboard counter and a badge widget pointed at it
        (``account_move_views.xml:350,1873``). Nothing else is touched: the
        line keeps its suspense counterpart and no value changes.
        """
        self.ensure_one()
        self.statement_line_id.move_id.review_state = "anomaly"
        return True

    # -- preconditions and hooks ---------------------------------------------

    def _assert_preconditions(self):
        """Bank's safety rests on a setting this code does not own."""
        self._assert_review_gate(self.company_id | self.env.company)
        return True

    def _undo(self):
        """Put the line back in suspense. This is what makes `high -> apply`
        defensible here and nowhere else yet."""
        self.ensure_one()
        self.statement_line_id.action_undo_reconciliation()
        return True

    def _cluster_key(self):
        """First two words of the label plus the journal.

        Crude, and it shows -- on real data it produced useful keys like
        "CLICKUP" and "CHIEF ARCHITECT" alongside noise like "OFFER 04" and
        "OTHER FIN". A better key is a real refinement, not a tidy-up.
        """
        self.ensure_one()
        label = (self.payment_ref or "").strip().upper()
        prefix = " ".join(label.split()[:2])
        return (prefix, self.journal_id.id) if prefix else None

    @api.model
    def _cluster_label(self, key):
        prefix, journal_id = key
        return f"{prefix} ({self.env['account.journal'].browse(journal_id).code})"

    @api.model
    def _draft_rule(self, key, proposals):
        """An inactive reconcile model, so nothing changes until accepted.

        The account is deliberately left empty: choosing it is the judgement
        being asked for.
        """
        prefix, journal_id = key
        return self.env["account.reconcile.model"].create(
            {
                "name": f"[proposed] {prefix}",
                "rule_type": "reco_model",
                "active": False,
                "match_label": "contains",
                "match_label_param": prefix,
                "match_journal_ids": [fields.Command.set([journal_id])],
                "company_id": proposals[0].company_id.id,
            }
        )

    @api.model
    def _assert_review_gate(self, companies):
        """Refuse to run where the review gate is inert.

        The separation of duties this module relies on -- the agent may write
        'todo'/'anomaly' but not 'reviewed' -- is enforced by
        ``_check_review_state_access`` (``account_move.py:3932``). That method
        short-circuits on its first line when ``set_to_review_documents`` is
        off on either the move's company or ``env.company``:

            if self.env.su or not all(self.mapped('set_to_review_documents')) \
               or not self.env.company.set_to_review_documents:
                return

        ``set_to_review_documents`` is a plain Boolean with no default
        (``company.py:273``), so **off is the shipped state**, and with it off a
        user holding only ``group_account_invoice`` can write 'reviewed'
        freely. Measured, not inferred.

        So the guarantee is conditional on a setting this module does not own.
        Rather than document that and hope, the agent refuses to operate where
        the gate is inert -- which makes the guarantee unconditional in
        practice, because there is no configuration in which the agent runs
        and the gate does not hold.
        """
        inert = (companies | self.env.company).filtered(
            lambda c: not c.set_to_review_documents
        )
        if inert:
            raise UserError(
                self.env._(
                    "Review data ('Accounting Firms mode') is off for: "
                    "%(names)s.\n\nWith it off, Odoo skips the access check "
                    "that stops this agent marking its own work reviewed, so "
                    "the agent will not run. Turn it on in Settings -> "
                    "Accounting for those companies.",
                    names=", ".join(inert.mapped("name")),
                )
            )
        return True
