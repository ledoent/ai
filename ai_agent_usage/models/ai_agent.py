from odoo import fields, models


class AiAgent(models.Model):
    _inherit = "ai.agent"

    usage_budget_ids = fields.One2many(
        "ai.agent.usage.budget",
        "agent_id",
        string="Usage budgets",
        groups="base.group_system",
    )
