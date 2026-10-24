from odoo.tests import tagged

from odoo.addons.ai_agent_runtime.tests.common import AgentRuntimeCommon


@tagged("post_install", "-at_install")
class TestGoalLoop(AgentRuntimeCommon):
    """The loop, driven by a scripted provider so every assertion is exact.

    A stub rather than a real model on purpose: what needs proving here is the
    control flow and that the gates still hold when a *model* is choosing the
    tools rather than a human writing a plan. Whether the model chooses well is
    a different question, needs a real provider, and cannot be asserted.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["ai.agent.provider"].create(
            {"name": "Stub", "kind": "stub"}
        )
        cls.agent.provider_id = cls.provider

    def goal_run(self, goal, script, **vals):
        run = self.Run.create(
            dict({"agent_id": self.agent.id, "goal": goal, "plan": "[]"}, **vals)
        )
        run.action_start()
        return run.with_context(stub_script=script)

    # -- the happy path ------------------------------------------------------

    def test_loop_calls_a_tool_then_answers(self):
        run = self.goal_run(
            "How many contacts?",
            [
                {"kind": "tool", "tool": "count_partners", "args": {"domain": []}},
                {"kind": "final", "answer": "There are some contacts."},
            ],
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "done")
        self.assertEqual(run.answer, "There are some contacts.")
        self.assertEqual(len(run.step_ids), 1)
        self.assertEqual(run.step_ids.tool_id, self.tool_count)

    def test_loop_sees_results_and_can_chain(self):
        """The property a static plan cannot have: the second call is chosen
        after the first result exists."""
        run = self.goal_run(
            "Count twice.",
            [
                {"kind": "tool", "tool": "count_partners", "args": {"domain": []}},
                {"kind": "tool", "tool": "count_partners", "args": {"domain": []}},
                {"kind": "final", "answer": "done"},
            ],
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "done")
        self.assertEqual(run.step_ids.mapped("sequence"), [0, 1])

    def test_transcript_reaches_the_provider(self):
        run = self.goal_run("x", [{"kind": "final", "answer": "a"}])
        self.assertEqual(run._transcript(), [])
        run.action_run_to_completion()
        # one step executed -> transcript of length 1 on the next decision
        run2 = self.goal_run(
            "y",
            [
                {"kind": "tool", "tool": "count_partners", "args": {"domain": []}},
                {"kind": "final", "answer": "b"},
            ],
        )
        run2.action_run_to_completion()
        transcript = run2._transcript()
        self.assertEqual(len(transcript), 1)
        self.assertEqual(transcript[0]["tool"], "count_partners")
        self.assertTrue(transcript[0]["result"])

    # -- the gates still hold when a model is choosing -----------------------

    def test_model_cannot_choose_an_ungranted_tool(self):
        """The allowlist is not advisory just because a model picked it."""
        run = self.goal_run(
            "Count users.",
            [{"kind": "tool", "tool": "count_users", "args": {"domain": []}}],
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "failed")
        self.assertIn("not allowed", run.error)
        self.assertEqual(run.call_ids.state, "refused")

    def test_model_cannot_invent_a_tool_name(self):
        run = self.goal_run(
            "Do magic.",
            [{"kind": "tool", "tool": "drop_everything", "args": {}}],
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.call_ids.state, "refused")

    def test_model_choice_still_runs_as_the_agents_user(self):
        """Identity is unchanged by who picked the tool."""
        run = self.goal_run(
            "Count mail servers.",
            [
                {
                    "kind": "tool",
                    "tool": "count_mail_servers",
                    "args": {"domain": []},
                },
                {"kind": "final", "answer": "could not"},
            ],
        )
        run.action_run_to_completion()
        self.assertTrue(run.step_ids[0].failed)
        # ...and the failure is visible to the model rather than fatal, so it
        # can recover -- which is why the run still finishes.
        self.assertEqual(run.state, "done")

    def test_gated_tool_still_pauses_when_a_model_chose_it(self):
        before = self.env["res.partner"].search_count([])
        run = self.goal_run(
            "Create a contact called Loop.",
            [
                {
                    "kind": "tool",
                    "tool": "create_partner",
                    "args": {"vals_list": [{"name": "Loop"}]},
                }
            ],
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "awaiting_approval")
        self.assertEqual(run.pending_tool_id, self.tool_create)
        self.assertEqual(self.env["res.partner"].search_count([]), before)

    def test_approval_executes_what_the_human_saw_not_a_new_choice(self):
        """After approval the pending call is executed verbatim.

        If the loop asked the provider again, the model could return a
        *different* tool or different arguments, and the approval would have
        authorised something the human never saw. That is the subtle failure an
        approval gate in a model-driven loop has to avoid.
        """
        run = self.goal_run(
            "Create a contact called Verbatim.",
            [
                {
                    "kind": "tool",
                    "tool": "create_partner",
                    "args": {"vals_list": [{"name": "Verbatim"}]},
                },
                # If this were consulted after approval, a Trojan would be
                # created instead.
                {
                    "kind": "tool",
                    "tool": "create_partner",
                    "args": {"vals_list": [{"name": "Trojan"}]},
                },
            ],
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "awaiting_approval")
        run.action_approve()
        run.with_context(stub_script=[])._advance()
        self.assertEqual(
            self.env["res.partner"].search_count([("name", "=", "Verbatim")]), 1
        )
        self.assertEqual(
            self.env["res.partner"].search_count([("name", "=", "Trojan")]), 0
        )

    # -- the budget ----------------------------------------------------------

    def test_step_budget_stops_a_runaway_loop(self):
        self.agent.max_steps = 3
        run = self.goal_run(
            "Loop forever.",
            [{"kind": "tool", "tool": "count_partners", "args": {"domain": []}}] * 20,
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "failed")
        self.assertIn("budget", run.error)
        self.assertEqual(len(run.step_ids), 3)

    def test_a_plan_run_is_unaffected_by_the_provider(self):
        """Backward compatibility: no goal means the static path, untouched."""
        run = self.make_run(("count_partners", {"domain": []}))
        run.action_run_to_completion()
        self.assertEqual(run.state, "done")
        self.assertFalse(run.answer)


@tagged("post_install", "-at_install")
class TestDerivedSchemas(AgentRuntimeCommon):
    """What signature derivation does and does not give you -- E2's answer."""

    def test_schema_is_derived_from_the_real_signature(self):
        schema = self.tool_count._tool_schema()
        self.assertEqual(schema["name"], "count_partners")
        props = schema["input_schema"]["properties"]
        # res.partner.search_count(domain, limit=None)
        self.assertIn("domain", props)
        self.assertIn("domain", schema["input_schema"]["required"])
        self.assertNotIn("limit", schema["input_schema"]["required"])
        self.assertNotIn("self", props)

    def test_description_is_the_hand_written_one(self):
        """The finding: derivation cannot supply *why* to use a tool.

        Odoo's docstrings explain mechanics to a developer who has already
        decided to call the method. So the schema comes free and the
        description does not -- which is precisely the curation ai_tool adds,
        and why "derive or hand-write" is a false choice.
        """
        schema = self.tool_count._tool_schema()
        self.assertEqual(schema["description"], self.tool_count.description)

    def test_schema_carries_the_risk_flags(self):
        schema = self.tool_create._tool_schema()
        self.assertTrue(schema["_requires_approval"])
        self.assertEqual(schema["_classification"], "writing")

    def test_agent_only_offers_its_allowlisted_tools(self):
        names = {s["name"] for s in self.agent._tool_schemas()}
        self.assertIn("count_partners", names)
        self.assertNotIn("count_users", names)

    def test_varargs_are_not_emitted_as_parameters(self):
        """*args/**kwargs cannot be expressed in JSON Schema properties."""
        tool = self.Tool.create(
            {
                "name": "name_search_tool",
                "description": "Find records by name.",
                "model_name": "res.partner",
                "method_name": "name_search",
            }
        )
        props = tool._tool_schema()["input_schema"]["properties"]
        self.assertNotIn("args", props)
        self.assertNotIn("kwargs", props)


