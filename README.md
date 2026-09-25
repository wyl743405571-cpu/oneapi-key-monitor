# oneapi-key-monitor

**Watch your LLM gateway's keys — not just its balance.**

A single-file, dependency-free monitor for self-hosted LLM gateways
([One API](https://github.com/songquanpeng/one-api) / New API / LiteLLM).
It catches the failure mode a balance check structurally cannot see:
**a customer key that has silently stopped working.**

Python 3 standard library only. No `pip install`. No telemetry. No SaaS.

---

## The incident this exists for

On **2026-09-18**, a one-person LLM API reseller's shared billing pool hit zero.

- **19 customer keys returned `403` on every call for ~45 hours.**
- No alert fired. No email went out.
- The admin dashboard looked **healthy** — the account balance field was fine.
- It surfaced only because a customer opened a support ticket: *"why is my integration dead?"*

### Why the balance check missed it

The existing "health check" queried the **pool account's remaining quota** once an hour.
That catches exactly one failure mode:

> "I'm about to run out of credits."

It is blind to the far more common one:

> "A key has been revoked, expired, rate-limited or misconfigured — and now returns 403 on every call."

**A pool can look perfectly alive while every key in it is a corpse.** The balance was fine. The keys were dead. And the gateway kept happily routing traffic to them.

You cannot detect this from account-level metrics. **You have to probe the keys.**

---

## What it does

Two independent checks on every run:

| Check | What it reads | What it catches |
|---|---|---|
| **Pool quota** | the pool user's `quota` in the gateway DB | the money well running dry (`WARN` / `CRIT` thresholds) |
| **Live probe** | samples a real customer key and makes a **1-token chat completion** | *any* failure mode — empty pool, dead key, downstream 5xx |

Plus two details that matter more than they look:

- **`n_cust == 0` is its own alarm.** If your filter matches no keys, the script says so instead of silently reporting "no problems". A monitor that passes on an empty result set is worse than no monitor.
- **6-hour cooldown + state file.** Persistent problems alert once, not every 10 minutes, so you don't learn to ignore it.

---

## Quick start

```bash
# 1. grab it (no dependencies — Python 3 stdlib only)
curl -O https://raw.githubusercontent.com/wyl743405571-cpu/oneapi-key-monitor/main/oneapi_pool_monitor.py

# 2. look, don't touch — prints findings, sends nothing
ONEAPI_DB=/opt/one-api/data/one-api.db python3 oneapi_pool_monitor.py --dry-run

# 3. prove your alert channel actually works BEFORE you depend on it
ALERT_WEBHOOK='https://hooks.slack.com/services/...' python3 oneapi_pool_monitor.py --test
```

If step 2 prints a live probe result, you're done configuring.

### Sample output

```
Pool (user 1): quota=4820000 (~$9.6400) used=1180000
Customer tokens: 19, remaining commitment total ~$34.20
Live probe: cust-7f3a1b -> 200  {"id":"chatcmpl-...","choices":[...]}
OK: no issues
```

On failure:

```
One API pool / customer key ALERT
Pool (user 1) remaining: 0 (~$0.0000)
Customer tokens: 19, remaining commitment total ~$34.20
Live probe cust-7f3a1b -> HTTP 403

Problems:
  - sampled customer key unusable (HTTP 403)
  - pool quota exhausted (0 ~$0.0000)

Fix: the pool is the shared billing-account quota; every customer call deducts from it.
      If you only set per-customer token quotas without topping the pool back up,
      the pool only ever decreases. Top it up: PUT http://127.0.0.1:3000/api/user/ (root token) set quota.
```

---

## Configuration

Everything is env-var driven — no editing required.

| Env var | Default | Meaning |
|---|---|---|
| `ONEAPI_DB` | `/opt/one-api/data/one-api.db` | Path to the One API SQLite DB (opened **read-only**) |
| `ONEAPI_URL` | `http://127.0.0.1:3000` | Gateway base URL, used for the live probe |
| `POOL_USER_ID` | `1` | The user id whose quota is the shared pool |
| `CUST_KEY_LIKE` | `%` | SQL `LIKE` filter selecting **customer** keys. Default matches all — set your prefix (e.g. `cust-%`) to scope it |
| `ALERT_WEBHOOK` | *(empty)* | Any inbound webhook (Slack / Discord / Telegram / Feishu). Empty = print only |
| `WARN_QUOTA` | `5000000` | Pool warning threshold (`500000` = $1 in One API quota units) |
| `CRIT_QUOTA` | `500000` | Pool critical threshold |
| `COOLDOWN_SEC` | `21600` | Suppress repeat alerts of the same severity for this long |
| `PROBE_MODEL` | `gpt-4o-mini` | Model for the live probe. **Must be enabled on the sampled key** — otherwise you get a false alarm |
| `STATE_FILE` | `/var/log/llm_ops/pool_state.json` | Where cooldown state lives |

> **`CUST_KEY_LIKE` is the setting people get wrong.** If you name customer keys with a prefix, set it. If the filter matches nothing, the script warns loudly (`n_cust == 0`) rather than reporting all-clear.

## Cron

```cron
*/10 * * * * ALERT_WEBHOOK='https://hooks.slack.com/services/...' \
  CUST_KEY_LIKE='cust-%' \
  ONEAPI_DB=/opt/one-api/data/one-api.db \
  /usr/bin/python3 /opt/scripts/oneapi_pool_monitor.py >> /var/log/pool_monitor.log 2>&1
```

Every 10 minutes is a sane default. The probe costs a fraction of a cent — it's a 1-token call on your cheapest model.

---

## Design notes (why it's built this way)

1. **Probe keys, not balances.** One tiny completion call per key per cycle. Cheap, and it catches silent failures that no account metric reveals.
2. **Alert on empty results too.** `n_cust == 0` is an alarm, not a green checkmark.
3. **Make the health check free.** Cheapest model + `max_tokens=1`. If probing costs real money you'll disable it to save budget, and you're back to square one.
4. **Open the DB read-only** (`mode=ro`). A monitor must never be able to corrupt what it's watching.
5. **Alert the operator, but also the customer.** This script only does the operator half — see below.

---

## Scope: what this is not

This is **one script solving the single highest-severity failure mode.** It is deliberately not a whole product.

Not included here:

- Per-customer low-quota emails (telling the *customer* before they get blocked)
- Cost / margin / pricing math for reselling
- Per-customer usage reporting queries
- Ready-made dashboard panels

If the five scripts it came from are useful to you, they're packaged — with the SQL and a import-ready Grafana dashboard — as a one-time pack:

**→ [LLM Ops Pack](https://llm-ops-pack.app.workbuddy.host/)**

That's the honest deal: this repo is the part that fixes the outage, given away because an outage like this shouldn't happen to anyone twice. The pack is the rest of the operational set. **MIT-licensed here, use it freely whether or not you ever buy anything.**

---

## Compatibility

| Gateway | Status |
|---|---|
| **One API** | Works out of the box (SQLite schema) |
| **New API** | Same `users` / `tokens` schema — works |
| **LiteLLM** | Point `ONEAPI_DB` at its DB and adapt the two queries in `collect()`; the probe logic is unchanged |
| **MySQL-backed gateways** | Set `ONEAPI_MYSQL_DSN` support by swapping `collect()` — the file documents the columns used |

Verified on Python 3.8+.

---

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with the One API, New API or LiteLLM projects. "One API" is used descriptively.

---

**If you've lived through a similar silent outage, I'd like to hear how you caught it — open an issue.**
