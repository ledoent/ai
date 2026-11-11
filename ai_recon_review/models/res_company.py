from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    ai_recon_reviewer_ids = fields.Many2many(
        "res.users",
        "res_company_ai_recon_reviewer_rel",
        string="Reconciliation reviewers",
        help="Who receives the reconciliation review batch. These are the "
        "people asked to approve what the agent was unsure about, so they "
        "need rights to sign work off -- account.group_account_user or above.",
    )
