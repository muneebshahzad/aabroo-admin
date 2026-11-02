import os
from datetime import date
from decimal import Decimal

import psycopg2
from psycopg2.extras import RealDictCursor


_LAST_DB_ERROR = ""


def _set_last_db_error(message: str):
    global _LAST_DB_ERROR
    _LAST_DB_ERROR = message


def get_last_db_error() -> str:
    return _LAST_DB_ERROR


def _ensure_app_settings_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


ACCOUNT_DEFAULTS = ("Bank", "Al Karamat")

ACCOUNT_CATEGORY_DEFAULTS = {
    "income": {
        "Sales": ("Call Courier", "Leopard", "Lahore", "Other"),
        "Owner Investment": ("Al Karamat",),
        "Other": ("Adjustment",),
    },
    "expense": {
        "Product Cost": ("Javed", "Al Karamat", "Raiz", "Other"),
        "Packaging": ("Boxes", "Sheets", "Wrap", "Other"),
        "Delivery": ("Delivery",),
        "Website & Marketing": ("Facebook", "Shopify", "Other"),
        "Salary": ("Employee Salary",),
        "Office Expense": ("Rent", "Utilities", "Other"),
        "Profit Withdrawal": ("Al Karamat",),
        "Refund": ("Refund",),
        "Other": ("Other",),
    },
}


