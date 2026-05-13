"""Bootstrap a new channel profile by sampling messages from a Telegram
channel and classifying each with OpenAI. Produces an editable JSON draft
the operator reviews + tunes before running the (separate) compile step
that turns the draft into a production `channels/<name>.json`.

Auth / session:
    Reads creds from `ajmclen.env` and uses `ajmclen.session` so the
    running listener (which uses `.env` + `forexcopier1987.session`) keeps
    trading without interruption. `ajmclen.env` must contain:
        TG_API_ID, TG_API_HASH, TG_PHONE, OPENAI_API_KEY

Usage:
    python scripts/build_channel_profile.py --name SMC
    python scripts/build_channel_profile.py --name SMC --limit 1000
    python scripts/build_channel_profile.py --name SMC --since 2026-04-01
    python scripts/build_channel_profile.py --name SMC --model gpt-5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "ajmclen.env"
SESSION_FILE = "ajmclen"  # Telethon appends .session
OUT_DIR = ROOT / "channels"

BATCH_SIZE = 15  # messages per OpenAI call

CLASSIFIER_SYSTEM_PROMPT = """You are categorising messages from a forex/trading signal channel to bootstrap a processing dictionary that another LLM will use at runtime.

For each input message, output a structured classification:

- "category": SHORT descriptive snake_case label. Be SPECIFIC so a human reviewer can group/refine later. Good examples:
    "open_buy_with_limit_keyword", "open_buy_directional_no_params",
    "move_sl_explicit_price", "move_sl_shorthand_two_digits",
    "close_partial_50pct", "context_market_commentary",
    "noise_greeting", "noise_emoji_only".
  Bad (too generic): "signal", "open", "context".

- "action": ONE of these labels matching what our trading bot should do:
    OPEN          – full structured signal (entry + SL + TPs + side)
    OPEN_INSTANT  – bare directional command, no prices ("buy", "sell")
    MOVE_SL       – explicit new SL price
    MOVE_SL_BE    – move SL to entry (break-even)
    CLOSE_PARTIAL – close fraction of position (default 50%)
    CLOSE_FULL    – close entire position
    REINFORCE     – close + reopen same direction
    REOPEN_LAST   – re-enter the last closed position
    TIGHTEN_SL    – reduce SL distance
    MODIFY_TPS    – change TP ladder
    ALERT_INFO    – commentary / context / price ping, no trade action
    ALERT_WARN    – ambiguous trade-ish message missing required params
    IGNORE        – pure noise (greetings, emojis only, religious phrases, off-topic chat)
    UNCERTAIN     – cannot confidently classify (mark and move on)

- "side": "BUY" | "SELL" | null (only when the message clearly states a direction)

- "extracted": object with any structured fields you can identify, omitting absent ones:
    {"entry": 4666.03, "sl": 4662.03, "tps": [4690.03]} for a structured signal
    {"price": 4720} for an explicit SL move
    {"fraction": 0.5} for partial close
    Use null / omit when nothing parseable.

- "notes": one short sentence describing the message's intent.

Output a single JSON object (no prose, no markdown fences):
    {"results": [{"i": <index>, "category": "...", "action": "...", "side": "BUY"|"SELL"|null, "extracted": {...}, "notes": "..."}, ...]}

