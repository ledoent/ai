from odoo import api, fields, models
from odoo.exceptions import ValidationError


class AiAgent(models.Model):
    """An agent: an identity plus the set of tools it is allowed to use."""

    _name = "ai.agent"
    _description = "AI Agent"
    _order = "name"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    user_id = fields.Many2one(
        "res.users",
        string="Acts as",
        required=True,
        help="Every tool call is executed with this user's access rights. "
        "Give the agent its own user with the narrowest groups that let it do "
        "its job -- this field is the agent's real security boundary.",
    )
    tool_ids = fields.Many2many("ai.agent.tool", string="Allowed tools")
    run_ids = fields.One2many("ai.agent.run", "agent_id", string="Runs")

    @api.constrains("user_id")
    def _check_not_superuser(self):
        """An agent must never act as the superuser.

        ``ai_agent_surface``'s ``test_sudo_no_audit`` shows why this has to be a
        hard constraint rather than advice: under ``env.su`` every access
        predicate short-circuits to True, so a superuser agent cannot be
        audited, cannot be meaningfully restricted, and makes the allowlist the
        only remaining control.
        """
        for agent in self:
            if agent.user_id.id == self.env.ref("base.user_root").id:
                raise ValidationError(
                    self.env._(
                        "An agent cannot act as the superuser: access checks are "
                        "skipped entirely for that user, so nothing it does can "
                        "be restricted or audited. Give it a dedicated user."
                    )
                )

    def _tool_by_name(self, name):
        """Resolve a tool name against **this agent's allowlist only**."""
        self.ensure_one()
        return self.tool_ids.filtered(lambda t: t.name == name)[:1]
