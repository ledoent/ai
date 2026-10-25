from odoo import fields, models
from odoo.exceptions import AccessError


class AiReconMatchRejectWizard(models.TransientModel):
    _name = "ai.recon.match.reject.wizard"
    _description = "Reject Reconciliation Match"

    match_id = fields.Many2one("ai.recon.match", required=True, readonly=True)
    reason = fields.Text(required=True)

    def action_reject(self):
        self.ensure_one()
        if not self.reason or not self.reason.strip():
            raise AccessError(self.env._("A rejection reason is required."))
        self.match_id.action_reject(self.reason.strip())
        return {"type": "ir.actions.act_window_close"}


class AiReconMatchWithdrawWizard(models.TransientModel):
    _name = "ai.recon.match.withdraw.wizard"
    _description = "Withdraw Approved Reconciliation Match"

    match_id = fields.Many2one("ai.recon.match", required=True, readonly=True)
    reason = fields.Text(required=True)

    def action_withdraw(self):
        self.ensure_one()
        if not self.reason or not self.reason.strip():
            raise AccessError(self.env._("A withdrawal reason is required."))
        self.match_id.action_withdraw_approved(self.reason.strip())
        return {"type": "ir.actions.act_window_close"}
