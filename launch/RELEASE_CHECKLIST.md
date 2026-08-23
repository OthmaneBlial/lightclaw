# Release and Public Launch Checklist

Unchecked boxes are deliberate gates, not missing marketing polish.

## Any release

- [ ] Version, tag, changelog, and upgrade/rollback notes agree.
- [ ] Required CI, CodeQL, dependency review/audit, and showcase replay are green.
- [ ] Open critical/high security findings are zero.
- [ ] Wheel and source distribution install in a clean supported environment.
- [ ] Release assets include the machine-readable runtime footprint.
- [ ] Known limitations and failed/paused milestones appear in release notes.
- [ ] `showcase/featured.json` names one consented external workflow, or release notes state
      explicitly why the community feature gate is not yet satisfied.

## `v0.1.0`

- [ ] Deterministic demo completes from the five-minute quickstart.
- [ ] Three curated recipes replay without credentials or network.
- [ ] Packaging, install, doctor, undo, and uninstall documentation is current.
- [ ] A real private-alpha aggregate covers 10–20 self-hosters.

## `v0.2.0`

- [ ] Private Run Receipt export and redaction are manually verified.
- [ ] Telegram approval cards and high-risk confirmation are manually verified.
- [ ] Durable restart/resume/cancel and user-work preservation are verified.
- [ ] Patch acceptance/selective apply/undo are verified.
- [ ] A real Telegram screen recording is captioned, sanitized, and reproducible.

## Public launch

- [ ] GitHub Release is published and package/container links are live.
- [ ] Technical article uses raw benchmark links and current limitations.
- [ ] Show HN and community posts follow channel rules and are posted once.
- [ ] Telegram demo post clearly labels fixture versus live evidence.
- [ ] Discussions update thread exists for questions, fixes, and follow-up evidence.

