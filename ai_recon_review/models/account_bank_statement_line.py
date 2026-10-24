from odoo import fields, models


class AccountBankStatementLine(models.Model):
    _inherit = "account.bank.statement.line"

    ai_recon_proposal_ids = fields.One2many(
        "ai.recon.proposal", "statement_line_id", string="AI review proposals"
    )
