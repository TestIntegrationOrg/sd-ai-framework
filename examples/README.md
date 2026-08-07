# Example

After installing the CLI from the repository root:

```bash
mkdir /tmp/sdai-demo && cd /tmp/sdai-demo
sdai init
sdai feature SCRIPT-123 \
  --title "KMS-backed script signing" \
  --description "Sign PowerShell artifacts with a non-exportable key"
sdai run SCRIPT-123 --workflow standard
sdai validate SCRIPT-123 --workflow standard
```

Open `specs/SCRIPT-123/architecture/decision-matrix.md` and replace sample scores with evidence before approving the ADR.
