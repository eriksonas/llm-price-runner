import aiosqlite
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/data/pricerunner.db")


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id TEXT NOT NULL,
                input_usd_per_1m REAL NOT NULL,
                output_usd_per_1m REAL NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_overrides (
                model_id TEXT PRIMARY KEY,
                input_usd_per_1m REAL NOT NULL,
                output_usd_per_1m REAL NOT NULL,
                notes TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_price_history_model_recorded "
            "ON price_history(model_id, recorded_at DESC)"
        )
        await db.commit()


async def record_prices(models: list):
    """Record a single price snapshot per model per UTC day.

    The scheduler refreshes every 6h, but a daily granularity keeps the
    history table compact and the chart readable. We dedupe by checking
    the most recent row per model and skipping inserts when the date and
    both prices are unchanged.
    """
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    now_iso = now.isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for m in models:
            async with db.execute(
                "SELECT input_usd_per_1m, output_usd_per_1m, recorded_at "
                "FROM price_history WHERE model_id=? ORDER BY recorded_at DESC LIMIT 1",
                (m["id"],),
            ) as cursor:
                last = await cursor.fetchone()
            if last:
                same_day = (last["recorded_at"] or "")[:10] == today
                same_price = (
                    last["input_usd_per_1m"] == m["input_usd_per_1m"]
                    and last["output_usd_per_1m"] == m["output_usd_per_1m"]
                )
                if same_day and same_price:
                    continue
            await db.execute(
                "INSERT INTO price_history (model_id, input_usd_per_1m, output_usd_per_1m, recorded_at) VALUES (?, ?, ?, ?)",
                (m["id"], m["input_usd_per_1m"], m["output_usd_per_1m"], now_iso),
            )
        await db.commit()


async def get_price_history(model_id: str, limit: int = 30) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM price_history WHERE model_id=? ORDER BY recorded_at DESC LIMIT ?",
            (model_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def save_override(model_id: str, input_price: float, output_price: float, notes: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO price_overrides (model_id, input_usd_per_1m, output_usd_per_1m, notes, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(model_id) DO UPDATE SET
                 input_usd_per_1m=excluded.input_usd_per_1m,
                 output_usd_per_1m=excluded.output_usd_per_1m,
                 notes=excluded.notes,
                 updated_at=excluded.updated_at""",
            (model_id, input_price, output_price, notes, now),
        )
        await db.commit()


async def get_overrides() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM price_overrides") as cursor:
            rows = await cursor.fetchall()
            return {r["model_id"]: dict(r) for r in rows}
