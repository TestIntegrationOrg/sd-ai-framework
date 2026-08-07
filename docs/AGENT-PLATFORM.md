# SD-AI Agent Platform

SD-AI v0.2 adds a provider-neutral agent platform for routing lifecycle work to different AI assistants.

The platform supports provider adapters, agent profiles, reusable skills, prompt templates, and capability-based routing. Workflows reference capabilities such as architecture, coding, testing, review, and security instead of embedding vendor-specific commands.

Supported profile examples include Codex, GitHub Copilot, Claude, Gemini, and local/custom command-line agents. Providers remain optional and replaceable.

Skills are reusable version-controlled instruction packages. Prompts live under `.sdai/prompts/` and are rendered before an agent invocation. Agent profiles can combine a provider, capabilities, skills, and a prompt template.

The framework keeps specifications and approved architecture artifacts as the source of truth regardless of which agent performs the work.
