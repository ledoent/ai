import json
from typing import ClassVar

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .protected import digest


class MatchProposal(models.Model):
    _name = "ai.recon.match.proposal"
    _description = "Immutable Bank Match Proposal"
    _inherit: ClassVar[list[str]] = ["ai.recon.protected"]
    _order = "id desc"
    _rec_name = "statement_line_id"

    profile_id = fields.Many2one("ai.recon.profile", required=True, ondelete="restrict")
    company_id = fields.Many2one(related="profile_id.company_id", store=True)
    agent_id = fields.Many2one(related="profile_id.agent_id", store=True)
    batch_id = fields.Many2one("ai.recon.match.batch", ondelete="restrict")
    assessment_id = fields.Many2one("ai.recon.assessment", ondelete="restrict")
    statement_line_id = fields.Many2one(
        "account.bank.statement.line", required=True, ondelete="restrict"
    )
    policy_revision = fields.Integer(required=True)
    payload = fields.Json(required=True)
    payload_hash = fields.Char(required=True)
    economic_key = fields.Char(required=True)
    provenance = fields.Selection(
        [(x, x.title()) for x in ("rule", "llm", "human")], required=True
    )
    rationale = fields.Text()
    origin_call_id = fields.Many2one("ai.agent.call", ondelete="restrict")
    supersedes_id = fields.Many2one("ai.recon.match.proposal", ondelete="restrict")
    state = fields.Selection(
        [
            (x, x.title())
            for x in (
                "proposed",
                "observed",
                "approved",
                "applied",
                "rejected",
                "blocked",
                "superseded",
                "reversed",
                "external",
            )
        ],
        default="proposed",
        required=True,
    )
    error = fields.Text()
    preview = fields.Text(compute="_compute_preview")
    allocation_ids = fields.One2many("ai.recon.match.allocation", "proposal_id")
    _one_call = models.Constraint(
        "UNIQUE(origin_call_id)", "An invocation belongs to one proposal."
    )
    _one_live = models.UniqueIndex(
        "(statement_line_id) WHERE state IN ('proposed','observed','approved')",
        "This transaction already has a live matching proposal.",
    )

    @api.depends("payload")
    def _compute_preview(self):
        for p in self:
            p.preview = json.dumps(p.payload, indent=2, default=str)

    def _check_reviewer(self):
        self.check_access("read")
        self.agent_id._check_reviewer()
        if (
            not self.env.user.has_group("account.group_account_user")
            or self.company_id not in self.env.user.company_ids
        ):
            raise AccessError(
                self.env._(
                    "Bank matching review requires accounting review rights in this company."
                )
            )

    def _validate_snapshot(self):
        self.ensure_one()
        self.profile_id._check_ready()
        if (
            self.policy_revision != self.profile_id.revision
            or self.payload_hash != digest(self.payload)
            or self.state in ("superseded", "rejected", "reversed", "external")
        ):
            raise UserError(self.env._("The matching snapshot is no longer current."))
        self.env["ai.recon.match.service"]._validate_plan(
            self.profile_id, self.statement_line_id, self.payload
        )

    def action_accept(self):
        for p in self:
            p._check_reviewer()
            p.origin_call_id.action_accept()
        return True

    def action_reject(self):
        for p in self:
            p._check_reviewer()
            p.origin_call_id.action_reject("Rejected by accounting reviewer")
        return True

    def action_execute(self):
        for p in self:
            p._check_reviewer()
            p.origin_call_id.run_id.action_run_to_completion()
        return True

    def action_native(self):
        self.ensure_one()
        self.check_access("read")
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.bank.statement.line",
            "res_id": self.statement_line_id.id,
            "view_mode": "form",
        }

    def action_replace(self):
        self.ensure_one()
        self._check_reviewer()
        return {
            "type": "ir.actions.act_window",
            "res_model": "ai.recon.replace.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_proposal_id": self.id},
        }

    def action_undo(self):
        self.ensure_one()
        self._check_reviewer()
        return self.env["ai.recon.undo.wizard"]._prepare(self)

    def _submit(self):
        self.ensure_one()
        method = "post_match" if self.payload["posting"] else "match_items"
        tool = self.env.ref("ai_recon_agent.tool_" + method)
        run = (
            self.env["ai.agent.run"]
            .sudo()
            .create(
                {
                    "agent_id": self.agent_id.id,
                    "company_ids": [fields.Command.set(self.company_id.ids)],
                    "plan": json.dumps(
                        [
                            {
                                "tool": tool.name,
                                "args": {
                                    "proposal_id": self.id,
                                    "payload_hash": self.payload_hash,
                                },
                            }
                        ]
                    ),
                }
            )
        )
        run.action_start()
        run._advance()
        return run


class Allocation(models.Model):
    _name = "ai.recon.match.allocation"
    _description = "Frozen Matching Allocation"
    _inherit: ClassVar[list[str]] = ["ai.recon.protected"]

    proposal_id = fields.Many2one(
        "ai.recon.match.proposal", required=True, ondelete="restrict"
    )
    company_id = fields.Many2one(related="proposal_id.company_id", store=True)
    agent_id = fields.Many2one(related="proposal_id.agent_id", store=True)
    move_line_id = fields.Many2one(
        "account.move.line", required=True, ondelete="restrict"
    )
    transaction_amount = fields.Float(required=True)
    target_amount = fields.Float(required=True)


class MatchBatch(models.Model):
    _name = "ai.recon.match.batch"
    _description = "Company Matching Intake Batch"
    _inherit: ClassVar[list[str]] = ["ai.recon.protected"]
    _order = "id desc"

    profile_id = fields.Many2one("ai.recon.profile", required=True, ondelete="restrict")
    company_id = fields.Many2one(related="profile_id.company_id", store=True)
    agent_id = fields.Many2one(related="profile_id.agent_id", store=True)
    date = fields.Datetime(default=fields.Datetime.now)
    items_seen = fields.Integer()
    truncated = fields.Boolean()
    assessment_ids = fields.One2many("ai.recon.assessment", "batch_id")
    proposal_ids = fields.One2many("ai.recon.match.proposal", "batch_id")
    digest_mail_id = fields.Many2one("mail.mail", ondelete="set null")
