# 08 — Open Questions

Every `UNCLEAR` accumulated during the audit, organized by topic. The owner will answer.

## Channel and signal coverage

1. **Does the source Telegram channel ever post signals as images / OCR'd photos rather than text?** If yes, the listener's `msg.message or ""` (`src/listener.py:537`) drops them silently. Profile JSON shows only text examples.
2. **Are channel-edit events relevant?** Telethon's `events.NewMessage` does not fire on edits. If the channel publishes "SL 4686" and corrects to "SL 4866" via Telegram edit, the corrected signal is never seen. Has this been observed in practice?
3. **Are there other channels you intend to run (beyond Forex Engineer + SMC)?** `services/install_services.bat` codes for exactly those two. The v2 multi-channel work suggests yes, but the deferred Step 12 says "no central multi-channel API yet".
4. **For multi-instrument signals in a single message (e.g. GOLD + BTC together), what is the desired outcome?** Code does not deterministically handle this — depends on the LLM emitting the symbol-mismatch ALERT for the BTC piece.

## EA / broker integration

5. **What's the current code-signing state of `dist/CopyTrades.exe`?** `scripts/sign_exe.bat` exists but the cert is not in the repo (correctly) and signature verification on the installer is not enforced in any code path I can see.
6. **Is `ea/compile_ea.bat` known to work on the operator's machine?** It depends on `metaeditor64.exe` at a path not parameterized in the file. The build artifact `ea/CopyTrades.compile.log` exists, so it has worked, but the path-resolution mechanism is unclear.
7. **What broker is in production use (FBS, Exness, IC Markets, OANDA, etc.)?** Affects: hedging mode behavior, partial-close semantics, commission per side (`CommissionMultiplier=2.0` default), broker's tolerance for the EA's WebRequest cadence, weekend close handling.
8. **What is `MarketClosedHoliday` handling?** No explicit market-hours guard in the EA was found. Broker rejects become `failed` actions — is that the desired UX or should there be a pre-OrderSend market-hours check?
9. **Has `MaxLotsPerSignal=100.0` (the default cap) ever bound in practice?** The hint at the input declaration says it's intentionally wide; what was the rationale for not setting a tighter cap?

## State / data

10. **Does the operator run a backup of `<APPDATA>/CopyTrades/<stack>/copytrades.db`?** The Backup tab exists in the GUI but cadence isn't enforced in code. What is the actual backup hygiene today?
11. **What happens if `copytrades.db` is moved to a new Windows machine?** DPAPI ciphertext is machine-bound; secrets become unreadable. Has this migration ever been tested?
12. **For the cleanup of orphan messages**: `_cleanup_message_if_orphan` is now a no-op (correct fix). Has anyone been running a manual prune since? Code suggests "~70k rows/year is fine for SQLite" — confirmed?
13. **What is the timezone the EA's `TimeCurrent()` returns?** Broker server time, not UTC. Used in `ReconcileClosedPositions` 48h scan. Does any broker DST/offset edge case bite here?

## AI pipeline / cost

14. **What is the actual daily LLM spend in production?** The `cost_daily_budget_usd=5.00` default seems conservative; what's the real running cost? Does it ever trip the cost_guard kill switch in normal operation?
15. **Are prompt-injection attacks an observed threat from the source channel?** No regression tests for the UNTRUSTED INPUT POLICY defense. Has the operator seen this in real traffic?
16. **What's the rationale for `evaluator_version="v2"` as the new default?** Code comments say "flip back to v1 if v2 produces obviously bad scores during the rollout window". Did rollout complete cleanly?
17. **Does `EnableScoreTiedSizing` get turned ON in production?** Default is OFF. If the evaluator's score has been validated against realized R-multiples (the `scripts/score_calibration.py` exists), at what point does sizing-by-score go live?
18. **What is the trigger-matcher embedding cache invalidation cost?** Cache lives in process memory; profile mtime check fires per message. With 1000 triggers and 1500 ms embeddings on first cold start — has cold-start latency been measured?

## v2 multi-channel scaffolding

19. **What's the status of `docs/plans/2026-05-23-multi-channel-routing.md`?** I see Steps 1–7, 11, 14, 15, 18, 20, 21 referenced as wired in code. Step 12 explicitly deferred. Where does the plan sit overall?
20. **Are there any production stacks using v2 routing today, or is it still single-stack-per-channel in prod?**
21. **The `bot_outbox` table — is the v2 tailer path the live notification path, or is everything still on the legacy `notification_dispatcher` polling?**

## GUI / installer / deployment

22. **Is the GUI installer being shipped to anyone, or is it internal-only at this point?** The Inno Setup file, code signing script, and PyInstaller spec exist but the audience is unclear.
23. **What state does the v1→v2 migration leave operators in?** `src/migrations/config_v1_to_v2.py` exists; is it idempotent across re-runs? Is rollback safe?
24. **Which test failures are tolerated?** I see 78 test files in `tests/` and CLAUDE.md mentions "166 total in the hermetic suite". What's the current pass rate? Are there flaky tests being kept in `tests/test_management_replay.py` because they depend on live AI?
25. **The "stack switcher" header offers multi-stack management — but `REVIEW.md` says MainWindow has single-active-stack assumptions baked in.** Is this still the current behavior?

## Operations / SLO

26. **What is the realistic uptime target / hours of operation?** Forex/gold trades 24/5; do you run continuously, or only during specific sessions (London/NY)?
27. **Has the Telegram session ever been revoked in production?** What was the recovery path?
28. **Is there any sort of operator on-call rotation, or is this strictly one-person?**

## Productization-direction questions (the strategy conversation hinges on these)

29. **Is the target subscriber base** trading-curious retail (low-deposit, expects "auto-trade my whole account"), professional retail (high deposit, expects fine-grained risk controls), or institutional (KYC, segregated accounts)? Each implies a very different product.
30. **Will subscribers bring their own MT5 broker account**, or will the service white-label a broker relationship (introducing broker / B-book) and OPEN trades through a central MetaApi?
31. **Is the channel relationship licensed?** Are you the channel operator yourself, or is Forex Engineer a third-party whose IP would need a partnership / revenue share?
32. **Geography**: which jurisdictions do you want to operate in first? This shapes the compliance gap-fill priority (FCA UK, ASIC AU, CySEC EU, FINRA US all very different) for copy-trading specifically.
33. **Pricing model intuition**: flat sub, per-trade, P&L share, or hybrid? The metering work depends heavily on this choice.
34. **Operational team size in the next 12 months**: solo founder, 2–3 person team, or funded with engineering hires? Determines whether "self-hosted Windows box" continues or whether a Linux server / cloud migration is on the roadmap.
35. **Does the existing IP (interpreter prompt + EA staged management) carry value to a third-party engineering team** building a fresh codebase, or is the goal to commercialize **this** codebase incrementally? The two answers imply very different paths.
