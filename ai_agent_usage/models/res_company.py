from odoo import api, fields, models
from odoo.exceptions import AccessError


class ResCompany(models.Model):
    _inherit = "res.company"

    ai_usage_enabled = fields.Boolean(
        string="Enforce AI usage budgets",
        groups="base.group_system",
        help="When enabled, provider requests require a rate card and request "
        "ceiling and reserve that ceiling against configured budgets.",
    )
    ai_usage_monthly_limit = fields.Monetary(
        string="Monthly AI budget",
        currency_field="currency_id",
        groups="base.group_system",
        help="Zero means that this company has no monthly cap. Reservations "
        "and settled cost use this company's currency.",
    )
    ai_usage_timezone = fields.Char(
        string="AI budget timezone",
        default="UTC",
        groups="base.group_system",
        help="Calendar-month boundaries for AI budgets. Invalid or empty values "
        "fall back to UTC.",
    )
    ai_usage_tracking_started_at = fields.Datetime(
        string="AI usage tracking started",
        readonly=True,
        groups="base.group_system",
        help="Earlier provider calls were not imported or backfilled.",
    )
    ai_usage_lock_version = fields.Integer(default=0, readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        if any("ai_usage_lock_version" in vals for vals in vals_list):
            raise AccessError(
                self.env._("The AI usage reservation lock is server-owned.")
            )
        return super().create(vals_list)

    def write(self, vals):
        if "ai_usage_lock_version" in vals:
            raise AccessError(
                self.env._("The AI usage reservation lock is server-owned.")
            )
        if {
            "ai_usage_enabled",
            "ai_usage_monthly_limit",
            "ai_usage_timezone",
        }.intersection(vals):
            for company_id in sorted(self.ids):
                self.env.cr.execute(
                    """UPDATE res_company
                          SET ai_usage_lock_version = ai_usage_lock_version + 1
                        WHERE id = %s""",
                    [company_id],
                )
            self.invalidate_recordset(["ai_usage_lock_version"])
        return super().write(vals)