@tagged("post_install", "-at_install")
class TestRecoverableToolFailure(AgentRuntimeCommon):
    """A bad argument *value* is feedback, not a crash.

    Regression test for a defect a live model found within two tasks: it used
    ``('type', '=', 'out_invoice')`` -- ``move_type``'s pre-13.0 name -- and the
    resulting ``ValueError`` escaped the run entirely. A model inventing a field
    name is going to be the most common failure in the system, so it has to
    reach the transcript where the model can correct it.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent.provider_id = cls.env["ai.agent.provider"].create(
            {"name": "Stub", "kind": "stub"}
        )

    def _run(self, script):
        run = self.Run.create(
            {"agent_id": self.agent.id, "goal": "count things", "plan": "[]"}
        )
        run.action_start()
        return run.with_context(stub_script=script)

    def test_invalid_field_in_domain_is_recorded_not_raised(self):
        run = self._run(
            [
                # 'nonexistent_field' makes the ORM raise ValueError, which is
                # neither AccessError nor UserError.
                {
                    "kind": "tool",
                    "tool": "count_partners",
                    "args": {"domain": [["nonexistent_field", "=", 1]]},
                },
                {"kind": "final", "answer": "recovered"},
            ]
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "done")
        self.assertEqual(run.answer, "recovered")
        self.assertEqual(len(run.step_ids), 1)
        self.assertTrue(run.step_ids.failed)
        self.assertIn("nonexistent_field", run.step_ids.result)

    def test_the_failure_reaches_the_transcript(self):
        """So a model can actually see what it did wrong."""
        run = self._run(
            [
                {
                    "kind": "tool",
                    "tool": "count_partners",
                    "args": {"domain": [["nonexistent_field", "=", 1]]},
                },
                {"kind": "final", "answer": "x"},
            ]
        )
        run._advance()
        transcript = run._transcript()
        self.assertEqual(len(transcript), 1)
        self.assertIn("ValueError", transcript[0]["result"])

    def test_the_run_can_recover_on_the_next_step(self):
        """The whole point: wrong call, then right call, then an answer."""
        run = self._run(
            [
                {
                    "kind": "tool",
                    "tool": "count_partners",
                    "args": {"domain": [["nonexistent_field", "=", 1]]},
                },
                {"kind": "tool", "tool": "count_partners", "args": {"domain": []}},
                {"kind": "final", "answer": "corrected"},
            ]
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "done")
        self.assertEqual(run.answer, "corrected")
        self.assertEqual(run.step_ids.mapped("failed"), [True, False])

    def test_repeated_failure_is_bounded_by_the_budget(self):
        self.agent.max_steps = 4
        run = self._run(
            [
                {
                    "kind": "tool",
                    "tool": "count_partners",
                    "args": {"domain": [["nonexistent_field", "=", 1]]},
                }
            ]
            * 20
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "failed")
        self.assertIn("budget", run.error)
        self.assertEqual(len(run.step_ids), 4)


@tagged("post_install", "-at_install")
class TestTruncationAnnotation(AgentRuntimeCommon):
    """Telling the model when its view is partial.

    Regression test for the worst failure found in the whole exercise, on real
    production data: asked how many posted invoices were unpaid and what they
    totalled, a model made two *correct* tool calls and answered "1, 7097.74".
    The true answer was 3 and 10597.74 -- the other two belonged to a company
    its user could not see. Odoo was right; our wrapper was the liar.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent.provider_id = cls.env["ai.agent.provider"].create(
            {"name": "Stub", "kind": "stub"}
        )
        # ir.mail_server is base.group_system-only and the agent has no such
        # group, so this record is invisible to it. Reuse the fixture's tool for
        # that model -- ai.agent.tool is UNIQUE(model_name, method_name).
        cls.env["ir.mail_server"].create(
            {"name": "hidden-from-agent", "smtp_host": "example.invalid"}
        )
        cls.hidden_tool = cls.tool_forbidden
        cls.hidden_tool.report_truncation = True
        # _annotate_result needs a real run: it reads agent_id.user_id.
        cls.probe = cls.Run.create(
            {"agent_id": cls.agent.id, "goal": "probe", "plan": "[]"}
        )

    def _restrict_partners(self):
        """Hide one contact from every non-superuser, via a record restriction.

        This is the shape of the production failure: the agent *can* read the
        model, so the call succeeds -- it just silently returns fewer rows. A
        fully-denied model is a different and much safer case, because there the
        call raises and the model is told.
        """
        hidden = self.env["res.partner"].create({"name": "WITHHELD-FROM-AGENT"})
        self.env["ir.access"].create(
            {
                "name": "test: hide one partner",
                "model_id": self.env["ir.model"]._get_id("res.partner"),
                "operation": "r",
                "domain": "[('name', '!=', 'WITHHELD-FROM-AGENT')]",
            }
        )
        self.tool_count.report_truncation = True
        return hidden

    def test_annotation_reports_withheld_records(self):
        """A call that succeeds but under-reports says so."""
        self._restrict_partners()
        Partner = self.env["res.partner"]
        visible = Partner.with_user(self.agent_user).search_count([])
        total = Partner.sudo().search_count([])
        self.assertLess(visible, total)  # precondition: the rule actually bites
        rendered = self.probe._annotate_result(self.tool_count, {"domain": []}, visible)
        self.assertIn(f"{visible} of {total}", rendered)
        self.assertIn(f"{total - visible} are excluded", rendered)
        self.assertIn("excluded by this user's access rights", rendered)
        self.assertIn("partial", rendered)

    def test_no_annotation_when_nothing_is_withheld(self):
        """A tool the user can fully see gets no noise appended."""
        self.tool_count.report_truncation = True
        visible = self.env["res.partner"].with_user(self.agent_user).search_count([])
        rendered = self.probe._annotate_result(self.tool_count, {"domain": []}, visible)
        self.assertEqual(rendered, repr(visible))

    def test_annotation_is_opt_in(self):
        """Off by default, because it costs a second query."""
        self.assertFalse(self.tool_count.report_truncation)
        rendered = self.probe._annotate_result(self.tool_count, {"domain": []}, 7)
        self.assertEqual(rendered, repr(7))

    def test_annotation_reaches_the_transcript(self):
        """End to end: the note has to be where the model will read it."""
        run = self.Run.create(
            {"agent_id": self.agent.id, "goal": "count servers", "plan": "[]"}
        )
        run.action_start()
        run = run.with_context(
            stub_script=[
                {
                    "kind": "tool",
                    "tool": self.hidden_tool.name,
                    "args": {"domain": []},
                },
                {"kind": "final", "answer": "done"},
            ]
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "done")
        step = run.step_ids[0]
        # The call itself was refused (the agent cannot read the model), and
        # that refusal is what reaches the model -- the annotation path is for
        # the subtler case where a call *succeeds* but under-reports.
        self.assertTrue(step.failed)
        self.assertIn("AccessError", step.result)

    def test_annotation_never_breaks_a_call(self):
        """A bad domain in the annotation path must not fail the step."""
        rendered = self.probe._annotate_result(
            self.hidden_tool, {"domain": [["no_such_field", "=", 1]]}, 0
        )
        self.assertEqual(rendered, repr(0))

    def test_annotation_reaches_the_transcript_on_a_truncated_read(self):
        """End to end, on the dangerous case: the note must be where the model reads."""
        self._restrict_partners()
        run = self.Run.create(
            {"agent_id": self.agent.id, "goal": "count contacts", "plan": "[]"}
        )
        run.action_start()
        run = run.with_context(
            stub_script=[
                {
                    "kind": "tool",
                    "tool": self.tool_count.name,
                    "args": {"domain": []},
                },
                {"kind": "final", "answer": "done"},
            ]
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "done")
        self.assertFalse(run.step_ids[0].failed)  # it SUCCEEDED, just partially
        self.assertIn("are excluded", run.step_ids[0].result)
        self.assertIn("are excluded", run._transcript()[0]["result"])


@tagged("post_install", "-at_install")
class TestArgsSnapshot(AgentRuntimeCommon):
    """The audit trail must record what was approved, not what the ORM left.

    Found on real data: res.partner.create mutates the vals dict in place
    (adding properties_base_definition_id), so serialising args after the call
    recorded arguments that differed from the approved ones.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent.provider_id = cls.env["ai.agent.provider"].create(
            {"name": "Stub", "kind": "stub"}
        )

    def test_step_args_match_what_was_approved(self):
        approved = {"vals_list": [{"name": "Snapshot Check"}]}
        run = self.Run.create(
            {"agent_id": self.agent.id, "goal": "create one", "plan": "[]"}
        )
        run.action_start()
        run = run.with_context(
            stub_script=[{"kind": "tool", "tool": "create_partner", "args": approved}]
        )
        run.action_run_to_completion()
        self.assertEqual(run.state, "awaiting_approval")
        pending = run.pending_args
        run.action_approve()
        run.with_context(stub_script=[])._advance()
        self.assertEqual(len(run.step_ids), 1)
        # The recorded args are byte-identical to what the human approved.
        self.assertEqual(run.step_ids.args, pending)
        self.assertNotIn("properties_base_definition_id", run.step_ids.args)
