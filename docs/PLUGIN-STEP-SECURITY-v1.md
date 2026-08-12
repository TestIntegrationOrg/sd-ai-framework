# Plugin Step Security v1

This document accompanies the #77 permission SDK review. The authoritative detailed contract is `docs/PLUGIN-STEP-SECURITY.md` when present on the branch.

Key invariants:

- no arbitrary module/callable loading from YAML
- no shell command-string primitive
- `shell=False` for framework argv execution
- explicit trusted publisher + registered executor ID
- org→repo→user allowlists only narrow; denies union
- protected SDAI/spec/workflow/CI paths are not writable through framework services
- network permission fails closed in v1 until SDAI has an enforceable cross-platform boundary
- dry-run validates manifest/policy without executing code
- structured pass/fail result and findings contract
