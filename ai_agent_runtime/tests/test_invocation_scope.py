from odoo.exceptions import AccessError
from odoo.tests import tagged

from ..invocation import claim, invocation
from .common import AgentRuntimeCommon


@tagged("post_install", "-at_install")
class TestInvocationScope(AgentRuntimeCommon):
    def test_scope_is_transaction_identity_target_and_arguments_bound(self):
        run = self.make_run(("count_partners", {"domain": []}))
        run._advance()
        call = run.call_ids
        target = call._execution_record()
        with self.assertRaises(AccessError):
            claim(target.with_context(internal=True), "search_count", {"domain": []})
        with invocation(call):
            with self.assertRaises(AccessError):
                claim(target, "search_count", {"domain": [("id", "=", -1)]})
            self.assertEqual(claim(target, "search_count", {"domain": []}), call)
            with self.assertRaises(AccessError):
                claim(target, "search_count", {"domain": []})
        with self.assertRaises(AccessError):
            claim(target, "search_count", {"domain": []})

    def test_force_approval_cannot_be_forged(self):
        run = self.make_run(("count_partners", {"domain": []}))
        run._advance()
        with self.assertRaises(AccessError):
            run.call_ids.write({"force_approval": False, "promotion_eligible": True})
