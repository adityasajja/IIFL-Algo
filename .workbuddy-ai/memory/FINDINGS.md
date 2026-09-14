# atr — the statistical record

The measured evidence behind every claim the platform makes. `MEMORY.md` is the
short index; this file is the detail. Operational reference (UI, broker, build,
sandbox, code traps) lives in `REFERENCE.md`.

Every number here was produced by a script under `scripts/` and persisted to
`data/self_learning/*.json`. If a number is not in this file it has not been
measured.

---

## The principle

> "I want to develop an intelligent but fact based system which acts on pure
> statistical and strategic data, not on emotions."

"Emotion" = **overfitting / cherry-picking**. Every result must be:

- **out-of-sample** (`atr research` / walk-forward). `atr backtest` is in-sample
  by construction — a tempting number and a meaningless one.
- **corrected for trial count** (deflated Sharpe). Searching harder must raise the
  bar, not the answer.
- compared to **buy-and-hold** on the same universe, and to a **control** that
  isolates the signal's own contribution. Significance ≠ usefulness:
  cross-sectional momentum cleared deflated Sharpe (0.976) but sat at the 25th
  percentile of random (z=−0.71).
- never a **citation presented as a measurement**. `papers.py`'s hardcoded win
  rates (0.58–0.65) all proved 3–12 points optimistic once measured.

Baselines: `data/self_learning/*.json`, served `GET /validation` and
`GET /validation/episodic-pivot`, rendered Evidence → Measured results.

## Headline: 0 of 27 pre-registered tests pass — and one that does

Honest sample **2015-01-01 → 2026-09-11** (2,899 bars, 9 walk-forward folds),
spanning 2015/2018/2020/2022. Fetch `fetch_long_history.py --days 4400`.
2020-05 → 2026-09 was one bull run that flattered every long-only rule.

Paper 0/7 · production entry rules 0/4 · Episodic Pivot 0/9 · alpha hunt 0/7 short,
1/6 long-and-fragile. **Every one of those loses to simply being invested.**

**The exception is cross-sectional momentum, found 2026-09-13** — see the momentum
section below. It clears deflated Sharpe, survives walk-forward (13/15 folds
positive), and beats both its random control and buy-and-hold on Sharpe in mid and
small caps. It is the only thing in this repo that does. Its size is modest
(~0.2–1.5%/week) and its most recent two years are flat.

## Quote a real instrument for absolute numbers

Never an equal-weight basket — those are *today's* index members, double-counting
survivorship and equal-weighting. Over 11.69 years:

| instrument | total | CAGR | maxDD | Sharpe | Rs 5L becomes |
|---|---|---|---|---|---|
| NIFTYBEES | +215.4% | 10.3% | 36.3% | 0.70 | Rs 15.77L |
| JUNIORBEES | +310.5% | 12.8% | 38.7% | 0.75 | Rs 20.53L |

vs the **+477.78%** the survivor basket reported for "Nifty 50 B&H". Same-universe
comparisons stay valid (survivorship cancels); absolute projections do not.
NIFTYBEES worst DD −36.3%, 2020-01-17 → 2020-03-23.

## Weekly-return reality — the 5%/week question (2026-09-13)

`scripts/research_weekly_target.py` → `data/self_learning/weekly_target_feasibility.json`.

- 5%/wk = **+1,164%/yr = 12.6x**; 10%/wk = **+14,104%/yr = 142x**.
- NIFTYBEES, 610 weeks: mean +0.209%, sd 2.04%, best +11.13%, worst −12.59%.
  **7 weeks ≥+5% (1.1%), 1 ≥+10%.** Best 52-week window ever +83.28%;
  **0 of 559 rolling 52-week windows reached 5%/wk pace.** 200k bootstrapped
  52-week paths: median 1.104x, best of all 200k 2.15x, none reached 5%/wk.
- The premise *is* abundant: **14.62%** of 211,153 stock-weeks across 413 names are
  ≥+5%, 4.52% ≥+10%.
- But random equal-weight portfolios are ≥+5% in only: N=1 14.66%, N=5 8.63%,
  **N=10 6.25%**, N=20 4.49%, N=50 3.39% of weeks. Median single stock 1.31%; the
  median stock's mean week is **−0.038%**.
- Perfect foresight of the weekly top name = 100%. **The 5%/week target is
  definitionally the oracle** — it needs a 100% hit rate, which nothing but
  perfect foresight delivers.
