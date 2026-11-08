import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    start_param TEXT,
    created_at  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    blocked     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_categories (
    user_id   INTEGER NOT NULL,
    category  TEXT NOT NULL,
    added_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, category)
);
CREATE TABLE IF NOT EXISTS payments (
    order_id    TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    tariff      TEXT NOT NULL,
    amount      REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  INTEGER NOT NULL,
    paid_at     INTEGER,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_cat ON user_categories(category);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    # ---------- users ----------
    async def upsert_user(self, uid: int, username: str | None, first_name: str | None,
                          start_param: str | None = None) -> bool:
        """Returns True if the user is new."""
        now = int(time.time())
        cur = await self.conn.execute("SELECT 1 FROM users WHERE id=?", (uid,))
        exists = await cur.fetchone()
        if exists:
            await self.conn.execute(
                "UPDATE users SET username=?, first_name=?, last_seen=?, blocked=0 WHERE id=?",
                (username, first_name, now, uid))
        else:
            await self.conn.execute(
                "INSERT INTO users (id, username, first_name, start_param, created_at, last_seen)"
                " VALUES (?,?,?,?,?,?)",
                (uid, username, first_name, start_param, now, now))
        await self.conn.commit()
        return not exists

    async def get_user(self, uid: int):
        cur = await self.conn.execute("SELECT * FROM users WHERE id=?", (uid,))
        return await cur.fetchone()

    async def find_user(self, query: str):
        q = query.lstrip("@")
        if q.isdigit():
            return await self.get_user(int(q))
        cur = await self.conn.execute("SELECT * FROM users WHERE lower(username)=lower(?)", (q,))
        return await cur.fetchone()

    async def set_blocked(self, uid: int, blocked: bool = True):
        await self.conn.execute("UPDATE users SET blocked=? WHERE id=?", (int(blocked), uid))
        await self.conn.commit()

    # ---------- categories ----------
    async def categories(self, uid: int) -> set[str]:
        cur = await self.conn.execute("SELECT category FROM user_categories WHERE user_id=?", (uid,))
        return {r["category"] for r in await cur.fetchall()}

    async def add_categories(self, uid: int, cats):
        now = int(time.time())
        await self.conn.executemany(
            "INSERT OR IGNORE INTO user_categories (user_id, category, added_at) VALUES (?,?,?)",
            [(uid, c, now) for c in cats])
        await self.conn.commit()

    async def remove_categories(self, uid: int, cats):
        await self.conn.executemany(
            "DELETE FROM user_categories WHERE user_id=? AND category=?", [(uid, c) for c in cats])
        await self.conn.commit()

    async def audience(self, any_of: list[str] | None = None, none_of: list[str] | None = None) -> list[int]:
        """Active (not blocked) users filtered by categories. any_of=None → all."""
        sql = "SELECT id FROM users u WHERE blocked=0"
        args: list = []
        if any_of:
            sql += f" AND EXISTS (SELECT 1 FROM user_categories c WHERE c.user_id=u.id" \
                   f" AND c.category IN ({','.join('?' * len(any_of))}))"
            args += any_of
        if none_of:
            sql += f" AND NOT EXISTS (SELECT 1 FROM user_categories c WHERE c.user_id=u.id" \
                   f" AND c.category IN ({','.join('?' * len(none_of))}))"
            args += none_of
        cur = await self.conn.execute(sql, args)
        return [r["id"] for r in await cur.fetchall()]

    # ---------- payments ----------
    async def create_payment(self, order_id: str, uid: int, tariff: str, amount: float):
        await self.conn.execute(
            "INSERT INTO payments (order_id, user_id, tariff, amount, created_at) VALUES (?,?,?,?,?)",
            (order_id, uid, tariff, amount, int(time.time())))
        await self.conn.commit()

    async def get_payment(self, order_id: str):
        cur = await self.conn.execute("SELECT * FROM payments WHERE order_id=?", (order_id,))
        return await cur.fetchone()

    async def mark_paid(self, order_id: str, raw: str) -> bool:
        """Returns True only on the first transition to 'paid' (idempotent)."""
        cur = await self.conn.execute(
            "UPDATE payments SET status='paid', paid_at=?, raw=? WHERE order_id=? AND status!='paid'",
            (int(time.time()), raw, order_id))
        await self.conn.commit()
        return cur.rowcount > 0

    # ---------- stats / export ----------
    async def stats(self) -> dict:
        async def one(sql, *a):
            cur = await self.conn.execute(sql, a)
            return (await cur.fetchone())[0]
        day_ago = int(time.time()) - 86400
        res = {
            "users": await one("SELECT COUNT(*) FROM users"),
            "active": await one("SELECT COUNT(*) FROM users WHERE blocked=0"),
            "new_24h": await one("SELECT COUNT(*) FROM users WHERE created_at>?", day_ago),
            "paid_count": await one("SELECT COUNT(*) FROM payments WHERE status='paid'"),
            "paid_sum": await one("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid'"),
        }
        cur = await self.conn.execute(
            "SELECT category, COUNT(*) n FROM user_categories GROUP BY category ORDER BY n DESC")
        res["categories"] = [(r["category"], r["n"]) for r in await cur.fetchall()]
        return res

    async def export_rows(self):
        cur = await self.conn.execute("""
            SELECT u.id, u.username, u.first_name, u.start_param, u.created_at, u.blocked,
                   COALESCE(GROUP_CONCAT(c.category, ' '), '') AS categories
            FROM users u LEFT JOIN user_categories c ON c.user_id=u.id
            GROUP BY u.id ORDER BY u.created_at""")
        return await cur.fetchall()
