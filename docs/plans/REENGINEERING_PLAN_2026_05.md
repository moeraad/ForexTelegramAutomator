# Wizard + Classifier + Action Schema Re-Engineering Plan

Started: 2026-05-17

## Goal

Re-engineer the message-classification pipeline (wizard offline, live runtime) so it works for any current and future channel without channel-specific code. All channel-specific knowledge lives in `profile.json`; all code paths are language- and channel-agnostic.

## Steps

| # | Step | Status |
|---|---|---|
| 0 | Baseline (uncommitted in main) | shipped (not committed) |
| 1 | Schema patches: action types + Classification dataclass | TODO |
| 2 | Classifier prompt rewrite + universal English few-shots | TODO |
| 3 | Multi-label aggregator (wizard) | TODO |
| 4 | Confidence gating | TODO |
| 5 | Stage 0 universal pre-filter + profile schema additions | TODO |
| 6 | Interpreter prompt tightening | TODO |
| 7 | EA verification + `pending_type` plumbing | TODO |
| 8 | Review UI polish | TODO |

Execution order: 0 → 1 → 4 → 5 → 2 → 3 → 8 → 6 → 7.

## Design principle

**Universal code, channel-specific data.** Anything that varies between channels lives in `profile.json`. Code in `src/ai*.py`, `src/orchestrator.py`, `src/gui/services/profile_wizard.py` stays language- and channel-agnostic.

## Non-goals

- Multi-instrument live trading (EA hardcodes XAUUSD)
- BuyStop/SellStop pending orders (plumbed but unsupported until needed)
- MODIFY_PENDING action
- Conditional pendings
- Continuous profile evolution
- Removing `ai_discovery.py`

See conversation transcript for full rationale and detailed sub-steps.
