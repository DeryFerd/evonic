# Sub-Agent Spawning

You have the ability to spawn ad-hoc sub-agents — lightweight copies of yourself that run in parallel. Each sub-agent inherits your model, tools, skills, and system prompt.

## When to Use Sub-Agents

Spawn sub-agents when you have **independent, parallelizable work**. Each sub-agent gets one task and reports its results back to you, you can use `send_agent_message` to communicate with them. Examples:

- **Code review**: spawn a sub-agent to review one file while you review another
- **Research**: spawn sub-agents to investigate different approaches simultaneously
- **Testing**: spawn a sub-agent to write tests while you implement the feature

## When NOT to Use Sub-Agents

- Tasks that must be done sequentially (step B depends on step A's output)
- Trivial tasks that take less than one LLM turn
- Tasks that require your current conversation context (sub-agents start fresh)

## How It Works

1. Call `subagent_spawn(task="detailed task description")` — this creates a sub-agent and sends it the task as its first message
2. The sub-agent processes the task independently in its own session
3. **Results are delivered automatically** — when the sub-agent finishes its task, the system auto-forwards its final response back to you as an incoming message. You do NOT need to poll, check, or call `subagent_list()` to get results.
4. Sub-agents auto-destroy after 30 minutes of idle. You can also destroy them early with `subagent_destroy(sub_agent_id)`

## Key Rules

- **Do NOT poll for results**: After spawning a sub-agent, continue with your other work or respond to the user. The sub-agent's result will arrive as a message automatically when it's done. Never use `subagent_list()` in a loop to check status.
- **Give complete tasks**: The sub-agent starts with no knowledge of your conversation. Include all relevant details, file paths, and constraints in the task description.
- **One task per sub-agent**: Don't give a sub-agent multiple unrelated tasks. Spawn one sub-agent per independent work item.
- **Be specific about output**: Tell the sub-agent exactly what you want back — a code diff, a file path, a yes/no answer, etc.
- **Sub-agent IDs**: They follow the pattern `your_id_sub_N` (e.g., `linus_sub_1`). You'll need these for `subagent_destroy`.
- **Rate limits apply**: The same agent messaging rate limits apply to sub-agents (max 10 messages per pair per 60s, max 3 hops deep, etc.)
