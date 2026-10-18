#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄金云端播报 + 价格预警 + 历史记录

配置文件：仓库根目录 alerts.json
  {
    "intl": [{"direction":"above","price":4250},{"direction":"below","price":4100}],
    "cmbc": [{"direction":"above","price":900}],
    "config": {"window_start":"09:00","window_end":"23:50","interval_minutes":10}
  }

行为
  - 只在 config 指定的时间段内检查（默认 09:00–23:50 北京时间）
  - 按 interval_minutes 控制检查频率（cron 每 10 分钟触发一次，脚本自己节流）
  - 定时播报：09:00 / 11:30 / 15:00 / 20:00；周末只 09:30 播一次（带 😴）
  - 价格穿越阈值时立即推送（涨破 / 跌破，国际金价 + 民生积存金都支持）
  - 每次检查把国际金价追加到 docs/history.json，整点提交（每小时 1 个 commit）

环境变量：NTFY_TOPIC / NTFY_SERVER / NTFY_PRIORITY / FORCE
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
STATE_FILE = ROOT / "state" / "state.json"
ALERT_FILE = ROOT / "alerts.json"
HISTORY_FILE = ROOT / "docs" / "history.json"
DATA_DIR = ROOT / "data"
PRICE_FILE = DATA_DIR / "price.json"
ALERT_LOG_FILE = DATA_DIR / "alert-log.json"

TZ_CN = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

DEFAULT_CONFIG = {"window_start": "09:00", "window_end": "23:50", "interval_minutes": 10}
DIGEST_SLOTS = ["09:00", "11:30", "15:00", "20:00"]
DIGEST_WINDOW_MIN = 12
DIGEST_MIN_GAP_MIN = 45
WEEKEND_WINDOW = (9 * 60 + 15, 10 * 60 + 30)
HISTORY_KEEP_DAYS = 7          # 10 分钟级明细保留 7 天
HISTORY_DAILY_DAYS = 120       # 日线保留 120 天
HISTORY_POINT_SECONDS = 10 * 60


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def http_get(url, headers=None, timeout=20, retries=3):
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return r.read()
        except Exception as exc:          # noqa: BLE001
            last = exc
            if i < retries - 1:
                time.sleep(2)
    raise RuntimeError("%s 请求失败: %s" % (url, last))


def http_post_json(url, payload, timeout=20):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"User-Agent": UA, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read(1000).decode("utf-8", "ignore")


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:                 # noqa: BLE001
            pass
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def hhmm_minutes(text, fallback):
    try:
        h, m = str(text).split(":")
        return int(h) * 60 + int(m)
    except Exception:                     # noqa: BLE001
        return hhmm_minutes(fallback, "00:00")


# --------------------------------------------------------------------------- #
# 行情
# --------------------------------------------------------------------------- #
def fetch_intl():
    try:
        raw = http_get("https://hq.sinajs.cn/rn=%d&list=hf_XAU" % int(time.time() * 1000),
                       headers={"Referer": "https://finance.sina.com.cn"})
        body = raw.decode("gbk", "ignore").split('"', 2)[1]
        p = [x.strip() for x in body.split(",")]
        price = float(p[0])
        if price > 0:
            return {"price": price,
                    "pre_close": float(p[7]) if p[7] else None,
                    "high": float(p[4]) if p[4] else None,
                    "low": float(p[5]) if p[5] else None}
    except Exception as exc:              # noqa: BLE001
        print("新浪源失败: %s" % exc, file=sys.stderr)

    data = json.loads(http_get("https://api.gold-api.com/price/XAU").decode("utf-8", "ignore"))
    return {"price": float(data["price"]), "pre_close": None, "high": None, "low": None}


def fetch_cmbc():
    raw = http_get(
        "https://api.jdjygold.com/gw2/generic/produTools/h5/m/getGoldPrice?goldCode=CMBC-JCJ",
        headers={"Referer": "https://gold-price-pro.pf.jd.com/",
                 "Origin": "https://gold-price-pro.pf.jd.com"})
    d = json.loads(raw.decode("utf-8", "ignore")).get("resultData", {}).get("data", {})
    return {"name": d.get("name") or "民生银行积存金",
            "price": float(d["lastPrice"]),
            "pre_close": d.get("preClose"),
            "high": d.get("highPrice"), "low": d.get("lowPrice")}


# --------------------------------------------------------------------------- #
# 预警与播报判断
# --------------------------------------------------------------------------- #
def arrow_pct(value):
    if value is None:
        return "—"
    if value > 0:
        return "▲%.2f%%" % abs(value)
    if value < 0:
        return "▼%.2f%%" % abs(value)
    return "0.00%"


