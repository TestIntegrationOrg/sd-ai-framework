# Plugin Step Security v1

This document accompanies the #77 permission SDK review. The detailed contract is `docs/PLUGIN-STEP-SECURITY.md`.

Key invariants:

- `PluginStep` is a strict shared `sdai/v1` extension kind.
- YAML cannot import/register executable code or declare a shell command string.
- framework command execution always uses literal argv and `shell=False`.
- commands resolve only through administrator-controlled `SDAI_PLUGIN_TRUSTED_COMMAND_PATH`; ambient/workspace `PATH` is never used.
- custom publishers require explicit trust and executors must already be registered by trusted installed code.
- org → repo → user allowlists only narrow while denies union.
- protected paths are compared case-insensitively and include SDAI/spec/Git/CI namespaces plus every supported CODEOWNERS location.
- framework file services reject symlink path components and resolved aliases into protected paths.
- network permission fails closed in v1 until an enforceable cross-platform boundary exists.
- plugin inputs/results reject non-finite JSON numbers and runtime-template syntax.
- dry-run validates manifest/policy without executor side effects.
- structured pass/fail result and findings contracts remain provider-neutral.
