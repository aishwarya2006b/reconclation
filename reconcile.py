"""
Reconciliation Engine — Payments Platform
Matches internal ledger transactions against bank settlement files.
Detects: Timing Gaps, Rounding Differences, Duplicates, Orphan Refunds
"""

import json
import random
from datetime import datetime, timedelta
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

# ─────────────────────────────────────────────
# PHASE 1 — SCHEMA & SYNTHETIC DATA GENERATION
# ─────────────────────────────────────────────

"""
INTERNAL LEDGER SCHEMA
─────────────────────────────────────────────
{
  "txn_id":        str,   # UUID, primary key  e.g. "TXN-00042"
  "type":          str,   # "CHARGE" | "REFUND"
  "amount":        float, # Positive for charges, negative for refunds (e.g. -15.00)
  "currency":      str,   # ISO 4217  e.g. "USD"
  "initiated_at":  str,   # ISO-8601 datetime (when merchant submitted)
  "merchant_id":   str,   # e.g. "MER-007"
  "status":        str,   # "PENDING" | "SETTLED" | "FAILED"
  "ref_txn_id":    str|null  # For REFUNDs: the txn_id being refunded
}

BANK SETTLEMENT FILE SCHEMA
─────────────────────────────────────────────
{
  "settlement_id": str,   # Bank-side ID  e.g. "SET-00099"
  "txn_id":        str,   # References internal ledger txn_id
  "settled_amount":float, # May differ slightly due to rounding
  "currency":      str,
  "settled_at":    str,   # ISO-8601 datetime (when bank cleared it)
  "batch_date":    str,   # YYYY-MM-DD, settlement batch date
  "status":        str,   # "COMPLETED" | "REVERSED"
}
"""

random.seed(42)

def rand_date(start: datetime, days: int) -> datetime:
    return start + timedelta(days=random.randint(0, days), hours=random.randint(0, 23))

BASE_DATE = datetime(2024, 1, 1)
NEXT_MONTH = datetime(2024, 2, 1)

# ── Generate internal ledger ──────────────────
ledger = []

# Normal charges (will get normal settlements)
for i in range(1, 41):
    ledger.append({
        "txn_id":       f"TXN-{i:04d}",
        "type":         "CHARGE",
        "amount":       round(random.uniform(10, 500), 2),
        "currency":     "USD",
        "initiated_at": rand_date(BASE_DATE, 28).isoformat(),
        "merchant_id":  f"MER-{random.randint(1,10):03d}",
        "status":       "SETTLED",
        "ref_txn_id":   None,
    })

# Refunds tied to real transactions
for i in [3, 7, 15, 22, 33]:
    orig = next(t for t in ledger if t["txn_id"] == f"TXN-{i:04d}")
    ledger.append({
        "txn_id":       f"TXN-R{i:04d}",
        "type":         "REFUND",
        "amount":       -orig["amount"],
        "currency":     "USD",
        "initiated_at": (datetime.fromisoformat(orig["initiated_at"]) + timedelta(days=2)).isoformat(),
        "merchant_id":  orig["merchant_id"],
        "status":       "SETTLED",
        "ref_txn_id":   orig["txn_id"],
    })

# Orphan refunds (no matching original)
for i in range(41, 45):
    ledger.append({
        "txn_id":       f"TXN-{i:04d}",
        "type":         "REFUND",
        "amount":       -round(random.uniform(10, 100), 2),
        "currency":     "USD",
        "initiated_at": rand_date(BASE_DATE, 28).isoformat(),
        "merchant_id":  f"MER-{random.randint(1,10):03d}",
        "status":       "SETTLED",
        "ref_txn_id":   f"TXN-GHOST-{i}",   # non-existent
    })

# ── Generate bank settlements ─────────────────
settlements = []
settle_id = 1

