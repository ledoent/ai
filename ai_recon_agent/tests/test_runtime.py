import ast
import json

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import Command, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestReconciliationRuntime(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.ref("ai_agent_runtime.group_ai_reviewer").user_ids += cls.env.user
        cls.company = cls.env.company
        cls.reviewer = new_test_user(
            cls.env,
            login="recon_agent_reviewer",
            groups=(
                "base.group_user,account.group_account_user,"
                "account.group_account_invoice,ai_agent_runtime.group_ai_reviewer"
            ),
        )
        cls.execution_user = new_test_user(
            cls.env,
            login="recon_agent_executor",
            groups=(
                "base.group_user,base.group_partner_manager,"
                "account.group_account_invoice"
            ),
        )
        cls.execution_user.write({"company_ids": [Command.set([cls.company.id])]})
        cls.company.write(
            {
                "ai_recon_agent_enabled": True,
                "ai_recon_reviewer_ids": [Command.set([cls.reviewer.id])],
            }
        )
        Tool = cls.env["ai.agent.tool"]
        cls.candidate_tool = Tool.create(
            {
                "name": "candidate_packet",
                "description": "Return a bounded list of open documents.",
                "model_name": "ai.recon.match.service",
                "method_name": "candidate_packet",
            }
        )
        cls.match_tool = Tool.create(
            {
                "name": "match_items",
                "description": "Create an immutable candidate match.",
                "model_name": "ai.recon.match.service",
                "method_name": "match_items",
            }
        )
        cls.post_tool = Tool.create(
            {
                "name": "post_match",
                "description": "Post an approved reconciliation.",
                "model_name": "ai.recon.match.service",
                "method_name": "post_match",
                "requires_approval": True,
            }
        )
        cls.agent = cls.env["ai.agent"].create(
            {
                "name": "Reconciliation fixture",
                "user_id": cls.execution_user.id,
                "reviewer_ids": [Command.set([cls.reviewer.id, cls.env.uid])],
                "tool_ids": [
                    Command.set(
                        [
                            cls.candidate_tool.id,
                            cls.match_tool.id,
                            cls.post_tool.id,
                        ]
                    )
                ],
            }
        )
        cls.agent._capability(cls.match_tool).action_set_level(
            "apply", "The tool only creates a review proposal."
        )
        cls.agent._capability(cls.post_tool).action_set_level(
            "propose", "Posting stays behind exact call approval."
        )

    def make_invoice_and_statement_line(
        self, suffix, amount=100, foreign_currency=False
    ):
        today = fields.Date.today()
        invoice = self._create_invoice(
            post=True,
            date=today,
            invoice_date=today,
            invoice_line_ids=[
                self._prepare_invoice_line(
                    name=f"Phase 3 fixture {suffix}",
                    price_unit=100,
                    tax_ids=[],
                )
            ],
        )
        receivable = invoice.line_ids.filtered(
            lambda line: line.account_id.account_type == "asset_receivable"
        )
        statement_vals = {
            "journal_id": self.company_data["default_journal_bank"].id,
            "date": today,
            "payment_ref": invoice.name,
            "partner_id": invoice.partner_id.id,
            "amount": amount,
        }
        if foreign_currency:
            currency = (
                self.env["res.currency"]
                .with_context(active_test=False)
                .search([("id", "!=", self.company.currency_id.id)], limit=1)
            )
            self.assertTrue(currency, "The base database needs a second currency")
            currency.active = True
            statement_vals.update(
                foreign_currency_id=currency.id, amount_currency=amount
            )
        statement_line = self.env["account.bank.statement.line"].create(statement_vals)
        return invoice, receivable, statement_line

    def make_invoice_for_partner(self, partner, suffix, amount=40):
        today = fields.Date.today()
        return self._create_invoice(
            post=True,
            date=today,
            invoice_date=today,
            partner_id=partner.id,
            invoice_line_ids=[
                self._prepare_invoice_line(
                    name=f"Phase 3 fixture {suffix}",
                    price_unit=amount,
                    tax_ids=[],
                )
            ],
        )

    def make_run(self, tool, args):
        run = self.env["ai.agent.run"].create(
            {
                "agent_id": self.agent.id,
                "company_ids": [Command.set([self.company.id])],
                "plan": json.dumps([{"tool": tool.name, "args": args}]),
            }
        )
        run.action_start()
        return run

    def make_goal_run(self, script):
        run = self.env["ai.agent.run"].create(
            {
                "agent_id": self.agent.id,
                "company_ids": [Command.set([self.company.id])],
                "goal": "Match this bank transaction to its accounting document.",
                "plan": "[]",
            }
        )
        run.action_start()
        return run.with_context(stub_script=script)

    def test_match_proposal_dispatch_has_trusted_adapter_and_reversible_undo(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "undo"
        )
        _liquidity, suspense, _other = statement_line._seek_for_lines()
        original_suspense_account = suspense.account_id
        original_write_date = statement_line.move_id.write_date
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        run.action_run_to_completion()
        call = run.call_ids
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", call.id)]
        )
        self.assertTrue(call.receipt)
        self.assertTrue(self.match_tool._undo_adapter())
        self.assertEqual(match.state, "proposed")
        self.assertFalse(statement_line.is_reconciled)
        statement_line.move_id.invalidate_recordset()
        statement_line.invalidate_recordset()
        self.assertEqual(statement_line.move_id.write_date, original_write_date)
        self.assertEqual(suspense.account_id, original_suspense_account)
        self.assertTrue(receivable.account_id.reconcile)
        call.action_undo("Withdraw the unreviewed proposal")
        self.assertEqual(call.undo_state, "done")
        self.assertEqual(call.state, "undone")
        self.assertEqual(match.state, "undone")
        self.assertEqual(suspense.account_id, original_suspense_account)
        self.assertFalse(statement_line.is_reconciled)

    def test_candidate_packet_is_bounded_and_contains_only_matching_facts(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "packet"
        )
        unrelated_partner = self.env["res.partner"].create(
            {"name": "Unrelated reconciliation counterparty", "customer_rank": 1}
        )
        unrelated_invoice = self.make_invoice_for_partner(
            unrelated_partner, "packet-unrelated"
        )
        unrelated_lines = unrelated_invoice.line_ids.filtered(
            lambda line: line.account_id.account_type == "asset_receivable"
        )
        run = self.make_run(
            self.candidate_tool, {"statement_line_id": statement_line.id}
        )
        run.action_run_to_completion()

        packet = ast.literal_eval(run.call_ids.result)
        self.assertEqual(packet["status"], "candidates")
        self.assertTrue(packet["candidate_set_complete"])
        self.assertEqual(
            [item["id"] for item in packet["candidates"]], receivable.ids
        )
        self.assertFalse(
            set(unrelated_lines.ids) & {item["id"] for item in packet["candidates"]}
        )
        self.assertEqual(
            set(packet["candidates"][0]),
            {"id", "document", "reference", "date", "open_amount", "currency"},
        )
        self.assertNotIn("account_number", packet["bank_transaction"])

    def test_candidate_packet_rejects_boolean_ids(self):
        run = self.make_run(self.candidate_tool, {"statement_line_id": True})
        run.action_run_to_completion()

        self.assertEqual(run.state, "failed")
        self.assertIn("positive integer", run.call_ids.result)

    def test_payable_document_can_be_proposed_and_posted(self):
        vendor = self.env["res.partner"].create(
            {"name": "Phase 3 payable vendor", "supplier_rank": 1}
        )
        payable = self.env["account.account"].search(
            [
                ("company_ids", "in", [self.company.id]),
                ("account_type", "=", "liability_payable"),
                ("reconcile", "=", True),
            ],
            limit=1,
        )
        expense = self.env["account.account"].search(
            [
                ("company_ids", "in", [self.company.id]),
                ("account_type", "=", "expense"),
            ],
            limit=1,
        )
        self.assertTrue(payable and expense)
        vendor.with_company(self.company).property_account_payable_id = payable
        bill = self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "partner_id": vendor.id,
                "invoice_date": fields.Date.today(),
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Phase 3 payable fixture",
                            "quantity": 1,
                            "price_unit": 100,
                            "account_id": expense.id,
                        }
                    )
                ],
            }
        )
        bill.action_post()
        payable_line = bill.line_ids.filtered(
            lambda line: line.account_id.account_type == "liability_payable"
        )
        statement_line = self.env["account.bank.statement.line"].create(
            {
                "journal_id": self.company_data["default_journal_bank"].id,
                "date": fields.Date.today(),
                "payment_ref": bill.name,
                "partner_id": vendor.id,
                "amount": -100,
            }
        )
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": payable_line.ids,
            },
        )
        run.action_run_to_completion()
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", run.call_ids.id)]
        )
        self.assertEqual(run.call_ids.state, "dispatched", run.call_ids.result)
        self.assertEqual(match.allocation_ids.amount, -100)
        match.with_user(self.reviewer).action_approve("Vendor bill and payment agree")

        post_run = self.make_run(
            self.post_tool,
            {"proposal_id": match.id, "payload_hash": match.payload_hash},
        )
        post_run._advance()
        post_run.with_user(self.reviewer).action_approve()
        post_run._advance()

        bill.invalidate_recordset()
        self.assertEqual(post_run.call_ids.state, "dispatched", post_run.call_ids.result)
        self.assertTrue(statement_line.is_reconciled)
        self.assertEqual(bill.payment_state, "paid")

    def test_rejection_records_one_negative_business_judgment(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "reject"
        )
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        run.action_run_to_completion()
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", run.call_ids.id)]
        )

        match.with_user(self.reviewer).action_reject("The invoice does not match.")

        evidence = run.call_ids.evidence_ids.filtered(lambda row: row.kind == "business")
        self.assertEqual(match.state, "rejected")
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence.verdict, "reject")
        self.assertEqual(run.call_ids.capability_id._statistics()["rejections"], 1)
        self.assertFalse(statement_line.is_reconciled)

    def test_reconciliation_audit_records_follow_agent_scope(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "audit-scope"
        )
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        run.action_run_to_completion()
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", run.call_ids.id)]
        )
        unassigned = new_test_user(
            self.env,
            login="recon_unassigned_reader",
            groups="base.group_user,account.group_account_invoice",
        )

        for record in (match, match.assessment_id, match.allocation_ids):
            with self.assertRaises(AccessError):
                record.with_user(unassigned).check_access("read")
            record.with_user(self.reviewer).check_access("read")
            record.with_user(self.execution_user).check_access("read")

    def test_reviewed_post_uses_exact_approved_runtime_snapshot(self):
        invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "post"
        )
        match_run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        match_run.action_run_to_completion()
        match_call = match_run.call_ids
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", match_call.id)]
        )
        match.with_user(self.reviewer).action_approve("Invoice and bank amount agree")
        match_call.capability_id.invalidate_recordset()
        self.assertEqual(match_call.capability_id._statistics()["approvals"], 1)

        post_run = self.make_run(
            self.post_tool,
            {"proposal_id": match.id, "payload_hash": match.payload_hash},
        )
        post_run._advance()
        self.assertEqual(post_run.state, "awaiting_approval")
        self.assertFalse(statement_line.is_reconciled)
        post_run.with_user(self.reviewer).action_approve()
        post_run._advance()
        statement_line.invalidate_recordset()
        invoice.invalidate_recordset()
        self.assertEqual(
            post_run.call_ids.state, "dispatched", post_run.call_ids.result
        )
        self.assertTrue(statement_line.is_reconciled)
        self.assertTrue(invoice.payment_state == "paid")
        self.assertEqual(match.state, "applied")
        self.assertTrue(match.posting_receipt["partial_ids"])
        self.assertFalse(post_run.call_ids.receipt)

    def test_single_document_partial_payment_posts_only_the_bank_amount(self):
        invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "partial", amount=60
        )
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        run.action_run_to_completion()
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", run.call_ids.id)]
        )
        self.assertEqual(match.allocation_ids.amount, 60)
        match.with_user(self.reviewer).action_approve("Partial remittance agrees")

        post_run = self.make_run(
            self.post_tool,
            {"proposal_id": match.id, "payload_hash": match.payload_hash},
        )
        post_run._advance()
        post_run.with_user(self.reviewer).action_approve()
        post_run._advance()

        receivable.invalidate_recordset()
        invoice.invalidate_recordset()
        self.assertTrue(statement_line.is_reconciled)
        self.assertEqual(receivable.amount_residual, 40)
        self.assertEqual(invoice.payment_state, "partial")
        self.assertEqual(match.state, "applied")

    def test_full_group_matches_all_selected_documents(self):
        today = fields.Date.today()
        first_invoice, first_receivable, statement_line = (
            self.make_invoice_and_statement_line("group-first", amount=140)
        )
        second_invoice = self._create_invoice(
            post=True,
            date=today,
            invoice_date=today,
            invoice_line_ids=[
                self._prepare_invoice_line(
                    name="Phase 3 group second", price_unit=40, tax_ids=[]
                )
            ],
        )
        receivables = first_receivable | second_invoice.line_ids.filtered(
            lambda line: line.account_id.account_type == "asset_receivable"
        )
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivables.ids,
            },
        )
        run.action_run_to_completion()
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", run.call_ids.id)]
        )
        self.assertEqual(
            set(match.allocation_ids.move_line_id.ids), set(receivables.ids)
        )
        self.assertEqual(sum(match.allocation_ids.mapped("amount")), 140)
        match.with_user(self.reviewer).action_approve("Both invoices make up the total")

        post_run = self.make_run(
            self.post_tool,
            {"proposal_id": match.id, "payload_hash": match.payload_hash},
        )
        post_run._advance()
        post_run.with_user(self.reviewer).action_approve()
        post_run._advance()

        first_invoice.invalidate_recordset()
        second_invoice.invalidate_recordset()
        self.assertEqual(first_invoice.payment_state, "paid")
        self.assertEqual(second_invoice.payment_state, "paid")
        self.assertEqual(match.state, "applied")

    def test_changed_bank_facts_refuse_an_approved_snapshot(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "stale"
        )
        match_run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        match_run.action_run_to_completion()
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", match_run.call_ids.id)]
        )
        match.with_user(self.reviewer).action_approve("Initial facts agree")
        statement_line.write({"amount": 90})

        post_run = self.make_run(
            self.post_tool,
            {"proposal_id": match.id, "payload_hash": match.payload_hash},
        )
        post_run._advance()
        post_run.with_user(self.reviewer).action_approve()
        post_run._advance()

        self.assertEqual(post_run.state, "failed")
        self.assertIn("Financial facts changed", post_run.call_ids.result)
        self.assertFalse(statement_line.is_reconciled)
        self.assertEqual(match.state, "approved")

    def test_changed_statement_move_lines_refuse_an_approved_snapshot(self):
        _invoice, _receivable, statement_line = self.make_invoice_and_statement_line(
            "stale-move-lines"
        )
        _liquidity, suspense, _other = statement_line._seek_for_lines()
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": _receivable.ids,
            },
        )
        run.action_run_to_completion()
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", run.call_ids.id)]
        )
        match.with_user(self.reviewer).action_approve("Initial statement facts agree")

        liquidity, _suspense, _other = statement_line._seek_for_lines()
        posting_context = {
            "check_move_validity": False,
            "skip_readonly_check": True,
            "skip_account_move_synchronization": True,
        }
        suspense.with_context(**posting_context).write({"credit": 90})
        liquidity.with_context(**posting_context).write({"debit": 90})

        post_run = self.make_run(
            self.post_tool,
            {"proposal_id": match.id, "payload_hash": match.payload_hash},
        )
        post_run._advance()
        post_run.with_user(self.reviewer).action_approve()
        post_run._advance()

        self.assertEqual(post_run.state, "failed")
        self.assertIn("Financial facts changed", post_run.call_ids.result)
        self.assertFalse(statement_line.is_reconciled)
        self.assertEqual(match.state, "approved")

    def test_second_live_match_for_the_same_bank_line_is_refused(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "duplicate"
        )
        args = {
            "statement_line_id": statement_line.id,
            "candidate_line_ids": receivable.ids,
        }
        first = self.make_run(self.match_tool, args)
        first.action_run_to_completion()
        second = self.make_run(self.match_tool, args)
        second.action_run_to_completion()

        self.assertEqual(second.state, "failed")
        self.assertIn("live match proposal", second.call_ids.result)
        self.assertEqual(
            self.env["ai.recon.match"].search_count(
                [("statement_line_id", "=", statement_line.id)]
            ),
            1,
        )

    def test_match_attribution_uses_the_invoking_agent_with_shared_identity(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "shared-identity"
        )
        other_agent = self.env["ai.agent"].create(
            {
                "name": "Second reconciliation fixture",
                "user_id": self.execution_user.id,
                "reviewer_ids": [Command.set([self.reviewer.id])],
                "tool_ids": [Command.set([self.candidate_tool.id, self.match_tool.id])],
            }
        )
        other_agent._capability(self.match_tool).action_set_level(
            "apply", "Second agent proposal capability"
        )
        run = self.env["ai.agent.run"].create(
            {
                "agent_id": other_agent.id,
                "company_ids": [Command.set([self.company.id])],
                "plan": json.dumps(
                    [
                        {
                            "tool": self.match_tool.name,
                            "args": {
                                "statement_line_id": statement_line.id,
                                "candidate_line_ids": receivable.ids,
                            },
                        }
                    ]
                ),
            }
        )
        run.action_start()
        run.action_run_to_completion()

        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", run.call_ids.id)]
        )
        self.assertEqual(run.state, "done")
        self.assertEqual(match.agent_id, other_agent)

    def test_legacy_review_proposal_blocks_attributed_matching(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "legacy"
        )
        batch = self.env["ai.recon.batch"].create(
            {
                "items_seen": 1,
                "company_ids": [Command.set([self.company.id])],
            }
        )
        self.env["ai.recon.proposal"].create(
            {
                "batch_id": batch.id,
                "statement_line_id": statement_line.id,
                "confidence": "none",
            }
        )
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        run.action_run_to_completion()

        self.assertEqual(run.state, "failed")
        self.assertIn("legacy proposal", run.call_ids.result)
        self.assertFalse(
            self.env["ai.recon.match"].search(
                [("statement_line_id", "=", statement_line.id)]
            )
        )

    def test_lock_dates_block_posting_before_reconciliation(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "locked"
        )
        match_run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        match_run.action_run_to_completion()
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", match_run.call_ids.id)]
        )
        match.with_user(self.reviewer).action_approve("Review before close")
        self.env.cr.execute(
            "UPDATE res_company SET hard_lock_date = %s WHERE id = %s",
            [fields.Date.to_string(fields.Date.today()), self.company.id],
        )
        self.company.invalidate_recordset(["hard_lock_date", "user_hard_lock_date"])

        post_run = self.make_run(
            self.post_tool,
            {"proposal_id": match.id, "payload_hash": match.payload_hash},
        )
        post_run._advance()
        post_run.with_user(self.reviewer).action_approve()
        post_run._advance()

        self.assertEqual(post_run.state, "failed")
        self.assertIn("lock dates", post_run.call_ids.result)
        self.assertFalse(statement_line.is_reconciled)
        self.assertEqual(match.state, "approved")

    def test_foreign_currency_bank_transaction_is_refused_on_both_paths(self):
        invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "foreign-currency", foreign_currency=True
        )
        packet_run = self.make_run(
            self.candidate_tool, {"statement_line_id": statement_line.id}
        )
        packet_run.action_run_to_completion()
        self.assertEqual(packet_run.state, "failed")
        self.assertIn("Foreign-currency", packet_run.call_ids.result)

        match_run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        match_run.action_run_to_completion()
        self.assertEqual(match_run.state, "failed")
        self.assertIn("Foreign-currency", match_run.call_ids.result)
        self.assertFalse(
            self.env["ai.recon.assessment"].search(
                [("statement_line_id", "=", statement_line.id)]
            )
        )
        self.assertEqual(invoice.payment_state, "not_paid")
        self.assertFalse(statement_line.is_reconciled)

    def test_allocation_cannot_exceed_open_document_residual(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "allocation-overflow"
        )
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
                "allocations": [{"line_id": receivable.id, "amount": 101}],
            },
        )
        run.action_run_to_completion()

        self.assertEqual(run.state, "failed")
        self.assertIn("exceeds its open balance", run.call_ids.result)
        self.assertFalse(statement_line.is_reconciled)
        self.assertFalse(
            self.env["ai.recon.match"].search(
                [("statement_line_id", "=", statement_line.id)]
            )
        )

        boolean_run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
                "allocations": [{"line_id": receivable.id, "amount": True}],
            },
        )
        boolean_run.action_run_to_completion()
        self.assertEqual(boolean_run.state, "failed")
        self.assertIn("Malformed allocation", boolean_run.call_ids.result)

    def test_truncated_candidate_packet_cannot_be_matched(self):
        invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "truncated", amount=140
        )
        self.make_invoice_for_partner(invoice.partner_id, "truncated-second")
        self.company.write(
            {
                "ai_recon_agent_max_candidates": 1,
                "ai_recon_agent_max_allocations": 1,
            }
        )

        packet_run = self.make_run(
            self.candidate_tool, {"statement_line_id": statement_line.id}
        )
        packet_run.action_run_to_completion()
        self.assertIn("'status': 'truncated'", packet_run.call_ids.result)

        match_run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        match_run.action_run_to_completion()
        self.assertEqual(match_run.state, "failed")
        self.assertIn("Candidate set is truncated", match_run.call_ids.result)
        self.assertFalse(statement_line.is_reconciled)
        self.assertFalse(
            self.env["ai.recon.match"].search(
                [("statement_line_id", "=", statement_line.id)]
            )
        )

    def test_reviewed_match_cannot_be_automatically_undone(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "reviewed-undo"
        )
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        run.action_run_to_completion()
        call = run.call_ids
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", call.id)]
        )
        match.with_user(self.reviewer).action_approve("Reviewed proposal")

        call.with_user(self.reviewer).action_undo("Try to withdraw reviewed proposal")

        self.assertEqual(call.state, "dispatched")
        self.assertEqual(call.undo_state, "failed")
        self.assertIn("cannot be automatically withdrawn", call.undo_error)
        self.assertEqual(match.state, "approved")

    def test_reviewer_can_withdraw_approved_unposted_match_for_reassessment(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "manual-withdraw"
        )
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        run.action_run_to_completion()
        call = run.call_ids
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", call.id)]
        )
        match.with_user(self.reviewer).action_approve("Initial review")

        match.with_user(self.reviewer).action_withdraw_approved(
            "Invoice was credited after the initial review"
        )

        self.assertEqual(match.state, "stale")
        self.assertEqual(match.resolution_reviewer_id, self.reviewer)
        self.assertIn("credited", match.resolution_reason)
        self.assertEqual(call.undo_state, "failed")
        self.assertEqual(call.state, "dispatched")
        self.assertEqual(call.capability_id.level, "propose")
        self.assertFalse(statement_line.is_reconciled)

    def test_llm_path_uses_the_company_provider_and_records_provenance(self):
        invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "llm"
        )
        provider = self.env["ai.agent.provider"].create(
            {"name": "Company reconciliation provider", "kind": "stub"}
        )
        self.company.ai_recon_agent_provider_id = provider
        self.agent.provider_id = provider
        run = self.make_goal_run(
            [
                {
                    "kind": "tool",
                    "tool": self.candidate_tool.name,
                    "args": {"statement_line_id": statement_line.id},
                },
                {
                    "kind": "tool",
                    "tool": self.match_tool.name,
                    "args": {
                        "statement_line_id": statement_line.id,
                        "candidate_line_ids": receivable.ids,
                    },
                },
                {"kind": "final", "answer": "A match was proposed."},
            ]
        )
        run.action_run_to_completion()

        match = self.env["ai.recon.match"].search(
            [("assessment_id.call_id.run_id", "=", run.id)]
        )
        self.assertEqual(run.state, "done")
        self.assertEqual(match.provenance, "llm")
        self.assertEqual(match.assessment_id.provider_id, provider)
        self.assertEqual(match.assessment_id.statement_line_id, statement_line)
        self.assertEqual(invoice.payment_state, "not_paid")

    def test_llm_path_refuses_a_provider_not_selected_by_the_company(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "provider-mismatch"
        )
        provider = self.env["ai.agent.provider"].create(
            {"name": "Unselected provider", "kind": "stub"}
        )
        self.agent.provider_id = provider
        run = self.make_goal_run(
            [
                {
                    "kind": "tool",
                    "tool": self.candidate_tool.name,
                    "args": {"statement_line_id": statement_line.id},
                },
                {"kind": "final", "answer": "No candidate packet was returned."},
            ]
        )
        run.action_run_to_completion()

        self.assertEqual(run.state, "done")
        self.assertEqual(run.call_ids[0].state, "failed")
        self.assertIn("provider explicitly selected", run.call_ids[0].result)
        self.assertFalse(
            self.env["ai.recon.assessment"].search(
                [("statement_line_id", "=", statement_line.id)]
            )
        )

    def test_exact_call_approval_does_not_count_as_match_quality_evidence(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "quality-evidence"
        )
        self.match_tool.requires_approval = True
        self.agent.promotion_samples = 1
        capability = self.agent._capability(self.match_tool)
        capability.action_set_level("propose", "Test reviewed proposal tool")
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        run._advance()
        self.assertEqual(run.state, "awaiting_approval")
        run.with_user(self.reviewer).action_approve()
        run._advance()
        call = run.call_ids
        self.assertEqual(call.state, "dispatched")
        self.assertEqual(call.capability_id._statistics()["approvals"], 0)
        self.assertFalse(capability._evaluate_promotion())

        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", call.id)]
        )
        match.with_user(self.reviewer).action_approve("The match is correct")
        self.assertEqual(call.capability_id._statistics()["approvals"], 1)
        self.assertTrue(capability._evaluate_promotion())
        self.assertEqual(capability.level, "apply")

    def test_accepted_observed_match_intent_can_promote_to_propose(self):
        capability = self.agent._capability(self.match_tool)
        capability.action_set_level("observe", "Verify observed-intent promotion")
        run = self.make_run(self.match_tool, {"statement_line_id": 999})
        run._advance()

        call = run.call_ids
        self.assertEqual(call.state, "observed")
        call.with_user(self.reviewer).action_accept(
            "The proposed matching task is in scope"
        )

        self.assertEqual(call.capability_id._statistics()["approvals"], 1)

    def test_unassigned_reviewer_and_agent_identity_cannot_review_matches(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "reviewer-scope"
        )
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        run.action_run_to_completion()
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", run.call_ids.id)]
        )
        unassigned = new_test_user(
            self.env,
            login="recon_unassigned_reviewer",
            groups=(
                "base.group_user,account.group_account_user,"
                "account.group_account_invoice,ai_agent_runtime.group_ai_reviewer"
            ),
        )
        with self.assertRaises(AccessError):
            match.with_user(unassigned).action_approve("Unassigned account")

        self.execution_user.write(
            {
                "group_ids": [
                    Command.link(self.env.ref("ai_agent_runtime.group_ai_reviewer").id)
                ]
            }
        )
        self.agent.write({"reviewer_ids": [Command.link(self.execution_user.id)]})
        self.company.write(
            {"ai_recon_reviewer_ids": [Command.link(self.execution_user.id)]}
        )
        with self.assertRaises(AccessError):
            match.with_user(self.execution_user).action_approve(
                "Execution account cannot review"
            )

    def test_posting_cannot_be_called_outside_an_approved_snapshot(self):
        _invoice, receivable, statement_line = self.make_invoice_and_statement_line(
            "forgery"
        )
        run = self.make_run(
            self.match_tool,
            {
                "statement_line_id": statement_line.id,
                "candidate_line_ids": receivable.ids,
            },
        )
        run.action_run_to_completion()
        call = run.call_ids
        match = self.env["ai.recon.match"].search(
            [("assessment_call_id", "=", call.id)]
        )
        match.with_user(self.reviewer).action_approve("Approve test proposal")
        service = self.env["ai.recon.match.service"].with_context(
            ai_agent_invocation={
                "model": "ai.recon.match.service",
                "method": "post_match",
            }
        )
        with self.assertRaises(AccessError):
            service.post_match(match.id, match.payload_hash)
        self.assertFalse(statement_line.is_reconciled)
