# Telegram Approval Contract

LightClaw treats a Telegram request and an execution approval as separate events.

## Plan review

Before a multi-agent run, the bot shows:

- declared changed paths, acceptance commands, and each command's working directory, labeled by worker;
- the capability profile and computed risk level;
- a bounded duration range based on lane count;
- an explicit notice when cost is unavailable from the local CLI before execution;
- controls for approval, editing, denial, and cancellation.

Approval review shows up to six commands. Plans with more than six command checks cannot be approved until the plan is edited to expose every command.

If scope is missing, the review says so rather than inventing paths. Editing a scope regenerates the plan and requires a new approval.

A new planning request or edit immediately invalidates the older review in that
chat. Late planner results and errors are discarded after a newer request,
`/agent multi cancel`, `/clear`, or a global memory wipe. Canceling planning does
not interrupt a model call already in progress; its result cannot create a new approval.

`command_succeeds` checks run as host subprocesses after approval. They receive a minimal environment and an in-workspace working directory, but LightClaw does not add an operating-system sandbox around them. Review each command; use OS/container isolation for untrusted repositories.

Worker handoff JSON must be a regular file no larger than 1 MiB. Oversized or symlinked handoffs fail their acceptance checks.

Control and directional formatting characters in Telegram review text are shown as replacement marks to reduce filename and command spoofing; attached patch bytes remain unchanged.

## High-risk confirmation

Destructive language and commands, privileged or network commands, publishing/deployment, credential changes, or external-system scope require two ordered confirmations. The shared execution gate enforces the second confirmation for button, text, and slash-command entry points. A forged second-confirmation callback is rejected unless the first approval was recorded. This keyword check is heuristic; it does not sandbox commands. Trusted host execution retains its separate confirmation gate, bound to the requesting Telegram user. A confirmation attempt from another group member leaves the unexpired request pending and executes nothing.

The plan preview shows worker responsibilities, expected outputs, owned paths, and acceptance commands. The risk scan checks the goal, worker roles, responsibilities, expected inputs and outputs, owned paths, and commands. These text checks are heuristic; review the full plan before approving.

### Trusted host runs

`/agent trusted <agent> <task>` shows the agent, complete task, and sandbox-removal
warning before execution. Long reviews span several messages; approval buttons
appear only on the final part. Review every part. Control and directional characters
are replaced with visible marks in both the displayed task and the task sent to the agent.

Approve host run and Discard are bound to that specific request and its requester.
An older review cannot approve a replacement. Failed or interrupted review delivery
invalidates that request. A confirmation arriving before delivery completes is rejected.
Approval consumes the request before execution, so repeated confirmations cannot start
another run.

For terminal use, copy `/agent trusted confirm <review-id>` or
`/agent trusted discard <review-id>` from the review. The former bare
`/agent trusted confirm` command now returns guidance and executes nothing; it cannot
identify which request was reviewed. Requests still expire after 90 seconds.

## Voice goals

Voice transcription is limited to 20 MB and six requests per Telegram user per minute. Oversized declared files are rejected before download; any oversized downloaded data is discarded. Captions appear beside the transcription before approval, and both are part of the approved request. A request too long for one Telegram review message is rejected without creating a pending approval. A new valid voice request invalidates an older pending approval, and late results from older requests are discarded. Accepted input is displayed as “not executed” and retained in memory only as a short-lived pending action. Pending plans, voice approvals, and trusted-run confirmations carry wall-clock and monotonic deadlines; whichever is reached first expires them. Monotonic time prevents clock rollback from extending them, while wall time makes sleep consume their lifetime. Nothing enters the normal agent loop until the user taps Use transcription. Discard and expiry execute nothing.

## Result controls

Completed runs show View diff, Retry failed lane when applicable, Accept result, and Cancel. Retry still obeys durable idempotency and attempt bounds; unsafe retries fail closed. Accept changes only the local durable disposition and never pushes or publishes.

Responses larger than 6,000 characters or detected as large code dumps are written to an owner-only Markdown artifact and attached to Telegram. If attachment fails, the artifact stays local and LightClaw does not split the response into chat walls.

## Chat file edits

Model-generated chat file blocks read and write at most 2 MiB per file, keeping diffs and retry context bounded. Larger changes should use a delegated local-agent task or a local edit; an oversized target is left unchanged.
