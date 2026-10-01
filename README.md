# Corporate Issuance v2

EDGAR-first S&P 500 bond issuance monitor. Copy-ready headlines and evidence for
**ANNOUNCED**, **LAUNCHED**, and **PRICED** events. Launch requires explicit launch
language; coupon presence or a generic shelf does not prove launch or pricing.

## Run locally

```bash
python -m pip install -r requirements.txt
streamlit run CIapp.py
```

`CIapp.py` remains the Streamlit entry point. Set `SEC_USER_AGENT` to a descriptive
application name and contact email to override the existing repository contact.
Set `ISSUANCE_DATA_DIR` to a persistent writable directory (default `data`).

A GitHub Actions workflow refreshes the shared S&P 500 snapshot on the 1st at 07:00 UTC, once this branch is merged into main. GitHub schedules can be delayed; branch rules may require allowing the Actions bot to push the snapshot. Workflow failures are visible in GitHub Actions.

The app also refreshes on the first open/scan of each London calendar month.
If the app is closed on the first, it catches up on the next open. Membership and
company names come from Wikipedia's constituent table, verified against SEC CIKs;
optional yfinance enrichment updates names only. It cannot determine index membership.
A failed refresh retains the last good snapshot with a visible warning.

Click **Scan EDGAR now**. The default **Latest filings (fast)** mode checks five
SEC Atom feeds, each limited to the latest 100 entries, then reads only unseen
S&P 500 matches. This is a bounded latest-feed check, not complete historical
coverage. Feed errors and window limits are visible.

Choose **Full catch-up** to scan each unique issuer's SEC submissions for the
selected date range (default seven days), including older archive blocks.
Both modes read primary documents and linked 8-K EX-99 and EX-1.1 exhibits. Documents are read completely up to a 12 MB safety limit; oversize documents
remain unprocessed with an error. Requests have short connect/read timeouts,
a 20-second body-download deadline and one retry. Failed reads remain retryable.
Six issuer workers share one rate limiter. A 45-second per-issuer budget stops
a bank's large filing backlog from holding up the scan; unfinished filings are
reported and can be continued on the next scan. Progress and activity update
while other issuers work. The budget is checked between filings, so a filing
with several documents can exceed it before the next check.
It uses at most four SEC request starts/second per process. Avoid multiple scanners
sharing an IP if their combined traffic would exceed SEC limits.

Auto-scan operates only while an app session is active. The monthly universe workflow
does not run issuance scans unattended. Full catch-up can still take several minutes. Fast-mode duration depends on
unseen matching filings and SEC availability; it does not query 500 submissions.

## Output and persistence

SQLite stores processed accessions and deal events transactionally. Reopening the
app keeps history if the data directory persists. Streamlit Community Cloud local
disk is **not durable across redeployments**; use persistent hosting/storage for
continuous production history. Separate sessions share the database in one process.

Identical terms/stage updates are deduplicated across filings. Later stages link
only when currency, maturity set, size and a seven-day window match unambiguously.
Unknown or materially changed terms stay separate rather than guessing a deal ID.
Announcement-to-pricing links therefore depend on matching securities being stated.

Each card includes copyable headline, SEC source, evidence, and linked timeline.
Times are SEC acceptance times in New York and London, with daylight saving handled
by `zoneinfo`; observed time is stored separately. They are not exact syndicate
launch timestamps. Missing acceptance times are explicitly labelled.

## Parser scope and limitations

Rule-based extraction supports common prose/cover lists of USD/EUR/GBP note amounts,
coupons and maturity years, with floating-rate labels and use-of-proceeds evidence.
It excludes structured/retail notes, exchange offers and tender/redemption notices.
S-3 shelves are not scanned. Debt issuance through private placements may appear
in 8-K releases; announcements not filed on EDGAR cannot be detected.

Unusual tables may yield a stage without complete terms. Labelled pricing term-sheet sections can also supply Treasury spreads, yields and
issue prices, and labelled bookrunner lists are displayed when available. Other
layouts may leave these fields empty: check the linked source. Never treat missing parsed fields as evidence that those terms do not exist.
Do not assume all deals will be captured; test against desk examples before relying
on the feed. The legacy `monitor.py`, `classify.py`, `universe.py` remain unused by v2.

## Validation

```bash
python -m unittest discover -s tests -v
```

Tests also verify fast mode skips issuer submissions and seen filings, slow
issuers do not block others, deferred filings remain retryable, bounded request
retries, feed universe filtering and direct supplement fetches.

Tests cover lifecycle labels, false positives, multi-tranche parsing, currency,
timezones, persistence/deduplication, distinct deals, failed-filings retries and
monthly refresh failures. Live network validation is separate from these fixtures.

## Financials tab

Non-financials opens first. Financials has its own minimum known USD deal size,
defaulting to USD 1bln, and includes undisclosed-size announcements by default.
Both controls can be changed independently of Non-financials. Known non-USD amounts
remain visible without an FX conversion. Stage and issuer search apply to both tabs;
each tab exports only its displayed headlines.

Classification uses the constituent table's GICS Sector, refreshed with the monthly
universe. Old saved events are classified through their CIK against the current
snapshot. The enriched shared snapshot upgrades old local caches automatically.
Historical issuers without sector metadata remain visible under Unclassified.
