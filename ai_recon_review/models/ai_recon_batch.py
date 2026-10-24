import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AiReconBatch(models.Model):
    """One nightly run, and the thing the daily email is *about*.

    A record rather than just an email so there is a stable URL to return to, a
    chatter to argue in, and somewhere for the counts to live after the mail is
    deleted.
    """

    _name = "ai.recon.batch"
    _description = "AI Reconciliation Batch"
    _inherit = "ai.batch.mixin"

    name = fields.Char(compute="_compute_name", store=True)
    date = fields.Datetime(default=fields.Datetime.now, required=True)
    company_ids = fields.Many2many("res.company", string="Companies scanned")
    proposal_ids = fields.One2many("ai.recon.proposal", "batch_id")

    @api.depends("date")
    def _compute_name(self):
        for batch in self:
            batch.name = (
                f"Reconciliation review {batch.date and batch.date.date() or ''}"
            )

    @api.depends("proposal_ids.confidence")
    def _recipients(self):
        """Who gets asked. Configured per company, deliberately not guessed."""
        self.ensure_one()
        # pylint: disable=no-search-all
        # Every company, deliberately: a batch with no company_ids set is the
        # all-companies case, and silently reviewing a subset is the failure
        # this whole design exists to avoid. There are five of them.
        companies = self.company_ids or self.env["res.company"].search([])
        return companies.mapped("ai_recon_reviewer_ids")

    @api.model
    def _cron_build_and_send(self):
        """Build tonight's batch and mail it. Wired to an inactive cron.

        The cron ships inactive on purpose -- an always-on job that emails
        people is not something a module install should switch on for you. The
        interval is the cron record's own, so changing the cadence is a
        settings change rather than a code change.
        """
        BSL = self.env["account.bank.statement.line"]
        lines = BSL.search([("is_reconciled", "!=", True)])
        if not lines:
            _logger.info("ai.recon.batch: nothing unreconciled, no batch sent")
            return False
        batch = self.create(
            {
                "items_seen": len(lines),
                "company_ids": [fields.Command.set(lines.company_id.ids)],
            }
        )
        batch._classify(lines)
        batch.action_send()
        _logger.info(
            "ai.recon.batch %s: %s lines seen across %s companies, sent to %s",
            batch.id,
            len(lines),
            len(lines.company_id),
            ", ".join(batch._recipients().mapped("login")),
        )
        return batch

    def _classify(self, lines):
        """Create a proposal per line and route it.

        The hook. Today it records every line as "no idea", which is honest
        for a run with no agent attached -- the batch still reports what was
        seen and still clusters the unmatched into candidate rules. Override or
        replace this to plug in whatever is doing the judging.
        """
        self.ensure_one()
        Proposal = self.env["ai.recon.proposal"]
        proposals = Proposal.create(
            [
                {
                    "batch_id": self.id,
                    "statement_line_id": line.id,
                    "confidence": "none",
                }
                for line in lines
            ]
        )
        Proposal._route(proposals)
        return proposals

    # -- hooks ---------------------------------------------------------------

    @api.model
    def _proposal_model(self):
        return "ai.recon.proposal"

    def _mail_template(self):
        return self.env.ref("ai_recon_review.mail_template_recon_batch")

    @api.model
    def _candidates(self):
        return self.env["account.bank.statement.line"].search(
            [("is_reconciled", "!=", True)]
        )
