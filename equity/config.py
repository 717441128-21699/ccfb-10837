"""股权穿透采集分析系统 —— 全局配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def _env(key: str, default):
    val = os.environ.get(f"EQS_{key}")
    if val is None:
        return default
    if isinstance(default, bool):
        return val.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(val)
    if isinstance(default, float):
        return float(val)
    return val


@dataclass
class Settings:
    # 路径
    sim_db: str = str(DATA_DIR / "sim_disclosure.db")
    warehouse_db: str = str(DATA_DIR / "warehouse.db")
    clock_state: str = str(DATA_DIR / "sim_clock.json")

    # 仿真站
    sim_host: str = "127.0.0.1"
    sim_port: int = 8080
    admin_token: str = "eqs-admin-token"
    virtual_day_seconds: int = 120        # 仿真站一个自然日对应的真实秒数
    account_count: int = 5
    daily_quota: int = 800                # 每账号每虚拟日查询上限

    # 数据生成
    company_count: int = 3000
    rng_seed: int = 20261115

    # 采集器
    sim_base_url: str = "http://127.0.0.1:8080"
    concurrency: int = 5
    request_delay: float = 0.05
    max_retries: int = 4
    recrawl_age_hours: int = 24

    # Web
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # 实控人判定默认规则（可在网页上改）
    default_rules: dict = field(default_factory=lambda: {
        "voting_threshold": 0.50,        # 有效表决权超过该比例 → 单独控制
        "joint_threshold": 0.40,         # 多人同时超过 → 共同控制
        "plurality_min": 0.30,           # 相对控制要求的最低表决权
        "plurality_margin": 0.10,        # 第一大相对第二大的领先幅度
        "chain_control": True,           # 递归追溯上层公司的实控人
        "follow_nominee": True,          # 代持是否穿透到实际权益人
        "max_chain_depth": 30,
    })

    def env_override(self) -> "Settings":
        for k in asdict(self):
            if k == "default_rules":
                continue
            setattr(self, k, _env(k.upper(), getattr(self, k)))
        return self


SETTINGS = Settings().env_override()