def pct(cur, base):
    if not base:
        return None
    return (cur - base) / base * 100.0


def detect_crossings(alerts, last_price, current_price):
    fired = []
    if last_price is None or current_price is None:
        return fired
    for item in alerts:
        t = item["price"]
        if item["direction"] == "above" and last_price < t <= current_price:
            fired.append(item)
        elif item["direction"] == "below" and last_price > t >= current_price:
            fired.append(item)
    return fired


def parse_alerts(raw):
    out = {}
    for market in ("intl", "cmbc"):
        items = []
        for item in raw.get(market) or []:
            try:
                direction = str(item.get("direction", "")).lower()
                price = float(item["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if direction in ("above", "below") and price > 0:
                items.append({"direction": direction, "price": price})
        out[market] = items
    out["config"] = dict(DEFAULT_CONFIG)
    for k, v in (raw.get("config") or {}).items():
        if k in DEFAULT_CONFIG:
            out["config"][k] = v
    try:
        out["config"]["interval_minutes"] = max(5, int(out["config"]["interval_minutes"]))
    except (TypeError, ValueError):
        out["config"]["interval_minutes"] = 10
    return out


def digest_due(now, state, forced):
    if forced:
        return True, "手动触发"
    if now.weekday() >= 5:
        minutes = now.hour * 60 + now.minute
        if WEEKEND_WINDOW[0] <= minutes <= WEEKEND_WINDOW[1]:
            return True, "周末 09:30 播报"
        return False, ""
    last = state.get("last_digest")
    if last:
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_CN)
            if (now - last_dt).total_seconds() < DIGEST_MIN_GAP_MIN * 60:
                return False, ""
        except ValueError:
            pass
    for slot in DIGEST_SLOTS:
        h, m = map(int, slot.split(":"))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if abs((now - target).total_seconds()) <= DIGEST_WINDOW_MIN * 60:
            return True, "定时播报 %s" % slot
    return False, ""


# --------------------------------------------------------------------------- #
# 历史记录
# --------------------------------------------------------------------------- #
def record_history(state, price, now, commit):
    """把 10 分钟价格点暂存到 state，提交时合并进 docs/history.json。"""
    ts = int(now.timestamp() // HISTORY_POINT_SECONDS * HISTORY_POINT_SECONDS)
    cutoff = int(now.timestamp()) - HISTORY_KEEP_DAYS * 86400
    pending = [p for p in (state.get("pending") or [])
               if isinstance(p, list) and len(p) == 2 and p[0] >= cutoff]
    if pending and pending[-1][0] == ts:
        pending[-1][1] = round(price, 2)
    else:
        pending.append([ts, round(price, 2)])
    state["pending"] = pending

    if not commit:
        return

    data = load_json(HISTORY_FILE, {})
    points = [p for p in (data.get("points") or [])
              if isinstance(p, list) and len(p) == 2 and p[0] >= cutoff]
    known = set(p[0] for p in points)
    for pt in pending:
        if pt[0] not in known:
            points.append(pt)
            known.add(pt[0])
    points.sort(key=lambda x: x[0])
    points = points[-2000:]

    daily = [d for d in (data.get("daily") or [])
             if isinstance(d, list) and len(d) == 5
             and d[0] >= int(now.timestamp()) - HISTORY_DAILY_DAYS * 86400]
    daily_map = {d[0]: d for d in daily}

    for ts_point, price_point in points:
        day_start = int((ts_point + 8 * 3600) // 86400 * 86400 - 8 * 3600)
        row = daily_map.get(day_start)
        if row is None:
            daily_map[day_start] = [day_start, price_point, price_point, price_point, price_point]
        else:
            row[2] = price_point
            row[3] = max(row[3], price_point)
            row[4] = min(row[4], price_point)
    daily = sorted(daily_map.values(), key=lambda x: x[0])

    save_json(HISTORY_FILE, {"updated": now.strftime("%Y-%m-%d %H:%M:%S"),
                             "points": points, "daily": daily})
    state["pending"] = []
    try:
        git_commit_history(now)
    except Exception as exc:              # noqa: BLE001
        print("历史提交失败: %s" % exc, file=sys.stderr)


def write_price(now, markets):
    """把最新报价写到 data/price.json，供网页直接读取。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    def pack(d):
        if not d:
            return None
        return {"price": round(float(d["price"]), 2),
                "pre_close": d.get("pre_close"),
                "high": d.get("high"),
                "low": d.get("low")}

    save_json(PRICE_FILE, {
        "updated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": int(now.timestamp()),
        "intl": pack(markets.get("intl")),
        "cmbc": pack(markets.get("cmbc")),
    })


def append_alert_log(now, fired, markets):
    """把已触发的预警追加到 data/alert-log.json（保留最近 500 条）。"""
    if not fired:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = load_json(ALERT_LOG_FILE, {"items": []})
    items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
    for market, item in fired:
        d = markets.get(market) or {}
        items.append({
            "ts": int(now.timestamp()),
            "market": market,
            "direction": item.get("direction"),
            "threshold": item.get("price"),
            "price": round(float(d["price"]), 2) if d.get("price") else None,
        })
    items.sort(key=lambda x: x.get("ts") or 0)
    items = items[-500:]
    save_json(ALERT_LOG_FILE, {"updated": now.strftime("%Y-%m-%d %H:%M:%S"),
                               "items": items})


def git_commit_data(now, message):
    """提交 data/ 目录（不在 docs 下，不会触发 Pages 重建）。"""
    def run(args):
        return subprocess.run(["git"] + args, cwd=str(ROOT),
                              capture_output=True, text=True)
    run(["config", "user.name", "gold-alert-bot"])
    run(["config", "user.email", "gold-alert-bot@users.noreply.github.com"])
    run(["add", "data"])
    if run(["diff", "--cached", "--quiet"]).returncode == 0:
        return
    run(["commit", "-m", message])
    push = None
    for _ in range(2):
        push = run(["push", "origin", "HEAD:main"])
        if push.returncode == 0:
            return
        run(["pull", "--rebase", "--autostash", "origin", "main"])
    raise RuntimeError((push.stderr or push.stdout or "push failed").strip()[:300])


def git_commit_history(now):
    def run(args):
        return subprocess.run(["git"] + args, cwd=str(ROOT), capture_output=True, text=True)
    run(["config", "user.name", "gold-alert-bot"])
    run(["config", "user.email", "gold-alert-bot@users.noreply.github.com"])
    run(["add", "docs/history.json"])
    if run(["diff", "--cached", "--quiet"]).returncode == 0:
        return
    run(["commit", "-m", "data: gold history %s" % now.strftime("%Y-%m-%d %H:%M")])
    for attempt in range(2):
        push = run(["push", "origin", "HEAD:main"])
        if push.returncode == 0:
            return
        run(["pull", "--rebase", "--autostash", "origin", "main"])
    raise RuntimeError((push.stderr or push.stdout).strip()[:300])


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                     # noqa: BLE001
        pass

    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        print("缺少环境变量 NTFY_TOPIC", file=sys.stderr)
        return 2
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    priority = int(os.environ.get("NTFY_PRIORITY") or 4)

    now = datetime.now(TZ_CN)
    forced = (os.environ.get("FORCE") == "1"
              or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch")
    is_weekend = now.weekday() >= 5

    raw_alerts = load_json(ALERT_FILE, {})
    alerts = parse_alerts(raw_alerts)
    cfg = alerts["config"]
    state = load_json(STATE_FILE, {})

    # 时间段判断
    start = hhmm_minutes(cfg.get("window_start"), "09:00")
    end = hhmm_minutes(cfg.get("window_end"), "23:50")
    minutes = now.hour * 60 + now.minute
    in_window = start <= minutes <= end if start <= end else (minutes >= start or minutes <= end)
    if not in_window and not forced:
        try:
            quiet = {}
            try:
                quiet["intl"] = fetch_intl()
            except Exception:             # noqa: BLE001
                pass
            try:
                quiet["cmbc"] = fetch_cmbc()
            except Exception:             # noqa: BLE001
                pass
            if quiet:
                write_price(now, quiet)
                git_commit_data(now, "data: price %s" % now.strftime("%m-%d %H:%M"))
        except Exception as exc:          # noqa: BLE001
            print("价格写入失败: %s" % exc, file=sys.stderr)
        print("不在提醒时间段 %s-%s 内，跳过（%s）"
              % (cfg.get("window_start"), cfg.get("window_end"), now.strftime("%H:%M")))
        return 0

    # 频率节流（定时播报不受影响）
    digest_ok, digest_reason = digest_due(now, state, forced)
    last_check = state.get("last_check")
    due = forced or not last_check
    if not due:
        try:
            last_dt = datetime.strptime(last_check, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_CN)
            due = (now - last_dt).total_seconds() >= int(cfg["interval_minutes"]) * 60
        except ValueError:
            due = True
    if not due and not digest_ok:
        print("距上次检查不足 %s 分钟，跳过（%s）" % (cfg["interval_minutes"], now.strftime("%H:%M")))
        return 0

    # 抓价
    markets = {}
    errors = []
    try:
        markets["intl"] = fetch_intl()
    except Exception as exc:              # noqa: BLE001
        errors.append("国际金价: %s" % exc)
    try:
        markets["cmbc"] = fetch_cmbc()
    except Exception as exc:              # noqa: BLE001
        errors.append("民生积存金: %s" % exc)
    if not markets:
        print("两个数据源都失败: " + "; ".join(errors), file=sys.stderr)
        return 3

    # 预警
    fired = []
    for market in ("intl", "cmbc"):
        cur = markets.get(market, {}).get("price")
        last = state.get(market + "_last")
        for item in detect_crossings(alerts.get(market) or [], last, cur):
            fired.append((market, item))

    # 播报内容
    price_lines, parts = [], []
    if markets.get("intl"):
        i = markets["intl"]
        price_lines.append("国际  %s 美元/盎司  %s"
                           % (format(i["price"], ",.2f"), arrow_pct(pct(i["price"], i.get("pre_close")))))
        parts.append("国际 %s %s" % (format(i["price"], ",.2f"),
                                     arrow_pct(pct(i["price"], i.get("pre_close")))))
    else:
        price_lines.append("国际  ⚠️ 获取失败")
    if markets.get("cmbc"):
        c = markets["cmbc"]
        price_lines.append("民生  %s 元/克  %s"
                           % (format(c["price"], ",.2f"), arrow_pct(pct(c["price"], c.get("pre_close")))))
        parts.append("民生 %s %s" % (format(c["price"], ",.2f"),
                                     arrow_pct(pct(c["price"], c.get("pre_close")))))
    else:
        price_lines.append("民生  ⚠️ 获取失败")

    should_push = bool(fired) or digest_ok

    try:
        write_price(now, markets)
        append_alert_log(now, fired, markets)
        git_commit_data(now, "data: price %s" % now.strftime("%m-%d %H:%M"))
    except Exception as exc:              # noqa: BLE001
        print("数据提交失败: %s" % exc, file=sys.stderr)

    # 距离上次历史提交超过 55 分钟就落一次盘（不依赖整点）
    last_hist = state.get("last_hist_commit")
    history_commit = forced
    if not history_commit:
        if not last_hist:
            history_commit = True
        else:
            try:
                last_dt = datetime.strptime(last_hist, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_CN)
                history_commit = (now - last_dt).total_seconds() >= 55 * 60
            except ValueError:
                history_commit = True
    if markets.get("intl"):
        record_history(state, markets["intl"]["price"], now, history_commit)
        if history_commit:
            state["last_hist_commit"] = now.strftime("%Y-%m-%d %H:%M:%S")

    if should_push:
        if fired:
            title = "⚡ 金价预警 · " + " · ".join(parts)
            body = []
            for market, item in fired:
                data = markets.get(market)
                if not data:
                    continue
                name = "国际金价" if market == "intl" else "民生积存金"
                unit = "美元/盎司" if market == "intl" else "元/克"
                verb = "涨破" if item["direction"] == "above" else "跌破"
                body.append("⚡ %s%s %s %s（现价 %s）"
                            % (name, verb, format(item["price"], ",.2f"), unit,
                               format(data["price"], ",.2f")))
            body.append("")
            body.extend(price_lines)
        else:
            title = "黄金速报 %s · %s" % (now.strftime("%H:%M"), " · ".join(parts))
            body = list(price_lines)
        if is_weekend and (digest_reason.startswith("周末") or forced):
            body.append("周末休息😴")
        if errors:
            body.append("｜".join(errors))
        message = "\n".join(body)
        resp = http_post_json(server + "/", {
            "topic": topic, "title": title, "message": message,
            "priority": 5 if fired else priority,
            "tags": ["rotating_light"] if fired else ["moneybag"]})
        print("已推送(%s): %s" % (digest_reason if digest_ok and not fired else "价格预警",
                                  message.replace("\n", " / ")))
        print("服务端返回: " + resp[:300])
    else:
        print("无预警，且不在播报时段（%s）" % now.strftime("%H:%M"))

    # 状态更新
    state["intl_last"] = markets.get("intl", {}).get("price")
    state["cmbc_last"] = markets.get("cmbc", {}).get("price")
    state["last_check"] = now.strftime("%Y-%m-%d %H:%M:%S")
    if digest_ok and not fired:
        state["last_digest"] = now.strftime("%Y-%m-%d %H:%M:%S")
    save_json(STATE_FILE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())