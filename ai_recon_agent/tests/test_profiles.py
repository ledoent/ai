from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestProfiles(TransactionCase):
    def test_protected_records_cannot_be_forged(self):
        for name in (
            "ai.recon.match.proposal",
            "ai.recon.assessment",
            "ai.recon.match.batch",
            "ai.recon.match.allocation",
        ):
            with self.subTest(model=name), self.assertRaises(AccessError):
                self.env[name].with_context(
                    internal=True, default_state="applied"
                ).create({})

    def test_profile_starts_disabled(self):
        p = self.env["ai.recon.profile"].create({"company_id": self.env.company.id})
        self.assertFalse(p.enabled)
        self.assertFalse(p.llm_enabled)
        with self.assertRaises(AccessError):
            p.write({"revision": 999})

    def test_direct_service_rpc_is_refused(self):
        for method in ("match_items", "post_match"):
            with self.assertRaises(AccessError):
                getattr(
                    self.env["ai.recon.match.service"].with_context(internal=True),
                    method,
                )(1, "fake")
