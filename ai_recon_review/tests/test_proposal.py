from psycopg2.errors import UniqueViolation

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger, sql


@tagged("post_install", "-at_install")
class ReconCommon(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.ref("base.main_company")
        # The review gate is inert unless the company switch is on
        # (account_move.py:3940). Turning it on is a precondition of the whole
        # design, not a detail.
        cls.company.set_to_review_documents = True

        chart = cls.env["account.chart.template"]
        if not cls.env["account.account"].search(
            [("company_ids", "in", cls.company.id)], limit=1
        ):
            chart.try_loading("generic_coa", company=cls.company, install_demo=False)

        cls.journal = cls.env["account.journal"].create(
            {"name": "Lab Bank", "type": "bank", "code": "LABB"}
        )
        cls.expense = cls.env["account.account"].create(
            {
                "name": "Lab Expense",
                "code": "699999",
                "account_type": "expense",
            }
        )
        cls.st_line = cls.env["account.bank.statement.line"].create(
            {
                "journal_id": cls.journal.id,
                "payment_ref": "GITHUB INC HTTPSGITHUB",
                "amount": -25.0,
                "date": fields.Date.today(),
            }
        )
        cls.batch = cls.env["ai.recon.batch"].create({"items_seen": 1})

    def make(self, confidence="medium", account=None, line=None):
        return self.env["ai.recon.proposal"].create(
            {
                "batch_id": self.batch.id,
                "statement_line_id": (line or self.st_line).id,
                "confidence": confidence,
                "account_id": (account or self.expense).id,
                "rationale": "test",
            }
        )


class TestTheGate(ReconCommon):
    """Unapproved cannot take effect -- by construction, not by blocking."""

    def test_execute_refuses_unless_approved(self):
        proposal = self.make()
        for state in ("proposed", "noted", "rejected"):
            proposal.state = state
            with self.subTest(state=state), self.assertRaises(UserError):
                proposal._execute()

    def test_a_proposal_changes_nothing_until_approved(self):
        """The whole argument for staging intent as data.

        The statement line keeps its suspense counterpart and the move is
        untouched, because nothing has been written anywhere -- there is no
        half-finished entry that needed to be frozen.
        """
        before = self.st_line.move_id.write_date
        suspense = self.journal.suspense_account_id
        self.make(confidence="medium")
        self.st_line.invalidate_recordset()
        self.assertIn(suspense, self.st_line.line_ids.account_id)
        self.assertEqual(self.st_line.move_id.write_date, before)

    def test_approving_records_who_and_when(self):
        proposal = self.make()
        proposal.action_approve()
        self.assertEqual(proposal.state, "applied")
        self.assertEqual(proposal.approved_by, self.env.user)
        self.assertTrue(proposal.approved_date)

    def test_rejecting_leaves_the_line_alone(self):
        suspense = self.journal.suspense_account_id
        proposal = self.make()
        proposal.action_reject()
        self.assertEqual(proposal.state, "rejected")
        self.st_line.invalidate_recordset()
        self.assertIn(suspense, self.st_line.line_ids.account_id)

    def test_cannot_apply_without_an_account(self):
        proposal = self.make()
        proposal.account_id = False
        proposal.state = "approved"
        with self.assertRaises(UserError):
            proposal._execute()


class TestRouting(ReconCommon):
    """Each confidence tier reaches the surface it should."""

    def test_high_applies_and_queues_for_review(self):
        proposal = self.make(confidence="high")
        self.env["ai.recon.proposal"]._route(proposal)
        self.assertEqual(proposal.state, "applied")
        self.st_line.invalidate_recordset()
        self.assertIn(self.expense, self.st_line.line_ids.account_id)
        self.assertNotIn(
            self.journal.suspense_account_id, self.st_line.line_ids.account_id
        )
        # the native queue, not one of ours
        self.assertEqual(self.st_line.move_id.review_state, "todo")

    def test_medium_flags_without_touching_the_entry(self):
        proposal = self.make(confidence="medium")
        self.env["ai.recon.proposal"]._route(proposal)
        self.assertEqual(proposal.state, "proposed")
        self.st_line.invalidate_recordset()
        # still in suspense -- nothing was reconciled
        self.assertIn(
            self.journal.suspense_account_id, self.st_line.line_ids.account_id
        )
        # but visible in core's "To Review" filter, which searches
        # review_state in ('todo', 'anomaly')
        self.assertEqual(self.st_line.move_id.review_state, "anomaly")

    def test_none_records_the_observation_only(self):
        proposal = self.make(confidence="none")
        self.env["ai.recon.proposal"]._route(proposal)
        self.assertEqual(proposal.state, "noted")
        self.st_line.invalidate_recordset()
        self.assertIn(
            self.journal.suspense_account_id, self.st_line.line_ids.account_id
        )
        self.assertEqual(self.st_line.move_id.review_state, "no_review")

    def test_applying_is_reversible(self):
        """Which is what makes 'apply the confident ones' defensible."""
        proposal = self.make(confidence="high")
        self.env["ai.recon.proposal"]._route(proposal)
        proposal.action_undo()
        self.st_line.invalidate_recordset()
        self.assertEqual(proposal.state, "proposed")
        self.assertIn(
            self.journal.suspense_account_id, self.st_line.line_ids.account_id
        )

    def test_suspense_is_resolved_through_the_journal(self):
        """Never through an account code.

        On the production copy, code 101402 is Bank Suspense in one company and
        Citibank -- a liquidity account -- in another. Five companies, five
        suspense accounts, two codes.
        """
        proposal = self.make()
        self.assertEqual(proposal._suspense_account(), self.journal.suspense_account_id)


class TestAgentCannotSignOff(ReconCommon):
    """The structural claim: the agent queues work it cannot approve.

    Asserted rather than assumed, because the whole separation of duties rests
    on it. Writing 'todo'/'anomaly' is unrestricted; 'reviewed' requires
    account.group_account_user (account_move.py:3932-3948).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent_user = new_test_user(
            cls.env,
            login="recon_agent",
            groups="base.group_user,account.group_account_invoice",
        )

    def test_agent_can_queue_work(self):
        move = self.st_line.move_id.with_user(self.agent_user)
        move.review_state = "todo"
        self.assertEqual(move.review_state, "todo")
        move.review_state = "anomaly"
        self.assertEqual(move.review_state, "anomaly")

    def test_agent_cannot_mark_it_reviewed(self):
        move = self.st_line.move_id.with_user(self.agent_user)
        with self.assertRaises(AccessError):
            move.review_state = "reviewed"

    def test_agent_cannot_mark_it_supervised(self):
        move = self.st_line.move_id.with_user(self.agent_user)
        with self.assertRaises(AccessError):
            move.review_state = "supervised"

    def test_agent_cannot_approve_its_own_proposal(self):
        """The path the three tests above do not cover.

        Those assert that the agent cannot write core's `review_state`. They say
        nothing about `action_approve`, which is the route that actually applies
        a proposal -- and `security/ir.access.csv` grants
        `account.group_account_invoice` full `crud` on `ai.recon.proposal`, the
        exact group `readme/USAGE.md` tells the operator to give the agent.

        Measured over `/json/2` before the mixin guard existed: the agent's own
        credential created a medium-confidence proposal, approved it, and the
        suspense counterpart was rewritten to the proposed account with no
        `ai.agent.call` record. See `ai-rfc/E7-RESULTS.md`.
        """
        # The group membership above is not what makes a user an agent; being
        # named as some ai.agent's execution identity is.
        self.env["ai.agent"].create(
            {
                "name": "Recon agent",
                "user_id": self.agent_user.id,
                "tool_ids": [],
            }
        )
        proposal = self.make()
        before = {line.id: line.account_id.id for line in self.st_line.move_id.line_ids}

        with self.assertRaises(AccessError) as caught:
            proposal.with_user(self.agent_user).action_approve()
        self.assertIn("cannot judge its own proposal", str(caught.exception))

        self.assertEqual(proposal.state, "proposed")
        self.assertFalse(proposal.approved_by)
        self.assertEqual(
            {line.id: line.account_id.id for line in self.st_line.move_id.line_ids},
            before,
            "no accounting state may move when approval is refused",
        )


class TestRuleDiscovery(ReconCommon):
    """The part that compounds: unmatched lines become candidate rules."""

    def _line(self, label):
        return self.env["account.bank.statement.line"].create(
            {
                "journal_id": self.journal.id,
                "payment_ref": label,
                "amount": -9.0,
                "date": fields.Date.today(),
            }
        )

    def test_clusters_group_by_vendor_prefix_and_journal(self):
        for label in ("AMZN Mktp US*2K4", "AMZN Mktp US*9QQ", "AMZN Mktp US*1AA"):
            self.make(confidence="none", line=self._line(label)).state = "noted"
        self.make(
            confidence="none", line=self._line("SOLO VENDOR ONCE")
        ).state = "noted"
        clusters = self.batch._unmatched_clusters()
        self.assertEqual(len(clusters), 1, "singletons are not a pattern")
        (prefix, _journal), proposals = next(iter(clusters.items()))
        self.assertEqual(prefix, "AMZN MKTP")
        self.assertEqual(len(proposals), 3)

    def test_drafted_rules_are_inactive_and_change_nothing(self):
        for label in ("AMZN Mktp US*2K4", "AMZN Mktp US*9QQ"):
            self.make(confidence="none", line=self._line(label)).state = "noted"
        rules = self.batch.action_draft_rules()
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertFalse(rule.active, "a drafted rule must not act until accepted")
        self.assertEqual(rule.match_label, "contains")
        self.assertEqual(rule.match_label_param, "AMZN MKTP")
        self.assertIn(self.journal, rule.match_journal_ids)
        # deliberately left for the human -- choosing it is the judgement
        self.assertFalse(rule.line_ids)

    def test_a_criteria_less_rule_would_match_everything(self):
        """Why the catch-all stays off -- measured on the production copy.

        A reconcile model with no match_label, match_amount or match_partner_ids
        becomes proposable purely by being auto_reconcile
        (_compute_can_be_proposed, account_reconcile_model.py:197) and then
        matches every line in its journals. On the real backlog that was 714 of
        754 lines swept into an "Uncategorized" account -- reconciled, and with
        the rule-discovery signal destroyed.
        """
        catchall = self.env["account.reconcile.model"].create(
            {"name": "sweep everything", "rule_type": "reco_model"}
        )
        self.assertFalse(catchall.can_be_proposed)
        catchall.trigger = "auto_reconcile"
        self.assertTrue(
            catchall.can_be_proposed,
            "a rule with no criteria becomes proposable on trigger alone",
        )


class TestBatchEmail(ReconCommon):
    """One mail about a batch, to configured people, or a clear refusal."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.reviewer = new_test_user(
            cls.env,
            login="batch_reviewer",
            groups="base.group_user,account.group_account_user",
        )
        cls.reviewer.email = "reviewer@example.invalid"

    def test_refuses_when_nobody_is_configured(self):
        """Silently sending to nobody is the failure mode worth preventing."""
        self.batch.company_ids = self.company
        self.company.ai_recon_reviewer_ids = False
        with self.assertRaises(UserError):
            self.batch.action_send()

    def test_sends_one_mail_to_the_configured_reviewers(self):
        self.batch.company_ids = self.company
        self.company.ai_recon_reviewer_ids = self.reviewer
        self.make(confidence="medium")
        before = self.env["mail.mail"].search_count([])
        self.batch.action_send()
        self.assertEqual(self.env["mail.mail"].search_count([]) - before, 1)

    def test_recipients_come_from_the_company(self):
        self.batch.company_ids = self.company
        self.company.ai_recon_reviewer_ids = self.reviewer
        self.assertIn(self.reviewer, self.batch._recipients())

    def test_cron_builds_classifies_and_sends(self):
        self.company.ai_recon_reviewer_ids = self.reviewer
        before = self.env["mail.mail"].search_count([])
        batch = self.env["ai.recon.batch"]._cron_build_and_send()
        self.assertTrue(batch)
        self.assertGreater(batch.items_seen, 0)
        self.assertTrue(batch.proposal_ids, "every line seen gets a proposal")
        self.assertEqual(self.env["mail.mail"].search_count([]) - before, 1)

    def test_cron_refuses_rather_than_mailing_nobody(self):
        """A batch that reaches no one is worse than a loud failure."""
        self.env["res.company"].search([]).write({"ai_recon_reviewer_ids": False})
        with self.assertRaises(UserError):
            self.env["ai.recon.batch"]._cron_build_and_send()

    def test_unclassified_lines_still_cluster(self):
        """With no agent attached the batch is still useful: it reports what
        was seen and which repeats look like a missing rule."""
        self.company.ai_recon_reviewer_ids = self.reviewer
        batch = self.env["ai.recon.batch"]._cron_build_and_send()
        self.assertEqual(batch.count_none, len(batch.proposal_ids))
        self.assertTrue(all(p.state == "noted" for p in batch.proposal_ids))

    def test_top_clusters_renders_for_the_template(self):
        for label in ("AMZN Mktp A", "AMZN Mktp B"):
            line = self.env["account.bank.statement.line"].create(
                {
                    "journal_id": self.journal.id,
                    "payment_ref": label,
                    "amount": -3.0,
                    "date": fields.Date.today(),
                }
            )
            self.make(confidence="none", line=line).state = "noted"
        clusters = self.batch._top_clusters()
        self.assertTrue(clusters)
        # The mail contract is generic -- label and count only. The bank
        # cluster key used to leak its own field names into the template,
        # which coupled the email to reconciliation.
        self.assertEqual(set(clusters[0]), {"label", "count"})
        self.assertEqual(clusters[0]["count"], 2)
        label = clusters[0]["label"]
        self.assertIn("AMZN MKTP", label)
        self.assertIn(self.journal.code, label)


