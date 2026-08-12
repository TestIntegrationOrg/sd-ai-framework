# UTF-8 Portability

SD-AI v0.5.4 defines UTF-8 as an application protocol boundary so behavior does
not depend on Windows code pages, Linux locales, containers, or terminal settings.

## Provider processes

All command-line AI providers inherit the shared CliProvider boundary:

- prompts sent on standard input are encoded as strict UTF-8 bytes;
- standard output and standard error are decoded as strict UTF-8;
- a leading UTF-8 BOM on standard output is removed before normal output parsing;
- invalid output bytes fail the invocation instead of being replaced or ignored;
- diagnostics identify the provider, stream, byte offset, and escaped invalid byte
  sequence without exposing surrounding provider output.

This applies to Codex, GitHub Copilot, Claude Code, Gemini CLI, and custom command
providers. Provider-specific environment or terminal encoding settings are not
required.

## Repository text

SD-AI accepts repository text encoded as UTF-8, with or without a leading UTF-8
BOM. Invalid UTF-8, including Windows-1252-only bytes, produces a file-specific
diagnostic and must be converted rather than guessed.

Framework-generated text is written as UTF-8 without a BOM and uses LF line
endings on Windows and Linux.

## CI coverage

The GitHub Actions test matrix runs on Ubuntu and Windows with Python 3.11 and
3.12. Unicode regression tests cover café, 東京, emoji, punctuation, BOM
handling, invalid-byte diagnostics, and portable artifact writes.