for txn in ledger:
    if txn["status"] != "SETTLED":
        continue

    initiated = datetime.fromisoformat(txn["initiated_at"])

    # Decide settlement date
    if txn["txn_id"] in ["TXN-0005", "TXN-0017", "TXN-0029"]:
        # Timing gap: settled in NEXT month
        settled = rand_date(NEXT_MONTH, 14)
    else:
        settled = initiated + timedelta(days=random.randint(1, 3))

    # Decide amount (introduce rounding diffs for a few)
    if txn["txn_id"] in ["TXN-0008", "TXN-0019", "TXN-0036"]:
        raw = abs(txn["amount"]) + random.choice([0.01, -0.01, 0.02])
        settled_amount = round(raw, 2)
    else:
        settled_amount = abs(txn["amount"])

    # Sign: refunds settle as negative
    if txn["type"] == "REFUND":
        settled_amount = -settled_amount

    settlements.append({
        "settlement_id": f"SET-{settle_id:05d}",
        "txn_id":        txn["txn_id"],
        "settled_amount":settled_amount,
        "currency":      "USD",
        "settled_at":    settled.isoformat(),
        "batch_date":    settled.strftime("%Y-%m-%d"),
        "status":        "COMPLETED",
    })
    settle_id += 1

# Inject duplicates into bank file
duplicates_to_inject = ["TXN-0012", "TXN-0025"]
for txn_id in duplicates_to_inject:
    orig = next((s for s in settlements if s["txn_id"] == txn_id), None)
    if orig:
        dup = orig.copy()
        dup["settlement_id"] = f"SET-{settle_id:05d}-DUP"
        settlements.append(dup)
        settle_id += 1

# Inject a duplicate in internal ledger too
ledger.append({**next(t for t in ledger if t["txn_id"] == "TXN-0031"),
               "txn_id": "TXN-0031-DUP"})


# ─────────────────────────────────────────────
# PHASE 2 — DISCREPANCY DETECTION ENGINE
# ─────────────────────────────────────────────

def detect_timing_gaps(ledger, settlements, threshold_days=25):
    """Transactions where bank settled more than threshold_days after initiation."""
    results = []
    settle_map = {s["txn_id"]: s for s in settlements}

    for txn in ledger:
        s = settle_map.get(txn["txn_id"])
        if not s:
            continue
        initiated = datetime.fromisoformat(txn["initiated_at"])
        settled   = datetime.fromisoformat(s["settled_at"])
        gap_days  = (settled - initiated).days
        if gap_days > threshold_days:
            results.append({
                "txn_id":        txn["txn_id"],
                "initiated_at":  txn["initiated_at"][:10],
                "settled_at":    s["settled_at"][:10],
                "gap_days":      gap_days,
                "amount":        txn["amount"],
                "merchant_id":   txn["merchant_id"],
            })
    return sorted(results, key=lambda x: -x["gap_days"])


def detect_rounding_differences(ledger, settlements, tolerance=0.005):
    """Amount mismatches between ledger and settlement (after sign normalisation)."""
    results = []
    settle_map = {s["txn_id"]: s for s in settlements}

    for txn in ledger:
        s = settle_map.get(txn["txn_id"])
        if not s:
            continue
        ledger_amt  = Decimal(str(txn["amount"]))
        settled_amt = Decimal(str(s["settled_amount"]))
        diff = abs(ledger_amt + settled_amt)   # both signs cancel for charge; abs handles refund
        # For CHARGE: ledger positive, bank positive → diff = |pos + pos| would be huge
        # Use directional comparison:
        if txn["type"] == "CHARGE":
            diff = abs(ledger_amt - settled_amt)
        else:
            diff = abs(abs(ledger_amt) - abs(settled_amt))

        if Decimal(str(tolerance)) < diff < Decimal("1.00"):
            results.append({
                "txn_id":       txn["txn_id"],
                "type":         txn["type"],
                "ledger_amount":float(ledger_amt),
                "settled_amount":float(settled_amt),
                "difference":   float(diff),
                "merchant_id":  txn["merchant_id"],
            })
    return sorted(results, key=lambda x: -x["difference"])


