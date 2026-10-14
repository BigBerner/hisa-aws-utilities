"""Persist send-script transaction IDs for selective, repeatable cleanup."""

import re
import sqlite3
from pathlib import Path


TRANSACTIONS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "sent_transactions.sqlite3"
)


def record_transaction_id(transaction_id: str) -> None:
    """Commit an ID before polling so failed/timed-out cycles remain tracked."""
    if not transaction_id or transaction_id != transaction_id.strip():
        raise ValueError("A nonempty transaction ID without outer whitespace is required")
    TRANSACTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(TRANSACTIONS_PATH, timeout=30) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS transactions (id TEXT PRIMARY KEY NOT NULL)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO transactions (id) VALUES (?)", (transaction_id,)
        )


def load_transaction_ids() -> set[str]:
    """Load all tracked IDs; a missing ledger never authorizes deletion."""
    if not TRANSACTIONS_PATH.exists():
        return set()
    with sqlite3.connect(
        f"{TRANSACTIONS_PATH.as_uri()}?mode=ro", uri=True, timeout=30
    ) as connection:
        return {row[0] for row in connection.execute("SELECT id FROM transactions")}


def transaction_id_pattern(transaction_ids: set[str]) -> re.Pattern[str]:
    """Match entire IDs in keys, including #, /, and . separated key formats."""
    if not transaction_ids or any(not value.strip() for value in transaction_ids):
        raise ValueError("Nonempty transaction IDs are required for cleanup")
    alternatives = "|".join(re.escape(value) for value in sorted(transaction_ids))
    return re.compile(r"(?<![A-Za-z0-9_-])(?:" + alternatives + r")(?![A-Za-z0-9_-])")
