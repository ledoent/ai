import json
import logging
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
        return self.base_url or ENDPOINTS.get(self.kind)

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
        return getattr(self, f"_decide_{self.kind}")(goal, transcript, tool_schemas)

    def _decide_stub(self, goal, transcript, tool_schemas):
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
            return {"kind": "final", "answer": "stub: nothing further"}
        return script[step]

    def _describe_call(self, entry):
        """Render a past tool call back into the conversation.

        Both adapters replay history as plain text rather than as the vendor's
        native tool-call/tool-result blocks. That keeps one transcript shape
        working across providers, at the cost of not using their structured
        forms -- worth revisiting if a provider starts handling its own format
        measurably better.
        """
        return f"Calling {entry['tool']} with {json.dumps(entry['args'])}"

    def _decide_anthropic(self, goal, transcript, tool_schemas):
        messages = [{"role": "user", "content": goal}]
        for entry in transcript:
            messages.append(
                {"role": "assistant", "content": self._describe_call(entry)}
            )
            messages.append({"role": "user", "content": f"Result: {entry['result']}"})
        payload = {
            "model": self.model,
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
        response = requests.post(
            self._endpoint(),
            headers={
                "x-api-key": self._api_key(),
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
                return {
                    "kind": "tool",
                    "tool": block["name"],
                    "args": block.get("input") or {},
                }
        text = " ".join(
            b.get("text", "")
            for b in body.get("content", [])
            if b.get("type") == "text"
        )
        return {"kind": "final", "answer": text.strip() or "(no answer)"}

    def _decide_openai(self, goal, transcript, tool_schemas):
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": goal},
        ]
        for entry in transcript:
            messages.append(
                {"role": "assistant", "content": self._describe_call(entry)}
            )
            messages.append({"role": "user", "content": f"Result: {entry['result']}"})
        payload = {
            "model": self.model,
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
        response = requests.post(
            self._endpoint(),
            headers={
                "Authorization": f"Bearer {self._api_key()}",
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
        choice = (response.json().get("choices") or [{}])[0]
        message = choice.get("message") or {}
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            return {"kind": "tool", "tool": fn.get("name"), "args": args}
        return {
            "kind": "final",
            "answer": (message.get("content") or "").strip() or "(no answer)",
        }
