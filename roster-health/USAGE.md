# Running everything from your phone (GitHub app)

Every module has a GitHub Actions workflow you can trigger from the **GitHub
mobile app** (or github.com): open the repo → **Actions** tab → pick the
workflow on the left → **Run workflow**. Results push to your phone via ntfy.

**One-time setup (repo → Settings → Secrets and variables → Actions):**

| Secret | Needed for | Example |
|---|---|---|
| `NTFY_TOPIC` | all phone pushes | `starter-changes-9f3k2x` |
| `ESPN_SWID` | ESPN features | `{XXXXXXXX-....}` (keep braces) |
| `ESPN_S2` | ESPN features | long URL-encoded string |
| `ESPN_LEAGUE_ID` | ESPN features (optional; already in config) | `400979538` |
| `ESPN_TEAM_ID` | ESPN features (optional; already in config) | `15` |

---

## The four workflows

### 1. `espn-check` — confirm your cookies work
- **Inputs:** none. Just **Run workflow**.
- **Result:** `✅ ESPN auth OK — N players, M free agents` or a clear ❌.
- Run this first after adding the ESPN secrets.

### 2. `roster-health` — current injury / lineup / IR status
- **Inputs:** none. Just **Run workflow**.
- **Result:** a push if anything changed (🚨 don't-start alerts, IR moves).
- Also runs itself Wed–Sun automatically.

### 3. `roster-report` — weekly report + start/sit
- **Inputs:** none. Just **Run workflow**.
- **Result:** two pushes — the full weekly report (lineup/byes/waivers/IR) and
  the start/sit optimizer.
- Also runs itself Sunday morning automatically.

### 4. `trade-check` — fact-check a trade offer
- **Inputs** (all optional except you need at least one player somewhere):

  | Field | What to type | Sample |
  |---|---|---|
  | `give` | players you'd send (comma-sep) | `Malik Nabers, Quinshon Judkins` |
  | `get` | players you'd receive | `Bijan Robinson` |
  | `give_faab` | $ you'd send | `0` |
  | `get_faab` | $ you'd receive | `10` |
  | `give_picks` | picks you'd send | `2027 1st` |
  | `get_picks` | picks you'd receive | *(leave blank)* |

- **Result:** a push with each player's status/role + net FAAB + pick delta.

---

## Sample inputs to test each one right now

1. **espn-check** → Run (no input). Expect ✅ (after cookies) or ❌.
2. **roster-health** → Run (no input). Expect a status push.
3. **roster-report** → Run (no input). Expect report + start/sit pushes.
4. **trade-check** → `give` = `Malik Nabers`, `get` = `Bijan Robinson`,
   `get_faab` = `5`. Expect a trade-facts push.

If a run doesn't push to your phone, open the run's log (Actions → the run →
the step) — it prints exactly why (missing `NTFY_TOPIC`, bad topic, etc.).
