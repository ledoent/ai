"""Server-only, transaction-bound invocation authority; never an ORM context flag."""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from odoo.exceptions import AccessError

_scope = ContextVar("ai_agent_invocation", default=None)


@dataclass
class Invocation:
    call: object
    claimed: bool = False


@contextmanager
def invocation(call):
    token = _scope.set(Invocation(call))
    try:
        yield
    finally:
        _scope.reset(token)


def claim(record, method, arguments):
    scope = _scope.get()
    call = scope.call if scope else None
    if (
        not call
        or scope.claimed
        or record.env.cr is not call.env.cr
        or record.env.uid != call.user_id.id
        or record._name != call.model_name
        or method != call.method_name
        or json.dumps(arguments, sort_keys=True)
        != json.dumps(call._arguments(), sort_keys=True)
    ):
        raise AccessError(
            record.env._("This operation requires its originating invocation.")
        )
    scope.claimed = True
    return call