- Required Sharpe vs the N=10 baseline (mean 0.413%/wk, sd 3.183%/wk): 5%/wk needs
  **+4.59pp/week = 1.44 sd = Sharpe 10.39**; 10%/wk needs **Sharpe 21.72**.
- **Leverage is not the bridge.** To reach 5%/wk: nifty50 needs **32.8x**, midcap150
  **7.4x**, smallcap250 **10.7x**. The account is wiped by an unlevered drawdown of
  **−3.1% / −13.6% / −9.4%**, against actual worst drawdowns of −34.7% / −35.5% /
  −65.7%. Ruin is certain, not probable. Borrowing at ~11%/yr also costs
  70–349%/yr at those levels — **more than the entire edge**.

## Weekly momentum selection — the strongest signal found so far (2026-09-13)

`scripts/research_weekly_momentum.py` → `data/self_learning/weekly_momentum.json`
(+ `_lag1.json`). Pre-registered grid, 30 configs × 3 universes = **90 trials**,
each against a **matched random control** (same N, same dates, same costs, 8
seeds) — survivorship-neutral, because both sides use the same universe.

**This is the first thing in the repo that beats both its control and buy & hold
on Sharpe.** Cross-sectional momentum at a **6-month lookback with a 1-week skip**:

| universe | best config | mean/wk | Sharpe | B&H Sharpe | z vs control | maxDD |
|---|---|---|---|---|---|---|
| midcap150 | L=26w skip=1 N=5 | **+0.918%** | **1.83** | 1.172 | **+9.87** | −35.5% |
| midcap150 | L=26w skip=1 N=10 | +0.749% | 1.80 | 1.172 | **+14.98** | −30.2% |
| smallcap250 | L=12w skip=1 N=20 | +0.612% | **1.31** | 1.072 | **+10.45** | −57.7% |
| nifty50 | L=26w skip=1 N=5 | +0.371% | 0.84 | 1.041 | +3.97 | −34.7% |

Configs beating the control at t=1.895: nifty50 12/30, midcap150 15/30, smallcap250
18/30. Beating B&H on Sharpe: 0/30, 6/30, 7/30. **Momentum works in mid/small caps
at long lookbacks and does nothing on the Nifty 50.**

- **Short horizons are reversal, not momentum.** L=1–2w with skip=0 has *negative*
  z (−1.6 to −4.0) — the documented short-horizon reversal. This is consistent
  with `reversal_short_horizon`; the two are the same effect from opposite ends.
- **Robust to execution lag.** `--exec-lag 1` (trade a week after the signal)
  *improves* it: midcap150 best +0.978%/wk, 26/30 beating control. Not a
  same-bar artefact.
- **Concentration buys variance, not edge.** midcap150 L=26 skip=1: N=5 → P(≥5%)
  12.86% / P(≤−5%) 7.03%; N=20 → 4.80% / 3.60%. The ratio stays ~1.7 at every N,
  so the *odds* improve and the *magnitude* does not. Raising the hit rate means
  concentrating, and concentration raises the downside in step (maxDD −24.9% →
  −35.5%).
- **It is still 5.4x short of the ask.** Best of 90 configs averages **0.918%/wk**
  vs a 5%/wk target; **0 of 90 average ≥5%/wk**; max P(week ≥+5%) = **16.86%**
  (smallcap250 L=4w skip=1 N=5) — and that config carries P(≤−5%)=12.23% and
  **maxDD −85.1%**.
- **Leverage is not the bridge.** To reach 5%/wk: nifty50 needs **32.8x**, midcap150
  **7.4x**, smallcap250 **10.7x**. The account is wiped by an unlevered drawdown of
  **−3.1% / −13.6% / −9.4%** respectively, against actual worst drawdowns of
  −34.7% / −35.5% / −65.7%. Ruin is certain, not probable. Borrowing at ~11%/yr
  also costs 70–349%/yr at those levels — **more than the entire edge**.
- **`skip` matters and is not cosmetic.** Including the most recent week flips
  short-horizon results; the standard Jegadeesh-Titman correction is to omit it.

### It survives the multiple-testing and out-of-sample checks — first PASS here

`scripts/validate_momentum_deflated.py` → `data/self_learning/momentum_deflated.json`.
Uses `atr.research.validate.deflated_sharpe_ratio` (Bailey & López de Prado) with
all 30 trial Sharpes per universe plus the winner's skew/kurtosis (weekly returns
are fat-tailed: kurtosis 4.06–6.10).

