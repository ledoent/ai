from odoo import fields, models


class AiAgent(models.Model):
    _inherit = "ai.agent"

    provider_id = fields.Many2one(
        "ai.agent.provider",
        help="When set together with a run's goal, the provider chooses each "
        "next action instead of following a fixed plan.",
    )
    max_steps = fields.Integer(
        default=12,
        required=True,
        help="Hard ceiling on tool calls per run. A model that keeps choosing "
        "tools otherwise runs until something external stops it.",
    )

    def _tool_schemas(self):
        """Derived schemas for exactly the tools this agent is allowed."""
        self.ensure_one()
        return [tool._tool_schema() for tool in self.tool_ids]
