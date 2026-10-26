from odoo.tests import tagged

from .common import SurfaceScanCommon


@tagged("post_install", "-at_install")
class TestFieldAccess(SurfaceScanCommon):
    """Field-level gating is a separate axis from model-level gating.

    Asserted directly rather than through the scan, because ``has_field_access``
    **does not consult model access** (``odoo/orm/models.py:2703-2719`` ->
    ``_has_field_access`` at ``:2721-2734`` looks only at ``field.groups``). That
    is what makes this provable at all: with only ``base`` installed there is no
    model that two users can both read carrying a field only one of them can --
    every group-restricted field in ``base`` sits on a model that is itself
    ``base.group_system``-only. So the field axis has to be probed on its own.
    """

    _user_prefix = "fld"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.MailServer = cls.env["ir.mail_server"]

    # Field names, not Field objects. A Field assigned to a class attribute is
    # still a descriptor, so `self.restricted` would invoke Field.__get__ with
    # the TestCase as the record and blow up on `record._ids`. Resolve in-test.
    RESTRICTED = "smtp_pass"  # groups='base.group_system', ir_mail_server.py:153
    UNRESTRICTED = "smtp_host"  # no groups= at all -- the control

    def _field(self, name):
        return self.MailServer._fields[name]

    def test_restricted_field_differs_by_group(self):
        field = self._field(self.RESTRICTED)
        as_plain = self.MailServer.with_user(self.plain_user)
        as_system = self.MailServer.with_user(self.system_user)
        self.assertFalse(as_plain.has_field_access(field, "read"))
        self.assertTrue(as_system.has_field_access(field, "read"))

    def test_unrestricted_field_is_visible_to_both(self):
        """Proves it is ``groups=`` doing the work, not a blanket denial."""
        for user in (self.plain_user, self.system_user):
            with self.subTest(user=user.login):
                self.assertTrue(
                    self.MailServer.with_user(user).has_field_access(
                        self._field(self.UNRESTRICTED), "read"
                    )
                )

    def test_sudo_sees_everything(self):
        """Same short-circuit as ``has_access``: ``_has_field_access`` returns
        True on ``env.su`` before consulting ``field.groups`` (``:2730``)."""
        self.assertTrue(
            self.MailServer.with_user(self.plain_user)
            .sudo()
            .has_field_access(self._field(self.RESTRICTED), "read")
        )

    def test_cross_user_comparison_is_not_poisoned_by_caching(self):
        """The per-user caches are safe to compare inside one transaction.

        It looks unsafe and is not: ``_field_access_memo`` is a
        ``cached_property`` on the *Environment* (``odoo/orm/environments.py:534-537``),
        and environments are keyed by uid, so ``with_user()`` yields a clean
        memo. Asserted in both orders because the memo only ever caches
        positives -- if it leaked across users, querying the system user first
        would make the plain user's answer wrong.
        """
        field = self._field(self.RESTRICTED)
        as_system = self.MailServer.with_user(self.system_user)
        as_plain = self.MailServer.with_user(self.plain_user)
        self.assertTrue(as_system.has_field_access(field, "read"))
        self.assertFalse(as_plain.has_field_access(field, "read"))
        self.assertTrue(as_system.has_field_access(field, "read"))