| universe | best Sharpe | hurdle | P(edge) |
|---|---|---|---|
| nifty50 | 0.845 | 0.648 | 0.9997 |
| midcap150 | 1.834 | 1.268 | 1.000 |
| smallcap250 | 1.340 | 1.004 | 1.000 |
| **pooled, 90 trials** | **1.834** | **1.221** | clears |

**Walk-forward, 5 disjoint 101-week test blocks** (2016-12→2018-11, →2020-10,
→2022-09, →2024-08, →2026-08), config chosen on the train window only:
**13 of 15 universe-fold results positive.** OOS Sharpe mean 0.871 / **2.025** /
1.435; OOS mean weekly 0.23–0.75% / 0.11–1.54% / 0.08–1.65%.

- **Selection costs 0.41–0.57 Sharpe** — the chosen config sits consistently ~0.5
  below the hindsight-best. That is the real price of not knowing in advance.
- **The edge is decaying.** Fold 5 (2024-09 → 2026-08) is **−0.800** nifty50,
  **+0.059** midcap150, **+0.045** smallcap250 — flat or negative everywhere. The
  headline Sharpes are carried by 2020–2024. A live warning, not a footnote.
- Long lookbacks (12–26 weeks) win on train in 14 of 15 folds; short-horizon
  configs are never selected, consistent with 1–2 week returns being reversal.
- **Bug:** labelling each fold's window with `weeks[-1]` rather than
  `weeks[test_end-1]` prints one end date for all folds and makes *disjoint* test
  blocks look *overlapping*. The slice was right, the label lied. Verify fold
  boundaries explicitly.

### Forward paper record — the only remaining test

`scripts/track_momentum_paper.py` → `data/paper_momentum/{picks,settlements}.jsonl`,
both **append-only** so the record survives a restart and cannot be revised.
`picks.jsonl` writes names, entry prices **and the backtest's expectation** before
the outcome exists — a prediction recorded afterwards is not a prediction.
Settles from the **daily cache**, independently of the panel the rule was built on.
Wired into the weekly automation (Fridays 18:00). Rule = `top_decile_ret_26w`.

First 14 settled weeks (2026-06-05 → 2026-09-04, **in-sample** — harness
validation, not proof):

| | forward | backtest expected |
|---|---|---|
| per-pick hit ≥5% | **21.43%** | 20.17% |
| basket mean/week | **+0.392%** | +0.88% |

cumulative +5.19%, best week **+6.17%** (5/10 picks ≥5%), worst −2.65%, 6/14
positive. **The hit rate reproduces; the basket does not** — it earns less than
half the modelled mean, so the losers are bigger than modelled or the winners run
less. Same signal as fold 5.

**Three bugs caught building it, all the repo's documented traps:** settlement
reported a clean **+0.00%** week because the exit week's data did not exist yet and
the lookup fell back to the last bar (today's price as next week's — an unknown
reported as a zero); fixed with `close_at()`, which refuses unless the series
reaches the date. Picks were **labelled with statistics of a filter the selection
never applied** — each filter now applies its own mask before ranking. And three
sequential decile masks leave ~0.1% of the universe, so the first backfill
returned **zero weeks** instead of erroring.

Caveat unchanged: the universe is today's index membership applied backwards, so
absolute returns are survivorship-inflated. The **momentum-vs-control** comparison
is not, because both sides share the universe — that is the valid evidence here.

## Stock-level hit rate — can a filter pick a 5% week? (2026-09-13)

`scripts/research_weekly_stock_picks.py` → `data/self_learning/weekly_stock_picks.json`
(panel cached at `data/self_learning/stock_week_panel.parquet`: 329 liquid NSE
names ≥ Rs 1cr median daily turnover, 174k stock-weeks, 2015-12 → 2026-09). The
stock-level companion to the portfolio study: how often does a *flagged name*
actually gain 5%?

**Base rate: 14.23% of stock-weeks gain ≥5% the next week** (mean +0.415%). That
base rate already contains every "some of them work" — it is not the constraint.

| filter | fires | P(≥5%) | lift | mean next wk | P(≤−5%) |
|---|---|---|---|---|---|
| `momentum_volume_52w_high` | 0.99% | **20.61%** | 1.45x | +0.566% | **21.19%** |
| `momentum_and_volume` | 1.36% | 20.42% | 1.44x | +0.592% | 20.51% |
| `momentum_and_52w_high` | 3.69% | 20.20% | 1.42x | **+0.950%** | 16.10% |
| `top_decile_ret_26w` | 10.0% | 20.17% | 1.42x | +0.882% | 15.02% |
| `top_decile_ret_12w` | 10.0% | 19.59% | 1.38x | +0.845% | 14.52% |
| `breakout_4w` / `near_52w_high` | 33% / 11% | 14.21 / 14.88% | 1.00 / 1.05x | +0.49 / +0.64% | 10.7 / 11.0% |

