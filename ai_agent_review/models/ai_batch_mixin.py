from collections import Counter

from odoo import api, fields, models
from odoo.exceptions import UserError


class AiBatchMixin(models.AbstractModel):
    """One scheduled run, and the thing the review email is *about*.

    A record rather than just an email so there is a stable URL to return to
    after the mail is deleted, somewhere for the counts to live, and a chatter
    to argue in.
    """

    _name = "ai.batch.mixin"
    _description = "AI Review Batch Mixin"
    _inherit = "mail.thread"
    _order = "id desc"

    date = fields.Datetime(default=fields.Datetime.now, required=True)
    items_seen = fields.Integer(readonly=True)
    truncated = fields.Boolean(
        readonly=True,
        help="Set when the scanning user could not see every record it was "
        "asked about. A batch that silently covered a subset is worse than no "
        "batch, so this is recorded rather than inferred.",
    )
    count_high = fields.Integer("Confident", compute="_compute_counts", store=True)
    count_medium = fields.Integer(
        "Needs a human", compute="_compute_counts", store=True
    )
    count_none = fields.Integer("No idea", compute="_compute_counts", store=True)

    @api.depends("proposal_ids.confidence")
    def _compute_counts(self):
        for batch in self:
            counts = Counter(batch.proposal_ids.mapped("confidence"))
            batch.count_high = counts.get("high", 0)
            batch.count_medium = counts.get("medium", 0)
            batch.count_none = counts.get("none", 0)

    # -- clustering ----------------------------------------------------------

    def _unmatched_clusters(self, min_size=2):
        """Group the 'no idea' pile into candidate rules.

        The part that compounds. A cluster of records sharing a key is one
        missing deterministic rule away from never needing an agent again, so
        the agent's own success metric is the share of work it stops having to
        do. Singletons are not a pattern and are excluded.
        """
        self.ensure_one()
        buckets = {}
        for proposal in self.proposal_ids.filtered(lambda p: p.confidence == "none"):
            key = proposal._cluster_key()
            if key is None:
                continue
            buckets.setdefault(key, proposal.browse())
            buckets[key] |= proposal
        return {k: v for k, v in buckets.items() if len(v) >= min_size}

    def _top_clusters(self, limit=10):
        """Render clusters for the email without leaking the key's shape.

        An earlier version returned the bank vertical's key fields directly,
        which coupled the mail template to reconciliation. The label is the
        vertical's business now.
        """
        self.ensure_one()
        ordered = sorted(
            self._unmatched_clusters().items(), key=lambda kv: -len(kv[1])
        )[:limit]
        Proposal = self.env[self._proposal_model()]
        return [
            {"label": Proposal._cluster_label(key), "count": len(proposals)}
            for key, proposals in ordered
        ]

    def action_draft_rules(self):
        """Draft one inactive rule per cluster, for a human to accept."""
        self.ensure_one()
        Proposal = self.env[self._proposal_model()]
        created = []
        for key, proposals in self._unmatched_clusters().items():
            rule = Proposal._draft_rule(key, proposals)
            if rule:
                created.append(rule)
        return created

    # -- notification --------------------------------------------------------

    def _recipients(self):
        """Who gets asked. Configured on the role, deliberately not guessed."""
        self.ensure_one()
        return self.env["res.users"].browse()

    def action_send(self):
        """One email per batch, to the configured reviewers, or a clear refusal.

        One message about a batch, not one per proposal: scheduling a
        ``mail.activity`` emails the assignee on every record
        (``mail_activity.py:344``), which for a long backlog is a mailbox
        nobody reads.

        Deliberately not the ``digest`` module -- it models scalar KPIs with no
        per-record list, and ``_check_daily_logs`` silently downgrades a daily
        digest to weekly when the recipient has not logged in, which is exactly
        when it matters most.
        """
        template = self._mail_template()
        for batch in self:
            users = batch._recipients()
            if not users:
                raise UserError(
                    self.env._(
                        "No reviewers configured for this batch. A batch that "
                        "reaches nobody is worse than a loud failure."
                    )
                )
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

    # -- hooks a vertical must supply ---------------------------------------

    @api.model
    def _proposal_model(self):
        raise NotImplementedError

    def _mail_template(self):
        raise NotImplementedError

    @api.model
    def _candidates(self):
        """The records this role considers in scope."""
        raise NotImplementedError
