"""仿真站数据快速体检：预设答案分布、闭合性、环、代持、同名、变更模式。"""
from __future__ import annotations

import sqlite3
import sys

from equity.config import SETTINGS


def main(db_path: str | None = None) -> int:
    conn = sqlite3.connect(db_path or SETTINGS.sim_db)
    conn.row_factory = sqlite3.Row

    def dump(title: str, sql: str) -> None:
        print(f"--- {title} ---")
        for row in conn.execute(sql):
            print(dict(row))

    dump("预设实控人分布",
         "SELECT controller_type, COUNT(*) n FROM preset_controllers GROUP BY 1")
    dump("详情页模式",
         "SELECT profile_mode, COUNT(*) n FROM companies GROUP BY 1")
    dump("股东表未闭合(股权)",
         "SELECT company_id, ROUND(SUM(shares_pct),4) s FROM holdings "
         "GROUP BY company_id HAVING ABS(s-100)>0.001 LIMIT 5")
    dump("股东表未闭合(表决权)",
         "SELECT company_id, ROUND(SUM(voting_pct),4) s FROM holdings "
         "GROUP BY company_id HAVING ABS(s-100)>0.001 LIMIT 5")
    dump("代持事件数",
         "SELECT COUNT(*) n FROM holding_events WHERE is_nominee=1")
    dump("纯环公司股东",
         "SELECT c.name, h.holder_id, h.shares_pct FROM holdings h "
         "JOIN companies c ON c.company_id=h.company_id "
         "WHERE c.name LIKE '环宇%' ORDER BY c.name, h.shares_pct DESC")
    dump("同名不同人前10组",
         "SELECT name, COUNT(*) n FROM persons GROUP BY name "
         "HAVING n>1 ORDER BY n DESC, name LIMIT 10")
    dump("公司别名示例",
         "SELECT c.name, a.alias FROM company_aliases a "
         "JOIN companies c ON c.company_id=a.company_id LIMIT 6")
    dump("只给变更记录的公司里，事件最多的5家",
         "SELECT c.company_id, c.name, COUNT(*) n FROM holding_events e "
         "JOIN companies c ON c.company_id=e.company_id "
         "WHERE c.profile_mode='change' GROUP BY c.company_id "
         "ORDER BY n DESC LIMIT 5")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
