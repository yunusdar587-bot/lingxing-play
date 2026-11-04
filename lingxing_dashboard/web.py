"""本地网页看板：只读 JSON 接口 + 唯一写入操作（确认启用新版本价目表）。"""
from __future__ import annotations

import json
import logging
import mimetypes
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .calc import MODE_DAILY, MODE_LABELS, MODE_ORDER
from .jobs import Engine
from .sites import SITES
from .timeutil import iso, local_now

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

REASON_LABELS = {
    "no_q": "无退货率 Q",
    "no_age": "无库龄",
    "no_volume": "无体积",
    "no_rate": "价目表无匹配行",
    "no_fx": "无汇率",
    "unknown_lot": "含开账未知批次",
    "no_t0": "尚未开账",
}


def _hide_adjusted(summary: dict) -> None:
    for mode in (MODE_DAILY, MODE_ORDER):
        m = summary.get(mode)
        if m:
            m["adjusted"] = None
            m["margin"] = None
            for s in m["steps"]:
                if s["key"] in ("return_cost", "storage"):
                    s["amount"] = None
            m["steps_total"] = None


class DashboardService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.db = engine.db

    def _sign(self) -> dict:
        return self.db.get("sign_check") or {"status": "pending", "cost_sign": self.engine.cfg.cost_sign}

    def _snapshots_today(self, sid: int, site_code: str) -> list:
        today = local_now(SITES[site_code].tz, self.engine.clock()).date().isoformat()
        return self.db.query("SELECT id, taken_at, summary FROM snapshots WHERE sid=? AND local_date=? ORDER BY id",
                             (sid, today))

    def dashboard(self) -> dict:
        sign = self._sign()
        hide = sign.get("status") == "failed"
        stores, notices = [], []
        totals = {MODE_DAILY: 0.0, MODE_ORDER: 0.0}
        total_tz = []
        for s in self.engine.stores():
            site = s["site"]
            snaps = self._snapshots_today(s["sid"], site.code)
            entry = {"sid": s["sid"], "name": s["name"], "site": site.code, "site_name": site.name,
                     "tz": site.tz_name, "local_date": local_now(site.tz, self.engine.clock()).date().isoformat(),
                     "snapshot": None}
            if snaps:
                cur = json.loads(snaps[-1]["summary"])
                prev = json.loads(snaps[-2]["summary"]) if len(snaps) > 1 else None
                modes = {}
                for mode in (MODE_DAILY, MODE_ORDER):
                    m = cur[mode]
                    delta = None
                    if prev is not None and prev[mode]["adjusted"] is not None and m["adjusted"] is not None:
                        delta = m["adjusted"] - prev[mode]["adjusted"]
                    modes[mode] = {"adjusted": None if hide else m["adjusted"],
                                   "margin": None if hide else m["margin"],
                                   "delta": None if hide else delta,
                                   "excluded": m["excluded"], "excluded_reasons": m["excluded_reasons"],
                                   "fallback_to_daily": m.get("fallback_to_daily", False),
                                   "order_unready_mskus": m.get("order_unready_mskus")}
                    if m["adjusted"] is not None:
                        totals[mode] += m["adjusted"]
                entry["snapshot"] = {"taken_at": snaps[-1]["taken_at"], "local_time": cur["local_time"],
                                     "modes": modes, "msku_count": cur["msku_count"], "no_q": cur["no_q_mskus"]}
                total_tz.append({"name": s["name"], "site": site.name, "tz": site.tz_name,
                                 "local_date": cur["local_date"],
                                 "counted": {k: cur[k]["adjusted"] is not None for k in (MODE_DAILY, MODE_ORDER)}})
                notices.extend(self._store_notices(s, cur))
            else:
                notices.append({"level": "warn", "kind": "no_snapshot", "sid": s["sid"],
                                "text": f"{s['name']}：本店今天（{site.tz_name}）尚无快照"})
            stores.append(entry)
        price = self.engine.price_state()
        notices.extend(self._global_notices(price, sign))
        return {
            "generated_at": iso(self.engine.clock()),
            "mode_labels": MODE_LABELS,
            "sign_check": sign,
            "adjusted_hidden": hide,
            "stores": stores,
            "total": {"amounts": None if hide else totals, "currency": "CNY", "stores": total_tz},
            "price": price,
            "notices": notices,
            "polls": [dict(r) for r in self.db.query("SELECT * FROM poll_runs ORDER BY id DESC LIMIT 12")],
            "anomalies": [dict(r) for r in self.db.query("SELECT * FROM anomalies ORDER BY id DESC LIMIT 30")],
            "reason_labels": REASON_LABELS,
        }

    def _store_notices(self, s: dict, cur: dict) -> list[dict]:
        name = s["name"]
        out = []
        sid = s["sid"]
        if cur["unmapped_fnsku_count"]:
            out.append({"level": "warn", "kind": "unmapped", "sid": sid,
                        "text": f"{name}：{cur['unmapped_fnsku_count']} 个 fnsku 未映射到 msku，对应单件弃置费按 0 显示"
                                f"（{', '.join(cur['unmapped_fnskus'][:5])}{' …' if cur['unmapped_fnsku_count'] > 5 else ''}）"})
        if cur["storage_report_pending"]:
            if cur["dims_history"]:
                out.append({"level": "warn", "kind": "storage_report", "sid": sid,
                            "text": f"{name}：上月仓储费报告未出，沿用 {cur['dims_month']} 的重量、体积和弃置费，每天重试"})
            else:
                out.append({"level": "warn", "kind": "storage_report", "sid": sid,
                            "text": f"{name}：上月仓储费报告未出，尚无历史尺寸，弃置费显示 0"})
        if cur["age_snapshot_date"] is None and not cur.get("age_fetched_at"):
            out.append({"level": "warn", "kind": "age", "sid": sid, "text": f"{name}：尚无库龄快照"})
        elif cur["age_stale"]:
            out.append({"level": "warn", "kind": "age", "sid": sid,
                        "text": f"{name}：库龄快照日期 {cur['age_snapshot_date'] or '未知'} 早于昨天，已标记过期，仍照常使用"})
        order = cur[MODE_ORDER]
        if order.get("fallback_to_daily"):
            out.append({"level": "info", "kind": "order_storage", "sid": sid,
                        "text": f"{name}：订单仓储未就绪（今日售出件数含开账未知批次 {order['order_unknown_units']} 件），"
                                f"该模式下毛利仍按每日仓储费计算"})
        elif order.get("order_unready_mskus"):
            out.append({"level": "info", "kind": "order_storage", "sid": sid,
                        "text": f"{name}：订单仓储未就绪 {order['order_unready_mskus']} 个 msku，未计入合计"})
        fx = cur.get("fx")
        if fx:
            out.append({"level": "warn" if fx["fallback"] else "info", "kind": "fx", "sid": sid,
                        "text": f"{name}：汇率 {fx['code']}→CNY {fx['rate']:.4f}，月份 {fx['month']}（{fx['source']}）"
                                + ("，当月未发布，使用最近已发布月份" if fx["fallback"] else "")})
        else:
            out.append({"level": "warn", "kind": "fx", "sid": sid, "text": f"{name}：没有可用汇率，仓储费和弃置费未就绪"})
        qw = cur.get("q_window")
        if qw:
            out.append({"level": "info", "kind": "q", "sid": sid,
                        "text": f"{name}：Q 统计窗口 {qw['start']} ~ {qw['end']}，更新于 {qw['computed_at']}"
                                + (f"；{cur['no_q_mskus']} 个 msku 无 Q，未计入合计" if cur["no_q_mskus"] else "")})
        else:
            out.append({"level": "warn", "kind": "q", "sid": sid, "text": f"{name}：退货率 Q 尚未计算成功"})
        return out

    def _global_notices(self, price: dict, sign: dict) -> list[dict]:
        a = price["active"]
        out = [{"level": "info", "kind": "price",
                "text": f"当前价目版本 {a['version']}，生效日期 {a['effective_date']}"
                        + (f"，确认于 {a['confirmed_at']}" if a.get("confirmed_at") else "（初始版本）")}]
        for p in price["pending"]:
            out.append({"level": "warn", "kind": "price",
                        "text": f"新版本 {p['version']}（生效 {p['effective_date']}）已写入代码，待人工确认后启用"})
        for u in price["unverified"]:
            out.append({"level": "warn", "kind": "price",
                        "text": f"价目表 {u['country']} {u['section']} 未逐项核对官方页：{u['source']}"})
        if sign.get("status") == "failed":
            out.append({"level": "error", "kind": "sign",
                        "text": f"符号验收未通过（msku {sign.get('msku')}）：加回原退款和原仓储后利润没有按费用量级上升，"
                                f"调整后毛利暂不展示，请核对 COST_SIGN"})
        elif sign.get("status") == "pending":
            out.append({"level": "warn", "kind": "sign", "text": "符号验收等待样本：还没有原退款或原仓储不为 0 的 msku"})
        since = iso(self.engine.clock() - timedelta(hours=24))
        skipped = self.db.one("SELECT COUNT(*) n FROM poll_runs WHERE status='skipped' AND scheduled_at>=?", (since,))
        if skipped and skipped["n"]:
            out.append({"level": "warn", "kind": "poll", "text": f"近 24 小时有 {skipped['n']} 次轮询因上一轮未完成而跳过"})
        return out

    def store_detail(self, sid: int) -> dict | None:
        st = self.engine.store(sid)
        if not st:
            return None
        sign = self._sign()
        hide = sign.get("status") == "failed"
        snaps = self._snapshots_today(sid, st["site"].code)
        if not snaps:
            return {"sid": sid, "name": st["name"], "summary": None, "rows": [], "curve": [], "sign_check": sign}
        latest = self.db.one("SELECT summary, rows FROM snapshots WHERE id=?", (snaps[-1]["id"],))
        summary = json.loads(latest["summary"])
        rows = json.loads(latest["rows"] or "[]")
        curve = []
        for r in snaps:
            sm = json.loads(r["summary"])
            curve.append({"taken_at": r["taken_at"], "local_time": sm["local_time"],
                          MODE_DAILY: None if hide else sm[MODE_DAILY]["adjusted"],
                          MODE_ORDER: None if hide else sm[MODE_ORDER]["adjusted"]})
        if hide:
            _hide_adjusted(summary)
            for row in rows:
                row["adjusted"] = {MODE_DAILY: None, MODE_ORDER: None}
        return {"sid": sid, "name": st["name"], "summary": summary, "rows": rows, "curve": curve,
                "sign_check": sign, "adjusted_hidden": hide}


def make_handler(service: DashboardService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LingxingDashboard/1.0"

        def log_message(self, fmt, *args):  # noqa: N802
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _json(self, payload, status=HTTPStatus.OK):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, rel: str):
            path = (STATIC / rel).resolve()
            if STATIC.resolve() not in path.parents or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/"):])
                if path == "/api/dashboard":
                    return self._json(service.dashboard())
                if path.startswith("/api/store/"):
                    detail = service.store_detail(int(path.rsplit("/", 1)[1]))
                    return self._json(detail) if detail else self._json({"error": "店铺不存在"}, HTTPStatus.NOT_FOUND)
            except ValueError:
                return self._json({"error": "参数错误"}, HTTPStatus.BAD_REQUEST)
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/api/price-table/confirm":
                return self._json({"error": "只读看板，不支持该写入"}, HTTPStatus.METHOD_NOT_ALLOWED)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                state = service.engine.confirm_price(str(payload.get("version") or ""))
            except ValueError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return self._json({"ok": True, "price": state})

    return Handler


def serve(service: DashboardService, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    log.info("看板地址 http://%s:%d/", host, port)
    return httpd
