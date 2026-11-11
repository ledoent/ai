# pylint: disable=method-required-super,no-raise-unlink,context-overridden
# Reviewer evidence is append-only and can only be created by private methods.
from odoo import api, fields, models
from odoo.exceptions import AccessError


class AiAgentEvidence(models.Model):
    _name = "ai.agent.evidence"
    _description = "AI Agent Review Evidence"
    _order = "id"

    call_id = fields.Many2one("ai.agent.call", required=True, ondelete="restrict")
    agent_id = fields.Many2one(related="call_id.agent_id", store=True)
    capability_id = fields.Many2one(
        "ai.agent.capability", required=True, ondelete="restrict"
    )
    epoch = fields.Integer(required=True)
    kind = fields.Selection(
        [
            ("judgment", "Invocation judgment"),
            ("business", "Business outcome judgment"),
            ("undo", "Undo"),
        ],
        required=True,
    )
    verdict = fields.Selection(
        [(v, v.title()) for v in ("accept", "reject", "request")],
        required=True,
    )
    reviewer_id = fields.Many2one("res.users", required=True, ondelete="restrict")
    reason = fields.Text()
    business_key = fields.Char(index=True)
    judged_at = fields.Datetime(default=fields.Datetime.now, required=True)

    _unique_evidence = models.Constraint(
        "UNIQUE(call_id, kind)",
        "A call can have only one judgment and one undo signal.",
    )
    _valid_kind = models.Constraint(
        "CHECK((kind IN ('judgment', 'business') AND verdict IN ('accept', 'reject')) "
        "OR (kind = 'undo' AND verdict = 'request'))",
        "Invalid evidence kind.",
    )
    _unique_business_intent = models.Constraint(
        "UNIQUE(capability_id, epoch, business_key)",
        "This business intent can be judged only once in a capability epoch.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(self.env._("Evidence can only be recorded through review."))

    def write(self, vals):
        raise AccessError(self.env._("Evidence is append-only."))

    def unlink(self):
        raise AccessError(self.env._("Evidence history cannot be deleted."))

    @api.model
    def _record(self, call, kind, verdict, reason):
        return super(AiAgentEvidence, self.sudo().with_context({})).create(
            {
                "call_id": call.id,
                "capability_id": call.capability_id.id,
                "epoch": call.capability_epoch,
                "kind": kind,
                "verdict": verdict,
                "reviewer_id": self.env.uid,
                "reason": reason,
            }
        )

    @api.model
    def _record_business(self, call, verdict, reason, business_key):
        if verdict not in ("accept", "reject") or not business_key:
            raise AccessError(self.env._("Invalid business evidence."))
        domain = [
            ("capability_id", "=", call.capability_id.id),
            ("epoch", "=", call.capability_epoch),
            ("business_key", "=", business_key),
        ]
        if self.sudo().search_count(domain):
            return self.browse()
        return super(AiAgentEvidence, self.sudo().with_context({})).create(
            {
                "call_id": call.id,
                "capability_id": call.capability_id.id,
                "epoch": call.capability_epoch,
                "kind": "business",
                "verdict": verdict,
                "reviewer_id": self.env.uid,
                "reason": reason,
                "business_key": business_key,
            }
        )
