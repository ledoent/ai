from collections import Counter

from odoo import api, fields, models


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
