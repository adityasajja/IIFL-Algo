import sys, traceback
sys.path.insert(0, 'src')
sys.path.insert(0, 'scripts')
try:
    import strategy_hunt as sh
    from atr.research.hunt import run_weights, STOCK_COSTS, buy_and_hold
    from pathlib import Path
    import pandas as pd, numpy as np

    ROOT = Path('.').resolve()
    syms = list(set(sh.parse_universe(ROOT / 'data' / 'universe' / 'n50.txt') +
                   sh.parse_universe(ROOT / 'data' / 'universe' / 'mid150.txt')))
    print('Loaded', len(syms), 'symbols')
    panel = sh.build_universe_panel(syms, pd.Timestamp('2020-01-01'), pd.Timestamp('2024-05-15'), min_bars=250)
    print('Panel:', panel.shape)
    mid = len(panel) // 2
    train_panel = panel.iloc[:mid]
    print('Train panel:', train_panel.shape)

    # Benchmark
    bh = buy_and_hold(train_panel, panel.columns[0], STOCK_COSTS)
    bhm = bh.metrics()
    print('B&H CAGR:', bhm['cagr_pct'], 'Sharpe:', bhm['sharpe'], 'Weekly:', bhm.get('weekly_mean_pct', 'N/A'))
    print('B&H metrics keys:', list(bhm.keys()))

    sig = sh.signal_momentum(train_panel, lookback=5)
    invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
    invvol = invvol.replace([np.inf, -np.inf], np.nan)
    weights = sh._build_weights(train_panel, sig, invvol, train_panel.index[0], train_panel.index[-1], 10, 5, 'equal')
    print('Weights:', weights.shape, 'sum:', weights.sum().sum(), 'NaN count:', weights.isna().sum().sum())
    result = run_weights(train_panel, weights, STOCK_COSTS)
    m = result.metrics()
    print('Strategy CAGR:', m['cagr_pct'], 'Sharpe:', m['sharpe'], 'Weekly:', m.get('weekly_mean_pct', 'N/A'))
    print('Strategy metrics keys:', list(m.keys()))
    print('Returns len:', len(result.returns), 'nonzero:', (result.returns != 0).sum())
except Exception:
    traceback.print_exc()
