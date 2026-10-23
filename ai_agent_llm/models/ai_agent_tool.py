import inspect
import re
import typing

from odoo import models
from odoo.models import get_public_method

# reST ``:param name: text`` -- the form Odoo's own docstrings use and the one
# api_doc parses.
_PARAM_RE = re.compile(r"^\s*:param\s+(?:\S+\s+)?(\w+)\s*:\s*(.+)$", re.MULTILINE)

_JSON_TYPES = {
    int: "integer",
    float: "number",
    bool: "boolean",
    str: "string",
    list: "array",
    dict: "object",
}


class AiAgentTool(models.Model):
    _inherit = "ai.agent.tool"

    def _docstring_params(self, method):
        doc = inspect.getdoc(method) or ""
        return dict(_PARAM_RE.findall(doc))

    def _json_type(self, annotation):
        """Best-effort JSON Schema type for a Python annotation."""
        if annotation is inspect.Parameter.empty:
            return None
        origin = typing.get_origin(annotation) or annotation
        if origin in _JSON_TYPES:
            return _JSON_TYPES[origin]
        # Sequence[int], list[int], Iterable[...] and friends all behave as
        # arrays over the wire.
        if origin in (list, tuple, set, frozenset):
            return "array"
        return None

    def _tool_schema(self):
        """Derive a provider-neutral tool definition from the real signature.

        This is the empirical answer to a question the OCA/ai thread parked
        (petrus-v proposed docstring/signature auto-discovery; etobella
        deferred it for lack of quorum). The finding: **derivation gets you the
        shape for free, and cannot get you the semantics.**

        Parameter names, which are required, their Python-typed JSON types and
        any ``:param:`` text all come out of ``inspect.signature`` and the
        docstring with no human effort. What does not is *when an agent should
        reach for this tool at all* -- Odoo's docstrings describe mechanics for
        a developer who already decided to call the method. So the
        ``description`` here stays hand-written, and that is exactly the
        curation ``ai_tool`` provides.

        Practical consequence: derive the schema, write the description. The two
        positions in that thread are answering different halves of the problem.
        """
        self.ensure_one()
        record = self.env[self.model_name]
        method = get_public_method(record, self.method_name)
        signature = inspect.signature(method)
        doc_params = self._docstring_params(method)

        properties, required = {}, []
        for name, param in signature.parameters.items():
            if name == "self" or param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            prop = {}
            json_type = self._json_type(param.annotation)
            if json_type:
                prop["type"] = json_type
            if name in doc_params:
                prop["description"] = doc_params[name]
            properties[name] = prop
            if param.default is inspect.Parameter.empty:
                required.append(name)

        return {
            "name": self.name,
            # Hand-written, and deliberately so -- see the docstring above.
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
            # Not sent to the provider; useful when reporting what an agent was
            # offered and how risky the offer was.
            "_classification": self.classification,
            "_requires_approval": self.requires_approval,
        }
