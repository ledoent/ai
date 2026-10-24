# pylint: disable=method-required-super,no-raise-unlink
# Only private server methods may mutate authority records.
from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

LEVELS = [(level, level.title()) for level in ("observe", "propose", "apply")]


class AiAgentCapability(models.Model):
    _name = "ai.agent.capability"
    _description = "AI Agent Capability"
    _rec_name = "tool_id"

    agent_id = fields.Many2one("ai.agent", required=True, ondelete="restrict")
    tool_id = fields.Many2one("ai.agent.tool", required=True, ondelete="restrict")
    level = fields.Selection(LEVELS, default="observe", required=True)
    granted = fields.Boolean(default=True)
    promotion_locked = fields.Boolean(default=False)
    epoch = fields.Integer(default=1, required=True)
    epoch_started = fields.Datetime(default=fields.Datetime.now, required=True)
    policy = fields.Text(required=True)
    reason = fields.Text()

    _unique_grant = models.Constraint(
        "UNIQUE(agent_id, tool_id)", "A capability already exists for this grant."
    )

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(
            self.env._("Capabilities are synchronized from agent grants.")
        )

    def write(self, vals):
        raise AccessError(self.env._("Use a reasoned authority transition."))

    def unlink(self):
        raise AccessError(self.env._("Keep revoked capabilities for attribution."))

    @api.model
    def _new(self, vals):
        return super(AiAgentCapability, self.sudo()).create(vals)

    def _set(self, vals):
        return super(AiAgentCapability, self.sudo()).write(vals)

    def _lock(self):
        if not self:
            return
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM ai_agent_capability WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(self.ids)],
        )
        self.invalidate_recordset()

    def _change(self, reason, **vals):
        self._lock()
        for cap in self:
            cap._set(
                {
                    "epoch": cap.epoch + 1,
                    "epoch_started": fields.Datetime.now(),
                    "policy": cap.tool_id._policy_signature(),
                    "reason": reason,
                    **vals,
                }
            )

    def _refresh_policy(self):
        self._lock()
        for cap in self:
            if cap.policy != cap.tool_id._policy_signature():
                cap._change("Tool or reversal contract changed.", level="observe")

    def action_set_level(self, level, reason):
        self.env["ai.agent"]._check_admin()
        if level not in dict(LEVELS) or not reason or not reason.strip():
            raise UserError(self.env._("A valid level and a reason are required."))
        for cap in self:
            cap._change(reason, level=level)
        return True

    def action_clear_lock(self, reason):
        self.env["ai.agent"]._check_admin()
        if not reason or not reason.strip():
            raise UserError(self.env._("A reason is required."))
        self._change(reason, promotion_locked=False)
        return True
