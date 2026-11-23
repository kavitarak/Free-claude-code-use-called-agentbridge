# AGENTS.md

AgentBridge is a small, dependency-free Python gateway: Anthropic `/v1/messages` and OpenAI `/v1/chat/completions` in, OpenAI-compatible providers out.

- Code is in `agentbridge/` (`config`, `translate`, `stream`, `upstream`, `server`, `cli`). Tests are in `tests/`.
- Run `python -m unittest discover -s tests -t .` before committing.
- Standard library only: do not add dependencies.
- Never log bodies, headers or keys; pass error text through `Upstream.scrub()`.
- Never hardcode API keys or personal data. Provider model ids in docs are placeholders (`<model-id>`).
- Keep the MIT license and write original code: do not copy code from AGPL or other copyleft projects.
