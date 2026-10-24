# pylint: disable=method-required-super,no-raise-unlink
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
    kind = fields.Selection([("judgment", "Judgment"), ("undo", "Undo")], required=True)
    verdict = fields.Selection(
        [(v, v.title()) for v in ("accept", "reject", "request")],
        required=True,
    )
    reviewer_id = fields.Many2one("res.users", required=True, ondelete="restrict")
    reason = fields.Text()
    judged_at = fields.Datetime(default=fields.Datetime.now, required=True)

    _unique_evidence = models.Constraint(
        "UNIQUE(call_id, kind)",
        "A call can have only one judgment and one undo signal.",
    )
    _valid_kind = models.Constraint(
        "CHECK((kind = 'judgment' AND verdict IN ('accept', 'reject')) "
        "OR (kind = 'undo' AND verdict = 'request'))",
        "Invalid evidence kind.",
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
        return super(AiAgentEvidence, self.sudo()).create(
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
