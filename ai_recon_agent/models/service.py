import json
import math

from psycopg2.errors import UniqueViolation

from odoo import api, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.float_utils import float_compare


class AiReconMatchService(models.AbstractModel):
    _name = "ai.recon.match.service"
    _description = "Trusted reconciliation candidate and posting service"

    @api.model
    def _ai_reversal_adapter(self, method_name):
        if method_name == "match_items":
            from ..adapters import MatchProposalAdapter

            return MatchProposalAdapter()
        return None

    @api.model
    def _execution_company(self):
        allowed = self.env.context.get("allowed_company_ids", [])
        companies = self.env["res.company"].browse(allowed).exists()
        if len(companies) != 1 or companies.id not in self.env.user.company_ids.ids:
            raise AccessError(
                self.env._("A reconciliation run must use exactly one allowed company.")
            )
        company = companies
        if not company.ai_recon_agent_enabled:
            raise AccessError(
                self.env._("Attributed reconciliation is not enabled for this company.")
            )
        return company

    @api.model
    def _lock_facts(self, line, candidates):
        """Serialize with native accountants and refresh all snapshotted facts."""
        line.flush_recordset()
        candidates.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM account_move_line WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(sorted(set(candidates.ids + line.move_id.line_ids.ids)))],
        )
        self.env.cr.execute(
            "SELECT id FROM account_bank_statement_line WHERE id = %s FOR UPDATE",
            [line.id],
        )
        line.invalidate_recordset()
        line.move_id.invalidate_recordset()
        line.move_id.line_ids.invalidate_recordset()
        candidates.invalidate_recordset()

    @api.model
    def _financial_facts(self, line, candidates):
        return {
            "line": {
                "id": line.id,
                "company_id": line.company_id.id,
                "amount": line.amount,
                "date": str(line.move_id.date),
                "journal_id": line.journal_id.id,
                "partner_id": line.partner_id.id,
                "currency_id": line.currency_id.id,
                "foreign_currency_id": line.foreign_currency_id.id,
                "amount_currency": line.amount_currency,
            },
            "statement_move_lines": [
                {
                    "id": move_line.id,
                    "company_id": move_line.company_id.id,
                    "account_id": move_line.account_id.id,
                    "partner_id": move_line.partner_id.id,
                    "currency_id": move_line.currency_id.id,
                    "balance": move_line.balance,
                    "debit": move_line.debit,
                    "credit": move_line.credit,
                    "amount_currency": move_line.amount_currency,
                    "reconciled": move_line.reconciled,
                    "full_reconcile_id": move_line.full_reconcile_id.id,
                    "write_date": str(move_line.write_date),
                }
                for move_line in line.move_id.line_ids.sorted("id")
            ],
            "candidates": [
                {
                    "id": aml.id,
                    "company_id": aml.company_id.id,
                    "date": str(aml.date),
                    "account_id": aml.account_id.id,
                    "account_reconcile": aml.account_id.reconcile,
                    "account_type": aml.account_id.account_type,
                    "partner_id": aml.partner_id.id,
                    "currency_id": aml.currency_id.id,
                    "balance": aml.balance,
                    "amount_currency": aml.amount_currency,
                    "residual": aml.amount_residual,
                    "residual_currency": aml.amount_residual_currency,
                    "write_date": str(aml.write_date),
                    "move_id": aml.move_id.id,
                }
                for aml in candidates.sorted("id")
            ],
        }

    @api.model
    def _assert_unlocked_moves(self, company, moves):
        """Refuse reconciliation in periods closed by any effective lock date.

        Odoo's company API accounts for soft, hard, and exception lock dates. The
        statement and candidate moves are checked before we alter the suspense
        account or create any partial reconciliation.
        """
        for move in moves:
            journal_type = move.journal_id.type
            violations = company._get_lock_date_violations(
                move.date,
                fiscalyear=True,
                sale=journal_type == "sale",
                purchase=journal_type == "purchase",
                tax=True,
                hard=True,
            )
            if violations:
                raise UserError(
                    self.env._(
                        "Reconciliation is blocked by accounting lock dates: %s",
                        self.env["res.company"]._format_lock_dates(violations),
                    )
                )

    @api.model
    def _execution_run(self, binding, company):
        run = self.env["ai.agent.run"].browse(binding[1]).exists()
        call = self.env["ai.agent.call"].browse(binding[0]).exists()
        if (
            not run
            or not call
            or call.run_id != run
            or call.user_id.id != self.env.uid
            or binding[2] != self.env.uid
            or run.execution_user_id.id != self.env.uid
            or call.agent_id != run.agent_id
            or company.id not in run.execution_context.get("allowed_company_ids", [])
        ):
            raise AccessError(self.env._("Reconciliation invocation identity changed."))
        if (
            run._is_goal_driven()
            and company.ai_recon_agent_provider_id != run.provider_snapshot_id
        ):
            raise AccessError(
                self.env._(
                    "LLM matching requires the provider explicitly selected for "
                    "this company."
                )
            )
        return run

    @api.model
    def _assert_invocation(self, method, args):
        from odoo.addons.ai_agent_runtime.adapters import current_invocation

        binding = current_invocation()
        if not binding or binding[3:5] != (self._name, method):
            raise AccessError(
                self.env._(
                    "This service must be called from an authorized agent invocation."
                )
            )
        try:
            frozen = json.loads(binding[5])
        except (TypeError, ValueError) as exc:
            raise AccessError(self.env._("Invalid invocation snapshot.")) from exc
        if method == "match_items" and "allocations" not in frozen:
            frozen["allocations"] = None
        if frozen != args:
            raise AccessError(
                self.env._("Arguments differ from the authorized invocation snapshot.")
            )
        if method == "post_match":
            try:
                policy = json.loads(binding[6])
            except (TypeError, ValueError) as exc:
                raise AccessError(self.env._("Invalid tool policy snapshot.")) from exc
            if (
                len(policy) < 4
                or not policy[3]
                or binding[8] != "approved"
                or not binding[9]
            ):
                raise AccessError(
                    self.env._("Posting requires approval of this exact invocation.")
                )
        return binding

    @api.model
    def _candidate_domain(self, company, line):
        return [
            ("company_id", "=", company.id),
            ("partner_id", "=", line.partner_id.id),
            ("parent_state", "=", "posted"),
            ("reconciled", "=", False),
            ("account_id.reconcile", "=", True),
            (
                "account_id.account_type",
                "in",
                ["asset_receivable", "liability_payable"],
            ),
            ("currency_id", "=", company.currency_id.id),
        ]

    @api.model
    def _positive_id(self, value, label):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise UserError(self.env._("%s must be a positive integer.", label))

    @api.readonly
    @api.model
    def candidate_packet(self, statement_line_id):
        """Return a bounded, minimized list for rules or an opted-in LLM run."""
        self._positive_id(statement_line_id, self.env._("Statement line ID"))
        args = {"statement_line_id": statement_line_id}
        binding = self._assert_invocation("candidate_packet", args)
        company = self._execution_company()
        self._execution_run(binding, company)
        line = (
            self.env["account.bank.statement.line"].browse(statement_line_id).exists()
        )
        if not line or line.company_id != company or line.is_reconciled:
            raise UserError(self.env._("The bank transaction is no longer eligible."))
        if line.currency_id != company.currency_id or line.foreign_currency_id:
            raise UserError(
                self.env._("Foreign-currency bank transactions need accounting review.")
            )
        if not line.partner_id:
            return {"status": "abstain", "reason": "No verified bank counterparty."}
        if self.env["ai.recon.proposal"].search_count(
            [
                ("statement_line_id", "=", line.id),
                ("state", "in", ["observed", "proposed", "approved", "noted"]),
            ]
        ):
            return {
                "status": "abstain",
                "reason": "A legacy review proposal already owns this transaction.",
            }
        _liquidity, suspense, other = line._seek_for_lines()
        if not suspense or other:
            return {"status": "abstain", "reason": "Unsupported bank counterpart."}
        candidates = self.env["account.move.line"].search(
            self._candidate_domain(company, line),
            order="date, id",
            limit=company.ai_recon_agent_max_candidates + 1,
        )
        truncated = len(candidates) > company.ai_recon_agent_max_candidates
        candidates = candidates[: company.ai_recon_agent_max_candidates]
        packet = {
            "status": "truncated" if truncated else "candidates",
            "disclosure_version": company.ai_recon_agent_disclosure_version,
            "bank_transaction": {
                "id": line.id,
                "amount": line.amount,
                "currency": line.currency_id.name,
                "date": str(line.move_id.date),
                "reference": (line.payment_ref or "")[:160],
            },
            "candidates": [
                {
                    "id": aml.id,
                    "document": aml.move_id.name,
                    "reference": (
                        aml.move_id.ref or aml.move_id.payment_reference or ""
                    )[:120],
                    "date": str(aml.date),
                    "open_amount": aml.amount_residual,
                    "currency": aml.currency_id.name,
                }
                for aml in candidates
            ],
            "candidate_set_complete": not truncated,
        }
        return packet

    @api.model
    def match_items(  # noqa: C901
        self, statement_line_id, candidate_line_ids, allocations=None
    ):
        self._positive_id(statement_line_id, self.env._("Statement line ID"))
        args = {
            "statement_line_id": statement_line_id,
            "candidate_line_ids": candidate_line_ids,
            "allocations": allocations,
        }
        binding = self._assert_invocation("match_items", args)
        company = self._execution_company()
        run = self._execution_run(binding, company)
        line = (
            self.env["account.bank.statement.line"].browse(statement_line_id).exists()
        )
        if not isinstance(candidate_line_ids, list) or any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in candidate_line_ids
        ):
            raise UserError(
                self.env._("Candidate line IDs must be a list of integers.")
            )
        if not line or line.company_id != company or line.is_reconciled:
            raise UserError(self.env._("The bank transaction is no longer eligible."))
        if line.currency_id != company.currency_id or line.foreign_currency_id:
            raise UserError(
                self.env._("Foreign-currency bank transactions need accounting review.")
            )
        if not line.partner_id:
            raise UserError(self.env._("A verified bank counterparty is required."))
        if self.env["ai.recon.proposal"].search_count(
            [
                ("statement_line_id", "=", line.id),
                ("state", "in", ["observed", "proposed", "approved", "noted"]),
            ]
        ):
            raise UserError(
                self.env._(
                    "A legacy proposal already owns this transaction; resolve it first."
                )
            )
        candidates = self.env["account.move.line"].browse(candidate_line_ids).exists()
        if len(candidates) != len(set(candidate_line_ids)) or not candidates:
            raise UserError(
                self.env._("The candidate list is empty or contains missing records.")
            )
        if len(candidates) > company.ai_recon_agent_max_allocations:
            raise UserError(self.env._("The allocation limit was exceeded."))
        if len(candidates) > company.ai_recon_agent_max_candidates:
            raise UserError(self.env._("The candidate packet limit was exceeded."))
        allowed = self.env["account.move.line"].search(
            self._candidate_domain(company, line),
            order="date, id",
            limit=company.ai_recon_agent_max_candidates + 1,
        )
        if len(allowed) > company.ai_recon_agent_max_candidates:
            raise UserError(
                self.env._(
                    "Candidate set is truncated; this match requires manual review."
                )
            )
        if any(candidate not in allowed for candidate in candidates):
            raise UserError(
                self.env._(
                    "Candidates must come from the bounded company candidate packet."
                )
            )
        self._lock_facts(line, candidates)
        if line.is_reconciled or any(
            aml.reconciled or aml.parent_state != "posted" for aml in candidates
        ):
            raise UserError(
                self.env._("The bank transaction or a candidate changed; assess again.")
            )
        if self.env["ai.recon.match"].search_count(
            [
                ("statement_line_id", "=", line.id),
                ("state", "in", ["proposed", "approved", "applied"]),
            ]
        ):
            raise UserError(
                self.env._("A live match proposal already covers this transaction.")
            )
        if self.env["ai.recon.proposal"].search_count(
            [
                ("statement_line_id", "=", line.id),
                ("state", "in", ["observed", "proposed", "approved", "noted"]),
            ]
        ):
            raise UserError(
                self.env._(
                    "A legacy proposal already owns this transaction; resolve it first."
                )
            )
        if any(
            aml.company_id != company
            or aml.parent_state != "posted"
            or aml.reconciled
            or aml.account_id.reconcile is not True
            or aml.account_id.account_type
            not in ("asset_receivable", "liability_payable")
            or aml.currency_id != company.currency_id
            or aml.partner_id != line.partner_id
            for aml in candidates
        ):
            raise UserError(
                self.env._(
                    "Candidates must be posted, open, reconcilable lines for the same "
                    "partner and company."
                )
            )
        if len(candidates) > 1 and len(candidates.account_id) != 1:
            raise UserError(
                self.env._("Cross-account grouped matches require accounting review.")
            )
        liquidity, suspense, other = line._seek_for_lines()
        if not liquidity or not suspense or other:
            raise UserError(
                self.env._(
                    "Only a bank transaction with a single unresolved suspense "
                    "counterpart is supported."
                )
            )
        if allocations:
            if not isinstance(allocations, list) or len(allocations) != len(candidates):
                raise UserError(
                    self.env._("Allocations must match the candidate list.")
                )
            try:
                allocation_map = {}
                for item in allocations:
                    line_id = item["line_id"]
                    raw_value = item["amount"]
                    if isinstance(raw_value, bool):
                        raise TypeError("boolean amount")
                    value = float(raw_value)
                    if (
                        not isinstance(line_id, int)
                        or isinstance(line_id, bool)
                        or not math.isfinite(value)
                    ):
                        raise ValueError("invalid allocation value")
                    allocation_map[line_id] = value
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise UserError(self.env._("Malformed allocation list.")) from exc
            if set(allocation_map) != set(candidates.ids):
                raise UserError(
                    self.env._(
                        "Allocation lines must match the candidate list exactly."
                    )
                )
            if any(value * line.amount <= 0 for value in allocation_map.values()):
                raise UserError(
                    self.env._("Allocation signs must match the bank transaction.")
                )
        elif len(candidates) == 1:
            allocation_map = {candidates.id: float(line.amount)}
        else:
            allocation_map = {aml.id: float(aml.amount_residual) for aml in candidates}
        if not self.env.user.has_group("account.group_account_invoice"):
            raise AccessError(self.env._("The execution user needs accounting access."))
        amount = sum(allocation_map.values())
        if not amount or (amount > 0) != (line.amount > 0):
            raise UserError(
                self.env._(
                    "The allocation direction or total does not match the bank amount."
                )
            )
        if len(candidates) > 1 and float_compare(
            amount, line.amount, precision_rounding=line.currency_id.rounding
        ):
            raise UserError(
                self.env._(
                    "Grouped matching is limited to exact bank totals; review "
                    "partial groups manually."
                )
            )
        if any(
            float_compare(
                abs(allocation_map[aml.id]),
                abs(aml.amount_residual),
                precision_rounding=line.currency_id.rounding,
            )
            > 0
            for aml in candidates
        ):
            raise UserError(self.env._("An allocation exceeds its open balance."))
        if len(candidates) == 1 and float_compare(
            amount, line.amount, precision_rounding=line.currency_id.rounding
        ):
            raise UserError(
                self.env._("A single allocation must match the bank amount.")
            )
        facts = self._financial_facts(line, candidates)
        provenance = "llm" if run._is_goal_driven() else "rules"
        execution_call_id = binding[0]
        assessment_packet = {
            "company_id": company.id,
            "disclosure_version": company.ai_recon_agent_disclosure_version,
            "facts": facts,
            "bank_reference": (line.payment_ref or "")[:160],
            "candidates": [
                {
                    "id": aml.id,
                    "document": aml.move_id.name,
                    "reference": (
                        aml.move_id.ref or aml.move_id.payment_reference or ""
                    )[:120],
                    "date": str(aml.date),
                    "open_amount": aml.amount_residual,
                    "currency": aml.currency_id.name,
                }
                for aml in candidates.sorted("id")
            ],
        }
        assessment = self.env["ai.recon.assessment"]._new(
            {
                "company_id": company.id,
                "statement_line_id": line.id,
                "call_id": execution_call_id,
                "provenance": provenance,
                "provider_id": run.provider_snapshot_id.id
                if provenance == "llm"
                else False,
                "disclosure_version": company.ai_recon_agent_disclosure_version,
                "status": "complete",
                "candidate_ids": [(6, 0, candidates.ids)],
                "packet": assessment_packet,
            }
        )
        proposed = []
        for aml in candidates:
            proposed.append((aml.id, allocation_map[aml.id]))
        if len(candidates) > 1 and any(
            float_compare(
                allocation_map[aml.id],
                aml.amount_residual,
                precision_rounding=line.currency_id.rounding,
            )
            for aml in candidates
        ):
            raise UserError(
                self.env._(
                    "Grouped allocations must reconcile each selected document "
                    "in full; partial groups require manual review."
                )
            )
        payload = {
            "statement_line_id": line.id,
            "statement_move_id": line.move_id.id,
            "facts": facts,
            "company_id": company.id,
            "journal_id": line.journal_id.id,
            "currency_id": line.currency_id.id,
            "bank_amount": line.amount,
            "candidate_ids": candidates.ids,
            "allocation_map": allocation_map,
            "account_id": candidates[0].account_id.id,
            "partner_id": candidates[0].partner_id.id,
            "requires_posting": True,
            "originating_call_id": execution_call_id,
        }
        try:
            with self.env.cr.savepoint():
                match = self.env["ai.recon.match"]._new(
                    {
                        "assessment_id": assessment.id,
                        "agent_id": run.agent_id.id,
                        "assessment_call_id": execution_call_id,
                        "payload": payload,
                        "provenance": provenance,
                        "state": "proposed",
                        "requires_posting": payload["requires_posting"],
                    },
                    proposed,
                )
        except UniqueViolation as exc:
            if getattr(exc.diag, "constraint_name", None) == (
                "ai_recon_match_one_live_line"
            ):
                raise UserError(
                    self.env._(
                        "A live match proposal already covers this bank transaction."
                    )
                ) from exc
            raise
        return {
            "match_id": match.id,
            "payload_hash": match.payload_hash,
            "requires_posting": match.requires_posting,
        }

    @api.model
    def post_match(self, proposal_id, payload_hash):
        self._positive_id(proposal_id, self.env._("Match proposal ID"))
        args = {"proposal_id": proposal_id, "payload_hash": payload_hash}
        binding = self._assert_invocation("post_match", args)
        match = self.env["ai.recon.match"].browse(proposal_id).exists()
        if not match:
            raise UserError(self.env._("Match proposal no longer exists."))
        match._lock()
        if match.payload_hash != payload_hash or match.state != "approved":
            raise UserError(
                self.env._("Only the approved immutable proposal may be posted.")
            )
        if match.company_id != self._execution_company():
            raise AccessError(
                self.env._("Proposal company does not match the execution company.")
            )
        line = match.statement_line_id
        if line.is_reconciled or line.company_id != match.company_id:
            raise UserError(self.env._("Bank transaction changed; assess it again."))
        if not self.env.user.has_group("account.group_account_invoice"):
            raise AccessError(
                self.env._("The execution identity cannot post reconciliation.")
            )
        candidates = match.allocation_ids.mapped("move_line_id")
        if any(
            aml.company_id != match.company_id
            or aml.reconciled
            or aml.parent_state != "posted"
            or aml.partner_id != line.partner_id
            or aml.currency_id != match.company_id.currency_id
            or aml.account_id.reconcile is not True
            or aml.account_id.account_type
            not in ("asset_receivable", "liability_payable")
            for aml in candidates
        ):
            raise UserError(
                self.env._("Candidate documents changed; assess them again.")
            )
        self._lock_facts(line, candidates)
        facts = self._financial_facts(line, candidates)
        if facts != match.payload["facts"]:
            raise UserError(
                self.env._("Financial facts changed; assess this match again.")
            )
        self._assert_unlocked_moves(match.company_id, line.move_id | candidates.move_id)
        _liquidity, suspense, other = line._seek_for_lines()
        if not suspense or other:
            raise UserError(
                self.env._("The bank counterpart changed; assess it again.")
            )
        target_account = candidates[0].account_id
        if any(aml.account_id != target_account for aml in candidates):
            raise UserError(
                self.env._("Cross-account groups require accounting review.")
            )
        if (
            line.amount != match.payload["bank_amount"]
            or line.journal_id.id != match.payload["journal_id"]
        ):
            raise UserError(self.env._("Bank transaction changed; assess it again."))
        # Native reconciliation preserves Odoo's core allocation and invoice-paid
        # hooks. Posting tools have no automatic reversal adapter.
        suspense.with_context(
            check_move_validity=False,
            skip_readonly_check=True,
            skip_account_move_synchronization=True,
        ).write(
            {"account_id": target_account.id, "partner_id": candidates[0].partner_id.id}
        )
        before_partials = set(
            self.env["account.partial.reconcile"]
            .search(
                [
                    "|",
                    ("debit_move_id", "in", candidates.ids + suspense.ids),
                    ("credit_move_id", "in", candidates.ids + suspense.ids),
                ]
            )
            .ids
        )
        plan = [candidates + suspense]
        self.env["account.move.line"]._reconcile_plan(plan)
        after_partials = self.env["account.partial.reconcile"].search(
            [
                "|",
                ("debit_move_id", "in", candidates.ids + suspense.ids),
                ("credit_move_id", "in", candidates.ids + suspense.ids),
            ]
        )
        created = sorted(set(after_partials.ids) - before_partials)
        if not created:
            raise UserError(
                self.env._("Native reconciliation created no matching allocation.")
            )
        created_partials = after_partials.filtered(
            lambda partial: partial.id in created
        )
        if any(not partial.reconciled for partial in suspense):
            raise UserError(
                self.env._("Native reconciliation left an open bank counterpart.")
            )
        for allocation in match.allocation_ids:
            line_partials = created_partials.filtered(
                lambda partial, move_line_id=allocation.move_line_id: (
                    move_line_id in (partial.debit_move_id | partial.credit_move_id)
                )
            )
            actual = sum(line_partials.mapped("amount"))
            if float_compare(
                actual,
                abs(allocation.amount),
                precision_rounding=line.currency_id.rounding,
            ):
                raise UserError(
                    self.env._(
                        "Native reconciliation did not apply the exact allocation."
                    )
                )
        originating_call = binding[0]
        match._set(
            {
                "state": "applied",
                "posting_call_id": originating_call,
                "posting_receipt": {
                    "partial_ids": created,
                    "statement_line_id": line.id,
                    "candidate_ids": candidates.ids,
                },
            }
        )
        return {"match_id": match.id, "partial_ids": created}
