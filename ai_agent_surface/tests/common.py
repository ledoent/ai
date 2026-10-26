import json
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, TransactionCase, new_test_user

CT_JSON = {"Content-Type": "application/json; charset=utf-8"}


class TwoUsersMixin:
    """Builds two users differing *only* by ``base.group_system``.

    That single-group difference is the whole experimental control: anything the
    two users see differently is attributable to one group and nothing else.

    A plain mixin rather than a base class so both the ``TransactionCase`` and
    the ``HttpCase`` suites can share it -- the scan tests have no reason to pay
    ``HttpCase``'s server startup.

    Deliberately builds its own users rather than inheriting
    ``HttpCaseWithUserDemo``: 19.0 onwards ships no demo data unless
    ``--with-demo`` is passed, and OCA CI does not pass it, so ``base.user_demo``
    is not there to inherit.
    """

    @classmethod
    def _setup_two_users(cls, prefix):
        cls.plain_user = new_test_user(
            cls.env, login=f"{prefix}_plain", groups="base.group_user"
        )
        cls.system_user = new_test_user(
            cls.env,
            login=f"{prefix}_system",
            groups="base.group_user,base.group_system",
        )

    def scan_as(self, user):
        return self.env["ai.agent.surface"].with_user(user)._scan()


class SurfaceScanCommon(TwoUsersMixin, TransactionCase):
    """In-process suites: no HTTP, no server thread."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_two_users(cls._user_prefix)


class SurfaceHttpCommon(TwoUsersMixin, HttpCase):
    """Adds a scope-``rpc`` bearer key for ``plain_user``."""

    _user_prefix = "agent"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_two_users(cls._user_prefix)
        # _generate() attributes the key to env.user, so it has to run *as* the
        # key's owner -- the with_user() rebinding is what makes this
        # plain_user's key rather than the superuser's. Same shape as core's
        # test_webjson2.py:23-30. 0.5 days keeps it inside the non-system cap
        # that _check_expiration_date enforces (res_users.py:1670-1677).
        cls.plain_key = (
            cls.plain_user.with_user(cls.plain_user)
            .env["res.users.apikeys"]
            ._generate(
                scope="rpc",
                name="ai_agent_surface test",
                # fields.Datetime.now() rather than datetime.now(): naive UTC,
                # which is what the ORM stores, and it satisfies ruff's DTZ005.
                expiration_date=fields.Datetime.now() + timedelta(days=0.5),
            )
        )
        cls.bearer = {"Authorization": f"Bearer {cls.plain_key}"}

    def _json2(self, model, method, **payload):
        """POST to /json/2/<model>/<method> as ``plain_user``.

        Serialises the body itself and names the method explicitly rather than
        passing ``json=payload``. ``url_open`` only promotes a request to POST
        when ``data``/``files``/``json`` is *truthy*
        (``odoo/tests/common.py:2733-2741``), so a call with no keyword
        arguments -- ``json={}`` -- stays a GET. The two ``/json/2`` routes are
        split by method: ``web_json_2_rpc`` is ``methods=['POST']`` while the
        ``/json/2/<path:subpath>`` catch-all also accepts GET, so a GET silently
        lands on the catch-all and returns a 404 *"Did you mean POST ...?"* --
        which looks exactly like a routing or permission failure and is neither.
        """
        return self.url_open(
            f"/json/2/{model}/{method}",
            method="POST",
            data=json.dumps(payload),
            headers={**CT_JSON, **self.bearer},
        )
