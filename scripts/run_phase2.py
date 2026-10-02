"""Compact Phase 2 runner with file-based progress logging."""
import sys, json, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from datetime import datetime

import pandas as pd
import numpy as np

# Monkey-patch print to also log to file
_orig_print = print
_logf = open("data/research/phase2_progress.log", "w", buffering=1)

def log_print(msg="", **kwargs):
    _orig_print(msg, **kwargs)
    _logf.write(str(msg) + "\n")
    _logf.flush()

class _Tee:
    def __init__(self, *streams):
        self.streams = streams
    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()
    def flush(self):
        for s in self.streams:
            s.flush()

sys.stdout = _Tee(sys.stdout, _logf)
sys.stderr = _Tee(sys.stderr, _logf)

import strategy_hunt as sh

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research"

def main():
    start = pd.Timestamp("2020-01-01")
    end = pd.Timestamp("2026-09-18")
    symbols = list(set(sh.parse_universe(ROOT / "data" / "universe" / "n50.txt") +
                      sh.parse_universe(ROOT / "data" / "universe" / "mid150.txt")))

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Universe: {len(symbols)} symbols", flush=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Window: {start.date()} → {end.date()}", flush=True)

    result = sh.phase2_walkforward(symbols, start, end)

    out = OUT / "strategy_hunt_wf.json"
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Results saved to {out}", flush=True)

    if result.get("folds"):
        print(f"\n{'='*72}")
        print(f"PHASE 2 SUMMARY:")
        print(f"  Avg test Sharpe: {result['avg_test_sharpe']:.3f}")
        print(f"  Avg test CAGR: {result['avg_test_cagr_pct']:.2f}%")
        print(f"  Avg weekly mean: {result['avg_test_weekly_mean_pct']:+.4f}%")
        print(f"  Avg excess vs control: {result['avg_test_excess_vs_control']:+.3f}")
        print(f"  Deflated Sharpe: {result.get('deflated_sharpe')}")
        print(f"  Verdict: {result['verdict']}")
        for f in result["folds"]:
            print(f"\n  Fold: {f['signal']}/k{f['k']}/step{f['step']}/{f['sizing']}")
            print(f"    Train Sharpe={f['train_sharpe']:.3f} → Test Sharpe={f['test_sharpe']:.3f}")
            print(f"    Train weekly={f['train_weekly_mean']:+.4f}% → Test weekly={f['test_weekly_mean_pct']:+.4f}%")
            print(f"    Test CAGR={f['test_cagr_pct']:.2f}% DD={f['test_max_dd_pct']:.1f}%")
            print(f"    Test P(>=5%)={f['test_weekly_p_ge_5pct']}% ex_ctrl={f['test_excess_sharpe']:+.3f}")

if __name__ == "__main__":
    main()
