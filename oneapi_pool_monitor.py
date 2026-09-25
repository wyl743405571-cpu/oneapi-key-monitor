#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One API / LiteLLM pool + customer-key availability monitor (generic template).

WHAT IT DOES
    Two independent checks, because a health check that only watches your web app
    will miss the dependency that actually breaks customers:
      1. POOL QUOTA  — the billing/pool account's remaining quota (the money well).
      2. LIVE PROBE  — picks a real customer key and makes an actual chat completion
                       call. This catches ANY failure mode (empty pool, bad key,
                       downstream 5xx), not just the one you guessed.

    On 2026-09-18 a solo operator's pool hit zero and 19 customer keys returned 403
    for ~45h silently. This script is the fix.

    NOTE: the live probe sends a 1-token call on a REAL customer key. It costs ~0
    quota, but make sure PROBE_MODEL is enabled for that key — otherwise the probe
    returns 403/404 and you get a FALSE alarm for a healthy key.

CONFIGURE (edit the block below, or use env vars)
    ONEAPI_DB        path to one-api.db (SQLite) or set ONEAPI_MYSQL_DSN for MySQL
    ONEAPI_URL       base URL, e.g. http://127.0.0.1:3000
    POOL_USER_ID     the user id whose quota is the shared pool
    ALERT_WEBHOOK    any inbound webhook (Slack/Discord/Telegram/Feishu). Empty = print only.
    WARN_QUOTA / CRIT_QUOTA   pool thresholds (in One API quota units; 500000 = $1)
    COOLDOWN_SEC     suppress repeat alerts within this window
    PROBE_MODEL      model used for the live probe (must be enabled on the sampled key)

USAGE
    python3 oneapi_pool_monitor.py            # check + alert
    python3 oneapi_pool_monitor.py --dry-run  # print only, no alert
    python3 oneapi_pool_monitor.py --test     # send one test alert to verify channel
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

# ----------------------------- CONFIG -----------------------------------
ONEAPI_DB = os.environ.get("ONEAPI_DB", "/opt/one-api/data/one-api.db")
ONEAPI_URL = os.environ.get("ONEAPI_URL", "http://127.0.0.1:3000")
POOL_USER_ID = int(os.environ.get("POOL_USER_ID", "1"))   # your pool/billing account id; default 1 = first One API user
# token name prefix used for customer keys (so we probe a real paying customer, not staff)
CUST_KEY_LIKE = os.environ.get("CUST_KEY_LIKE", "%")   # match ALL tokens by default; set your key prefix (e.g. "cust-%") to scope to customers only
ALERT_WEBHOOK = os.environ.get("ALERT_WEBHOOK", "")
WARN_QUOTA = int(os.environ.get("WARN_QUOTA", "5000000"))    # $10
CRIT_QUOTA = int(os.environ.get("CRIT_QUOTA", "500000"))     # $1
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SEC", "21600"))  # 6h
STATE_FILE = os.environ.get("STATE_FILE", "/var/log/llm_ops/pool_state.json")
# model used for the live probe (cheap + MUST be enabled on the sampled key)
PROBE_MODEL = os.environ.get("PROBE_MODEL", "gpt-4o-mini")
# ------------------------------------------------------------------------

DRY = "--dry-run" in sys.argv
TEST = "--test" in sys.argv