def _money(value):
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ensure_accounts_tables(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts_accounts (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            opening_balance NUMERIC(14, 2) NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts_categories (
            id SERIAL PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (kind, name)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts_subcategories (
            id SERIAL PRIMARY KEY,
            category_id INTEGER NOT NULL REFERENCES accounts_categories(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (category_id, name)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts_transactions (
            id SERIAL PRIMARY KEY,
            tx_date DATE NOT NULL DEFAULT CURRENT_DATE,
            kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
            account_id INTEGER NOT NULL REFERENCES accounts_accounts(id),
            category_id INTEGER NOT NULL REFERENCES accounts_categories(id),
            subcategory_id INTEGER REFERENCES accounts_subcategories(id),
            amount NUMERIC(14, 2) NOT NULL CHECK (amount >= 0),
            note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


def ensure_accounts_schema():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_accounts_tables(cur)
                for account_name in ACCOUNT_DEFAULTS:
                    cur.execute(
                        """
                        INSERT INTO accounts_accounts (name, opening_balance)
                        VALUES (%s, 0)
                        ON CONFLICT (name) DO NOTHING
                        """,
                        (account_name,),
                    )
                for kind, categories in ACCOUNT_CATEGORY_DEFAULTS.items():
                    for category_name, subcategories in categories.items():
                        cur.execute(
                            """
                            INSERT INTO accounts_categories (kind, name)
                            VALUES (%s, %s)
                            ON CONFLICT (kind, name) DO UPDATE SET name = EXCLUDED.name
                            RETURNING id
                            """,
                            (kind, category_name),
                        )
                        category_id = cur.fetchone()[0]
                        for subcategory_name in subcategories:
                            cur.execute(
                                """
                                INSERT INTO accounts_subcategories (category_id, name)
                                VALUES (%s, %s)
                                ON CONFLICT (category_id, name) DO NOTHING
                                """,
                                (category_id, subcategory_name),
                            )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB ensure_accounts_schema error: {e}")
        return False


def get_conn():
    url = (
        os.getenv("DATABASE_URL", "")
        or os.getenv("POSTGRES_URL", "")
        or os.getenv("POSTGRESQL_URL", "")
    )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url:
        return psycopg2.connect(url)

    host = os.getenv("PGHOST") or os.getenv("POSTGRES_HOST")
    port = os.getenv("PGPORT") or os.getenv("POSTGRES_PORT") or "5432"
    user = os.getenv("PGUSER") or os.getenv("POSTGRES_USER")
    password = os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD")
    database = (
        os.getenv("PGDATABASE")
        or os.getenv("POSTGRES_DB")
        or os.getenv("POSTGRES_DATABASE")
    )

    if host and user and database:
        return psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=database,
        )

    raise RuntimeError(
        "Database configuration missing. Set DATABASE_URL (preferred) or PGHOST/PGUSER/PGPASSWORD/PGDATABASE."
    )


def init_db():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS order_statuses (
                        key TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                _ensure_app_settings_table(cur)
            conn.commit()
        _set_last_db_error("")
        print("DB initialized.")
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB init error: {e}")


def load_order_statuses() -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT key, status FROM order_statuses")
                _set_last_db_error("")
                return {row["key"]: row["status"] for row in cur.fetchall()}
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB load error: {e}")
        return {}


def upsert_order_status(key: str, status: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO order_statuses (key, status)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE
                        SET status = EXCLUDED.status,
                            updated_at = NOW()
                    """,
                    (key, status),
                )
            conn.commit()
        _set_last_db_error("")
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB upsert error: {e}")


def delete_order_status(key: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM order_statuses WHERE key = %s", (key,))
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB delete error: {e}")
        return False


def get_app_setting(key: str, default: str = "") -> str:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_app_settings_table(cur)
                cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
                row = cur.fetchone()
                _set_last_db_error("")
                return row[0] if row and row[0] is not None else default
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB get_app_setting error: {e}")
        return default


def set_app_setting(key: str, value: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                _ensure_app_settings_table(cur)
                cur.execute(
                    """
                    INSERT INTO app_settings (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            updated_at = NOW()
                    """,
                    (key, value),
                )
            conn.commit()
        _set_last_db_error("")
        return True
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB set_app_setting error: {e}")
        return False


def get_accounts_page_data():
    if not ensure_accounts_schema():
        return None
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, name, opening_balance FROM accounts_accounts ORDER BY id")
                accounts = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT
                        c.id AS category_id,
                        c.kind,
                        c.name AS category_name,
                        s.id AS subcategory_id,
                        s.name AS subcategory_name
                    FROM accounts_categories c
                    LEFT JOIN accounts_subcategories s ON s.category_id = c.id
                    ORDER BY c.kind DESC, c.name, s.name
                    """
                )
                categories = {"income": [], "expense": []}
                category_index = {}
                for row in cur.fetchall():
                    category_id = row["category_id"]
                    if category_id not in category_index:
                        category = {
                            "id": category_id,
                            "kind": row["kind"],
                            "name": row["category_name"],
                            "subcategories": [],
                        }
                        category_index[category_id] = category
                        categories[row["kind"]].append(category)
                    if row["subcategory_id"]:
                        category_index[category_id]["subcategories"].append(
                            {"id": row["subcategory_id"], "name": row["subcategory_name"]}
                        )

                cur.execute(
                    """
                    SELECT
                        t.id,
                        t.tx_date,
                        t.kind,
                        t.amount,
                        t.note,
                        t.created_at,
                        a.id AS account_id,
                        a.name AS account_name,
                        c.name AS category_name,
                        s.name AS subcategory_name
                    FROM accounts_transactions t
                    JOIN accounts_accounts a ON a.id = t.account_id
                    JOIN accounts_categories c ON c.id = t.category_id
                    LEFT JOIN accounts_subcategories s ON s.id = t.subcategory_id
                    ORDER BY t.tx_date DESC, t.id DESC
                    LIMIT 250
                    """
                )
                transactions = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN kind = 'income' THEN amount ELSE 0 END), 0) AS total_income,
                        COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount ELSE 0 END), 0) AS total_expense,
                        COALESCE(SUM(CASE WHEN kind = 'income' THEN amount ELSE -amount END), 0) AS net_profit,
                        COALESCE(SUM(CASE WHEN kind = 'income' AND date_trunc('month', tx_date) = date_trunc('month', CURRENT_DATE) THEN amount ELSE 0 END), 0) AS month_income,
                        COALESCE(SUM(CASE WHEN kind = 'expense' AND date_trunc('month', tx_date) = date_trunc('month', CURRENT_DATE) THEN amount ELSE 0 END), 0) AS month_expense,
                        COALESCE(SUM(CASE WHEN date_trunc('month', tx_date) = date_trunc('month', CURRENT_DATE) THEN CASE WHEN kind = 'income' THEN amount ELSE -amount END ELSE 0 END), 0) AS month_profit
                    FROM accounts_transactions
                    """
                )
                summary = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    SELECT
                        a.id,
                        a.name,
                        a.opening_balance
                            + COALESCE(SUM(CASE WHEN t.kind = 'income' THEN t.amount ELSE -t.amount END), 0) AS balance
                    FROM accounts_accounts a
                    LEFT JOIN accounts_transactions t ON t.account_id = a.id
                    GROUP BY a.id, a.name, a.opening_balance
                    ORDER BY a.id
                    """
                )
                balances = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT
                        to_char(date_trunc('month', tx_date), 'Mon YYYY') AS label,
                        date_trunc('month', tx_date) AS month_start,
                        COALESCE(SUM(CASE WHEN kind = 'income' THEN amount ELSE 0 END), 0) AS income,
                        COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount ELSE 0 END), 0) AS expense,
                        COALESCE(SUM(CASE WHEN kind = 'income' THEN amount ELSE -amount END), 0) AS profit
                    FROM accounts_transactions
                    WHERE tx_date >= date_trunc('month', CURRENT_DATE) - INTERVAL '11 months'
                    GROUP BY month_start
                    ORDER BY month_start
                    """
                )
                monthly = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT
                        c.kind,
                        c.name AS category_name,
                        COALESCE(SUM(t.amount), 0) AS total
                    FROM accounts_transactions t
                    JOIN accounts_categories c ON c.id = t.category_id
                    WHERE date_trunc('month', t.tx_date) = date_trunc('month', CURRENT_DATE)
                    GROUP BY c.kind, c.name
                    ORDER BY c.kind, total DESC
                    """
                )
                category_breakdown = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT
                        t.id,
                        t.tx_date,
                        t.kind,
                        t.amount,
                        t.note,
                        a.name AS account_name,
                        c.name AS category_name,
                        s.name AS subcategory_name
                    FROM accounts_transactions t
                    JOIN accounts_accounts a ON a.id = t.account_id
                    JOIN accounts_categories c ON c.id = t.category_id
                    LEFT JOIN accounts_subcategories s ON s.id = t.subcategory_id
                    WHERE
                        (
                            (t.kind = 'income' AND c.name = 'Owner Investment')
                            OR (t.kind = 'expense' AND c.name = 'Profit Withdrawal')
                        )
                        AND (s.name IS NULL OR s.name IN ('Agha', 'Al Karamat'))
                    ORDER BY t.tx_date, t.id
                    """
                )
                agha_rows = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT
                        t.id,
                        t.tx_date,
                        t.kind,
                        t.amount,
                        t.note,
                        a.id AS account_id,
                        a.name AS account_name,
                        c.name AS category_name,
                        s.name AS subcategory_name
                    FROM accounts_transactions t
                    JOIN accounts_accounts a ON a.id = t.account_id
                    JOIN accounts_categories c ON c.id = t.category_id
                    LEFT JOIN accounts_subcategories s ON s.id = t.subcategory_id
                    ORDER BY a.id, t.tx_date, t.id
                    """
                )
                ledger_rows = [dict(row) for row in cur.fetchall()]

        opening_by_account = {row["id"]: _money(row.get("opening_balance")) for row in accounts}
        running_by_account = dict(opening_by_account)
        ledgers = {row["id"]: {"account": row["name"], "rows": []} for row in accounts}
        for row in ledger_rows:
            account_id = row["account_id"]
            amount = _money(row["amount"])
            delta = amount if row["kind"] == "income" else -amount
            running_by_account[account_id] = running_by_account.get(account_id, 0.0) + delta
            item = dict(row)
            item["income"] = amount if row["kind"] == "income" else 0
            item["expense"] = amount if row["kind"] == "expense" else 0
            item["running_balance"] = running_by_account[account_id]
            ledgers.setdefault(account_id, {"account": row["account_name"], "rows": []})["rows"].append(item)
        for ledger in ledgers.values():
            ledger["rows"].reverse()

        agha_balance = 0.0
        agha_investment_total = 0.0
        agha_withdrawal_total = 0.0
        agha_ledger_rows = []
        for row in agha_rows:
            amount = _money(row["amount"])
            income = amount if row["kind"] == "income" else 0.0
            expense = amount if row["kind"] == "expense" else 0.0
            agha_investment_total += income
            agha_withdrawal_total += expense
            agha_balance += income - expense
            item = dict(row)
            item["income"] = income
            item["expense"] = expense
            item["running_balance"] = agha_balance
            agha_ledger_rows.append(item)
        agha_ledger_rows.reverse()
        agha_ledger = {
            "balance": agha_balance,
            "investment_total": agha_investment_total,
            "withdrawal_total": agha_withdrawal_total,
            "rows": agha_ledger_rows,
        }

        category_breakdown_by_kind = {
            "income": [row for row in category_breakdown if row.get("kind") == "income"],
            "expense": [row for row in category_breakdown if row.get("kind") == "expense"],
        }

        for collection in (accounts, transactions, balances, monthly, category_breakdown):
            for row in collection:
                for key, value in list(row.items()):
                    if isinstance(value, Decimal):
                        row[key] = float(value)
                    elif isinstance(value, date):
                        row[key] = value.isoformat()
        for row in agha_ledger["rows"]:
            for key, value in list(row.items()):
                if isinstance(value, Decimal):
                    row[key] = float(value)
                elif isinstance(value, date):
                    row[key] = value.isoformat()

        for key, value in list(summary.items()):
            if isinstance(value, Decimal):
                summary[key] = float(value)

        _set_last_db_error("")
        return {
            "accounts": accounts,
            "categories": categories,
            "transactions": transactions,
            "balances": balances,
            "summary": summary,
            "monthly": monthly,
            "category_breakdown": category_breakdown,
            "category_breakdown_by_kind": category_breakdown_by_kind,
            "agha_ledger": agha_ledger,
            "ledgers": list(ledgers.values()),
        }
    except Exception as e:
        _set_last_db_error(str(e))
        print(f"DB get_accounts_page_data error: {e}")
        return None


