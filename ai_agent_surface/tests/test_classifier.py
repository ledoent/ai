import re

from odoo.tests import TransactionCase, tagged

from ..models.ai_agent_surface import (
    IRREVERSIBLE,
    IRREVERSIBLE_PATTERNS,
    READONLY,
    WRITING,
)

# Real Odoo method names, and the bucket each must land in. These are asserted
# against the classifier directly rather than through a scan, because most of
# them live on models (mail.thread, account.move, sale.order) that this addon
# does not depend on -- the heuristic still has to be right for installs that
# do have them.
EXPECTED = {
    # read-only, read from core's own @api.readonly marker
    "search": READONLY,
    "search_read": READONLY,
    "search_count": READONLY,
    "read": READONLY,
    # plain writes
    "create": WRITING,
    "write": WRITING,
    "copy": WRITING,
    # Reversible: action_unarchive undoes it (odoo/orm/models.py:5279, :5290).
    # The heuristic deliberately does not call this irreversible.
    "action_archive": WRITING,
    # Both are pure reads, yet neither carries @api.readonly in 20.0
    # (fields_get at odoo/orm/models.py:2677, read_group at :1932) -- so
    # /json/2 hands them a read/write cursor and this module reports them as
    # writing. That is the honest answer: the classification reflects what the
    # route actually does, not what the method morally is.
    "fields_get": WRITING,
    "read_group": WRITING,
    # irreversible by heuristic
    "unlink": IRREVERSIBLE,
    "action_post": IRREVERSIBLE,
    "action_confirm": IRREVERSIBLE,
    "action_cancel": IRREVERSIBLE,
    "button_validate": IRREVERSIBLE,
    "button_immediate_install": IRREVERSIBLE,
    "action_invoice_sent": IRREVERSIBLE,
    "action_send_mail": IRREVERSIBLE,
    "action_quotation_send": IRREVERSIBLE,
    "send_mail": IRREVERSIBLE,  # mail.template.send_mail
    "message_post": IRREVERSIBLE,
    "message_post_with_source": IRREVERSIBLE,
    "message_mail": IRREVERSIBLE,
}


@tagged("post_install", "-at_install")
class TestClassifier(TransactionCase):
    """Pins the irreversible heuristic, which is the one opinion this addon holds.

    Everything else the scan reports is read out of core and cannot drift
    without core changing. ``IRREVERSIBLE_PATTERNS`` can drift silently, in two
    directions: a pattern can stop matching what it was written for, or it can
    never have matched at all.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Surface = cls.env["ai.agent.surface"]
        # res.partner carries most of the names above as real methods, and the
        # read-only verdicts have to come from a real class to be meaningful.
        cls.partner = cls.env["res.partner"]

    def test_known_names_land_in_the_right_bucket(self):
        for name, expected in EXPECTED.items():
            with self.subTest(method=name):
                self.assertEqual(self.Surface._classify(self.partner, name), expected)

    def test_every_pattern_is_reachable(self):
        """No pattern may be incapable of ever matching.

        This is a regression test for a real defect, not a hypothetical: an
        earlier revision carried ``r"_(send_mail|send_message)$"``, which can
        only match a name beginning with ``_`` -- and ``get_public_method``
        rejects every such name before the classifier ever sees it
        (``odoo/orm/models.py:206``). It had never matched anything, silently
        under-reporting the irreversible count.

        Guarding it generically: every pattern must match at least one of the
        realistic names in EXPECTED, all of which are public method names.
        """
        public_names = list(EXPECTED)
        for pattern in IRREVERSIBLE_PATTERNS:
            with self.subTest(pattern=pattern):
                matched = [n for n in public_names if re.match(pattern, n)]
                self.assertTrue(
                    matched,
                    f"pattern {pattern!r} matches none of the known public "
                    f"method names, so it can never fire",
                )

    def test_patterns_are_fully_anchored(self):
        """Partial anchoring is how the dead pattern got in unnoticed."""
        for pattern in IRREVERSIBLE_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertTrue(pattern.startswith("^"), "must anchor at the start")
                self.assertTrue(pattern.endswith("$"), "must anchor at the end")

    def test_readonly_beats_the_name_heuristic(self):
        """A read-only cursor cannot write, whatever the name suggests.

        ``search`` would not match any irreversible pattern anyway; the point is
        the precedence, so assert it on a name that *would* match if the
        read-only check did not run first.
        """
        self.assertTrue(
            any(re.match(p, "unlink") for p in IRREVERSIBLE_PATTERNS),
            "precondition: 'unlink' matches an irreversible pattern",
        )
        self.assertFalse(self.Surface._is_readonly(self.partner, "unlink"))
        self.assertEqual(self.Surface._classify(self.partner, "unlink"), IRREVERSIBLE)
        # ...and the mirror: a read-only method never reaches the name test.
        self.assertTrue(self.Surface._is_readonly(self.partner, "search"))
        self.assertEqual(self.Surface._classify(self.partner, "search"), READONLY)

    def test_private_and_absent_names_are_not_callable(self):
        """The three rejection branches of ``get_public_method``, via our wrapper."""
        for name in ("_scan", "sudo", "browse", "no_such_method_at_all", "mro"):
            with self.subTest(name=name):
                self.assertIsNone(
                    self.Surface._is_callable_remotely(self.partner, name)
                )

    def test_fields_are_not_callable(self):
        """``dir()`` yields field names too; they must not count as methods."""
        self.assertIn("name", dir(self.partner))
        self.assertIsNone(self.Surface._is_callable_remotely(self.partner, "name"))
