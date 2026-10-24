import logging
from collections import Counter

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AiReconBatch(models.Model):
    """One nightly run, and the thing the daily email is *about*.

    A record rather than just an email so there is a stable URL to return to, a
    chatter to argue in, and somewhere for the counts to live after the mail is
    deleted.
    """

    _name = "ai.recon.batch"
    _description = "AI Reconciliation Batch"
    _inherit = "mail.thread"
    _order = "id desc"

    name = fields.Char(compute="_compute_name", store=True)
    date = fields.Datetime(default=fields.Datetime.now, required=True)
    company_ids = fields.Many2many("res.company", string="Companies scanned")
    proposal_ids = fields.One2many("ai.recon.proposal", "batch_id")

    lines_seen = fields.Integer(readonly=True)
    count_high = fields.Integer("Applied", compute="_compute_counts", store=True)
    count_medium = fields.Integer(
        "Needs a human", compute="_compute_counts", store=True
    )
    count_none = fields.Integer("No idea", compute="_compute_counts", store=True)
    truncated = fields.Boolean(
        readonly=True,
        help="Set when the scanning user could not see every line it was asked "
        "about. A batch that silently covered a subset is worse than no batch, "
        "so this is recorded rather than inferred.",
    )

    @api.depends("date")
    def _compute_name(self):
        for batch in self:
            batch.name = (
                f"Reconciliation review {batch.date and batch.date.date() or ''}"
            )

    @api.depends("proposal_ids.confidence")
    def _compute_counts(self):
        for batch in self:
            counts = Counter(batch.proposal_ids.mapped("confidence"))
            batch.count_high = counts.get("high", 0)
            batch.count_medium = counts.get("medium", 0)
            batch.count_none = counts.get("none", 0)

    def _unmatched_clusters(self, min_size=2):
        """Group the 'no idea' lines into candidate rules.

        The point of the exercise. A cluster of lines sharing a vendor string
        in one journal is one missing ``account.reconcile.model`` away from
        never needing an agent again, so the agent's own success metric is the
        share of work it stops having to do.

        Clusters on the first two words of the label plus the journal, which is
        crude but matches how bank feeds actually name things ("AMZN Mktp US*2K4...",
        "GITHUB  INC. HTTPSGITHUB"). A smarter key is a later refinement; this
        one is explicable to whoever reads the email.
        """
        self.ensure_one()
        buckets = {}
        for proposal in self.proposal_ids.filtered(lambda p: p.confidence == "none"):
            label = (proposal.payment_ref or "").strip().upper()
            key = (" ".join(label.split()[:2]), proposal.journal_id.id)
            if not key[0]:
                continue
            buckets.setdefault(key, self.env["ai.recon.proposal"])
            buckets[key] |= proposal
        return {k: v for k, v in buckets.items() if len(v) >= min_size}

    def action_draft_rules(self):
        """Draft an inactive reconcile model per cluster, for a human to accept.

        Created ``active=False`` so nothing changes until someone activates it.
        The label match is seeded from the shared prefix; the account is left
        empty on purpose, because choosing it is the judgement we are asking
        for.
        """
        Model = self.env["account.reconcile.model"]
        created = Model.browse()
        for (prefix, journal_id), proposals in self._unmatched_clusters().items():
            created |= Model.create(
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
        return created

    # -- the daily batch -----------------------------------------------------

    def _recipients(self):
        """Who gets asked. Configured per company, deliberately not guessed."""
        self.ensure_one()
        # pylint: disable=no-search-all
        # Every company, deliberately: a batch with no company_ids set is the
        # all-companies case, and silently reviewing a subset is the failure
        # this whole design exists to avoid. There are five of them.
        companies = self.company_ids or self.env["res.company"].search([])
        return companies.mapped("ai_recon_reviewer_ids")

    def action_send(self):
        """Send one email per batch, to the configured reviewers.

        One message about a batch, not one per proposal. Scheduling a
        ``mail.activity`` emails the assignee on every single record
        (``mail_activity.py:344``), which for a 700-line backlog is a mailbox
        full of noise and a reviewer who stops reading. The batch record is the
        thing the mail is *about*, so there is a stable URL to return to after
        the mail is deleted.

        Deliberately **not** the ``digest`` module: it models scalar KPIs with
        no per-record list, and ``_check_daily_logs`` silently downgrades a
        daily digest to weekly when the recipient has not logged in -- which is
        exactly when you most want it to arrive.
        """
        template = self.env.ref("ai_recon_review.mail_template_recon_batch")
        for batch in self:
            users = batch._recipients()
            if not users:
                raise UserError(
                    self.env._(
                        "No reconciliation reviewers configured. Set them in "
                        "Settings -> Accounting, or on the company."
                    )
                )
            # recipient_ids only. Setting email_to as well delivers the same
            # message twice -- once to the partner through the notification
            # layer and once to the raw address.
            template.send_mail(
                batch.id,
                force_send=True,
                email_values={
                    "recipient_ids": [fields.Command.set(users.partner_id.ids)]
                },
                email_layout_xmlid="mail.mail_notification_layout",
            )
            batch.message_post(
                body=self.env._(
                    "Review batch sent to %(who)s.",
                    who=", ".join(users.mapped("name")),
                )
            )
        return True

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
                "lines_seen": len(lines),
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

    def _top_clusters(self, limit=10):
        """Rendering helper for the email's 'no idea' section."""
        self.ensure_one()
        clusters = self._unmatched_clusters()
        ordered = sorted(clusters.items(), key=lambda kv: -len(kv[1]))[:limit]
        Journal = self.env["account.journal"]
        return [
            {
                "prefix": prefix,
                "journal": Journal.browse(journal_id).code,
                "count": len(proposals),
            }
            for (prefix, journal_id), proposals in ordered
        ]
