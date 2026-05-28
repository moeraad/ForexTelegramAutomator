"""Derive the full v2 NSSM-service spec from a ``ConfigV2``.

Phase-3 v2 cleanup: bootstrap now iterates v2 entities directly instead
of going through the per-stack 1:1:1 ``service_names`` shim. Each
Destination, Bot, and Account contributes one service:

  - Destination → ``src.api``           service ``Destination.service_name``
  - Bot         → ``src.bot``           service ``Bot.service_name``
  - Account     → ``src.shared_listener`` service ``Account.service_name``
                  with ``--account-id <Account.id>``

For Bots and Accounts, only ones that ACTUALLY touch a destination via
bindings/channels are emitted — there's no point installing services
for orphan entries.
"""
from __future__ import annotations

from typing import Iterable, TypedDict

from src.config_v2 import ConfigV2


class ServiceSpecEntry(TypedDict):
    service: str
    module: str
    db_path: str
    account_id: str


def derive_v2_service_spec(cfg: ConfigV2) -> list[ServiceSpecEntry]:
    """Build the full N-service spec to install for this v2 config.

    Order: APIs (one per destination), then bots, then listeners. The
    elevated helper installs them in argv order; APIs-first means the
    bot-startup checks (``has_v2_binding_for_destination``) work before
    the listener is ready to dispatch.

    Skips:
      - Destinations with no enabled routes (no live traffic = no API
        useful to install).
      - Bots with no binding covering any destination (orphan bot row).
      - Accounts with no enabled channels (orphan account row).
    """
    out: list[ServiceSpecEntry] = []

    # APIs: one per destination that has at least one enabled route.
    dest_db_paths: dict[str, str] = {}
    for dest in cfg.destinations:
        if not any(r.enabled and r.destination_id == dest.id for r in cfg.routes):
            continue
        if not dest.service_name or not dest.db_path:
            continue
        dest_db_paths[dest.id] = dest.db_path
        out.append({
            "service": dest.service_name,
            "module": "src.api",
            "db_path": dest.db_path,
            "account_id": "",
        })

    # Bots: emit each bot referenced by at least one binding that covers
    # a destination we ARE installing.
    seen_bot_ids: set[str] = set()
    for dest_id, dest_db in dest_db_paths.items():
        for b in cfg.bindings_for_destination(dest_id):
            if b.bot_id in seen_bot_ids:
                continue
            bot = cfg.bot(b.bot_id)
            if bot is None or not bot.service_name:
                continue
            seen_bot_ids.add(bot.id)
            # Each bot binds to ONE destination DB as its primary; if
            # bound to multiple via mirror/aggregate, the multi-conn
            # tailer (Step 14) opens the rest at runtime. The "primary"
            # db_path the bot service points at is whichever destination
            # surfaced it first (deterministic = first dest in cfg).
            out.append({
                "service": bot.service_name,
                "module": "src.bot",
                "db_path": dest_db,
                "account_id": "",
            })

    # Listeners: one per account that owns at least one channel routing
    # to an installed destination. The shared_listener gets --account-id
    # so it knows which account to bind to (Step 13).
    seen_acct_ids: set[str] = set()
    for ch in cfg.channels:
        if not ch.enabled:
            continue
        # Find any enabled route from this channel hitting a real dest.
        target_db: str | None = None
        for r in cfg.routes_for_channel(ch.id):
            if r.enabled and r.destination_id in dest_db_paths:
                target_db = dest_db_paths[r.destination_id]
                break
        if not target_db:
            continue
        acct = cfg.account(ch.account_id)
        if acct is None or not acct.service_name or acct.id in seen_acct_ids:
            continue
        seen_acct_ids.add(acct.id)
        out.append({
            "service": acct.service_name,
            "module": "src.shared_listener",
            "db_path": target_db,
            "account_id": acct.id,
        })

    return out


def dedup_spec(entries: Iterable[ServiceSpecEntry]) -> list[ServiceSpecEntry]:
    """First-wins dedup by ``service`` name (mirror of the helper-side dedup)."""
    seen: set[str] = set()
    out: list[ServiceSpecEntry] = []
    for e in entries:
        svc = e.get("service") or ""
        if not svc or svc in seen:
            continue
        seen.add(svc)
        out.append(e)
    return out
