#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄金价格云端播报：抓取国际金价 + 民生积存金，推送到 ntfy。

无需任何第三方依赖（仅标准库），适配 GitHub Actions / 腾讯云函数 / 任意 Linux。
环境变量：
  NTFY_TOPIC   必填，ntfy 主题名
  NTFY_SERVER  可选，默认 https://ntfy.sh
  NTFY_PRIORITY 可选，默认 4（高优先级，手机横幅弹出）
  FORCE        可选，设为 1 时忽略周末判断
"""
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

TZ_CN = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
OZ_IN_GRAM = 31.1034768
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def http_get(url, headers=None, timeout=20, retries=3):
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                return resp.read()
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
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return resp.read(1000).decode("utf-8", "ignore")


def fetch_intl():
    """国际现货黄金：新浪伦敦金优先，gold-api 兜底"""
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


def arrow_pct(value):
    if value is None:
        return "—"
    if value > 0:
        return "▲%.2f%%" % abs(value)
    if value < 0:
        return "▼%.2f%%" % abs(value)
    return "0.00%"


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
    is_weekend = now.weekday() >= 5
    forced = (os.environ.get("FORCE") == "1"
              or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch")
    if is_weekend and not forced:
        minutes = now.hour * 60 + now.minute
        if not (9 * 60 + 15 <= minutes <= 10 * 60 + 30):
            print("周末只播报 09:30 一次，当前不在播报窗口，跳过")
            return 0

    intl = cmbc = None
    errors = []
    try:
        intl = fetch_intl()
    except Exception as exc:              # noqa: BLE001
        errors.append("国际金价: %s" % exc)
    try:
        cmbc = fetch_cmbc()
    except Exception as exc:              # noqa: BLE001
        errors.append("民生积存金: %s" % exc)

    if not intl and not cmbc:
        print("两个数据源都失败: " + "; ".join(errors), file=sys.stderr)
        return 3

    lines = []
    parts = []
    if intl:
        pct = ((intl["price"] - intl["pre_close"]) / intl["pre_close"] * 100
               if intl.get("pre_close") else None)
        lines.append("国际  %s 美元/盎司  %s" % (format(intl["price"], ",.2f"),
                                                 arrow_pct(pct)))
        parts.append("国际 %s %s" % (format(intl["price"], ",.2f"), arrow_pct(pct)))
    else:
        lines.append("国际  ⚠️ 获取失败")
    if cmbc:
        pct = ((cmbc["price"] - cmbc["pre_close"]) / cmbc["pre_close"] * 100
               if cmbc.get("pre_close") else None)
        lines.append("民生  %s 元/克  %s" % (format(cmbc["price"], ",.2f"),
                                             arrow_pct(pct)))
        parts.append("民生 %s %s" % (format(cmbc["price"], ",.2f"), arrow_pct(pct)))
    else:
        lines.append("民生  ⚠️ 获取失败")

    title = "黄金速报 %s · %s" % (now.strftime("%H:%M"), " · ".join(parts))
    message = "\n".join(lines)
    if errors:
        message += "\n" + "｜".join(errors)
    if is_weekend:
        message += "\n周末休息😴"

    resp = http_post_json(server + "/", {
        "topic": topic, "title": title, "message": message,
        "priority": priority, "tags": ["moneybag"]})
    print("已推送: " + message.replace("\n", " / "))
    print("服务端返回: " + resp[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())