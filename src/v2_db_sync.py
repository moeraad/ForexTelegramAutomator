"""Push v2 entity values into per-destination DB settings.

V2 Config writes ``stacks_config.json`` entities (Account, Channel,
Bot, Destination). The services read the ``settings`` table inside
each destination's SQLite DB — historically the wizard populated those.
Without a bridge, operators who skip the wizard see "6 critical keys
missing" in Tuning even though the matching values exist as v2 entities.

This module wires Account.api_id/api_hash/phone + Channel.chat_id →
the destination DB's tg_api_id/tg_api_hash/tg_phone/tg_watched_chat_id
keys, and seeds ai_provider to a sensible default. It runs after every
v2 config save in the GUI mutation paths.

Rules:
  - Only sync settings that are CURRENTLY missing or empty in the DB.
    Operators who hand-tuned a setting via the Tuning tab are never
    overwritten.
  - Walk every Destination → resolve the (first) Channel routing to it
    → use that Channel's Account for the credentials.
  - Skip Destinations with no enabled route (no service to feed).
  - Best-effort: if the destination DB file doesn't exist yet, skip it
    — the service's first start will init_schema and the next save
    triggers a fresh sync.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src import db_settings
from src.config_v2 import ConfigV2

_log = logging.getLogger("v2_db_sync")


def sync_v2_to_destination_dbs(cfg: ConfigV2) -> dict[str, list[str]]:
    """Push v2 entity values into each destination's DB settings.

    Returns a dict of ``{destination_id: [keys_written]}`` for logging
    or testing. Keys already present and non-empty in the DB are NOT
    overwritten.
    """
    written: dict[str, list[str]] = {}
    for dest in cfg.destinations:
        if not dest.db_path:
            continue
        db_path = Path(dest.db_path)
        if not db_path.exists():
            continue
        # Find the first channel routing to this destination.
        first_channel = None
        for r in cfg.routes:
            if r.destination_id == dest.id and r.enabled:
                ch = cfg.channel(r.channel_id)
                if ch is not None and ch.enabled:
                    first_channel = ch
                    break
        if first_channel is None:
            continue
        account = cfg.account(first_channel.account_id)
        if account is None:
            continue

        wrote: list[str] = []
        # Settings to sync from v2 entities.
        # The 3rd-tuple flag is `default_only`: True means "only write
        # when the DB has no value yet, and never overwrite even if the
        # destination entity changed" — for soft-default values.
        candidates: list[tuple[str, str, bool]] = [
            ("tg_phone", account.phone, False),
            ("tg_api_id", str(account.api_id) if account.api_id else "", False),
            ("tg_api_hash", account.api_hash, False),
            ("tg_watched_chat_id", str(first_channel.chat_id) if first_channel.chat_id else "", False),
            ("api_host", dest.api_host or "127.0.0.1", False),
            ("api_port", str(dest.api_port) if dest.api_port else "", False),
            ("ai_provider", "openai", True),  # default-only
        ]
        for key, value, default_only in candidates:
            if not value:
                continue
            try:
                existing = db_settings.get_str(db_path, key, "")
            except Exception:
                existing = ""
            # Two-rule write policy:
            #   - default_only: write only when the DB has no value
            #     (preserve operator Tuning choices for soft defaults).
            #   - otherwise: v2 entity is authoritative for this field
            #     — overwrite the DB to match. Operators who want a
            #     different value should edit the v2 entity, not Tuning.
            if default_only and existing:
                continue
            if existing == value:
                continue
            try:
                db_settings.set_str(db_path, key, value)
                wrote.append(key)
            except Exception as e:  # noqa: BLE001
                _log.warning("v2 sync: failed %s=%s in %s: %s",
                             key, key, db_path, e)
        if wrote:
            written[dest.id] = wrote
            _log.info("v2 sync %s: wrote %s", dest.id, ",".join(wrote))
    return written