The "i" field must match the index given to each message in the input."""


async def pick_chat(client: TelegramClient) -> tuple[int, str]:
    """List dialogs, prompt the operator to pick one by number."""
    print("\nYour Telegram dialogs:\n")
    dialogs: list[tuple[int, str, str]] = []
    async for d in client.iter_dialogs():
        kind = "channel" if d.is_channel else ("group" if d.is_group else "user")
        dialogs.append((d.id, kind, d.name or "<unnamed>"))
    if not dialogs:
        print("No dialogs found.", file=sys.stderr)
        sys.exit(1)
    for i, (cid, kind, name) in enumerate(dialogs):
        print(f"  [{i:>3}]  {cid:>18}  {kind:<8}  {name}")
    print()
    raw = input("Enter the index of the channel to analyse: ").strip()
    try:
        idx = int(raw)
        cid, _kind, name = dialogs[idx]
    except (ValueError, IndexError):
        print("Invalid selection.", file=sys.stderr)
        sys.exit(1)
    return cid, name


async def fetch_messages(
    client: TelegramClient, chat_id: int,
    limit: int, since: datetime | None,
) -> list[dict[str, Any]]:
    """Pull text messages newest-first, skipping media-only and empty.

    When `since` is supplied we still iterate newest-first (Telethon's
    natural order) but break the loop as soon as we see an older message.
    Then we reverse the result so the dictionary lists messages oldest-first.
    """
    out: list[dict[str, Any]] = []
    async for msg in client.iter_messages(chat_id, limit=limit):
        if since is not None and msg.date and msg.date < since:
            break
        text = (msg.text or msg.message or "").strip()
        if not text:
            continue
        out.append({
            "tg_id": msg.id,
            "date": msg.date.isoformat() if msg.date else None,
            "text": text,
        })
    out.reverse()
    return out


def classify_batch(
    openai_client: Any, model: str, batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One OpenAI call covering a batch of messages."""
    user_lines = [f"{i}: {m['text']}" for i, m in enumerate(batch)]
    user_text = "Classify these messages:\n" + "\n".join(user_lines)
    # gpt-5 family are reasoning models — `max_completion_tokens` covers
    # both reasoning and the visible output. `reasoning_effort="minimal"`
    # keeps the budget on classification, not deliberation.
    resp = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        max_completion_tokens=8192,
        reasoning_effort="minimal",
    )
    raw = (resp.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    s = raw.strip()
    obj_start = s.find("{")
    obj_end = s.rfind("}")
    if obj_start < 0 or obj_end <= obj_start:
        return [
            {"i": i, "category": "uncertain", "action": "UNCERTAIN",
             "side": None, "extracted": {}, "notes": "classifier returned no JSON"}
            for i in range(len(batch))
        ]
    try:
        data = json.loads(s[obj_start:obj_end + 1])
    except json.JSONDecodeError as e:
        return [
            {"i": i, "category": "uncertain", "action": "UNCERTAIN",
             "side": None, "extracted": {}, "notes": f"json decode error: {e}"}
            for i in range(len(batch))
        ]
    return data.get("results", []) or []


def build_clusters(classifications: list[dict[str, Any]]) -> dict[str, Any]:
    """Group per-message classifications by category. Dedupe sample texts so
    a channel that posts identical phrases dozens of times shows each phrase
    once, not dozens of times."""
    raw: dict[str, dict[str, Any]] = {}
    for c in classifications:
        key = c.get("category") or "uncertain"
        if key not in raw:
            raw[key] = {
                "action": c.get("action") or "UNCERTAIN",
                "sides": set(),
                "samples": [],
                "seen_texts": set(),
                "notes_samples": [],
                "sample_count": 0,
            }
        e = raw[key]
        e["sample_count"] += 1
        side = c.get("side")
        if side in ("BUY", "SELL"):
            e["sides"].add(side)
        text = c.get("_text") or ""
        if text and text not in e["seen_texts"] and len(e["samples"]) < 6:
            e["samples"].append(text)
            e["seen_texts"].add(text)
        notes = (c.get("notes") or "").strip()
        if notes and len(e["notes_samples"]) < 3:
            e["notes_samples"].append(notes)
    out: dict[str, Any] = {}
    for k, v in sorted(raw.items(), key=lambda kv: -kv[1]["sample_count"]):
        side_list = sorted(v["sides"])
        side_hint: Any = None
        if len(side_list) == 1:
            side_hint = side_list[0]
        elif len(side_list) > 1:
            side_hint = "/".join(side_list)
        out[k] = {
            "action": v["action"],
            "side": side_hint,
            "sample_count": v["sample_count"],
            "samples": v["samples"],
            "notes_samples": v["notes_samples"],
        }
    return out


def detect_language(messages: list[dict[str, Any]]) -> str:
    """Heuristic: count Arabic glyph chars vs total. >20% Arabic → "ar"."""
    arabic = 0
    total = 0
    for m in messages:
        for ch in m["text"]:
            total += 1
            if "؀" <= ch <= "ۿ":
                arabic += 1
    if total == 0:
        return "unknown"
    return "ar" if arabic / total > 0.2 else "en"


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a channel-profile draft from Telegram history",
    )
    parser.add_argument("--name", required=True,
                        help="Profile name; output -> channels/<name>_draft.json")
    parser.add_argument("--limit", type=int, default=500,
                        help="Max messages to fetch (default 500)")
    parser.add_argument("--since", type=str, default=None,
                        help="ISO date YYYY-MM-DD; only messages on/after this date")
    parser.add_argument("--model", default="gpt-5-mini",
                        help="OpenAI model for classification (default gpt-5-mini)")
    args = parser.parse_args()

    if not ENV_FILE.exists():
        print(
            f"Missing {ENV_FILE.name} — create it at the project root with "
            f"TG_API_ID, TG_API_HASH, TG_PHONE, OPENAI_API_KEY.",
            file=sys.stderr,
        )
        sys.exit(1)
    load_dotenv(dotenv_path=ENV_FILE, override=True)

    since_dt: datetime | None = None
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        except ValueError as e:
            print(f"Bad --since '{args.since}': {e}", file=sys.stderr)
            sys.exit(1)

    api_id = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    phone = os.getenv("TG_PHONE", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    missing = [k for k, v in {
        "TG_API_ID": api_id, "TG_API_HASH": api_hash,
        "TG_PHONE": phone, "OPENAI_API_KEY": openai_key,
    }.items() if not v]
    if missing:
        print(f"Missing in {ENV_FILE.name}: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
    await client.start(phone=phone)
    print(f"Connected via session: {SESSION_FILE}.session")

    chat_id, chat_name = await pick_chat(client)
    print(f"\nFetching up to {args.limit} messages from: {chat_name} ({chat_id})")
    if since_dt:
        print(f"  Filter: messages on/after {since_dt.isoformat()}")

    messages = await fetch_messages(client, chat_id, args.limit, since_dt)
    print(f"Fetched {len(messages)} text messages (media/empty skipped)")
    await client.disconnect()

    if not messages:
        print("Nothing to classify.", file=sys.stderr)
        sys.exit(1)

    from openai import OpenAI
    openai_client = OpenAI(api_key=openai_key)
    print(f"\nClassifying with {args.model} in batches of {BATCH_SIZE} ...")

    raw_classifications: list[dict[str, Any]] = []
    errors = 0
    for i in range(0, len(messages), BATCH_SIZE):
        batch = messages[i:i + BATCH_SIZE]
        try:
            results = classify_batch(openai_client, args.model, batch)
        except Exception as e:
            print(
                f"  batch {i // BATCH_SIZE + 1}: classifier call failed: {e}",
                file=sys.stderr,
            )
            errors += len(batch)
            for m in batch:
                raw_classifications.append({
                    "tg_id": m["tg_id"], "date": m["date"], "text": m["text"],
                    "category": "uncertain", "action": "UNCERTAIN",
                    "side": None, "extracted": {},
                    "notes": f"batch error: {e}",
                })
            continue
        by_idx = {int(r.get("i", -1)): r for r in results if isinstance(r, dict)}
        for j, m in enumerate(batch):
            r = by_idx.get(j) or {}
            raw_classifications.append({
                "tg_id": m["tg_id"],
                "date": m["date"],
                "text": m["text"],
                "category": (r.get("category") or "uncertain").strip(),
                "action": (r.get("action") or "UNCERTAIN").strip(),
                "side": r.get("side"),
                "extracted": r.get("extracted") or {},
                "notes": (r.get("notes") or "").strip(),
            })
        done = min(i + BATCH_SIZE, len(messages))
        print(f"  classified {done}/{len(messages)}")

    # Cluster: pass text through _text so build_clusters can store samples.
    for r in raw_classifications:
        r["_text"] = r["text"]
    clusters = build_clusters(raw_classifications)
    for r in raw_classifications:
        r.pop("_text", None)

    language = detect_language(messages)
    uncertain_count = sum(1 for r in raw_classifications if r["action"] == "UNCERTAIN")

    draft = {
        "name": args.name,
        "tg_chat_id": chat_id,
        "tg_chat_name": chat_name,
        "symbol": "XAUUSD",
        "language": language,
        "messages_analysed": len(messages),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "review_instructions": [
            "Edit `clusters` to refine the dictionary:",
            "  - rename keys to be more specific or merge similar clusters",
            "  - change `action` when the AI guessed wrong",
            "  - move sample phrases between clusters as needed",
            "  - delete clusters whose action should be IGNORE entirely",
            "Use `raw_classifications` to find the source message for any sample",
            "by its `tg_id`. When the dictionary looks right, run the compile",
            "step (TBD) to produce the production channels/<name>.json the AI",
            "prompt loads at runtime.",
        ],
        "clusters": clusters,
        "stats": {
            "total_messages_fetched": len(messages),
            "classified_ok": len(raw_classifications) - errors,
            "classifier_errors": errors,
            "uncertain": uncertain_count,
            "distinct_clusters": len(clusters),
        },
        "raw_classifications": raw_classifications,
    }

    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / f"{args.name}_draft.json"
    out_path.write_text(
        json.dumps(draft, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\nDraft profile written to: {out_path}")
    print(f"  language (heuristic): {language}")
    print(f"  total messages:       {len(messages)}")
    print(f"  classified ok:        {len(raw_classifications) - errors}")
    print(f"  classifier errors:    {errors}")
    print(f"  UNCERTAIN actions:    {uncertain_count}")
    print(f"  distinct clusters:    {len(clusters)}")
    print("\nTop clusters (by sample count):")
    for k, v in list(clusters.items())[:15]:
        side = f" side={v['side']}" if v["side"] else ""
        print(f"  {v['sample_count']:>4}  {k:<42} -> {v['action']:<14}{side}")
    print(
        "\nOpen the JSON in your editor and tune the `clusters` section. "
        "Move samples between clusters, fix action labels, rename categories, "
        "or merge near-duplicates."
    )


if __name__ == "__main__":
    asyncio.run(main())
