# pylint: disable=method-required-super,no-raise-unlink
# Protected audit records deliberately reject public mutation and deletion.
import json

from odoo import api, fields, models
from odoo.exceptions import AccessError


class AiAgentCall(models.Model):
    """Immutable invocation inputs; lifecycle changes belong to the dispatcher."""

    _name = "ai.agent.call"
    _description = "AI Agent Invocation"
    _order = "run_id, sequence"

    run_id = fields.Many2one("ai.agent.run", required=True, ondelete="restrict")
    agent_id = fields.Many2one(related="run_id.agent_id", store=True)
    sequence = fields.Integer(required=True)
    tool_id = fields.Many2one("ai.agent.tool", ondelete="restrict")
    tool_name = fields.Char(required=True)
    model_name = fields.Char()
    method_name = fields.Char()
    policy = fields.Text()
    args = fields.Text(required=True)
    user_id = fields.Many2one("res.users", required=True, ondelete="restrict")
    execution_context = fields.Json(required=True)
    state = fields.Selection(
        [
            (s, s.title())
            for s in (
                "pending",
                "approved",
                "observed",
                "refused",
                "failed",
                "dispatched",
            )
        ],
        required=True,
        default="pending",
    )
    approver_id = fields.Many2one("res.users", ondelete="restrict")
    result = fields.Text()
    capability_id = fields.Many2one("ai.agent.capability", ondelete="restrict")
    capability_epoch = fields.Integer()
    capability_level = fields.Selection(
        [(level, level.title()) for level in ("observe", "propose", "apply")]
    )
    _unique_call = models.Constraint(
        "UNIQUE(run_id, sequence)", "An invocation already exists at this sequence."
    )

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(self.env._("Only the dispatcher can create invocations."))

    def write(self, vals):
        raise AccessError(self.env._("Invocation snapshots are protected."))

    def unlink(self):
        raise AccessError(self.env._("Invocation history cannot be deleted."))

    @api.model
    def _new(self, vals):
        return super(AiAgentCall, self.sudo()).create(vals)

    def _set(self, vals):
        return super(AiAgentCall, self.sudo()).write(vals)

    def _validate(self):
        self.ensure_one()
        run, tool = self.run_id, self.tool_id
        if not tool:
            raise AccessError(self.env._("Agent is not allowed to use this tool."))
        cap = self.capability_id
        cap._refresh_policy()
        if not cap or not cap.granted or cap.epoch != self.capability_epoch:
            raise AccessError(self.env._("Capability changed; start a new invocation."))
        if (
            not tool
            or tool not in run.agent_id.tool_ids
            or not run.agent_id.active
            or run.agent_id.user_id != self.user_id
            or run.execution_user_id != self.user_id
            or not self.user_id.active
            or not set(self.execution_context["allowed_company_ids"]).issubset(
                self.user_id.company_ids.ids
            )
            or self.policy != tool._policy_signature()
        ):
            raise AccessError(
                self.env._("Invocation authority changed; start a new run.")
            )

    def _arguments(self):
        self.ensure_one()
        return json.loads(self.args)
