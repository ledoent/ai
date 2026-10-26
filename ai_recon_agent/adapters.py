import json

from odoo.exceptions import UserError

from odoo.addons.ai_agent_runtime.adapters import ReversalAdapter, current_invocation


class MatchProposalAdapter(ReversalAdapter):
    """Reverse only an unreviewed proposal made by the originating call."""

    database_local = True
    target = ("ai.recon.match.service", "match_items")
    contract_id = "ai-recon-match-proposal-v2"
    business_evidence_required = True

    def prepare(self, record, args):
        binding = current_invocation()
        if not binding or binding[3:5] != self.target:
            raise UserError(record.env._("Missing trusted match invocation."))
        if json.loads(binding[5]) != args:
            raise UserError(
                record.env._("Match arguments changed after authorization.")
            )
        line = (
            record.env["account.bank.statement.line"]
            .browse(args.get("statement_line_id"))
            .exists()
        )
        if not line:
            raise UserError(record.env._("Bank transaction no longer exists."))
        candidates = record.env["account.move.line"].browse(
            args.get("candidate_line_ids", [])
        )
        record._lock_facts(line, candidates)
        return {"statement_line_id": line.id, "call_id": binding[0]}

    def capture(self, record, prepared, result):
        match = record.env["ai.recon.match"].sudo().browse(result["match_id"]).exists()
        if (
            not match
            or match.state != "proposed"
            or match.assessment_call_id.id != prepared["call_id"]
        ):
            raise UserError(
                record.env._("The execution did not create its own pending proposal.")
            )
        return {
            "match_id": match.id,
            "payload_hash": match.payload_hash,
            "economic_key": match.economic_key,
            "call_id": prepared["call_id"],
            "statement_line_id": prepared["statement_line_id"],
        }

    def reverse(self, record, receipt):
        binding = current_invocation()
        if not binding or binding[0] != receipt.get("call_id"):
            raise UserError(record.env._("Undo receipt belongs to another invocation."))
        match = record.env["ai.recon.match"].sudo().browse(receipt["match_id"]).exists()
        if not match:
            raise UserError(record.env._("Match proposal no longer exists."))
        match._lock()
        if (
            match.state != "proposed"
            or match.payload_hash != receipt.get("payload_hash")
            or match.economic_key != receipt.get("economic_key")
            or match.assessment_call_id.id != receipt.get("call_id")
            or match.statement_line_id.id != receipt.get("statement_line_id")
        ):
            raise UserError(
                record.env._(
                    "The match proposal changed after execution; it cannot be "
                    "automatically withdrawn."
                )
            )
        match._set({"state": "undone", "reversal_call_id": binding[0]})
