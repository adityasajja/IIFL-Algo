"""The tensor sweep must reproduce the plain-loop version of the same screen."""

import numpy as np
import pytest

pytest.importorskip("torch", reason="the tensor sweep needs torch (gpu dependency group)")

from atr.compute.sweep import reference_sma_cross, sma_cross_sweep


def _prices(seed=3, bars=320, names=6):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0004, 0.015, size=(bars, names))
    close = 100 * np.cumprod(1 + steps, axis=0)
    close[:40, 0] = np.nan  # a name that lists late
    close[150:155, 2] = np.nan  # a gap in the middle
    return close


@pytest.mark.parametrize("pair", [(5, 20), (10, 40), (20, 100)])
def test_cpu_sweep_matches_the_reference(pair):
    close = _prices()
    got = next(r for r in sma_cross_sweep(close, [pair[0]], [pair[1]], device="cpu").rows)
    ref = reference_sma_cross(close, *pair)
    assert got["entries"] == ref["entries"]
    assert got["total_return"] == pytest.approx(ref["total_return"], abs=2e-3)
    assert got["max_drawdown"] == pytest.approx(ref["max_drawdown"], abs=2e-3)
    assert got["sharpe"] == pytest.approx(ref["sharpe"], abs=5e-2)


def test_only_fast_below_slow_pairs_are_evaluated():
    result = sma_cross_sweep(_prices(), [10, 30], [20, 30], device="cpu")
    assert {(r["fast"], r["slow"]) for r in result.rows} == {(10, 20), (10, 30)}


def test_asking_for_cuda_without_a_gpu_is_an_error_not_a_silent_cpu_run():
    import torch

    if torch.cuda.is_available():
        pytest.skip("this machine has a GPU")
    with pytest.raises(RuntimeError):
        sma_cross_sweep(_prices(), [5], [20], device="cuda")


def test_gpu_matches_cpu_when_available():
    import torch

    if not torch.cuda.is_available():
        pytest.skip("no GPU")
    close = _prices()
    a = sma_cross_sweep(close, [5, 10], [20, 40], device="cpu").rows
    b = sma_cross_sweep(close, [5, 10], [20, 40], device="cuda").rows
    for x, y in zip(a, b):
        assert x["entries"] == y["entries"]
        assert x["total_return"] == pytest.approx(y["total_return"], abs=1e-4)
