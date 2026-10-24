from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


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

    capability_ids = fields.One2many("ai.agent.capability", "agent_id", readonly=True)

    @api.model
    def _check_admin(self):
        if not self.env.user.has_group("base.group_system") or self.sudo().with_context(
            active_test=False
        ).search_count(
            [
                ("user_id", "=", self.env.uid),
            ]
        ):
            raise AccessError(
                self.env._("Only human administrators configure authority.")
            )

    @api.model_create_multi
    def create(self, vals_list):
        self._check_admin()
        if any("capability_ids" in vals for vals in vals_list):
            raise AccessError(self.env._("Capabilities are server-owned."))
        agents = super().create(vals_list)
        agents._sync_grants()
        return agents

    def write(self, vals):
        self._check_admin()
        if "capability_ids" in vals:
            raise AccessError(self.env._("Capabilities are server-owned."))
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM ai_agent WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(self.ids)],
        )
        self.invalidate_recordset()
        result = super().write(vals)
        if "tool_ids" in vals:
            self._sync_grants()
        if "user_id" in vals or "active" in vals:
            self.capability_ids._change(
                "Execution identity or active status changed.", level="observe"
            )
        return result

    def _sync_grants(self):
        Capability = self.env["ai.agent.capability"].sudo()
        for agent in self:
            caps = Capability.search([("agent_id", "=", agent.id)])
            for tool in agent.tool_ids:
                cap = caps.filtered(lambda c, t=tool: c.tool_id == t)
                if not cap:
                    Capability._new(
                        {
                            "agent_id": agent.id,
                            "tool_id": tool.id,
                            "policy": tool._policy_signature(),
                        }
                    )
                elif not cap.granted:
                    cap._change("Tool regranted.", granted=True, level="observe")
            for cap in caps.filtered(
                lambda c, a=agent: c.tool_id not in a.tool_ids and c.granted
            ):
                cap._change("Tool revoked.", granted=False, level="observe")

    def _capability(self, tool):
        self.ensure_one()
        return (
            self.env["ai.agent.capability"]
            .sudo()
            .search(
                [
                    ("agent_id", "=", self.id),
                    ("tool_id", "=", tool.id),
                ],
                limit=1,
            )
        )

    def unlink(self):
        self._check_admin()
        return super().unlink()
