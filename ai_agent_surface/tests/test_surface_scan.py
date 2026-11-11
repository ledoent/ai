from odoo.tests import tagged

from ..models.ai_agent_surface import IRREVERSIBLE, READONLY, WRITING
from .common import SurfaceScanCommon

# Floors, not expected values. The real figures are database-dependent -- they
# move with every installed module -- so pinning them exactly would be a flake
# generator across the odoo/ocb matrix legs.
#
# Set at roughly half of what was measured on a base+rpc+web install, which came
# out identical on stock odoo:20.0 (py3.12), on
# ghcr.io/oca/oca-ci/py3.12-odoo20.0 and on Python 3.15.0rc3:
#
#   plain internal user   1929 methods /  37 models
#                         (558 readonly, 1334 writing, 37 irreversible)
#   + base.group_system   4798 methods /  93 models
#                         (1399 readonly, 3301 writing, 98 irreversible)
#
# On this minimal install those 98 are six distinct names: unlink (on all 93
# models), ir.mail_server.send_email, ir.module.module's three
# button_immediate_* methods, and one action_cancel. Worth knowing when reading
# the figure -- it is dominated by unlink until business modules are installed.
#
# The INFO line _log_scan() emits is how you get the figures for your own
# install -- the point of the module is that you do not take ours.
MIN_MODELS = 18
MIN_METHODS = 950


@tagged("post_install", "-at_install")
class TestSurfaceScan(SurfaceScanCommon):
    """The headline measurement, plus the model-level half of access filtering.

    Both users are scanned exactly once, at class level: enumerating every model
    is genuinely expensive -- core raises its own HTTP timeout to
    ``12 + 0.01 * len(self.env)`` seconds for the same reason
    (``api_doc/tests/test_doc.py:33``) -- so re-scanning per test method would
    multiply that for no gain.
    """

    _user_prefix = "scan"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Surface = cls.env["ai.agent.surface"]
        # _log_scan() rather than _scan() plus our own logging: it is a shipped,
        # documented entry point, so exercising it here is the only coverage it
        # gets -- and it already emits the headline figures at INFO, which is
        # visible in the CI log and safe under checklog-odoo.
        cls.plain_scan = Surface.with_user(cls.plain_user)._log_scan()
        cls.system_scan = Surface.with_user(cls.system_user)._log_scan()

    # -- D: the headline number ---------------------------------------------

    def test_surface_is_non_trivial(self):
        self.assertGreaterEqual(self.plain_scan["model_count"], MIN_MODELS)
        self.assertGreaterEqual(self.plain_scan["method_count"], MIN_METHODS)

    def test_counts_are_internally_consistent(self):
        by_class = self.plain_scan["by_class"]
        self.assertEqual(sum(by_class.values()), self.plain_scan["method_count"])
        self.assertEqual(len(self.plain_scan["models"]), self.plain_scan["model_count"])
        flat = sum(len(m) for m in self.plain_scan["models"].values())
        self.assertEqual(flat, self.plain_scan["method_count"])

    def test_every_class_is_represented(self):
        """A scan that found no writing methods would mean the classifier broke."""
        for kind in (READONLY, WRITING, IRREVERSIBLE):
            with self.subTest(kind=kind):
                self.assertGreater(self.plain_scan["by_class"][kind], 0)

    def test_scan_output_carries_the_classifier_verdicts(self):
        """The scan wires the classifier through to its output.

        ``test_classifier.py`` covers the buckets themselves across a far wider
        name set; this only checks the plumbing between the two, so three
        representative names are enough.
        """
        partner = self.plain_scan["models"]["res.partner"]
        self.assertEqual(partner["search"], READONLY)
        self.assertEqual(partner["write"], WRITING)
        self.assertEqual(partner["unlink"], IRREVERSIBLE)

    def test_private_escapes_are_absent_from_the_scan(self):
        """The scan agrees with ``/json/2``'s own refusals (see A)."""
        partner = self.plain_scan["models"]["res.partner"]
        for method in ("browse", "sudo", "with_user", "with_env", "with_context"):
            with self.subTest(method=method):
                self.assertNotIn(method, partner)

    def test_this_module_is_not_in_its_own_surface(self):
        self.assertNotIn("ai.agent.surface", self.plain_scan["models"])

    # -- C1: model-level gating ---------------------------------------------

    def test_extra_group_widens_the_model_set(self):
        plain_models = set(self.plain_scan["models"])
        system_models = set(self.system_scan["models"])
        self.assertTrue(plain_models < system_models)
        # base/security/ir.access.csv:96,106 grant these to base.group_system only
        for model in ("ir.mail_server", "ir.actions.server"):
            with self.subTest(model=model):
                self.assertNotIn(model, plain_models)
                self.assertIn(model, system_models)

    def test_method_visibility_does_not_vary_by_user(self):
        """The asymmetry worth knowing: models differ, methods do not.

        Once both users can read a model, the set of methods ``/json/2`` will
        dispatch to on it is identical -- ``get_public_method`` consults only the
        class, never the user. So access rights alone cannot express *"this
        agent may read partners but not delete them"*; that needs an allowlist
        above the ACL layer. This single assertion is the strongest argument in
        the whole suite for a per-agent authorisation registry.
        """
        shared = set(self.plain_scan["models"]) & set(self.system_scan["models"])
        self.assertGreater(len(shared), MIN_MODELS)
        differing = [
            model
            for model in shared
            if self.plain_scan["models"][model] != self.system_scan["models"][model]
        ]
        self.assertEqual(differing, [])

    # -- robustness ----------------------------------------------------------

    def test_stale_ir_model_row_does_not_break_the_scan(self):
        """An ``ir.model`` row with no class in the registry must be skipped.

        These exist in the wild -- core carries the same guard at
        ``api_doc.py:162`` and its own regression test at
        ``api_doc/tests/test_doc.py:278-293``. Without the guard ``self.env[name]``
        raises ``KeyError``, which over HTTP becomes a 500.
        """
        self.env["ir.model"].create(
            {
                "model": "ai.agent.surface.test.ghost",
                "name": "Ghost Model",
                "state": "base",
            }
        )
        scan = self.scan_as(self.plain_user)
        self.assertNotIn("ai.agent.surface.test.ghost", scan["models"])
        # and the rest of the scan still came back intact
        self.assertGreaterEqual(scan["model_count"], MIN_MODELS)
