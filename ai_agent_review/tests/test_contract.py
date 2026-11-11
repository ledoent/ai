from typing import ClassVar

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.orm.model_classes import add_to_registry
from odoo.tests import TransactionCase, new_test_user, tagged


class ReviewContractProposal(models.Model):
    """A second vertical, registered only inside this test transaction."""

    _name = "ai.review.contract.proposal"
    _description = "Review Contract Proposal"
    _inherit: ClassVar[list[str]] = ["ai.proposal.mixin"]
    _module = "ai_agent_review"
    _register = False
    _log_access = False

    batch_id = fields.Many2one("ai.review.contract.batch", ondelete="cascade")
    subject_id = fields.Many2one("res.partner", required=True, ondelete="cascade")
    payload = fields.Char(default="Approved change")
    previous_ref = fields.Char()
    flagged = fields.Boolean()
    preconditions_ok = fields.Boolean(default=True)
    group_key = fields.Char()

    def _apply(self):
        self.ensure_one()
        self.previous_ref = self.subject_id.ref
        self.subject_id.ref = self.payload

    def _undo(self):
        self.ensure_one()
        self.subject_id.ref = self.previous_ref

    def _flag_for_attention(self):
        self.flagged = True

    def _assert_preconditions(self):
        if any(not proposal.preconditions_ok for proposal in self):
            raise UserError(self.env._("The test vertical is not ready."))
        return True

    def _cluster_key(self):
        self.ensure_one()
        return self.group_key or None

    @api.model
    def _cluster_label(self, key):
        return f"Group {key}"

    @api.model
    def _draft_rule(self, key, proposals):
        return self.env["ai.review.contract.rule"].create(
            {"name": key, "active": False, "source_count": len(proposals)}
        )


class ReviewContractBatch(models.Model):
    _name = "ai.review.contract.batch"
    _description = "Review Contract Batch"
    _inherit: ClassVar[list[str]] = ["ai.batch.mixin"]
    _module = "ai_agent_review"
    _register = False
    _log_access = False

    proposal_ids = fields.One2many("ai.review.contract.proposal", "batch_id")

    @api.model
    def _proposal_model(self):
        return "ai.review.contract.proposal"


class ReviewContractRule(models.Model):
    _name = "ai.review.contract.rule"
    _description = "Review Contract Rule"
    _module = "ai_agent_review"
    _register = False
    _log_access = False

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    source_count = fields.Integer()


