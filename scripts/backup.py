"""数据库备份：导出 stock_agent 全部表为 SQL 文件（--single-transaction 语义由 InnoDB 一致性读保证）。"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymysql

from app.config import get_settings

OUT_DIR = Path(__file__).resolve().parent.parent / "backups"


def esc(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, bytes):
        return "0x" + v.hex()
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def main() -> None:
    url = get_settings().database_url
    # mysql+asyncmy://user:pass@host:port/db?charset=utf8mb4
    auth, rest = url.split("//", 1)[1].split("@", 1)
    user, pwd = auth.split(":", 1)
    pwd = pwd.replace("%21", "!")
    host_port, db_part = rest.split("/", 1)
    host, port = host_port.split(":")
    db = db_part.split("?")[0]

    conn = pymysql.connect(host=host, port=int(port), user=user, password=pwd, database=db, charset="utf8mb4")
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"backup_{date.today().isoformat()}.sql"
    cur = conn.cursor()
    with out.open("w", encoding="utf-8") as f:
        f.write(f"-- stock_agent backup {date.today()}\nSET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\n")
        cur.execute("SHOW TABLES")
        tables = [r[0] for r in cur.fetchall()]
        total_rows = 0
        for t in tables:
            cur.execute(f"SELECT COUNT(*) FROM `{t}`")
            n = cur.fetchone()[0]
            f.write(f"\n-- table {t} ({n} rows)\n")
            if n:
                cur.execute(f"SELECT * FROM `{t}`")
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    vals = ", ".join(esc(v) for v in row)
                    f.write(f"INSERT INTO `{t}` ({', '.join('`'+c+'`' for c in cols)}) VALUES ({vals});\n")
            total_rows += n
        f.write("\nSET FOREIGN_KEY_CHECKS=1;\n")
    conn.close()
    print(f"backup OK: {out} ({total_rows} rows, {out.stat().st_size} bytes, {len(tables)} tables)")


if __name__ == "__main__":
    sys.exit(main())
