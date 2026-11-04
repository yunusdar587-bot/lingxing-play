from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    app_id: str = ""
    app_secret: str = ""
    base_url: str = "https://openapi.lingxing.com"
    db_path: str = "data/dashboard.sqlite3"
    host: str = "127.0.0.1"
    port: int = 8765
    poll_minutes: int = 15
    daily_local_time: str = "06:00"
    # "negative"：gross_profit 中成本以负数计入（领星默认）；"positive"：成本以正数存放并已从毛利中扣除。
    cost_sign: str = "negative"
    # OrderProfit 的 startDate/endDate 是否带时分秒。
    order_profit_datetime: bool = True
    # 新的一天首轮轮询前，补一次上一自然日全天的 OrderProfit，只用于消化队列尾差。
    fifo_close_previous_day: bool = True
    page_length: dict = field(
        default_factory=lambda: {
            "/basicOpen/finance/mreport/OrderProfit": 5000,
            "/erp/sc/routing/fba/fbaStock/getFbaAgeList": 1000,
            "/erp/sc/data/fba_report/receivedInventory": 1000,
            "/erp/sc/data/fba_report/storageFeeMonth": 1000,
            "/erp/sc/data/mws/listing": 1000,
        }
    )
    demo: bool = False

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> "Config":
        if env_file:
            _load_dotenv(Path(env_file))
        e = os.environ
        cfg = cls(
            app_id=e.get("LINGXING_APP_ID", ""),
            app_secret=e.get("LINGXING_APP_SECRET", ""),
            base_url=e.get("LINGXING_BASE_URL", cls.base_url).rstrip("/"),
            db_path=e.get("DASHBOARD_DB", cls.db_path),
            host=e.get("DASHBOARD_HOST", cls.host),
            port=int(e.get("DASHBOARD_PORT", cls.port)),
            poll_minutes=int(e.get("POLL_MINUTES", cls.poll_minutes)),
            daily_local_time=e.get("DAILY_LOCAL_TIME", cls.daily_local_time),
            cost_sign=e.get("COST_SIGN", cls.cost_sign).lower(),
            order_profit_datetime=e.get("ORDER_PROFIT_DATETIME", "1") not in ("0", "false", "no"),
            fifo_close_previous_day=e.get("FIFO_CLOSE_PREVIOUS_DAY", "1") not in ("0", "false", "no"),
            demo=e.get("DASHBOARD_DEMO", "0") in ("1", "true", "yes"),
        )
        if cfg.cost_sign not in ("negative", "positive"):
            raise ValueError("COST_SIGN 只能是 negative 或 positive")
        return cfg
