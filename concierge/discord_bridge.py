# SPDX-License-Identifier: MIT
"""Discord-to-chain wallet migration bridge.

Queries the Sophiacord Discord economy database (SQLite on NAS) via SSH,
and tracks migrations in a local database. Used by the ``concierge wallet
migrate`` CLI subcommand.
"""

import json
import os
import sqlite3
import subprocess

from concierge import config

# ---------------------------------------------------------------------------
# Local migration tracking
# ---------------------------------------------------------------------------

_TRACKING_DIR = os.path.join(os.path.expanduser("~"), ".concierge")
_TRACKING_DB = os.path.join(_TRACKING_DIR, "migrations.db")

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS migrations (
    id INTEGER PRIMARY KEY,
    discord_user_id TEXT NOT NULL,
    target_wallet TEXT NOT NULL,
    amount_rtc REAL NOT NULL,
    chain_tx_id TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(discord_user_id)
);
"""


def _init_tracking_db():
    """Ensure the local tracking database exists and has the schema."""
    os.makedirs(_TRACKING_DIR, exist_ok=True)
    con = sqlite3.connect(_TRACKING_DB)
    con.execute(_SCHEMA)
    con.commit()
    return con


def record_migration(discord_id, wallet, amount, chain_tx_id, status="completed"):
    """Record a completed migration in the local tracking DB."""
    con = _init_tracking_db()
    try:
        con.execute(
            "INSERT INTO migrations (discord_user_id, target_wallet, amount_rtc, "
            "chain_tx_id, status) VALUES (?, ?, ?, ?, ?)",
            (str(discord_id), wallet, amount, chain_tx_id, status),
        )
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # Already migrated
    finally:
        con.close()


def record_migration_force(discord_id, wallet, amount, chain_tx_id, status="completed"):
    """Record a migration, replacing any existing record for this user."""
    con = _init_tracking_db()
    try:
        con.execute(
            "INSERT OR REPLACE INTO migrations (discord_user_id, target_wallet, "
            "amount_rtc, chain_tx_id, status) VALUES (?, ?, ?, ?, ?)",
            (str(discord_id), wallet, amount, chain_tx_id, status),
        )
        con.commit()
        return True
    finally:
        con.close()


def get_migration_history():
    """Return all migration records, newest first."""
    con = _init_tracking_db()
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM migrations ORDER BY created_at DESC"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def already_migrated(discord_id):
    """Check if a Discord user has already been migrated."""
    con = _init_tracking_db()
    row = con.execute(
        "SELECT id FROM migrations WHERE discord_user_id = ?",
        (str(discord_id),),
    ).fetchone()
    con.close()
    return row is not None


# ---------------------------------------------------------------------------
# SSH queries to NAS Discord economy database
# ---------------------------------------------------------------------------

def _ssh_cmd():
    """Build the base SSH command list for connecting to the NAS."""
    return [
        "sshpass", "-p", config.DISCORD_NAS_PASSWORD,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{config.DISCORD_NAS_USER}@{config.DISCORD_NAS_HOST}",
        "python3",
    ]


def _ssh_run_script(script):
    """Run a Python script on the NAS via SSH stdin pipe.

    Piping via stdin avoids all shell quoting issues with parentheses,
    semicolons, and nested quotes that break when passed as -c args.

    Returns:
        (stdout, stderr, returncode) tuple.
    """
    if not config.DISCORD_NAS_PASSWORD:
        return ("", "DISCORD_NAS_PASSWORD not set", 1)

    try:
        result = subprocess.run(
            _ssh_cmd(),
            input=script, capture_output=True, text=True, timeout=30,
        )
        return (result.stdout.strip(), result.stderr.strip(), result.returncode)
    except subprocess.TimeoutExpired:
        return ("", "SSH timed out (30s)", 1)
    except FileNotFoundError:
        return ("", "sshpass not installed (apt install sshpass)", 1)


def _ssh_query(sql):
    """Run a SQL query on the NAS via SSH and return parsed JSON rows."""
    db_path = config.DISCORD_DB_PATH
    script = (
        "import sqlite3, json\n"
        f"c = sqlite3.connect({db_path!r})\n"
        "c.row_factory = sqlite3.Row\n"
        f"rows = c.execute({sql!r}).fetchall()\n"
        "print(json.dumps([dict(r) for r in rows]))\n"
        "c.close()\n"
    )
    stdout, stderr, rc = _ssh_run_script(script)
    if rc != 0:
        return {"error": f"SSH query failed: {stderr}"}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": f"Failed to parse result: {stdout[:200]}"}


def get_discord_balance(user_id):
    """Get the Discord economy balance for a single user.

    Returns:
        Dict with user_id, balance, total_earned, total_spent,
        or an error dict.
    """
    sql = (
        "SELECT user_id, balance, total_earned, total_spent "
        "FROM balances WHERE user_id = '%s'" % user_id
    )
    result = _ssh_query(sql)
    if isinstance(result, dict) and "error" in result:
        return result
    if not result:
        return {"error": f"Discord user {user_id} not found in economy DB"}
    return result[0]


def list_discord_holders(min_balance=0.1):
    """List all Discord economy holders with balance >= min_balance.

    Returns:
        List of dicts sorted by balance descending, or an error dict.
    """
    sql = (
        "SELECT user_id, balance, total_earned, total_spent "
        "FROM balances WHERE balance >= %s "
        "ORDER BY balance DESC" % min_balance
    )
    return _ssh_query(sql)


def debit_discord_balance(user_id, amount):
    """Debit a user's Discord economy balance and record the transaction.

    Runs UPDATE + INSERT in the same script for atomicity.

    Returns:
        True on success, error dict on failure.
    """
    db_path = config.DISCORD_DB_PATH
    script = (
        "import sqlite3\n"
        f"c = sqlite3.connect({db_path!r})\n"
        f"c.execute('UPDATE balances SET balance = balance - ?, "
        f"total_spent = total_spent + ? WHERE user_id = ?', "
        f"({amount}, {amount}, {user_id!r}))\n"
        f"c.execute('INSERT INTO transactions "
        f"(from_user, to_user, amount, type, description) "
        f"VALUES (?, ?, ?, ?, ?)', "
        f"({user_id!r}, 'CHAIN_MIGRATION', {amount}, "
        f"'migration', 'Migrated to on-chain RTC wallet'))\n"
        "c.commit()\n"
        "print('OK')\n"
        "c.close()\n"
    )
    stdout, stderr, rc = _ssh_run_script(script)
    if rc != 0:
        return {"error": f"Debit failed: {stderr}"}
    if "OK" in stdout:
        return True
    return {"error": f"Unexpected output: {stdout}"}
