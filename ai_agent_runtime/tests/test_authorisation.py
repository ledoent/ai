from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import AgentRuntimeCommon


@tagged("post_install", "-at_install")
class TestAuthorisation(AgentRuntimeCommon):
    """The two controls, and the proof they are not the same control.

    ``ai_agent_surface`` established that Odoo's access rights cannot express
    "may read partners but not delete them", because method visibility does not
    vary by user. So an agent needs *two* independent gates, and these tests
    exist to show neither substitutes for the other:

    * the **allowlist** -- what this agent may ask for
    * the **identity** -- what its user is permitted to do
    """

    def test_ungranted_tool_is_refused(self):
        """A tool record existing is not authorisation to use it.

        ``count_users`` is a perfectly valid tool, and the agent's user *can*
        read res.users -- so identity alone would allow this. Only the
        allowlist stops it.
        """
        run = self.make_run(("count_users", {"domain": []}))
        run._advance()
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.call_ids.state, "refused")
        self.assertEqual(run.step_no, 1)

    def test_allowlisted_tool_still_obeys_access_rights(self):
        """And the mirror: the allowlist does not grant access rights.

        ``count_mail_servers`` *is* on the agent's allowlist, but its user has
        only base.group_user and ir.mail_server is group_system-only. The run
        fails rather than succeeding, which is the proof the call really ran as
        the agent's user and not as sudo.
        """
        run = self.make_run(("count_mail_servers", {"domain": []}))
        run._advance()
        self.assertEqual(run.state, "failed")
        self.assertTrue(run.error)
        # The attempt is still recorded -- a refused action is audit-worthy.
        self.assertEqual(len(run.step_ids), 1)
        self.assertTrue(run.step_ids.failed)

    def test_the_same_call_succeeds_for_a_privileged_agent(self):
        """Isolates identity as the variable.

        Same tool, same plan, same allowlist -- only the agent's user differs.
        If this passed *and* the previous test passed for the wrong reason
        (a broken tool, say), this would fail too.
        """
        from odoo.tests import new_test_user

        admin_ish = new_test_user(
            self.env,
            login="agent_privileged",
            groups="base.group_user,base.group_system",
        )
        self.agent.user_id = admin_ish
        run = self.make_run(("count_mail_servers", {"domain": []}))
        run._advance()
        self.assertEqual(run.state, "done")
        self.assertFalse(run.step_ids.failed)

    def test_agent_cannot_act_as_superuser(self):
        """Because under env.su there is nothing left to audit or restrict."""
        with self.assertRaises(ValidationError):
            self.agent.user_id = self.env.ref("base.user_root")

    def test_tool_cannot_target_a_private_method(self):
        """Rejected at definition time, reusing ai_agent_surface's predicate."""
        for method in ("sudo", "with_user", "_scan", "no_such_method"):
            with self.subTest(method=method), self.assertRaises(ValidationError):
                self.Tool.create(
                    {
                        "name": f"escape_{method}",
                        "description": "Should never be creatable.",
                        "model_name": "res.partner",
                        "method_name": method,
                    }
                )

    def test_tool_cannot_target_a_missing_model(self):
        with self.assertRaises(ValidationError):
            self.Tool.create(
                {
                    "name": "nope",
                    "description": "Should never be creatable.",
                    "model_name": "no.such.model",
                    "method_name": "search_count",
                }
            )

    def test_tool_classification_comes_from_the_surface_module(self):
        self.assertEqual(self.tool_count.classification, "readonly")
        self.assertEqual(self.tool_create.classification, "writing")
