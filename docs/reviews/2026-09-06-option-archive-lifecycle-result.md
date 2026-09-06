# Option archive lifecycle qualification result

Date: 2026-09-06

## Decision

`INSUFFICIENT_ARCHIVE_LIFECYCLE`

The checksum-bound v2 archive is internally valid and contains paired delivery
evidence, but it does not retain the selected option pair through the complete
holding lifecycle.  It therefore cannot support a full entry-to-delivery hedge
or payoff validation for this case.

This is a data qualification result only.  It is not evidence for or against
profitability, Sharpe ratio, drawdown, promotion, demo activation, or live
activation.

## Frozen case

- Experiment: `btc_bybit_usdt_option_vrp_1d_sequential_payoff_v2`
- Window: `2026-09-01T06:00:00Z` to `2026-09-02T08:00:00Z` (26 hours)
- Call: `BTC-2SEP26-78750-C-USDT`
- Put: `BTC-2SEP26-78750-P-USDT`
- Hedge: `BTCUSDT`
- Executed release: `1462f5fa8dbd46b3e13541e5f95a018ba84f0114`
- Workflow run: `34027584731`
- Aggregate artifact id: `9987563239`
- Aggregate artifact digest: `sha256:a39c4bd141abd4d5dddc5b176db8c9bb00f55f55f4e87eabc3085536f15d3d0d`

## Observed evidence

| Check | Result |
| --- | ---: |
| Valid checksum-bound archive segments | 498 |
| Invalid archive segments | 0 |
| Snapshots in the lifecycle window | 1,652 |
| Fully qualified pair plus hedge snapshots | 889 |
| Unqualified snapshots | 763 |
| Exact call/put pair snapshots | 889 |
| Qualified hedge snapshots | 1,652 |
| Paired delivery evidence | valid |
| Coverage gaps above 180 seconds | 1 |
| Maximum/terminal gap | 43,230 seconds |

The final exact-pair observation is `1788292769844`, or
`2026-09-01T19:59:29.844Z`.  It is 43,230 seconds (12 hours and 30 seconds)
before delivery, corresponding to DTE `0.500347`.

The exact pair is incomplete in the remaining 763 snapshots.  The aggregate
reason index records `C_SYMBOL_MISSING=763` and also records the corresponding
put-symbol-missing reason.  The continuous terminal boundary matches the frozen
capture rule `minimum_dte_days=0.5`: after the next poll crosses that boundary,
a held symbol is no longer part of `scoped_options`.  The dynamic
moneyness/DTE universe is suitable for finding entry candidates, but it is not
a complete archive for a position that must be followed until delivery.

## Clock-semantics correction

The first production audit incorrectly rejected all 1,652 hedge books because
their exchange timestamps were later than the local snapshot timestamp.  The
collector records the local timestamp before issuing its network requests, so
a small positive exchange-book lead is expected.  The frozen payoff contract
already defines freshness as absolute timestamp distance no greater than 120
seconds.

The audit was corrected to use that frozen two-sided bound.  A book beyond 120
seconds in the past remains `HEDGE_BOOK_STALE`; a book beyond 120 seconds in the
future is `HEDGE_BOOK_CLOCK_SKEW_EXCEEDS_BOUND`.  After the correction, all
1,652 hedge snapshots qualify.  This correction changes no market data, case,
cost, strategy, account, or trading authority.

## Required next-stage architecture

The old v2 experiment and observation clock stay frozen.  They must not be
retrofitted or relabelled as complete lifecycle evidence.

The next collector generation must separate two universes:

1. A dynamic discovery universe may continue to use DTE and moneyness bounds
   for candidate selection.
2. Once an entry checkpoint selects a call/put pair, both symbols become a
   sticky tracked universe and remain captured through delivery regardless of
   subsequent DTE or moneyness.

The v3 capture contract must also preserve poll-start, exchange-event and
snapshot-completion timestamps; validate option and hedge BBO price/size on
every observed poll; retain paired delivery evidence; and start under a new
manifest identity and observation boundary.

The next gate is result-based rather than a 35-day wait: one newly selected
near-expiry pair must pass entry-edge, internal-gap, terminal-edge, hedge-book,
artifact-integrity and paired-delivery checks over its complete lifecycle.  Only
after that data gate passes can full payoff reconstruction and model comparison
resume.

No account was created, selected, funded, queried or traded for this audit.

## Reproducibility

An immediate audit of the preceding deployed release
`6c57e622aa3049c489321b6b2dd28d210ef88f72` (run `34026973780`, artifact
`9987376443`) produced the same lifecycle metrics and decision.  The only
expected difference was growth in the all-time archive inventory from 496 to
498 valid segments while collection continued; invalid segments stayed at
zero.  The fixed historical window, qualified counts, terminal gap, final pair
timestamp and delivery result were unchanged.
