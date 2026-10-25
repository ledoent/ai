from typing import ClassVar

from odoo import fields, models


class Assessment(models.Model):
    _name = "ai.recon.assessment"
    _description = "Frozen Matching Assessment"
    _inherit: ClassVar[list[str]] = ["ai.recon.protected"]
    _order = "id desc"

    profile_id = fields.Many2one("ai.recon.profile", required=True, ondelete="restrict")
    company_id = fields.Many2one(related="profile_id.company_id", store=True)
    agent_id = fields.Many2one(related="profile_id.agent_id", store=True)
    batch_id = fields.Many2one(
        "ai.recon.match.batch", required=True, ondelete="restrict"
    )
    statement_line_id = fields.Many2one(
        "account.bank.statement.line", required=True, ondelete="restrict"
    )
    policy_revision = fields.Integer(required=True)
    input_hash = fields.Char(required=True)
    packet = fields.Json(required=True)
    candidate_map = fields.Json(required=True)
    candidates_truncated = fields.Boolean()
    state = fields.Selection(
        [
            (x, x.title())
            for x in ("pending", "assessed", "abstained", "error", "stale", "native")
        ],
        default="pending",
        required=True,
    )
    decision = fields.Json()
    error = fields.Text()
    provider_kind = fields.Char()
    provider_model = fields.Char()
    duration = fields.Float()
    usage = fields.Json()

    _input_unique = models.Constraint(
        "UNIQUE(profile_id, statement_line_id, policy_revision, input_hash)",
        "This input already has an assessment.",
    )