def create_account_transaction(payload):
    ensure_accounts_schema()
    kind = (payload.get("kind") or "").strip().lower()
    if kind not in {"income", "expense"}:
        raise ValueError("Choose income or expense.")
    try:
        amount = Decimal(str(payload.get("amount") or "0"))
    except Exception as error:
        raise ValueError("Enter a valid amount.") from error
    if amount <= 0:
        raise ValueError("Amount must be greater than 0.")

    tx_date = (payload.get("tx_date") or "").strip() or date.today().isoformat()
    account_id = int(payload.get("account_id") or 0)
    category_id = int(payload.get("category_id") or 0)
    subcategory_id = payload.get("subcategory_id") or None
    subcategory_id = int(subcategory_id) if subcategory_id else None
    note = (payload.get("note") or "").strip()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT kind FROM accounts_categories WHERE id = %s", (category_id,))
            row = cur.fetchone()
            if not row or row[0] != kind:
                raise ValueError("Selected category does not match the transaction type.")
            if subcategory_id:
                cur.execute(
                    "SELECT 1 FROM accounts_subcategories WHERE id = %s AND category_id = %s",
                    (subcategory_id, category_id),
                )
                if not cur.fetchone():
                    raise ValueError("Selected subcategory does not belong to the category.")
            cur.execute("SELECT 1 FROM accounts_accounts WHERE id = %s", (account_id,))
            if not cur.fetchone():
                raise ValueError("Selected account does not exist.")
            cur.execute(
                """
                INSERT INTO accounts_transactions
                    (tx_date, kind, account_id, category_id, subcategory_id, amount, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (tx_date, kind, account_id, category_id, subcategory_id, amount, note),
            )
            transaction_id = cur.fetchone()[0]
        conn.commit()
    _set_last_db_error("")
    return transaction_id
