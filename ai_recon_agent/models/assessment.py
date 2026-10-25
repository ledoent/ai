import hashlib
import json

from odoo import api, fields, models
from odoo.exceptions import AccessError


class AiReconAssessment(models.Model):
    _name = "ai.recon.assessment"
    _description = "Reconciliation Candidate Assessment"
    _order = "id desc"

    company_id = fields.Many2one("res.company", required=True, index=True)
    statement_line_id = fields.Many2one(
        "account.bank.statement.line", required=True, ondelete="restrict", index=True
    )
    call_id = fields.Many2one(
        "ai.agent.call", required=True, ondelete="restrict", index=True
    )
    provenance = fields.Selection(
        [("rules", "Rules"), ("llm", "Language model")], required=True
    )
    provider_id = fields.Many2one("ai.agent.provider", ondelete="restrict")
    disclosure_version = fields.Char(required=True)
    packet_hash = fields.Char(required=True, index=True)
    status = fields.Selection(
        [(s, s.title()) for s in ("complete", "ambiguous", "truncated", "failed")],
        required=True,
    )
    candidate_ids = fields.Many2many(
        "account.move.line",
        "ai_recon_assessment_candidate_rel",
        "assessment_id",
        "line_id",
    )
    proposal_ids = fields.One2many("ai.recon.match", "assessment_id")

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(
            self.env._("Assessments are created by the matching service.")
        )

    def write(self, vals):
        raise AccessError(self.env._("Assessments are immutable."))

    def unlink(self):
        raise AccessError(self.env._("Assessment audit history cannot be deleted."))

    @api.model
    def _new(self, vals):
        vals["packet_hash"] = hashlib.sha256(
            json.dumps(vals.pop("packet"), sort_keys=True).encode()
        ).hexdigest()
        return super(AiReconAssessment, self.sudo().with_context({})).create(vals)


class AiReconMatchAllocation(models.Model):
    _name = "ai.recon.match.allocation"
    _description = "Proposed Reconciliation Allocation"
    _order = "sequence, id"

    match_id = fields.Many2one("ai.recon.match", required=True, ondelete="cascade")
    sequence = fields.Integer(default=10)
    move_line_id = fields.Many2one(
        "account.move.line", required=True, ondelete="restrict"
    )
    amount = fields.Monetary(required=True, currency_field="currency_id")
    currency_id = fields.Many2one(related="match_id.currency_id")

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(
            self.env._("Allocations are created by the matching service.")
        )

    def write(self, vals):
        raise AccessError(self.env._("Match allocations are immutable."))

    def unlink(self):
        raise AccessError(self.env._("Match allocation history cannot be deleted."))

    @api.model
    def _new(self, vals):
        return super(AiReconMatchAllocation, self.sudo().with_context({})).create(vals)
