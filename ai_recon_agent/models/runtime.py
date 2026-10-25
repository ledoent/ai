# pylint: disable=missing-return,context-overridden
from odoo import fields, models
from odoo.exceptions import AccessError, UserError


class Run(models.Model):
    _inherit = "ai.agent.run"

    def _snapshot_values(self, tool, name, args):
        vals = super()._snapshot_values(tool, name, args)
        if tool.model_name == "ai.recon.match.service":
            p = (
                self.env["ai.recon.match.proposal"]
                .sudo()
                .browse(args.get("proposal_id"))
                .exists()
            )
            if (
                not p
                or p.agent_id != self.agent_id
                or p.payload_hash != args.get("payload_hash")
                or p.origin_call_id
            ):
                raise AccessError(
                    self.env._("A fresh matching proposal from this agent is required.")
                )
            vals.update(
                recon_proposal_id=p.id,
                force_approval=p.payload["force_approval"],
                approval_reason=p.payload["approval_reason"],
                promotion_key=p.economic_key,
                promotion_eligible=p.provenance != "human",
            )
        return vals


class Call(models.Model):
    _inherit = "ai.agent.call"

    recon_proposal_id = fields.Many2one("ai.recon.match.proposal", ondelete="restrict")
    _proposal_unique = models.Constraint(
        "UNIQUE(recon_proposal_id)", "A proposal can originate only one invocation."
    )

    def _on_created(self):
        super()._on_created()
        for call in self.filtered("recon_proposal_id"):
            call.recon_proposal_id._set({"origin_call_id": call.id})

    def _validate_business(self):
        super()._validate_business()
        for call in self.filtered("recon_proposal_id"):
            try:
                call.recon_proposal_id.with_user(call.user_id).with_context(
                    dict(call.execution_context)
                )._validate_snapshot()
            except UserError as exc:
                raise AccessError(str(exc)) from exc

    def _on_outcome(self):
        super()._on_outcome()
        for call in self.filtered("recon_proposal_id"):
            p = call.recon_proposal_id
            judgment = call.evidence_ids.filtered(lambda e: e.kind == "judgment")
            state = {
                "pending": "proposed",
                "approved": "approved",
                "observed": "observed",
                "dispatched": "applied",
                "undone": "reversed",
                "failed": "blocked",
                "refused": "blocked",
            }[call.state]
            if judgment.verdict == "reject":
                state = "rejected"
            p._set(
                {
                    "state": state,
                    "error": call.undo_error
                    or (call.result if state == "blocked" else False),
                }
            )

    def _validate_undo_request(self, quote_id):
        super()._validate_undo_request(quote_id)
        if self.recon_proposal_id:
            quote = self.env["ai.recon.undo.wizard"].browse(quote_id).exists()
            if not quote or quote.proposal_id != self.recon_proposal_id:
                raise AccessError(
                    self.env._(
                        "Confirm the protected accounting reversal preview first."
                    )
                )
            quote._validate_quote()

    def action_open_undo(self):
        if self.recon_proposal_id:
            return self.recon_proposal_id.action_undo()
        return super().action_open_undo()
