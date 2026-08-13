# Isolated Review Compatibility

SDAI keeps compatibility with review contracts created by `build_review_contract(...)`, which predate the complete workspace snapshot field. Those contracts continue to validate their bound file context.

Review contracts created by `prepare_independent_review_contract(...)` include a bounded `workspace.diff` snapshot covering tracked changes and untracked worker-created files. The snapshot is checked again immediately before review execution.
