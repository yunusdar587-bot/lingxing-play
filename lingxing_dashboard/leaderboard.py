"""排行榜的配置与汇总。只读取已算好的快照，不做新的利润计算。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .calc import MODE_DAILY, MODE_LABELS, MODE_ORDER
from .jobs import Engine
from .scheduler import next_boundary
from .sites import SITES
from .timeutil import iso, local_now

CONFIG_KEY = "leaderboard_config"
COLUMNS = 3
NAME_MAX = 30


class ConfigError(ValueError):
    pass


def q_key(sid: int, msku: str) -> str:
    return f"{sid}|{msku}"


class LeaderboardService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.db = engine.db

    # ------------------------------------------------------------------ 数据读取
    def _sign_hidden(self) -> bool:
        return (self.db.get("sign_check") or {}).get("status") == "failed"

    def _today(self, site_code: str) -> str:
        return local_now(SITES[site_code].tz, self.engine.clock()).date().isoformat()

    def latest_today(self, sid: int, site_code: str):
        return self.db.one(
            "SELECT id, taken_at, summary, rows FROM snapshots WHERE sid=? AND local_date=? ORDER BY id DESC LIMIT 1",
            (sid, self._today(site_code)),
        )

    def _waiting_for(self, site_code: str) -> str:
        running = self.db.one("SELECT scheduled_at FROM poll_runs WHERE status='running' ORDER BY id DESC LIMIT 1")
        if running:
            return f"正在进行的轮询（计划于 {running['scheduled_at']}）"
        nxt = self.db.get("next_poll_at") or iso(next_boundary(self.engine.clock(), self.engine.cfg.poll_minutes))
        return f"下一次轮询（计划于 {nxt}）"

    def _last_poll_error(self, site_code: str) -> str | None:
        r = self.db.one("SELECT status, message FROM poll_runs WHERE status IN ('ok','partial','failed') "
                        "ORDER BY id DESC LIMIT 1")
        if not r or r["status"] == "ok":
            return None
        msg = r["message"] or ""
        if r["status"] == "failed":
            return msg
        parts = [p for p in msg.split("; ") if p.startswith(f"{site_code}:")]
        return parts[0] if parts else None

    def readiness(self) -> list[dict]:
        out = []
        for s in self.engine.stores():
            site = s["site"]
            snap = self.latest_today(s["sid"], site.code)
            entry = {"sid": s["sid"], "name": s["name"], "site": site.code, "site_name": site.name,
                     "tz": site.tz_name, "local_date": self._today(site.code), "ready": snap is not None}
            if snap:
                entry["snapshot_at"] = snap["taken_at"]
                entry["local_time"] = json.loads(snap["summary"])["local_time"]
            else:
                entry["waiting_for"] = self._waiting_for(site.code)
                entry["last_error"] = self._last_poll_error(site.code)
            out.append(entry)
        return out

    def _rows_by_store(self) -> dict[int, tuple[dict, list[dict]]]:
        out = {}
        for s in self.engine.stores():
            snap = self.latest_today(s["sid"], s["site"].code)
            if snap and snap["rows"]:
                out[s["sid"]] = (s, json.loads(snap["rows"]))
        return out

    # ------------------------------------------------------------------ 配置页
    def review_lists(self) -> dict:
        overrides = self.db.get("q_overrides") or {}
        q_missing, dims_rows, unknown_lots = [], [], []
        for sid, (s, rows) in self._rows_by_store().items():
            for r in rows:
                base = {"sid": sid, "store": s["name"], "msku": r["msku"]}
                needs_q = r["net_amount"] != 0 or r["afn_volume"] != 0
                if r.get("q_source") != "lingxing" and needs_q:
                    ov = overrides.get(q_key(sid, r["msku"]))
                    if ov is None:
                        current = {"kind": "unset", "text": "未设置（目前未就绪，不计入）"}
                    elif ov["kind"] == "zero":
                        current = {"kind": "zero", "text": "按 0 计入"}
                    else:
                        current = {"kind": "pct", "pct": ov["pct"], "text": f"{ov['pct']}%"}
                    q_missing.append({**base, "key": q_key(sid, r["msku"]), "current": current,
                                      "afn_volume": r["afn_volume"], "net_amount": r["net_amount"]})
                if r.get("dims_source") != "storage_report":
                    dims_rows.append({**base, "source": "none", "disposal_status": r["disposal_status"],
                                      "daily_storage_status": r["daily_storage_status"]})
                if r["order_unknown_units"] > 0 or r["order_storage_status"] == "unknown_lot":
                    unknown_lots.append({**base, "known_units": r["order_known_units"],
                                         "unknown_units": r["order_unknown_units"]})
        key = lambda x: (x["store"], x["msku"])  # noqa: E731
        return {
            "q_missing": sorted(q_missing, key=key),
            # 后端尺寸只取月仓储费报告，没有使用 Listing 包装尺寸的路径，因此这一组恒为空。
            "dims_from_listing": [],
            "dims_missing": sorted(dims_rows, key=key),
            "unknown_lots": sorted(unknown_lots, key=key),
        }

    def config_page(self) -> dict:
        saved = self.db.get(CONFIG_KEY)
        return {
            "saved": saved,
            "mode_labels": MODE_LABELS,
            "stores": self.readiness(),
            "review": self.review_lists(),
            "sign_check": self.db.get("sign_check") or {"status": "pending"},
            "price": self.engine.price_state(),
            "next_poll_at": self.db.get("next_poll_at"),
            "now": iso(self.engine.clock()),
        }

    def save_config(self, payload: dict) -> dict:
        mode = payload.get("mode")
        if mode not in (MODE_DAILY, MODE_ORDER):
            raise ConfigError("请选择仓储口径")
        cols = payload.get("columns")
        if not isinstance(cols, list) or len(cols) != COLUMNS:
            raise ConfigError("需要配置三列")
        stores = {s["sid"]: s for s in self.engine.stores()}
        seen: dict[int, str] = {}
        columns = []
        for i, c in enumerate(cols, 1):
            name = str((c or {}).get("name") or "").strip()
            if not name:
                raise ConfigError(f"第 {i} 列名字不能为空")
            if len(name) > NAME_MAX:
                raise ConfigError(f"第 {i} 列名字不超过 {NAME_MAX} 个字")
            sids = []
            for sid in c.get("sids") or []:
                sid = int(sid)
                if sid not in stores:
                    raise ConfigError(f"店铺 {sid} 不在四站店铺列表里")
                if sid in seen:
                    raise ConfigError(f"{stores[sid]['name']} 已在「{seen[sid]}」，一家店最多进一列")
                seen[sid] = name
                sids.append(sid)
            columns.append({"name": name, "sids": sids})

        ready = {r["sid"]: r for r in self.readiness()}
        for sid in seen:
            if not ready[sid]["ready"]:
                raise ConfigError(f"{stores[sid]['name']} 还没有本店今天的快照，等待{ready[sid]['waiting_for']}")

        if not payload.get("reviewed_dims"):
            raise ConfigError("请看完「尺寸来源」并勾选已查看")
        if not payload.get("reviewed_unknown_lots"):
            raise ConfigError("请看完「开账未知批次」并勾选已查看")

        review = self.review_lists()
        need = {r["key"] for r in review["q_missing"] if r["sid"] in seen}
        given = payload.get("q_overrides") or {}
        reviewed = set(payload.get("q_reviewed") or [])
        missing = sorted(need - reviewed)
        if missing:
            raise ConfigError(f"退货率还有 {len(missing)} 条没有逐条确认（列表可能刚随轮询更新，请重新核对）")
        overrides = dict(self.db.get("q_overrides") or {})
        for key in need:
            v = given.get(key)
            if not isinstance(v, dict) or v.get("kind") not in ("zero", "pct"):
                raise ConfigError(f"{key} 的退货率没有选择")
            if v["kind"] == "pct":
                try:
                    pct = float(v.get("pct"))
                except (TypeError, ValueError):
                    raise ConfigError(f"{key} 的退货率不是数字")
                if not 0 <= pct <= 100:
                    raise ConfigError(f"{key} 的退货率应在 0–100% 之间")
                overrides[key] = {"kind": "pct", "pct": round(pct, 4)}
            else:
                overrides[key] = {"kind": "zero"}

        changed = overrides != (self.db.get("q_overrides") or {})
        config = {"mode": mode, "columns": columns, "confirmed_at": iso(self.engine.clock())}
        with self.db.tx() as c:
            self.db.set("q_overrides", overrides, conn=c)
            self.db.set(CONFIG_KEY, config, conn=c)
        if changed:
            for s in self.engine.stores():
                self.engine.rebuild_latest_snapshot(s["sid"])
        return config

    # ------------------------------------------------------------------ 排行榜
    def leaderboard(self) -> dict:
        config = self.db.get(CONFIG_KEY)
        if not config:
            raise ConfigError("尚未确认配置")
        mode = config["mode"]
        hidden = self._sign_hidden()
        by_store = self._rows_by_store()
        names = {s["sid"]: s["name"] for s in self.engine.stores()}
        waiting = []
        columns = []
        for col in config["columns"]:
            rows = []
            for sid in col["sids"]:
                if sid not in by_store:
                    if sid in names:
                        st = next(r for r in self.readiness() if r["sid"] == sid)
                        waiting.append({"store": names[sid], "waiting_for": st.get("waiting_for", "")})
                    continue
                for r in by_store[sid][1]:
                    adj = r["adjusted"][mode]
                    if adj is None:
                        continue
                    rows.append({"sid": sid, "store": names[sid], "msku": r["msku"], "afn_volume": r["afn_volume"],
                                 "net_amount": r["net_amount"], "adjusted": adj})
            sales = sum(r["afn_volume"] for r in rows)
            profit = sum(r["adjusted"] for r in rows)
            net = sum(r["net_amount"] for r in rows)
            if hidden:
                rows.sort(key=lambda r: (-r["afn_volume"], r["store"], r["msku"]))
            else:
                rows.sort(key=lambda r: (-r["adjusted"], -r["afn_volume"], r["store"], r["msku"]))
            out_rows = [{"store": r["store"], "msku": r["msku"], "afn_volume": r["afn_volume"],
                         "adjusted": None if hidden else r["adjusted"],
                         "margin": None if hidden or not r["net_amount"] else r["adjusted"] / r["net_amount"]}
                        for r in rows]
            columns.append({
                "name": col["name"],
                "stores": [names.get(s, str(s)) for s in col["sids"]],
                "total": {"afn_volume": sales, "adjusted": None if hidden else profit,
                          "margin": None if hidden or not net else profit / net},
                "rows": out_rows,
            })
        snaps = [self.latest_today(s, by_store[s][0]["site"].code)["taken_at"] for s in by_store]
        return {
            "mode": mode,
            "mode_label": MODE_LABELS[mode],
            "confirmed_at": config["confirmed_at"],
            "adjusted_hidden": hidden,
            "columns": columns,
            "waiting": waiting,
            "latest_snapshot_at": max(snaps) if snaps else None,
            "next_poll_at": self.db.get("next_poll_at"),
            "now": iso(self.engine.clock()),
            "refresh_in_seconds": next_refresh_seconds(self.db.get("next_poll_at"), self.engine.clock(),
                                                       self.engine.cfg.poll_minutes),
        }


def next_refresh_seconds(next_poll_at: str | None, now: datetime, minutes: int = 15) -> float:
    """页面下一次拉取：下次轮询计划时刻后 90 秒；没有计划时 15 分钟后。"""
    if not next_poll_at:
        return minutes * 60.0
    target = datetime.fromisoformat(next_poll_at) + timedelta(seconds=90)
    return max(30.0, (target - now).total_seconds())