@tagged("post_install", "-at_install")
class TestReviewContract(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        definitions = (
            ReviewContractProposal,
            ReviewContractBatch,
            ReviewContractRule,
        )
        cls.env.transaction.will_change_registry()
        names = [definition._name for definition in definitions]

        def remove_test_models():
            cls.env.invalidate_all()
            for name in names:
                if name not in cls.registry:
                    continue
                for model in cls.registry.models.values():
                    model._inherit_children.discard(name)
                del cls.registry[name]

        cls.addClassCleanup(remove_test_models)
        for definition in definitions:
            add_to_registry(cls.registry, definition)
        cls.registry._setup_models__(cls.cr, names)
        cls.registry.init_models(cls.cr, names, {})
        cls.Proposal = cls.env["ai.review.contract.proposal"]
        cls.Batch = cls.env["ai.review.contract.batch"]
        cls.subject = cls.env["res.partner"].create(
            {"name": "Contract subject", "ref": "Original"}
        )
        cls.batch = cls.Batch.create({})

    def make_proposal(self, **values):
        return self.Proposal.create(
            {
                "batch_id": self.batch.id,
                "subject_id": self.subject.id,
                "confidence": "medium",
                **values,
            }
        )

    def test_staging_does_not_change_the_subject(self):
        proposal = self.make_proposal()
        self.assertEqual(proposal.state, "proposed")
        self.assertEqual(self.subject.ref, "Original")
        self.assertFalse(proposal.approved_by)
        self.assertFalse(proposal.approved_date)

    def test_execute_refuses_every_unapproved_state(self):
        proposal = self.make_proposal()
        for state in ("observed", "proposed", "noted", "rejected", "applied"):
            proposal.state = state
            with self.subTest(state=state), self.assertRaises(UserError):
                proposal._execute()
            self.assertEqual(self.subject.ref, "Original")

    def test_approval_executes_and_records_the_reviewer(self):
        proposal = self.make_proposal()
        proposal.action_approve()
        self.assertEqual(proposal.state, "applied")
        self.assertEqual(self.subject.ref, proposal.payload)
        self.assertEqual(proposal.approved_by, self.env.user)
        self.assertTrue(proposal.approved_date)

    def test_observed_proposal_can_be_approved(self):
        proposal = self.make_proposal(state="observed")
        proposal.action_approve()
        self.assertEqual(proposal.state, "applied")

    def test_rejection_records_judgement_without_applying(self):
        proposal = self.make_proposal()
        proposal.action_reject()
        self.assertEqual(proposal.state, "rejected")
        self.assertEqual(proposal.approved_by, self.env.user)
        self.assertTrue(proposal.approved_date)
        self.assertEqual(self.subject.ref, "Original")

    def test_terminal_proposal_cannot_be_judged_twice(self):
        proposal = self.make_proposal()
        proposal.action_approve()
        for action in (proposal.action_approve, proposal.action_reject):
            with self.assertRaises(UserError):
                action()

    def test_undo_restores_the_subject_and_reopens_the_proposal(self):
        proposal = self.make_proposal()
        proposal.action_approve()
        proposal.action_undo()
        self.assertEqual(proposal.state, "proposed")
        self.assertEqual(self.subject.ref, "Original")

    def test_undo_ignores_a_proposal_that_was_not_applied(self):
        proposal = self.make_proposal()
        proposal.action_undo()
        self.assertEqual(proposal.state, "proposed")
        self.assertEqual(self.subject.ref, "Original")

    def test_default_high_confidence_flags_instead_of_applying(self):
        proposal = self.make_proposal(confidence="high")
        self.Proposal._route(proposal)
        self.assertTrue(proposal.flagged)
        self.assertEqual(proposal.state, "proposed")
        self.assertEqual(self.subject.ref, "Original")

    def test_none_confidence_is_only_noted(self):
        proposal = self.make_proposal(confidence="none")
        self.Proposal._route(proposal)
        self.assertEqual(proposal.state, "noted")
        self.assertFalse(proposal.flagged)
        self.assertEqual(self.subject.ref, "Original")

    def test_preconditions_refuse_the_whole_route_before_any_effect(self):
        allowed = self.make_proposal(confidence="high")
        blocked = self.make_proposal(preconditions_ok=False)
        with self.assertRaises(UserError):
            self.Proposal._route(allowed | blocked)
        self.assertFalse(allowed.flagged)
        self.assertFalse(blocked.flagged)
        self.assertEqual(self.subject.ref, "Original")

    def test_execute_checks_preconditions_before_applying(self):
        proposal = self.make_proposal(state="approved", preconditions_ok=False)
        with self.assertRaises(UserError):
            proposal._execute()
        self.assertEqual(proposal.state, "approved")
        self.assertEqual(self.subject.ref, "Original")

    def test_approval_checks_preconditions_before_recording_judgement(self):
        allowed = self.make_proposal()
        blocked = self.make_proposal(preconditions_ok=False)
        with self.assertRaises(UserError):
            (allowed | blocked).action_approve()
        self.assertEqual((allowed | blocked).mapped("state"), ["proposed", "proposed"])
        self.assertFalse((allowed | blocked).approved_by)
        self.assertFalse(allowed.approved_date)
        self.assertFalse(blocked.approved_date)
        self.assertEqual(self.subject.ref, "Original")

    def test_approval_checks_all_states_before_recording_judgement(self):
        proposed = self.make_proposal()
        rejected = self.make_proposal(state="rejected")
        with self.assertRaises(UserError):
            (proposed | rejected).action_approve()
        self.assertEqual(proposed.state, "proposed")
        self.assertEqual(rejected.state, "rejected")
        self.assertFalse((proposed | rejected).approved_by)
        self.assertFalse(proposed.approved_date)
        self.assertEqual(self.subject.ref, "Original")

    def test_batch_counts_follow_confidence_changes(self):
        high = self.make_proposal(confidence="high")
        self.make_proposal(confidence="medium")
        self.make_proposal(confidence="none")
        self.assertEqual(
            (self.batch.count_high, self.batch.count_medium, self.batch.count_none),
            (1, 1, 1),
        )
        high.confidence = "none"
        self.assertEqual(
            (self.batch.count_high, self.batch.count_medium, self.batch.count_none),
            (0, 1, 2),
        )

    def test_clusters_use_vertical_keys_and_exclude_singletons(self):
        first = self.make_proposal(confidence="none", group_key="repeat")
        second = self.make_proposal(confidence="none", group_key="repeat")
        self.make_proposal(confidence="none", group_key="singleton")
        self.make_proposal(confidence="none")
        self.make_proposal(confidence="high", group_key="repeat")
        self.assertEqual(self.batch._unmatched_clusters(), {"repeat": first | second})

    def test_top_clusters_exposes_only_label_and_count(self):
        for _index in range(2):
            self.make_proposal(confidence="none", group_key="repeat")
        self.assertEqual(
            self.batch._top_clusters(), [{"label": "Group repeat", "count": 2}]
        )
        self.assertEqual(self.batch._top_clusters(limit=0), [])

    def test_drafted_rules_are_inactive_and_use_the_whole_cluster(self):
        for _index in range(3):
            self.make_proposal(confidence="none", group_key="repeat")
        self.make_proposal(confidence="none", group_key="singleton")
        rules = self.batch.action_draft_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].name, "repeat")
        self.assertEqual(rules[0].source_count, 3)
        self.assertFalse(rules[0].active)
        self.assertEqual(self.subject.ref, "Original")

    # -- the proposer is not the judge ---------------------------------------

    def _agent_identity(self):
        """A user that some ai.agent names as its execution identity."""
        user = new_test_user(
            self.env, login="contract_agent_exec", groups="base.group_user"
        )
        self.env["ai.agent"].create(
            {
                "name": "Contract agent",
                "user_id": user.id,
                "tool_ids": [],
            }
        )
        return user

    def test_execution_identity_cannot_approve_its_own_proposal(self):
        """Staging as data only means anything if approval needs someone else.

        A vertical has to grant the agent's own group write access to the
        proposal model so the agent can create proposals, and action_approve is
        reachable with that same grant -- so without this the agent approves its
        own work and _apply() runs. Measured against ai_recon_review before the
        check existed: a medium-confidence proposal, created and approved by the
        agent's credential over /json/2, rewrote a bank suspense line.
        """
        proposal = self.make_proposal()
        agent_user = self._agent_identity()
        with self.assertRaises(AccessError) as caught:
            proposal.with_user(agent_user).action_approve()
        self.assertIn("cannot judge its own proposal", str(caught.exception))
        self.assertEqual(proposal.state, "proposed")
        self.assertFalse(proposal.approved_by)
        self.assertEqual(self.subject.ref, "Original")

    def test_execution_identity_cannot_undo_an_applied_proposal(self):
        proposal = self.make_proposal()
        proposal.action_approve()
        self.assertEqual(proposal.state, "applied")
        agent_user = self._agent_identity()
        with self.assertRaises(AccessError):
            proposal.with_user(agent_user).action_undo()
        self.assertEqual(proposal.state, "applied")

    def test_a_human_reviewer_is_still_free_to_approve(self):
        """The guard keys on being an execution identity, not on lacking rights."""
        self._agent_identity()
        proposal = self.make_proposal()
        proposal.action_approve()
        self.assertEqual(proposal.state, "applied")
        self.assertEqual(proposal.approved_by, self.env.user)

    def test_rejecting_your_own_proposal_stays_allowed(self):
        """Declining applies nothing, and an agent may retract its own work."""
        proposal = self.make_proposal()
        agent_user = self._agent_identity()
        proposal.with_user(agent_user).sudo().action_reject()
        self.assertEqual(proposal.state, "rejected")
        self.assertEqual(self.subject.ref, "Original")