class TestReviewGatePrecondition(ReconCommon):
    """The separation of duties is conditional on a setting we do not own.

    `_check_review_state_access` returns immediately when
    `set_to_review_documents` is off (account_move.py:3940), and that Boolean
    has no default, so off is the shipped state. Measured: with it off, a user
    holding only group_account_invoice writes 'reviewed' without error.

    The earlier claim that the agent "structurally cannot sign off" was
    therefore true only because this test class's fixture turns the setting on.
    These tests pin both halves: the gate really is inert when off, and the
    agent refuses to run in that state rather than operating without it.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent_user = new_test_user(
            cls.env,
            login="gate_probe",
            groups="base.group_user,account.group_account_invoice",
        )

    def test_core_gate_is_inert_when_the_setting_is_off(self):
        """Documents the hole, so nobody re-derives the wrong conclusion."""
        self.company.set_to_review_documents = False
        move = self.st_line.move_id.with_user(self.agent_user)
        move.review_state = "reviewed"  # no AccessError
        self.assertEqual(move.review_state, "reviewed")

    def test_core_gate_holds_when_the_setting_is_on(self):
        self.company.set_to_review_documents = True
        move = self.st_line.move_id.with_user(self.agent_user)
        with self.assertRaises(AccessError):
            move.review_state = "reviewed"

    def test_routing_refuses_when_the_gate_is_inert(self):
        """Fail closed: no gate, no agent."""
        self.company.set_to_review_documents = False
        proposal = self.make(confidence="high")
        with self.assertRaises(UserError):
            self.env["ai.recon.proposal"]._route(proposal)

    def test_refusal_names_the_offending_company(self):
        self.company.set_to_review_documents = False
        proposal = self.make(confidence="high")
        with self.assertRaises(UserError) as caught:
            self.env["ai.recon.proposal"]._route(proposal)
        self.assertIn(self.company.name, str(caught.exception))

    def test_nothing_is_written_when_it_refuses(self):
        self.company.set_to_review_documents = False
        suspense = self.journal.suspense_account_id
        proposal = self.make(confidence="high")
        with self.assertRaises(UserError):
            self.env["ai.recon.proposal"]._route(proposal)
        self.st_line.invalidate_recordset()
        self.assertIn(suspense, self.st_line.line_ids.account_id)
        self.assertEqual(proposal.state, "proposed")

    def test_approval_rechecks_a_gate_disabled_after_staging(self):
        proposal = self.make()
        self.env["ai.recon.proposal"]._route(proposal)
        before = self.st_line.move_id.write_date
        self.company.set_to_review_documents = False
        with self.assertRaises(UserError):
            proposal.action_approve()
        self.assertEqual(proposal.state, "proposed")
        self.assertFalse(proposal.approved_by)
        self.assertFalse(proposal.approved_date)
        self.assertEqual(self.st_line.move_id.write_date, before)
        self.assertIn(
            self.journal.suspense_account_id, self.st_line.line_ids.account_id
        )

    def test_routing_checks_the_current_company_as_well_as_the_subject(self):
        other = self.env["res.company"].create(
            {"name": "Gate disabled current company", "set_to_review_documents": False}
        )
        proposal = self.make().with_company(other)
        with self.assertRaises(UserError):
            proposal._route(proposal)
        self.assertEqual(proposal.state, "proposed")


class TestRepeatedBatches(ReconCommon):
    def _fresh_line(self):
        return self.env["account.bank.statement.line"].create(
            {
                "journal_id": self.journal.id,
                "payment_ref": self.st_line.payment_ref,
                "amount": -25.0,
                "date": fields.Date.today(),
            }
        )

    def _drop_live_index(self):
        """Reproduce the legacy schema; TransactionCase rolls DDL back."""
        Proposal = self.env["ai.recon.proposal"]
        self.env.flush_all()
        sql.drop_index(
            self.cr,
            Proposal._one_live_per_line.full_name(Proposal),
            Proposal._table,
        )

    def test_open_proposal_excludes_line_across_batches(self):
        for state in ("observed", "proposed", "approved", "noted"):
            proposal = self.make()
            proposal.state = state
            with self.subTest(state=state):
                self.assertNotIn(self.st_line, self.batch._candidates())
            proposal.unlink()

    def test_rejected_proposal_allows_fresh_consideration(self):
        self.make().action_reject()
        self.assertIn(self.st_line, self.batch._candidates())

    def test_applied_history_does_not_suppress_an_external_undo(self):
        proposal = self.make()
        proposal.action_approve()
        self.st_line.action_undo_reconciliation()
        self.assertEqual(proposal.state, "applied")
        self.assertFalse(self.st_line.is_reconciled)
        self.assertIn(self.st_line, self.batch._candidates())

    def test_database_rejects_live_proposals_in_different_batches(self):
        self.make()
        other = self.env["ai.recon.batch"].create({})
        with (
            self.assertRaises(UniqueViolation),
            mute_logger("odoo.sql_db"),
            self.cr.savepoint(),
        ):
            self.env["ai.recon.proposal"].create(
                {
                    "batch_id": other.id,
                    "statement_line_id": self.st_line.id,
                    "confidence": "none",
                    "state": "noted",
                }
            )
            self.env.flush_all()

    def test_stale_candidate_list_cannot_create_a_second_proposal(self):
        candidates = self.st_line
        first = self.batch._classify(candidates)
        other = self.env["ai.recon.batch"].create({})
        self.assertFalse(other._classify(candidates))
        self.assertEqual(first.state, "noted")
        self.assertEqual(self.st_line.ai_recon_proposal_ids, first)

    def test_repeated_cron_does_not_grow_unknown_corpus_or_send_empty_mail(self):
        reviewer = new_test_user(
            self.env,
            login="repeat_reviewer",
            groups="base.group_user,account.group_account_user",
        )
        reviewer.email = "repeat@example.invalid"
        self.company.ai_recon_reviewer_ids = reviewer
        Batch = self.env["ai.recon.batch"]
        Proposal = self.env["ai.recon.proposal"]
        Batch._cron_build_and_send()
        before = Proposal.search_count([])
        mails = self.env["mail.mail"].search_count([])
        self.assertFalse(Batch._cron_build_and_send())
        self.assertEqual(Proposal.search_count([]), before)
        self.assertEqual(self.env["mail.mail"].search_count([]), mails)

    def test_cleanup_preserves_one_note_per_distinct_line_and_judgements(self):
        self._drop_live_index()
        first = self.make(confidence="none")
        first.state = "noted"
        other = self.env["ai.recon.batch"].create({})
        duplicate = first.copy({"batch_id": other.id, "state": "noted"})
        judged = self.make(line=self._fresh_line())
        judged.action_reject()
        distinct = self.make(confidence="none", line=self._fresh_line())
        distinct.state = "noted"
        first._gc_duplicate_observations()
        self.assertTrue(first.exists())
        self.assertTrue(distinct.exists())
        self.assertTrue(judged.exists())
        self.assertFalse(duplicate.exists())
        first._gc_duplicate_observations()
        self.assertTrue((first | distinct | judged).exists())

    def test_cleanup_preserves_distinct_lines_in_the_cluster(self):
        self._drop_live_index()
        first = self.make(confidence="none")
        second = self.make(
            confidence="none",
            line=self._fresh_line(),
        )
        (first | second).write({"state": "noted"})
        other = self.env["ai.recon.batch"].create({})
        first.copy({"batch_id": other.id, "state": "noted"})
        first._gc_duplicate_observations()
        self.assertEqual(self.batch._top_clusters()[0]["count"], 2)

    def test_upgrade_repairs_notes_before_creating_the_unique_index(self):
        self._drop_live_index()
        first = self.make(confidence="none")
        first.state = "noted"
        other = self.env["ai.recon.batch"].create({})
        duplicate = first.copy({"batch_id": other.id, "state": "noted"})
        # Real upgrade rows are already committed. Flush fixture changes so
        # the failed first index attempt cannot roll them back with its cache.
        self.env.flush_all()
        self.registry.init_models(
            self.cr,
            ["ai.recon.proposal"],
            {"module": "ai_recon_review"},
            install=False,
        )
        self.registry.finalize_constraints(self.cr)
        self.assertTrue(first.exists())
        self.assertFalse(duplicate.exists())
        self.assertTrue(first._one_live_per_line.matches_database(first))
        self.assertEqual(other.count_none, 0)

    def test_cleanup_prefers_a_live_human_proposal_over_a_note(self):
        self._drop_live_index()
        note = self.make(confidence="none")
        note.state = "noted"
        other = self.env["ai.recon.batch"].create({})
        human = note.copy({"batch_id": other.id, "state": "proposed"})
        note._gc_duplicate_observations()
        self.assertTrue(human.exists())
        self.assertFalse(note.exists())

    def test_upgrade_refuses_to_discard_conflicting_human_proposals(self):
        self._drop_live_index()
        first = self.make()
        other = self.env["ai.recon.batch"].create({})
        second = first.copy({"batch_id": other.id})
        with self.assertRaises(UserError):
            first.init()
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_clusters_include_distinct_unknowns_from_different_days(self):
        first = self.make(confidence="none")
        first.state = "noted"
        earlier = self.env["ai.recon.batch"].create(
            {"date": fields.Datetime.subtract(fields.Datetime.now(), days=1)}
        )
        second = self.make(
            confidence="none",
            line=self._fresh_line(),
        )
        second.write({"batch_id": earlier.id, "state": "noted"})
        self.assertEqual(self.batch._top_clusters()[0]["count"], 2)
        self.assertEqual(earlier._top_clusters()[0]["count"], 2)

    def test_legacy_duplicates_do_not_inflate_clusters_before_cleanup(self):
        self._drop_live_index()
        first = self.make(confidence="none")
        first.state = "noted"
        other = self.env["ai.recon.batch"].create({})
        first.copy({"batch_id": other.id, "state": "noted"})
        self.assertFalse(self.batch._top_clusters())
        self.assertFalse(other._top_clusters())

    def test_cluster_corpus_respects_batch_companies_and_unresolved_state(self):
        first = self.make(confidence="none")
        second = self.make(
            confidence="none",
            line=self._fresh_line(),
        )
        (first | second).write({"state": "noted"})
        self.assertEqual(self.batch._top_clusters()[0]["count"], 2)
        self.batch.company_ids = self.env["res.company"].create(
            {"name": "Other corpus company"}
        )
        self.assertFalse(self.batch._top_clusters())
        self.batch.company_ids = self.company
        second.state = "rejected"
        self.assertFalse(self.batch._top_clusters())
