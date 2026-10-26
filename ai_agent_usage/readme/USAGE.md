Install this addon with `ai_agent_runtime` and `ai_agent_llm`. Usage recording starts automatically; budget enforcement is off by default.

## Configure budgets

1. In **Settings → Companies → AI Usage**, enable budget enforcement, set the monthly company cap, and choose the company's budget timezone. The timezone controls calendar-month boundaries and defaults to UTC.
2. In **AI → AI Usage → Provider Rate Cards**, create a rate for each provider/model pair. Enter input and output prices per million tokens, the rate currency, and a positive per-request ceiling. Rate cards are immutable; create a new row to publish a new version.
3. In the agent's **Usage budgets** section, add an optional monthly cap and per-run cap for each billing company. The per-run cap applies to the whole run, including runs that cross a month boundary.
4. Select one billing company on runs that span multiple execution companies. Runs with one execution company use it automatically.

## Review usage

Use **AI → AI Usage** for the list and pivot views. Rows show provider/model snapshots, token usage and provenance, rate-card version, currency conversion, charged cost, and any held reservation. Access follows the run's company and agent/reviewer scope.

Provider-reported usage takes precedence. Missing usage is estimated with the available tokenizer or provider count-token endpoint. Costs from rate cards are labeled as estimates; reported provider cost keeps its reported currency and the company-currency conversion snapshot.

An uncertain or unpriced budgeted attempt retains its reservation and pauses its run. An administrator can enter an audited adjustment or release with a note, then explicitly resume the run. Budget edits and hold resolution do not resume runs automatically. A cost over the request ceiling is recorded as an overage; a tool decision already returned still passes through the normal dispatch and approval gates before the next provider request is paused.

Calls made before tracking started are not imported. The company **AI usage tracking started** timestamp marks when metering began.
