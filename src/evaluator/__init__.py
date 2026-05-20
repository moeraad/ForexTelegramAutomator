"""Style-aware directional-bias evaluator.

The new evaluator (Steps 4-5 of the rewrite) is layered:

  - `features` — pure helpers that gather inputs from the feature store
    and the EA snapshot into one flat dict.
  - `regime`   — pure classifier that labels the macro / vol / trend /
    session regime. The label conditions the rubric.
  - `scorers`  — one pure function per axis family (trend, levels,
    regime, macro, catalyst), each returning an `AxisScore` in [-1, +1]
    with a one-line rationale.
  - `compose`  — combines axis scores via the trading-style's weights,
    applies hard vetoes, and produces a single 0-100 score.

Step 5 adds an LLM synthesizer on top that reads all of the above and
produces multi-horizon probability estimates with a written thesis.
The deterministic layers here are testable in isolation and form the
calibration anchor for the LLM.
"""
