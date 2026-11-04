# lingxing-play

领星（Lingxing）美国、加拿大、墨西哥、巴西四站亚马逊店铺的**当日调整后利润看板**。所有计算都在服务端完成，数据存放在本地 SQLite；网页只读展示。页面上唯一的写入操作是「确认启用新版本亚马逊价目表」。

## 运行

```bash
pip install -r requirements.txt
cp .env.example .env        # 填 LINGXING_APP_ID / LINGXING_APP_SECRET
python3 -m lingxing_dashboard
# 打开 http://127.0.0.1:8765/
```

不连领星、使用内置假数据：

```bash
python3 -m lingxing_dashboard --demo
```

测试：

```bash
python3 -m unittest discover -s tests -t .
```

## 配置（`.env` 或环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LINGXING_APP_ID` / `LINGXING_APP_SECRET` | — | 领星开放平台凭证 |
| `LINGXING_BASE_URL` | `https://openapi.lingxing.com` | |
| `DASHBOARD_DB` | `data/dashboard.sqlite3` | 本地库 |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | `127.0.0.1` / `8765` | |
| `DAILY_LOCAL_TIME` | `06:00` | 日任务（以及每月 1 日的月任务）在各店站点本地时间几点运行 |
| `COST_SIGN` | `negative` | `gross_profit` 中成本字段的符号。符号验收不通过时改成 `positive` 并重启，系统会重新验收 |
| `ORDER_PROFIT_DATETIME` | `1` | OrderProfit 的 `startDate`/`endDate` 带时分秒；设为 `0` 只传日期 |
| `FIFO_CLOSE_PREVIOUS_DAY` | `1` | 每店进入新的一天、做首轮轮询前，补请求一次上一自然日全天的 OrderProfit，只用于消化队列尾差 |

## 结构

| 模块 | 内容 |
| --- | --- |
| `lingxing_dashboard/lingxing/` | 领星 OpenAPI 客户端：取 token / 刷新，AES 签名，同一路径串行请求，限流等待重试，offset 翻页 |
| `jobs.py` | 三个调度各自的任务：月任务、日任务（含开账）、15 分钟轮询 |
| `scheduler.py` | 轮询一个线程，日/月任务一个串行线程；上一轮未完成则跳过并记录 |
| `calc.py` | 预计退货成本、两种仓储费、调整后毛利、店铺构成、符号验收 |
| `fifo.py` | 先入先出队列（开账、签收、冲减、消化） |
| `pricing.py` | 价目表（带版本号、生效日期）与费用计算 |
| `refdata.py` | 尺寸选择、fnsku 映射、汇率、退货率、库龄桶 |
| `web.py` + `static/` | 本地网页与只读 JSON 接口 |
| `demo.py` | 演示 / 测试用的假领星后端（按真实路径返回数据并校验签名） |

## 口径说明

以下几处是需求没有写死、由实现选定的做法：

- **退货率 Q 的请求**：每店请求一次 OrderProfit，窗口为站点今天往前第 395 天到往前第 30 天。OrderProfit 本身就按 sid + msku 汇总，所以每个 msku 拿到的是这一整段窗口的 `return_rate`，和逐个 msku 请求得到的值相同，但请求次数少得多。
- **库龄附加费按桶取档**：日模式下 181–270、271–365 两个桶按「桶下限所在档」取费率（分别是 181–210 档和 271–300 档）；365+ 取 365 天以上第一档。订单仓储费按每一天的实际库龄逐档计算。
- **只在库存里、今天没有 OrderProfit 行的 msku**：也计入「今日已过时段的在库仓储」，它的调整后毛利就是负的仓储费。
- **预计退货成本的特例**：`net_amount` 和 `afn_volume` 都为 0 时结果为 0，即使这个 msku 没有 Q。
- **签收入账**：`received_date_report` 带时间时，要求晚于 T0；只有日期时，要求日期晚于 T0 所在日。签收行按内容去重，重复拉取同一天不会重复入账。
- **队列尾差**：`afn_volume` 是当天累计值，最后一轮轮询到午夜之间的销量会漏掉。所以每店进入新的一天时，会补请求一次上一自然日全天的数据，只用于消化队列（可用 `FIFO_CLOSE_PREVIOUS_DAY=0` 关闭）。
- **符号验收**：每店取一个原退款/原仓储不为 0 的 msku 手算，任何一店不通过就判定失败，并隐藏全部调整后毛利。
- **仓储模式开关**：在浏览器本地切换，不写服务端。两种模式的结果每次轮询都会同时算好。
- **订单仓储费的历史日**：尺寸按当天所在月份取最近一版月仓储费报告；费率统一用当前已确认的价目版本。

## 价目表

价目表写在 `pricing.py` 里。代码内置两个版本：

- `2025.1`：初始生效版本；
- `2026.1`：已写入代码，但需要在看板上人工确认后才会启用。美国 2026 年标准件 0–0.5 lb 弃置费降为 $0.84，366–455 天和 456 天以上的库龄附加费分两档。

以下几段没能和亚马逊官方页逐项核对（`verified=False`），看板会显示提示：

- 加拿大库龄附加费：取自第三方整理，其中 301–330 天这一档是插值；
- 加拿大弃置费：只找到区间 CAD 0.30–3.53 + 0.59/kg（超 5 kg 部分），分档金额是估计值；
- 墨西哥弃置费：官方只给出平均 MXN 13.00/件。

**正式使用前请核对这几行。**