- **The ceiling on "pick a stock that gains 5% next week" is ~1 in 5.** Best of 14
  pre-registered filters: 20.61%, a 1.45x lift on the 14.23% base. The filters that
  raise the hit rate most raise P(≤−5%) in step — `momentum_volume_52w_high` has
  *more* downside than upside, i.e. it selects volatility, not direction.
- **The basket arithmetic is the whole answer.** Portfolio return is the *average*
  of the picks. At a 21.2% hit rate, 10 picks yield ~2.1 winners; if those gain 5%
  and the other 8 go nowhere the portfolio returns **+1.06%**. For +5% the average
  pick must be +5% — all ten, not some.
- Best clean top-10 portfolio: **+0.70%/week, Sharpe 1.27–1.30**, P(portfolio ≥5%)
  **11.5%**, maxDD −50 to −57%. **82.5% of weeks had at least one of the 10 picks
  gain 5%** — the "some of them work" premise is true and the basket still lands
  at +0.7%.
- **`breakout_4w` and `near_52w_high` add nothing** (lift 1.00x / 1.05x) — the
  52-week-high effect is already inside 26-week momentum here.
- **Two artefacts caught by the same diagnostic.** `top_decile_gap_up` looked like
  the best rule on the board at **+1.81%/week** until its best week turned out to be
  **+781%** (one bad bar; median +0.37% vs mean +1.81%). Masked: **+0.32%/week**.
  `top_decile_range_expansion`: **+1.41% → +0.01%/week**. Only 2 of 174k stock-weeks
  exceeded ±300% and they were enough to manufacture a fake winner. **Always print
  best-week beside mean, and per-year means, before trusting a mean.**
- **Gotcha:** filtering the panel on a known `next_ret` silently drops the final
  week — the one you are picking *for*. Keep the outcome-NaN row; exclude it only
  when scoring.

## Costs decide which strategies are worth considering

`CommissionModel` is IBKR-style (Rs 0.005/share, no statutory levy) —
**understates NSE delivery ~28x**. Real round trip **~0.283% of turnover**, almost
all STT at 0.1% per leg. Use `IndianDeliveryCosts` (`--costs india`). The bill
scales with turnover: 1,900 trades faces 95x the cost of 20. Not modelled: STCG
20% vs LTCG 12.5%.

## A z-score needs the t table, not 2.0

First alpha-hunt run reported Nifty 50 reversal at **+2.51σ** — a PASS — from only
**3 control runs**. At df=2 the one-sided 95% t-critical is **2.92**. With 8 seeds
the control mean moved 0.466 → 0.778 and z collapsed to **+0.56σ**.
`t_critical_one_sided()` in `scripts/research_alpha_hunt.py` interpolates the real
table.

## Alpha hunt — pre-registered (2026-09-13)

`src/atr/strategy/strategies/alpha_candidates.py`; harness
`scripts/research_alpha_hunt.py`. Hypotheses written down **before** scoring, each
against a null removing only its signal: `reversal_short_horizon` (Lehmann 1990,
Jegadeesh 1990) vs **random names** at the same cadence/count;
`vol_managed_exposure` (Moreira & Muir 2017) and `trend_filtered_exposure`
(Moskowitz/Ooi/Pedersen 2012) vs the **same exposure sequence circularly shifted**
— preserving the exact multiset of exposures, moving only the dates. Control gets
the **same grid**, so it is not a weaker null.

**0/7 pass.** Nifty 50 (B&H Sharpe 1.272, +96.76%): reversal 1.039 (ctrl 0.778,
z=+0.56), vol-managed 1.244 (z=+0.15), trend 1.343 (ctrl 1.065, z=+1.00). Midcap
150 (1.677/+219.69%): reversal 1.193 (z=**−0.46**), vol-managed 1.575 (z=+0.90),
trend 1.578 (z=+0.67). Smallcap 250 (1.494/+221.32%): reversal 1.504 (z=+1.56).

