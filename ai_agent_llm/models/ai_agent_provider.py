import json
import logging
import math
import os
import types

import requests

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

TIMEOUT = 120

ENDPOINTS = types.MappingProxyType(
    {
        "anthropic": "https://api.anthropic.com/v1/messages",
        "openai": "https://api.openai.com/v1/chat/completions",
    }
)


class AiAgentProvider(models.Model):
    """Where the next action comes from.

    Kept behind a thin, provider-agnostic interface -- ``_decide()`` takes a
    goal, a transcript and a list of tool schemas, and returns either a tool
    call or a final answer. Every vendor's tool-calling API is a different
    shape, and none of them is a good thing to build an agent's control flow
    around directly.
    """

    _name = "ai.agent.provider"
    _description = "AI Agent Provider"

    name = fields.Char(required=True)
    kind = fields.Selection(
        [
            ("stub", "Stub (scripted, for tests)"),
            ("anthropic", "Anthropic Messages API"),
            ("openai", "OpenAI Chat Completions API"),
        ],
        required=True,
        default="stub",
    )
    model = fields.Char(default="claude-sonnet-5", help="Provider's model identifier.")
    api_key_env = fields.Char(
        default="ANTHROPIC_API_KEY",
        help="Name of the environment variable holding the API key. The key "
        "itself is deliberately NOT stored here: a credential in a database "
        "column ends up in every backup, every duplicate and every support "
        "export of that database.",
    )
    max_tokens = fields.Integer(default=2048)
    base_url = fields.Char(
        help="Leave empty to use the kind's default endpoint.",
    )

    def _endpoint(self):
        self.ensure_one()
        return self.base_url or ENDPOINTS.get(self._effective_kind())

    def _effective_kind(self):
        self.ensure_one()
        return self.env.context.get("_ai_usage_kind_snapshot") or self.kind

    def _effective_model(self):
        self.ensure_one()
        return self.env.context.get("_ai_usage_model_snapshot") or self.model

    def _reported_cost(self, body):
        """Read an optional provider cost in its explicitly reported currency."""
        raw = body.get("cost")
        if isinstance(raw, dict):
            amount = raw.get("amount")
            currency = raw.get("currency")
        else:
            amount = raw if raw is not None else body.get("cost_amount")
            currency = body.get("cost_currency")
        if (
            type(amount) in (int, float)
            and math.isfinite(amount)
            and amount >= 0
            and isinstance(currency, str)
            and currency
        ):
            return {"amount": amount, "currency": currency}
        return None

    SYSTEM_PROMPT = (
        "You are an agent operating an Odoo ERP. Use the provided tools to "
        "accomplish the goal. Call exactly one tool at a time. When you have "
        "the answer, reply with text and no tool call. Never guess an "
        "identifier or a total -- look it up with a tool first."
    )

    def _api_key(self):
        self.ensure_one()
        key = os.environ.get(self.api_key_env or "")
        if not key:
            raise UserError(
                self.env._(
                    "No API key found in environment variable %(var)s.",
                    var=self.api_key_env,
                )
            )
        return key

    # -- the interface the runtime depends on --------------------------------

    def _decide(self, goal, transcript, tool_schemas):
        """Return the next action.

        :param goal: what the user asked for, as text
        :param transcript: list of ``{"tool", "args", "result"}`` dicts, oldest
            first -- what has already happened
        :param tool_schemas: list of provider-neutral tool definitions
        :return: ``{"kind": "tool", "tool": name, "args": {...}}`` or
            ``{"kind": "final", "answer": "..."}``
        """
        self.ensure_one()
        return self._decide_result(goal, transcript, tool_schemas)["decision"]

    def _decide_result(self, goal, transcript, tool_schemas, on_send=None):
        """Return a normalized decision and provider response metadata.

        ``on_send`` is an internal hook used by the optional usage addon. It is
        called after local validation and immediately before a billable request
        is sent. The default contract stays provider-neutral and ``_decide``
        remains backwards compatible for callers that only need the decision.
        """
        self.ensure_one()
        return getattr(self, f"_decide_result_{self._effective_kind()}")(
            goal, transcript, tool_schemas, on_send=on_send
        )

    def _decide_result_stub(self, goal, transcript, tool_schemas, on_send=None):
        """Replay a scripted sequence, indexed by how much has happened.

        This is what makes the loop testable at all: the control flow, the
        gates and the durability can be asserted deterministically, with no
        network and no API key, which in turn means they can run in CI. Only
        the quality of the model's choices needs a real provider.

        The script travels in the context rather than in a field, so the stub
        needs no column of its own and no secret-shaped state: context
        propagates to related records, so setting it on the run reaches the
        provider.
        """
        script = self.env.context.get("stub_script") or []
        step = len(transcript)
        if step >= len(script):
            decision = {"kind": "final", "answer": "stub: nothing further"}
        else:
            decision = script[step]
        raw_cost = script[step].get("cost") if step < len(script) else None
        cost = self._reported_cost({"cost": raw_cost}) if raw_cost else None
        return {
            "decision": decision,
            "provider_kind": "stub",
            "model": self._effective_model() or "stub",
            "request_id": False,
            "usage": {"input_tokens": False, "output_tokens": False},
            "usage_status": "unavailable",
            "usage_provenance": "unavailable",
            "cost": cost,
            "cost_currency": cost["currency"] if cost else None,
            "cost_status": "reported" if cost else "unpriced",
            "cost_provenance": "provider" if cost else "unpriced",
        }

    def _describe_call(self, entry):
        """Render a past tool call back into the conversation.

        Both adapters replay history as plain text rather than as the vendor's
        native tool-call/tool-result blocks. That keeps one transcript shape
        working across providers, at the cost of not using their structured
        forms -- worth revisiting if a provider starts handling its own format
        measurably better.
        """
        return f"Calling {entry['tool']} with {json.dumps(entry['args'])}"

    def _anthropic_payload(self, goal, transcript, tool_schemas):
        messages = [{"role": "user", "content": goal}]
        for entry in transcript:
            messages.append(
                {"role": "assistant", "content": self._describe_call(entry)}
            )
            messages.append({"role": "user", "content": f"Result: {entry['result']}"})
        return {
            "model": self._effective_model(),
            "max_tokens": self.max_tokens,
            "system": self.SYSTEM_PROMPT,
            "messages": messages,
            "tools": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["input_schema"],
                }
                for t in tool_schemas
            ],
        }

    def _openai_payload(self, goal, transcript, tool_schemas):
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": goal},
        ]
        for entry in transcript:
            messages.append(
                {"role": "assistant", "content": self._describe_call(entry)}
            )
            messages.append({"role": "user", "content": f"Result: {entry['result']}"})
        return {
            "model": self._effective_model(),
            "max_completion_tokens": self.max_tokens,
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tool_schemas
            ],
        }

    def _decide_result_anthropic(self, goal, transcript, tool_schemas, on_send=None):
        payload = self._anthropic_payload(goal, transcript, tool_schemas)
        api_key = self._api_key()
        if on_send:
            on_send()
        response = requests.post(
            self._endpoint(),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            raise UserError(
                self.env._(
                    "Provider returned %(code)s: %(body)s",
                    code=response.status_code,
                    body=response.text[:500],
                )
            )
        body = response.json()
        for block in body.get("content", []):
            if block.get("type") == "tool_use":
                decision = {
                    "kind": "tool",
                    "tool": block["name"],
                    "args": block.get("input") or {},
                }
                break
        else:
            text = " ".join(
                b.get("text", "")
                for b in body.get("content", [])
                if b.get("type") == "text"
            )
            decision = {"kind": "final", "answer": text.strip() or "(no answer)"}
        usage = body.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        reported_cost = self._reported_cost(body)
        return {
            "decision": decision,
            "provider_kind": "anthropic",
            "model": body.get("model") or self._effective_model(),
            "request_id": response.headers.get("request-id")
            or response.headers.get("x-request-id")
            or body.get("id"),
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "usage_status": "reported"
            if type(input_tokens) is int and type(output_tokens) is int
            else "unavailable",
            "usage_provenance": "provider"
            if type(input_tokens) is int and type(output_tokens) is int
            else "unavailable",
            "cost": reported_cost,
            "cost_currency": reported_cost["currency"] if reported_cost else None,
            "cost_status": "reported" if reported_cost else "unpriced",
            "cost_provenance": "provider" if reported_cost else "unpriced",
        }

    def _decide_result_openai(self, goal, transcript, tool_schemas, on_send=None):
        payload = self._openai_payload(goal, transcript, tool_schemas)
        api_key = self._api_key()
        if on_send:
            on_send()
        response = requests.post(
            self._endpoint(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            raise UserError(
                self.env._(
                    "Provider returned %(code)s: %(body)s",
                    code=response.status_code,
                    body=response.text[:500],
                )
            )
        body = response.json()
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        decision = False
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            decision = {"kind": "tool", "tool": fn.get("name"), "args": args}
            break
        if not decision:
            decision = {
                "kind": "final",
                "answer": (message.get("content") or "").strip() or "(no answer)",
            }
        usage = body.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        reported_cost = self._reported_cost(body)
        return {
            "decision": decision,
            "provider_kind": "openai",
            "model": body.get("model") or self._effective_model(),
            "request_id": response.headers.get("x-request-id") or body.get("id"),
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "usage_status": "reported"
            if type(input_tokens) is int and type(output_tokens) is int
            else "unavailable",
            "usage_provenance": "provider"
            if type(input_tokens) is int and type(output_tokens) is int
            else "unavailable",
            "cost": reported_cost,
            "cost_currency": reported_cost["currency"] if reported_cost else None,
            "cost_status": "reported" if reported_cost else "unpriced",
            "cost_provenance": "provider" if reported_cost else "unpriced",
        }
