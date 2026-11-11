from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestSudoHasNoAuditMode(TransactionCase):
    """``sudo()`` cannot tell you what the user would not have been allowed.

    This test will not fail, and that is deliberate. It pins a premise rather
    than guarding a regression: every access predicate short-circuits
    unconditionally under ``env.su``, so changing this behaviour would be a core
    API break, not a bug.

    Its value is that a very natural agent design -- *"run the tool as sudo, but
    log what the user could not have seen, so we can warn them"* -- is
    impossible, and the reason is four one-line short-circuits rather than
    anything discoverable from documentation. The only honest shape is
    ``with_user()`` plus ``_filtered_access()``, which is what the next test
    demonstrates.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, login="su_probe", groups="base.group_user")
        # ir.mail_server is granted to base.group_system only
        # (base/security/ir.access.csv), so a plain internal user cannot read it.
        cls.denied = cls.env["ir.mail_server"].with_user(cls.user)

    def test_access_is_denied_without_sudo(self):
        self.assertFalse(self.denied.has_access("read"))

    def test_sudo_reports_access_and_cannot_be_asked_otherwise(self):
        granted = self.denied.sudo()
        # odoo/orm/models.py:3469-3470 -- `if self.env.su: return True`, before
        # any rule evaluation. There is no flag, context key or kwarg that makes
        # it report what the underlying user would have got.
        self.assertTrue(granted.has_access("read"))
        self.assertTrue(granted.env.su)

    def test_filtered_access_is_the_only_honest_answer(self):
        """``with_user()`` + ``_filtered_access()`` does what sudo cannot."""
        servers = self.env["ir.mail_server"].sudo().search([])
        as_user = servers.with_user(self.user)
        self.assertFalse(as_user._filtered_access("read"))
        # ...and the same recordset under sudo claims all of them, which is the
        # contrast that makes the point.
        self.assertEqual(as_user.sudo()._filtered_access("read"), servers)