def bj_now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def post_webhook(text):
    if not ALERT_WEBHOOK:
        print("[WARN] ALERT_WEBHOOK not set, print only")
        return False
    if DRY:
        print("[DRY-RUN] would push:\n" + text)
        return True
    # Slack/Discord-style; Feishu/TG need minor tweak — see README.
    payload = json.dumps({"text": text}).encode("utf-8")
    try:
        req = urllib.request.Request(ALERT_WEBHOOK, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            print("[webhook] %s" % r.status)
        return True
    except Exception as e:
        print("[ERROR] webhook failed: %r" % e)
        return False


def oneapi_chat(key):
    full = key if key.startswith("sk-") else ("sk-" + key)
    body = json.dumps({"model": PROBE_MODEL,
                       "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 1}).encode()
    req = urllib.request.Request(ONEAPI_URL + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + full})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")[:160]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:160]
    except Exception as e:
        return -1, repr(e)


def collect():
    con = sqlite3.connect("file:%s?mode=ro" % ONEAPI_DB, uri=True)
    cur = con.cursor()
    cur.execute("select quota, used_quota from users where id=?", (POOL_USER_ID,))
    row = cur.fetchone()
    pool_quota, pool_used = (row[0] or 0, row[1] or 0) if row else (0, 0)
    cur.execute("select count(*), sum(remain_quota) from tokens "
                "where user_id=? and name like ? and status=1", (POOL_USER_ID, CUST_KEY_LIKE))
    n_cust, cust_remain = cur.fetchone()
    n_cust, cust_remain = (n_cust or 0), (cust_remain or 0)
    cur.execute("select name, key from tokens where user_id=? and name like ? "
                "and status=1 and remain_quota>0 order by used_quota desc, id desc limit 1",
                (POOL_USER_ID, CUST_KEY_LIKE))
    s = cur.fetchone()
    con.close()
    return pool_quota, pool_used, n_cust, cust_remain, (s[1] if s else ""), (s[0] if s else "")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[WARN] state write failed: %r" % e)


def main():
    if TEST:
        return 0 if post_webhook("LLM Ops Pack — channel self-test\nIf you see this, the alert path works.\nTime: %s" % bj_now()) else 1

    try:
        pool_quota, pool_used, n_cust, cust_remain, skey, sname = collect()
    except Exception as e:
        post_webhook("One API pool monitor — DB read failed\n%r" % e)
        return 1
    if n_cust == 0:
        print("[WARN] no customer tokens matched CUST_KEY_LIKE=%r (default '%%' matches all). Set it to your key prefix." % CUST_KEY_LIKE)
    usd = lambda q: "%.4f" % (q / 500000.0)
    print("Pool (user %d): quota=%s (~$%s) used=%s" % (POOL_USER_ID, pool_quota, usd(pool_quota), pool_used))
    print("Customer tokens: %s, remaining commitment total ~$%s" % (n_cust, "%.2f" % (cust_remain / 500000.0)))

    code, body = (0, "(no customer key to probe)")
    if skey:
        code, body = oneapi_chat(skey)
        print("Live probe: %s -> %s  %s" % (str(sname)[:24], code, body[:130].replace("\n", " ")))

    problems = []
    if skey and code != 200:
        problems.append("CRIT|sampled customer key unusable (HTTP %s)" % code)
    if pool_quota < CRIT_QUOTA:
        problems.append("CRIT|pool quota exhausted (%s ~$%s)" % (pool_quota, usd(pool_quota)))
    elif pool_quota < WARN_QUOTA:
        problems.append("WARN|pool quota low (%s ~$%s)" % (pool_quota, usd(pool_quota)))

    if not problems:
        print("OK: no issues")
        st = load_state()
        if st.get("status") != "ok":
            save_state({"status": "ok", "ts": time.time(), "bj": bj_now()})
        return 0

    status = "crit" if any(p.startswith("CRIT") for p in problems) else "warn"
    st = load_state()
    if (st.get("status") == status) and (time.time() - float(st.get("ts") or 0) < COOLDOWN_SEC) and not DRY:
        print("Cooling down (same status %s already pushed, %.0f min until next)" % (
            status, (COOLDOWN_SEC - (time.time() - float(st.get("ts") or 0))) / 60))
        return 0

    head = "One API pool / customer key ALERT" if status == "crit" else "Pool quota low"
    lines = [head,
             "Pool (user %d) remaining: %s (~$%s)" % (POOL_USER_ID, pool_quota, usd(pool_quota)),
             "Customer tokens: %d, remaining commitment total ~$%s" % (n_cust, "%.2f" % (cust_remain / 500000.0))]
    if skey:
        lines.append("Live probe %s -> HTTP %s" % (str(sname)[:24], code))
    lines += ["", "Problems:"] + ["  - " + p.split("|", 1)[1] for p in problems]
    lines += ["", "Fix: the pool is the shared billing-account quota; every customer call deducts from it.",
              "      If you only set per-customer token quotas without topping the pool back up,",
              "      the pool only ever decreases. Top it up: PUT %s/api/user/ (root token) set quota." % ONEAPI_URL,
              "Time: %s" % bj_now()]
    post_webhook("\n".join(lines))
    save_state({"status": status, "ts": time.time(), "bj": bj_now(),
                "pool_quota": pool_quota, "sample_code": code})
    return 1 if status == "crit" else 0


if __name__ == "__main__":
    sys.exit(main())
