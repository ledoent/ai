from odoo import api, fields, models
from odoo.exceptions import UserError

from ..models.ai_agent_capability import LEVELS


class AiAgentAuthorityWizard(models.TransientModel):
    """Collect a reason; authority is always checked again by the target action."""

    _name = "ai.agent.authority.wizard"
    _description = "AI Agent Authority Decision"

    operation = fields.Selection(
        [(s, s.title()) for s in ("manual", "veto", "unlock", "undo")],
        required=True,
    )
    capability_id = fields.Many2one("ai.agent.capability")
    call_id = fields.Many2one("ai.agent.call")
    level = fields.Selection(LEVELS, default="observe", required=True)
    reason = fields.Text(required=True)

    @api.model
    def _open(self, operation, capability=None, call=None):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_operation": operation,
                "default_capability_id": capability.id if capability else False,
                "default_call_id": call.id if call else False,
            },
        }

    def action_confirm(self):
        self.ensure_one()
        self.check_access("read")
        if not self.reason or not self.reason.strip():
            raise UserError(self.env._("A reason is required."))
        if self.operation == "undo":
            if not self.call_id:
                raise UserError(self.env._("Choose an invocation."))
            self.call_id.action_undo(self.reason)
        else:
            cap = self.capability_id
            if not cap:
                raise UserError(self.env._("Choose a capability."))
            if self.operation == "manual":
                cap.action_set_level(self.level, self.reason)
            elif self.operation == "veto":
                cap.action_veto(self.reason)
            else:
                cap.action_clear_lock(self.reason)
        return {"type": "ir.actions.act_window_close"}