**The trend overlay is under-powered rather than wrong.** Nifty 50: B&H Sharpe
1.272 at 100% exposure; the *same* exposure sequence at random dates scores 1.065;
at actual dates 1.343 with Calmar 1.194 and maxDD 11.24% vs 18.99%. Cutting exposure
at random *hurts* (you give up upside); cutting it when the trend is down more than
recovers that — a +0.28 Sharpe timing effect. Four folds cannot establish it
(z=+1.00). Resolve with **more data**, never more parameters. Vol-targeting is
nothing: z=+0.15.

**The one candidate that survived its bars — and still isn't tradeable.**
`reversal_short_horizon` on Smallcap 250, long sample, Indian costs: +913.27%,
Sharpe 1.143, control 0.7745 (sd 0.137) → z=+2.70, P(edge)=0.981 over 54 trials.
Slippage sweep: 1.143 at 5 bps → 1.050 / 0.956 / 0.850 at 15/25/40 bps.
**Break-even ≈ 9 bps per leg**; Smallcap spreads are 10–30 bps, and 1,935 trades
pays every bp 1,935 times. **A passing backtest is only as good as its cost
assumption.** Also unproven: today's membership applied backwards, and in a
survivor-only set the biggest losers are disproportionately temporary dips in
eventual winners — the exact mechanism being tested. Max DD 58%.

## Episodic Pivot — Pradeep Bonde (2026-09-13)

`src/atr/strategy/strategies/episodic_pivot.py` (`_day1`/`_delayed`/`_9m`). A daily
bar cannot read an earnings release, so the catalyst is proxied by **abnormal gap +
abnormal volume** — the playbook's own EP 9M concession ("the volume itself is the
clue"). **0 of 6 pass**, and **Day 1 is 1.71σ *worse* than random entry dates** on
Midcap 150 even though that universe has the premise (33 ten-percent gaps vs Nifty
50's **0**). `scripts/research_episodic_pivot.py` counts the raw material first:
Nifty 50 gap ≥10% occurred **0 times in 73,488 sessions**, best 20-session move
**+71.7%**, zero windows over +100% — while the playbook calls 20–40% gaps routine
and cites +189% to +358%. **A rule cannot fire on a setup the market never
produces; check the premise exists before scoring the rule.**

## Traps that fail as plausible results

- **`data/universe/*.txt` are single-line comma-separated**, not one symbol per
  line. `.split()` returns one giant token and silently yields zero symbols — a
  study that then reports "no data" as if it were a finding.
- **Outliers wreck means, not medians.** Check the max before trusting a mean: one
  unhygiened bar moved a random-portfolio mean weekly return to a nonsense figure.
  A weekly move beyond ±300% is a corporate-action artefact — mask and *count* it,
  and report the count. Medians and hit rates are robust and are the honest summary.
- **The IIFL daily cache is corrupt on 2021-09-15/16**: 14 bars at 3–5× the
  surrounding level across BEL, IRCTC, IPCALAB, JUBLFOOD, KPRMILL, SRF, SCHAEFFLER.
  `atr.data.hygiene.drop_reverting_spikes` removes them, keyed on **reversion** (the
  level k sessions either side must agree while the bar is far from both) so genuine
  re-ratings survive. Effect: Nifty 50's best 20-session move fell +264% → +71.7%.
  **Reuse this filter in any new study.**
- **Panel gotcha:** filtering on a known outcome column silently drops the final
  period — the one you are forecasting *for* — and shifts every "latest" answer one
  period into the past.

Code-level traps (funding, daily reset, payload resolvers, NaN handling, logging,
frame padding, index labels) are in `REFERENCE.md` — they bite on every new
strategy, so read them before adding one.

## Conventions

- Diff outputs (equity curve + every fill), not just `num_trades >= 1`.
- Backtests must stay sweep-fast (~1.9s / 17.5k bars).
- Python 3.12 via `./.venv/Scripts/python.exe`. **No pip in the venv** — use
  `uv pip install --python .venv/Scripts/python.exe <pkg>`. No scipy.
- Live scanner and backtest strategy must call the **same `eval_entry`/`eval_exit`**.
- Pre-compute indicators in `prepare()` — per-bar recompute is ~15x slower.
- `min_history_bars` < test window, else "no trades" is indistinguishable from
  "no edge".
- Before trusting a new rule, count how many positions it flags in one day.
- `Verdict` checks must name both quantities they compare: a probability labelled
  "Sharpe" next to a Sharpe made a clear FAIL read as a near-miss. Regression-tested
  in `tests/test_validate.py`.
- Entry rules remain unvalidated: 0/4 beat B&H across 4 fold splits, OOS Sharpe
  spread 0.02–1.03 from the split choice alone. Do not present as actionable.
