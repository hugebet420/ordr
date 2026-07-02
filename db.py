"""
db.py — Couche SQLite pour ORDR
Le chemin de la base est configurable via DATABASE_PATH (pour Railway volume persistant).
"""

from __future__ import annotations
import os, json, sqlite3, time, uuid
from contextlib import contextmanager

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "data", "ordr.db"))

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS shops (
                id                        TEXT PRIMARY KEY,
                nom_commerce              TEXT,
                adresse                   TEXT,
                categories                TEXT  NOT NULL DEFAULT '[]',
                total_produits            INTEGER NOT NULL DEFAULT 0,
                stripe_account_id         TEXT,
                stripe_onboarding_complete INTEGER NOT NULL DEFAULT 0,
                created_at                TEXT  NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id                TEXT PRIMARY KEY,
                shop_id           TEXT NOT NULL,
                stripe_session_id TEXT UNIQUE,
                customer_name     TEXT,
                customer_phone    TEXT,
                slot              TEXT,
                items             TEXT NOT NULL DEFAULT '[]',
                total             REAL NOT NULL DEFAULT 0,
                status            TEXT NOT NULL DEFAULT 'payée',
                created_at        TEXT NOT NULL,
                FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_orders_shop    ON orders(shop_id);
            CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(stripe_session_id);
        """)


# ── Shops ──────────────────────────────────────────────────────────────────────

def _row_to_shop(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["categories"]                 = json.loads(d["categories"] or "[]")
    d["stripe_onboarding_complete"] = bool(d["stripe_onboarding_complete"])
    return d

def get_shop(shop_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()
    return _row_to_shop(row)

def list_shops() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM shops ORDER BY created_at DESC").fetchall()
    return [_row_to_shop(r) for r in rows]

def create_shop(data: dict) -> str:
    sid = data.get("id") or uuid.uuid4().hex[:8]
    with _conn() as con:
        con.execute("""
            INSERT OR REPLACE INTO shops
              (id, nom_commerce, adresse, categories, total_produits,
               stripe_account_id, stripe_onboarding_complete, created_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            sid,
            data.get("nom_commerce"),
            data.get("adresse"),
            json.dumps(data.get("categories", []), ensure_ascii=False),
            data.get("total_produits", 0),
            data.get("stripe_account_id"),
            1 if data.get("stripe_onboarding_complete") else 0,
            data.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
        ))
    return sid

def update_shop(shop_id: str, data: dict):
    with _conn() as con:
        con.execute("""
            UPDATE shops SET
              nom_commerce              = ?,
              adresse                   = ?,
              categories                = ?,
              total_produits            = ?,
              stripe_account_id         = ?,
              stripe_onboarding_complete = ?
            WHERE id = ?
        """, (
            data.get("nom_commerce"),
            data.get("adresse"),
            json.dumps(data.get("categories", []), ensure_ascii=False),
            data.get("total_produits", 0),
            data.get("stripe_account_id"),
            1 if data.get("stripe_onboarding_complete") else 0,
            shop_id,
        ))

def delete_shop(shop_id: str):
    with _conn() as con:
        con.execute("DELETE FROM shops WHERE id = ?", (shop_id,))


# ── Orders ─────────────────────────────────────────────────────────────────────

def _row_to_order(row) -> dict:
    d = dict(row)
    d["items"] = json.loads(d["items"] or "[]")
    return d

def order_exists(stripe_session_id: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM orders WHERE stripe_session_id = ?", (stripe_session_id,)
        ).fetchone()
    return row is not None

def create_order(shop_id: str, data: dict) -> str:
    oid = f"order_{shop_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    with _conn() as con:
        con.execute("""
            INSERT INTO orders
              (id, shop_id, stripe_session_id, customer_name, customer_phone,
               slot, items, total, status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            oid,
            shop_id,
            data.get("stripe_session_id"),
            data.get("customer_name"),
            data.get("customer_phone"),
            data.get("slot"),
            json.dumps(data.get("items", []), ensure_ascii=False),
            data.get("total", 0),
            data.get("status", "payée"),
            time.strftime("%Y-%m-%d %H:%M:%S"),
        ))
    return oid

def list_orders(shop_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM orders WHERE shop_id = ? ORDER BY created_at DESC", (shop_id,)
        ).fetchall()
    return [_row_to_order(r) for r in rows]