def detect_duplicates(ledger, settlements):
    """Double entries in either dataset."""
    results = {"ledger": [], "bank": []}

    # Ledger duplicates by (amount, merchant_id, date)
    ledger_keys = defaultdict(list)
    for txn in ledger:
        key = (txn["type"], txn["amount"], txn["merchant_id"],
               txn["initiated_at"][:10])
        ledger_keys[key].append(txn["txn_id"])
    for key, ids in ledger_keys.items():
        if len(ids) > 1:
            results["ledger"].append({
                "txn_ids":    ids,
                "type":       key[0],
                "amount":     key[1],
                "date":       key[3],
                "merchant_id":key[2],
            })

    # Bank duplicates by txn_id
    bank_keys = defaultdict(list)
    for s in settlements:
        bank_keys[s["txn_id"]].append(s["settlement_id"])
    for txn_id, ids in bank_keys.items():
        if len(ids) > 1:
            results["bank"].append({
                "txn_id":          txn_id,
                "settlement_ids":  ids,
                "count":           len(ids),
            })

    return results


def detect_orphan_refunds(ledger):
    """Refunds whose ref_txn_id doesn't exist in the ledger."""
    txn_ids = {t["txn_id"] for t in ledger}
    results = []
    for txn in ledger:
        if txn["type"] != "REFUND":
            continue
        ref = txn.get("ref_txn_id")
        if ref and ref not in txn_ids:
            results.append({
                "txn_id":      txn["txn_id"],
                "amount":      txn["amount"],
                "ref_txn_id":  ref,
                "initiated_at":txn["initiated_at"][:10],
                "merchant_id": txn["merchant_id"],
            })
    return results


# ─────────────────────────────────────────────
# PHASE 3 — RUN & COLLECT RESULTS
# ─────────────────────────────────────────────

timing_gaps   = detect_timing_gaps(ledger, settlements)
rounding_diffs = detect_rounding_differences(ledger, settlements)
duplicates    = detect_duplicates(ledger, settlements)
orphan_refunds = detect_orphan_refunds(ledger)

report = {
    "generated_at": datetime.now().isoformat(),
    "summary": {
        "total_ledger_transactions":    len(ledger),
        "total_bank_settlements":       len(settlements),
        "timing_gaps_found":            len(timing_gaps),
        "rounding_differences_found":   len(rounding_diffs),
        "duplicate_ledger_groups":      len(duplicates["ledger"]),
        "duplicate_bank_entries":       len(duplicates["bank"]),
        "orphan_refunds_found":         len(orphan_refunds),
    },
    "discrepancies": {
        "timing_gaps":          timing_gaps,
        "rounding_differences": rounding_diffs,
        "duplicates":           duplicates,
        "orphan_refunds":       orphan_refunds,
    }
}

# ─────────────────────────────────────────────
# PHASE 4 — HTML REPORT OUTPUT
# ─────────────────────────────────────────────

def fmt_currency(v):
    return f"${abs(v):,.2f}"

def fmt_diff(v):
    return f"${v:,.4f}"

def rows_timing(items):
    if not items:
        return '<tr><td colspan="5" class="empty">No timing gaps detected</td></tr>'
    html = ""
    for r in items:
        html += f"""<tr>
            <td><span class="mono">{r['txn_id']}</span></td>
            <td>{r['initiated_at']}</td>
            <td>{r['settled_at']}</td>
            <td><span class="badge badge-warn">{r['gap_days']}d</span></td>
            <td>{fmt_currency(r['amount'])}</td>
        </tr>"""
    return html

def rows_rounding(items):
    if not items:
        return '<tr><td colspan="5" class="empty">No rounding differences detected</td></tr>'
    html = ""
    for r in items:
        html += f"""<tr>
            <td><span class="mono">{r['txn_id']}</span></td>
            <td>{r['type']}</td>
            <td>{fmt_currency(r['ledger_amount'])}</td>
            <td>{fmt_currency(r['settled_amount'])}</td>
            <td><span class="badge badge-err">{fmt_diff(r['difference'])}</span></td>
        </tr>"""
    return html

def rows_dup_ledger(items):
    if not items:
        return '<tr><td colspan="4" class="empty">No ledger duplicates detected</td></tr>'
    html = ""
    for r in items:
        ids = ", ".join(r["txn_ids"])
        html += f"""<tr>
            <td><span class="mono">{ids}</span></td>
            <td>{r['type']}</td>
            <td>{fmt_currency(r['amount'])}</td>
            <td>{r['date']}</td>
        </tr>"""
    return html

