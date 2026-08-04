"""Telegram adapter over `core/`.

A thin translation layer and nothing more. No business rule, no raw SQL, no
arithmetic that changes a number. Every write goes through `core.ledger`, and
every uncommitted flow lives in `pending_entries` rather than in this process,
so restarting the service leaves every open keyboard still working.
"""
