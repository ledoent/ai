import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

CONFIDENCE = [
    ("high", "Confident"),
    ("medium", "Needs a human"),
    ("none", "No idea"),
]

STATE = [
    ("proposed", "Proposed"),
    ("applied", "Applied"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
    ("noted", "Noted only"),
]


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
    _order = "confidence, id"

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

    confidence = fields.Selection(CONFIDENCE, required=True, index=True)
    state = fields.Selection(STATE, default="proposed", required=True, index=True)

    partner_id = fields.Many2one("res.partner", string="Proposed contact")
    account_id = fields.Many2one("account.account", string="Proposed account")
    reconcile_model_id = fields.Many2one(
        "account.reconcile.model",
        string="Matched rule",
        help="The existing rule that produced this, if any. Empty means the "
        "proposal came from the agent's own judgement -- which is exactly the "
        "case a new rule should eventually cover.",
    )
    rationale = fields.Text(help="Why the agent proposed this, in its own words.")

    approved_by = fields.Many2one("res.users", readonly=True)
    approved_date = fields.Datetime(readonly=True)

    _one_open_per_line = models.Constraint(
        "UNIQUE(statement_line_id, batch_id)",
        "That statement line already has a proposal in this batch.",
    )

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

    # -- the gate ------------------------------------------------------------

    def _execute(self):
        self.ensure_one()
        if self.state != "approved":
            raise UserError(
                self.env._(
                    "Proposal %(id)s is %(state)s, not approved. Nothing was done.",
                    id=self.id,
                    state=self.state,
                )
            )
        self._apply()
        self.state = "applied"
        return True

    def action_approve(self):
        for proposal in self:
            if proposal.state not in ("proposed",):
                raise UserError(self.env._("Only a proposed line can be approved."))
            proposal.write(
                {
                    "state": "approved",
                    "approved_by": self.env.uid,
                    "approved_date": fields.Datetime.now(),
                }
            )
            proposal._execute()
        return True

    def action_reject(self):
        for proposal in self:
            if proposal.state != "proposed":
                raise UserError(self.env._("Only a proposed line can be rejected."))
            proposal.write(
                {
                    "state": "rejected",
                    "approved_by": self.env.uid,
                    "approved_date": fields.Datetime.now(),
                }
            )
        return True

    def action_undo(self):
        """Put an applied line back in suspense. Reconciliation is reversible."""
        for proposal in self.filtered(lambda p: p.state == "applied"):
            proposal.statement_line_id.action_undo_reconciliation()
            proposal.state = "proposed"
        return True

    # -- routing -------------------------------------------------------------

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
        inert = companies.filtered(lambda c: not c.set_to_review_documents)
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

    @api.model
    def _route(self, proposals):
        """Send each proposal down the path its confidence earns.

        high   -- apply now, flagged unreviewed. Reconciliation is reversible
                  (``action_undo_reconciliation``), so a wrong confident match
                  costs cleanup, not harm.
        medium -- touch nothing; flag the move so a human finds it.
        none   -- record the observation and leave the line alone. These are
                  the rule-discovery corpus, and the reason the catch-all rule
                  must stay off: sweeping them into an "Uncategorized" account
                  would reconcile them and destroy the signal.
        """
        self._assert_review_gate(proposals.company_id)
        for proposal in proposals:
            if proposal.confidence == "high":
                proposal._apply()
                proposal.state = "applied"
            elif proposal.confidence == "medium":
                proposal._flag_for_attention()
            else:
                proposal.state = "noted"
        return True