def rows_dup_bank(items):
    if not items:
        return '<tr><td colspan="3" class="empty">No bank duplicates detected</td></tr>'
    html = ""
    for r in items:
        ids = ", ".join(r["settlement_ids"])
        html += f"""<tr>
            <td><span class="mono">{r['txn_id']}</span></td>
            <td><span class="mono">{ids}</span></td>
            <td><span class="badge badge-err">{r['count']}x</span></td>
        </tr>"""
    return html

def rows_orphan(items):
    if not items:
        return '<tr><td colspan="4" class="empty">No orphan refunds detected</td></tr>'
    html = ""
    for r in items:
        html += f"""<tr>
            <td><span class="mono">{r['txn_id']}</span></td>
            <td>{fmt_currency(r['amount'])}</td>
            <td><span class="mono missing">{r['ref_txn_id']}</span></td>
            <td>{r['initiated_at']}</td>
        </tr>"""
    return html

s = report["summary"]
total_issues = (s["timing_gaps_found"] + s["rounding_differences_found"] +
                s["duplicate_ledger_groups"] + s["duplicate_bank_entries"] +
                s["orphan_refunds_found"])

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reconciliation Engine — Discrepancy Report</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:       #0a0c10;
    --surface:  #111318;
    --border:   #1e2128;
    --border2:  #2a2f3a;
    --text:     #cdd3de;
    --muted:    #555f70;
    --accent:   #00e5a0;
    --warn:     #f5a623;
    --err:      #ff5252;
    --info:     #4fc3f7;
    --mono:     'IBM Plex Mono', monospace;
    --sans:     'Syne', sans-serif;
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.6;
    min-height: 100vh;
  }}

  /* ── HEADER ── */
  header {{
    border-bottom: 1px solid var(--border2);
    padding: 36px 48px 28px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 24px;
    background: linear-gradient(135deg, #0d1117 0%, #0f1520 100%);
  }}
  .header-left h1 {{
    font-size: 26px;
    font-weight: 800;
    letter-spacing: -0.5px;
    color: #fff;
  }}
  .header-left h1 span {{ color: var(--accent); }}
  .header-left p {{
    color: var(--muted);
    font-size: 12px;
    margin-top: 4px;
    font-family: var(--mono);
  }}
  .header-meta {{
    text-align: right;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    line-height: 1.8;
  }}
  .header-meta strong {{ color: var(--text); }}

  /* ── SUMMARY CARDS ── */
  .summary-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 1px;
    background: var(--border);
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
  }}
  .stat-card {{
    background: var(--surface);
    padding: 20px 24px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }}
  .stat-card .val {{
    font-size: 32px;
    font-weight: 800;
    line-height: 1;
  }}
  .stat-card .label {{
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-family: var(--mono);
  }}
  .stat-card.accent .val {{ color: var(--accent); }}
  .stat-card.warn   .val {{ color: var(--warn); }}
  .stat-card.err    .val {{ color: var(--err); }}
  .stat-card.info   .val {{ color: var(--info); }}
  .stat-card.neutral .val {{ color: var(--text); }}

  /* ── MAIN LAYOUT ── */
  main {{ padding: 40px 48px; max-width: 1400px; }}

  /* ── SECTION ── */
  .section {{
    margin-bottom: 48px;
  }}
  .section-header {{
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border2);
  }}
  .section-icon {{
    width: 36px; height: 36px;
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px;
    flex-shrink: 0;
  }}
  .icon-warn  {{ background: rgba(245,166,35,0.15); border: 1px solid rgba(245,166,35,0.3); }}
  .icon-err   {{ background: rgba(255,82,82,0.15);  border: 1px solid rgba(255,82,82,0.3); }}
  .icon-info  {{ background: rgba(79,195,247,0.15); border: 1px solid rgba(79,195,247,0.3); }}
  .icon-accent{{ background: rgba(0,229,160,0.10); border: 1px solid rgba(0,229,160,0.25); }}
  .section-header h2 {{
    font-size: 16px;
    font-weight: 600;
    color: #fff;
  }}
  .section-header .count {{
    margin-left: auto;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    background: var(--border2);
    border-radius: 20px;
    padding: 2px 10px;
  }}
  .section-desc {{
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 14px;
    font-family: var(--mono);
  }}

  /* ── TABLE ── */
  .table-wrap {{ overflow-x: auto; border-radius: 8px; border: 1px solid var(--border2); }}
  table {{ width: 100%; border-collapse: collapse; }}
  thead tr {{
    background: #13161d;
    border-bottom: 1px solid var(--border2);
  }}
  thead th {{
    padding: 10px 16px;
    text-align: left;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-family: var(--mono);
    color: var(--muted);
    font-weight: 600;
    white-space: nowrap;
  }}
  tbody tr {{
    border-bottom: 1px solid var(--border);
    transition: background 0.1s;
  }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:hover {{ background: rgba(255,255,255,0.02); }}
  td {{
    padding: 10px 16px;
    font-size: 13px;
    vertical-align: middle;
  }}

  /* ── MISC ── */
  .mono    {{ font-family: var(--mono); font-size: 12px; color: #9ab; }}
  .missing {{ color: var(--err); }}
  .empty   {{ color: var(--muted); font-style: italic; text-align: center; padding: 24px; }}
  .badge {{
    display: inline-block;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 4px;
  }}
  .badge-warn {{ background: rgba(245,166,35,0.15); color: var(--warn); border: 1px solid rgba(245,166,35,0.35); }}
  .badge-err  {{ background: rgba(255,82,82,0.15);  color: var(--err);  border: 1px solid rgba(255,82,82,0.35); }}
  .badge-info {{ background: rgba(79,195,247,0.15); color: var(--info); border: 1px solid rgba(79,195,247,0.35); }}

  .sub-section {{ margin-top: 24px; }}
  .sub-label {{
    font-size: 11px;
    font-family: var(--mono);
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .sub-label::before {{
    content: '';
    display: inline-block;
    width: 3px; height: 12px;
    background: var(--border2);
    border-radius: 2px;
  }}
  .sub-label.ledger-label::before {{ background: var(--info); }}
  .sub-label.bank-label::before   {{ background: var(--err); }}

  footer {{
    border-top: 1px solid var(--border);
    padding: 20px 48px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
  }}
  footer span {{ color: var(--accent); }}
</style>
</head>
<body>

<header>
  <div class="header-left">
    <h1>Reconciliation <span>Engine</span></h1>
    <p>payments-platform / ledger ↔ bank-settlement / discrepancy scan</p>
  </div>
  <div class="header-meta">
    <div>Generated <strong>{report['generated_at'][:19].replace('T', ' ')}</strong></div>
    <div>Ledger records <strong>{s['total_ledger_transactions']}</strong></div>
    <div>Bank settlements <strong>{s['total_bank_settlements']}</strong></div>
    <div>Total issues <strong style="color:{'#ff5252' if total_issues > 0 else '#00e5a0'}">{total_issues}</strong></div>
  </div>
</header>

<div class="summary-grid">
  <div class="stat-card neutral">
    <div class="val">{s['total_ledger_transactions']}</div>
    <div class="label">Ledger Txns</div>
  </div>
  <div class="stat-card neutral">
    <div class="val">{s['total_bank_settlements']}</div>
    <div class="label">Settlements</div>
  </div>
  <div class="stat-card warn">
    <div class="val">{s['timing_gaps_found']}</div>
    <div class="label">Timing Gaps</div>
  </div>
  <div class="stat-card info">
    <div class="val">{s['rounding_differences_found']}</div>
    <div class="label">Rounding Diffs</div>
  </div>
  <div class="stat-card err">
    <div class="val">{s['duplicate_ledger_groups'] + s['duplicate_bank_entries']}</div>
    <div class="label">Duplicate Groups</div>
  </div>
  <div class="stat-card accent">
    <div class="val">{s['orphan_refunds_found']}</div>
    <div class="label">Orphan Refunds</div>
  </div>
</div>

<main>

  <!-- 1. TIMING GAPS -->
  <div class="section">
    <div class="section-header">
      <div class="section-icon icon-warn">⏱</div>
      <div>
        <h2>Timing Gaps</h2>
      </div>
      <div class="count">{s['timing_gaps_found']} found</div>
    </div>
    <p class="section-desc">Transactions where bank settlement occurred &gt;25 days after initiation — typically cross-month batching delays.</p>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>TXN ID</th><th>Initiated</th><th>Settled</th><th>Gap</th><th>Amount</th>
        </tr></thead>
        <tbody>{rows_timing(timing_gaps)}</tbody>
      </table>
    </div>
  </div>

  <!-- 2. ROUNDING DIFFERENCES -->
  <div class="section">
    <div class="section-header">
      <div class="section-icon icon-info">≈</div>
      <div>
        <h2>Rounding Differences</h2>
      </div>
      <div class="count">{s['rounding_differences_found']} found</div>
    </div>
    <p class="section-desc">Amount mismatches between ledger and bank where |Δ| is between $0.005 and $1.00 — floating-point accumulation or bank truncation.</p>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>TXN ID</th><th>Type</th><th>Ledger Amt</th><th>Settled Amt</th><th>Δ Diff</th>
        </tr></thead>
        <tbody>{rows_rounding(rounding_diffs)}</tbody>
      </table>
    </div>
  </div>

  <!-- 3. DUPLICATES -->
  <div class="section">
    <div class="section-header">
      <div class="section-icon icon-err">⚠</div>
      <div>
        <h2>Duplicate Entries</h2>
      </div>
      <div class="count">{s['duplicate_ledger_groups']} ledger · {s['duplicate_bank_entries']} bank</div>
    </div>
    <p class="section-desc">Double entries detected by matching (type, amount, merchant, date) in the ledger, and by repeated txn_id in the bank file.</p>

    <div class="sub-section">
      <div class="sub-label ledger-label">Internal Ledger Duplicates</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Duplicate TXN IDs</th><th>Type</th><th>Amount</th><th>Date</th></tr></thead>
          <tbody>{rows_dup_ledger(duplicates['ledger'])}</tbody>
        </table>
      </div>
    </div>

    <div class="sub-section">
      <div class="sub-label bank-label">Bank Settlement Duplicates</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>TXN ID</th><th>Settlement IDs</th><th>Count</th></tr></thead>
          <tbody>{rows_dup_bank(duplicates['bank'])}</tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- 4. ORPHAN REFUNDS -->
  <div class="section">
    <div class="section-header">
      <div class="section-icon icon-accent">↩</div>
      <div>
        <h2>Orphan Refunds</h2>
      </div>
      <div class="count">{s['orphan_refunds_found']} found</div>
    </div>
    <p class="section-desc">Refund transactions whose referenced original transaction ID does not exist in the ledger — potential fraud vector or data corruption.</p>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Refund TXN ID</th><th>Refund Amount</th><th>Missing Original ID</th><th>Initiated</th>
        </tr></thead>
        <tbody>{rows_orphan(orphan_refunds)}</tbody>
      </table>
    </div>
  </div>

</main>

<footer>
  <div>reconciliation-engine v1.0 · python 3.x · <span>4 discrepancy detectors</span></div>
  <div>report window: 2024-01-01 → 2024-02-28</div>
</footer>

</body>
</html>"""

output_path = "/mnt/user-data/outputs/reconciliation_report.html"
with open(output_path, "w") as f:
    f.write(HTML)

# Also write JSON for programmatic consumption
json_path = "/mnt/user-data/outputs/reconciliation_report.json"
with open(json_path, "w") as f:
    json.dump(report, f, indent=2)

print(f"✓ HTML report → {output_path}")
print(f"✓ JSON report → {json_path}")
print(f"\n{'─'*50}")
print(f"  SUMMARY")
print(f"{'─'*50}")
for k, v in s.items():
    print(f"  {k:<38} {v}")
print(f"{'─'*50}")
print(f"  TOTAL ISSUES: {total_issues}")
