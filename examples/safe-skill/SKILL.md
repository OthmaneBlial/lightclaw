---
name: Release Evidence Checklist
description: Review a release plan for concrete local evidence without changing files or using network access.
---

# Release Evidence Checklist

When the user asks to review a release plan:

1. Separate implementation, local tests, CI, publication, and live verification.
2. Mark unavailable evidence as unavailable; never infer that a deploy or release succeeded.
3. Return a short checklist with the exact evidence still needed.

Do not request credentials, run commands, access the network, or write files.
