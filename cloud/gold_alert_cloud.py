#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄金云端播报 + 价格预警

功能
  1. 定时播报：工作日 09:00 / 11:30 / 15:00 / 20:00；周末只在 09:30 播一次（带 😴）
  2. 价格预警：国际金价 / 民生积存金 穿越设定阈值时立即推送（涨破、跌破两种方向）
     高频 cron 每 10 分钟跑一次，非播报时段只在触发预警时才推送。

阈值配置：仓库根目录 alerts.json
  {
    "intl": [
      {"direction": "above", "price": 4200.0, "note": "涨破 4200"},
      {"direction": "below", "price": 4100.0, "note": "跌破 4100"}
    ],
    "cmbc": []
  }
  direction: above = 涨破，below = 跌破

状态（用于判断是否真的"穿越"，并避免重复播报）：state/state.json
  由 GitHub Actions 的 cache 在新旧两次运行之间传递。

环境变量
  NTFY_TOPIC     必填
  NTFY_SERVER    默认 https://ntfy.sh
  NTFY_PRIORITY  默认 4
  FORCE          1 = 强制播报（手动触发时自动生效）
"""
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
STATE_FILE = ROOT / "state" / "state.json"
ALERT_FILE = ROOT / "alerts.json"

TZ_CN = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

DIGEST_SLOTS = ["09:00", "11:30", "15:00", "20:00"]
DIGEST_WINDOW_MIN = 12
WEEKEND_WINDOW = (9 * 60 + 15, 10 * 60 + 30)
DIGEST_MIN_GAP_MIN = 45          # 防止同一时段被高频 cron 重复触发


# --------------------------------------------------------------------------- #
# 网络
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


def fetch_intl():
    """国际现货黄金 XAU/USD"""
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
                    "low": float(p[5]) if p[5] else None,
                    "ts": (p[12] + " " + p[6]) if len(p) > 12 and p[12] else ""}
    except Exception as exc:              # noqa: BLE001
        print("新浪源失败: %s" % exc, file=sys.stderr)

    data = json.loads(http_get("https://api.gold-api.com/price/XAU").decode("utf-8", "ignore"))
    return {"price": float(data["price"]), "pre_close": None,
            "high": None, "low": None, "ts": data.get("updatedAt", "")}


def fetch_cmbc():
    """民生银行积存金"""
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
# 状态与配置
# --------------------------------------------------------------------------- #
def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:                 # noqa: BLE001
            pass
    return default


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def load_alerts():
    data = load_json(ALERT_FILE, {})
    out = {"intl": [], "cmbc": []}
    for market in out:
        for item in data.get(market) or []:
            try:
                direction = str(item.get("direction", "")).lower()
                price = float(item["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if direction in ("above", "below") and price > 0:
                out[market].append({"direction": direction, "price": price,
                                    "note": item.get("note") or ""})
    return out


# --------------------------------------------------------------------------- #
# 判断
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
    """返回本次跨越的阈值列表；last_price 为 None 时只建立基线不触发"""
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


def build_alert_lines(fired, markets):
    lines = []
    for market, item in fired:
        data = markets.get(market)
        if not data:
            continue
        if market == "intl":
            name, unit = "国际金价", "美元/盎司"
        else:
            name, unit = "民生积存金", "元/克"
        verb = "涨破" if item["direction"] == "above" else "跌破"
        lines.append("⚡ %s%s %s %s（现价 %s）"
                     % (name, verb, format(item["price"], ",.2f"), unit,
                        format(data["price"], ",.2f")))
    return lines


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

    state = load_json(STATE_FILE, {})
    alerts = load_alerts()
    print("上次基线: intl=%s cmbc=%s last_digest=%s"
          % (state.get("intl_last"), state.get("cmbc_last"), state.get("last_digest")))

    # ---------- 抓价 ----------
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

    # ---------- 预警 ----------
    fired = []
    for market in ("intl", "cmbc"):
        cur = markets.get(market, {}).get("price")
        last = state.get(market + "_last")
        for item in detect_crossings(alerts.get(market) or [], last, cur):
            fired.append((market, item))

    # ---------- 播报内容 ----------
    price_lines = []
    parts = []
    intl = markets.get("intl")
    cmbc = markets.get("cmbc")
    if intl:
        price_lines.append("国际  %s 美元/盎司  %s"
                           % (format(intl["price"], ",.2f"),
                              arrow_pct(pct(intl["price"], intl.get("pre_close")))))
        parts.append("国际 %s %s" % (format(intl["price"], ",.2f"),
                                     arrow_pct(pct(intl["price"], intl.get("pre_close")))))
    else:
        price_lines.append("国际  ⚠️ 获取失败")
    if cmbc:
        price_lines.append("民生  %s 元/克  %s"
                           % (format(cmbc["price"], ",.2f"),
                              arrow_pct(pct(cmbc["price"], cmbc.get("pre_close")))))
        parts.append("民生 %s %s" % (format(cmbc["price"], ",.2f"),
                                     arrow_pct(pct(cmbc["price"], cmbc.get("pre_close")))))
    else:
        price_lines.append("民生  ⚠️ 获取失败")

    digest_ok, digest_reason = digest_due(now, state, forced)
    should_send = digest_ok or bool(fired)
    if not should_send:
        print("无预警触发，且不在播报时段，跳过（%s）" % now.strftime("%H:%M"))
        # 仍然更新基线，保证下次能判断"穿越"
        state["intl_last"] = markets.get("intl", {}).get("price")
        state["cmbc_last"] = markets.get("cmbc", {}).get("price")
        state["updated"] = now.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        return 0

    # ---------- 组装消息 ----------
    if fired:
        title = "⚡ 金价预警 · " + " · ".join(parts)
        body_lines = build_alert_lines(fired, markets) + [""] + price_lines
    else:
        title = "黄金速报 %s · %s" % (now.strftime("%H:%M"), " · ".join(parts))
        body_lines = price_lines

    if self_is_weekend := is_weekend:
        if digest_reason.startswith("周末") or forced:
            body_lines.append("周末休息😴")
    if errors:
        body_lines.append("｜".join(errors))

    message = "\n".join(body_lines)

    resp = http_post_json(server + "/", {
        "topic": topic, "title": title, "message": message,
        "priority": 5 if fired else priority,
        "tags": ["rotating_light"] if fired else ["moneybag"]})
    print("已推送(%s): %s" % (digest_reason if digest_ok else "价格预警",
                              message.replace("\n", " / ")))
    print("服务端返回: " + resp[:300])

    # ---------- 更新状态 ----------
    state["intl_last"] = markets.get("intl", {}).get("price")
    state["cmbc_last"] = markets.get("cmbc", {}).get("price")
    if digest_ok and not fired:
        state["last_digest"] = now.strftime("%Y-%m-%d %H:%M:%S")
    state["updated"] = now.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())