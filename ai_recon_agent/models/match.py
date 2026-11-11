# pylint: disable=method-required-super,no-raise-unlink,context-overridden

import hashlib
import json

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


class AiReconMatch(models.Model):
    _name = "ai.recon.match"
    _description = "Attributed Reconciliation Match"
    _order = "id desc"

    assessment_id = fields.Many2one(
        "ai.recon.assessment", required=True, ondelete="restrict", index=True
    )
    company_id = fields.Many2one(
        related="assessment_id.company_id", store=True, index=True
    )
    statement_line_id = fields.Many2one(
        related="assessment_id.statement_line_id", store=True, index=True
    )
    currency_id = fields.Many2one(related="statement_line_id.currency_id")
    allocation_ids = fields.One2many("ai.recon.match.allocation", "match_id")
    payload = fields.Json(required=True)
    payload_hash = fields.Char(required=True, index=True)
    economic_key = fields.Char(required=True, index=True)
    provenance = fields.Selection(
        [(s, s.title()) for s in ("rules", "llm", "human", "legacy")],
        required=True,
        default="rules",
    )
    agent_id = fields.Many2one(
        "ai.agent", required=True, ondelete="restrict", index=True
    )
    assessment_call_id = fields.Many2one(
        "ai.agent.call", required=True, ondelete="restrict"
    )
    state = fields.Selection(
        [
            (s, s.title())
            for s in (
                "proposed",
                "approved",
                "applied",
                "rejected",
                "stale",
                "failed",
                "undone",
            )
        ],
        default="proposed",
        required=True,
        index=True,
    )
    requires_posting = fields.Boolean(default=False)
    reviewer_id = fields.Many2one("res.users", readonly=True)
    review_reason = fields.Text(readonly=True)
    resolution_reviewer_id = fields.Many2one("res.users", readonly=True)
    resolution_reason = fields.Text(readonly=True)
    resolved_at = fields.Datetime(readonly=True)
    posting_call_id = fields.Many2one(
        "ai.agent.call", readonly=True, ondelete="restrict"
    )
    reversal_call_id = fields.Many2one(
        "ai.agent.call", readonly=True, ondelete="restrict"
    )
    posting_receipt = fields.Json(readonly=True)
    error = fields.Text(readonly=True)

    _one_live_line = models.UniqueIndex(
        "(statement_line_id) WHERE state IN ('proposed', 'approved', 'applied')",
        "A live attributed proposal already covers this bank transaction.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(self.env._("Matches are created by the matching service."))

    def write(self, vals):
        raise AccessError(self.env._("Match snapshots are immutable."))

    def unlink(self):
        raise AccessError(self.env._("Match history cannot be deleted."))

    @api.model
    def _new(self, vals, allocations):
        vals["payload_hash"] = hashlib.sha256(
            json.dumps(vals["payload"], sort_keys=True).encode()
        ).hexdigest()
        vals["economic_key"] = hashlib.sha256(
            json.dumps(
                [
                    vals["payload"].get("company_id"),
                    vals["payload"].get("statement_line_id"),
                    sorted(line_id for line_id, _amount in allocations),
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        match = super(AiReconMatch, self.sudo().with_context({})).create(vals)
        Allocation = self.env["ai.recon.match.allocation"]
        for sequence, (line_id, amount) in enumerate(allocations, start=1):
            Allocation._new(
                {
                    "match_id": match.id,
                    "move_line_id": line_id,
                    "amount": amount,
                    "sequence": sequence,
                }
            )
        return match

    def _reviewer(self):
        self.ensure_one()
        self.check_access("read")
        profile_users = (
            self.env["res.company"].browse(self.company_id.id).ai_recon_reviewer_ids
        )
        if (
            not self.env.user.has_group("ai_agent_runtime.group_ai_reviewer")
            or self.env.user not in self.agent_id.reviewer_ids
            or self.env.user not in profile_users
            or not self.env.user.has_group("account.group_account_user")
            or self.env["ai.agent"]
            .sudo()
            .with_context(active_test=False)
            .search_count([("user_id", "=", self.env.uid)])
        ):
            raise AccessError(
                self.env._("Review requires an assigned human AI reviewer.")
            )

    def _lock(self):
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM ai_recon_match WHERE id = %s FOR UPDATE", [self.id]
        )
        self.invalidate_recordset()

    def _record_business_judgment(self, verdict, reason):
        self.ensure_one()
        call = self.assessment_call_id
        call._review_lock()
        self._reviewer()
        if (
            call.state != "dispatched"
            or call.model_name != "ai.recon.match.service"
            or call.method_name != "match_items"
            or call.agent_id != self.agent_id
        ):
            raise UserError(
                self.env._(
                    "Only the successful originating match call can earn evidence."
                )
            )
        self._lock()
        if self.state != "proposed":
            raise UserError(self.env._("This match was already reviewed."))
        evidence = self.env["ai.agent.evidence"]._record_business(
            call, verdict, reason, self.economic_key
        )
        if evidence:
            call.capability_id._check_demotion()

    def action_approve(self, reason=""):
        for match in self:
            if match.state != "proposed":
                raise UserError(self.env._("Only a proposed match can be reviewed."))
            match._record_business_judgment("accept", reason)
            match._set(
                {
                    "state": "approved",
                    "reviewer_id": self.env.uid,
                    "review_reason": reason,
                }
            )
        return True

    def action_reject(self, reason):
        if not reason or not reason.strip():
            raise UserError(self.env._("A rejection reason is required."))
        for match in self:
            if match.state != "proposed":
                raise UserError(self.env._("Only a proposed match can be reviewed."))
            match._record_business_judgment("reject", reason.strip())
            match._set(
                {
                    "state": "rejected",
                    "reviewer_id": self.env.uid,
                    "review_reason": reason,
                }
            )
        return True

    def action_open_withdraw(self):
        self.ensure_one()
        self.check_access("read")
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Withdraw approved match"),
            "res_model": "ai.recon.match.withdraw.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_match_id": self.id},
        }

    def action_withdraw_approved(self, reason):
        if not reason or not reason.strip():
            raise UserError(self.env._("A withdrawal reason is required."))
        for match in self:
            call = match.assessment_call_id
            # Keep reviewer and row-lock order consistent with match judgment.
            call._review_lock()
            match._reviewer()
            match._lock()
            if match.state != "approved" or call.state != "dispatched":
                raise UserError(
                    self.env._("Only a reviewed, unposted match can be withdrawn.")
                )
            if call.undo_state == "none":
                call.action_undo("Reviewer withdrawal: " + reason.strip())
            if call.undo_state != "failed":
                raise UserError(
                    self.env._("The origin call is not in a failed-undo state.")
                )
            match._set(
                {
                    "state": "stale",
                    "error": call.undo_error,
                    "resolution_reviewer_id": self.env.uid,
                    "resolution_reason": reason.strip(),
                    "resolved_at": fields.Datetime.now(),
                }
            )
        return True

    def action_open_reject(self):
        self.ensure_one()
        self.check_access("read")
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Reject reconciliation match"),
            "res_model": "ai.recon.match.reject.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_match_id": self.id},
        }

    def _set(self, vals):
        return super(AiReconMatch, self.sudo()).write(vals)
