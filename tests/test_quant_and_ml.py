"""Unit tests for Quantitative Risk Analytics and ML Meta-Labeling."""
from __future__ import annotations

import pandas as pd
import pytest

from analysis.quant_performance import calculate_quant_metrics
from core.ml.meta_labeler import SignalMetaLabeler


def test_calculate_quant_metrics_on_mock_trades():
    trades = pd.DataFrame([
        {"current_status": "WIN_TP2", "pnl_pct": 2.5, "entry_date": "2026-08-01", "exit_date": "2026-08-02"},
        {"current_status": "WIN_TP2", "pnl_pct": 1.5, "entry_date": "2026-08-02", "exit_date": "2026-08-03"},
        {"current_status": "LOSS_SL", "pnl_pct": -1.0, "entry_date": "2026-08-03", "exit_date": "2026-08-04"},
        {"current_status": "LOSS_SL", "pnl_pct": -1.0, "entry_date": "2026-08-04", "exit_date": "2026-08-05"},
    ])
    closed_statuses = ("WIN_TP2", "LOSS_SL")
    m = calculate_quant_metrics(trades, closed_statuses, mc_simulations=50)

    assert m.total_trades == 4
    assert m.closed_trades == 4
    assert m.winning_trades == 2
    assert m.losing_trades == 2
    assert m.win_rate == 0.50
    assert m.profit_factor == 2.0  # (2.5 + 1.5) / (1.0 + 1.0)
    assert m.expectancy > 0
    assert m.max_drawdown >= 0.0


def test_meta_labeler_feature_extraction_and_training():
    records = []
    for i in range(60):
        is_win = (i % 3 != 0)  # 66% win rate
        pnl = 2.0 if is_win else -1.5
        status = "WIN_TP2" if is_win else "LOSS_SL"
        records.append({
            "ticker": f"SYM{i}",
            "asset_class": "stocks" if i % 2 == 0 else "crypto",
            "direction": "LONG",
            "entry_zone": [100.0, 100.5],
            "stop_loss": 98.0 if is_win else 92.0,  # Tighter stop correlates with win
            "target1": 103.0,
            "target2": 106.0,
            "current_status": status,
            "pnl_pct": pnl,
            "entry_date": "2026-08-10 14:00:00",
        })

    df = pd.DataFrame(records)
    labeler = SignalMetaLabeler(n_estimators=20, max_depth=3)
    feat_df, y = labeler.extract_features(df)

    assert len(feat_df) == 60
    assert "stop_dist_pct" in feat_df.columns
    assert "rr_ratio" in feat_df.columns

    report = labeler.train_and_evaluate(feat_df, y, confidence_threshold=0.50)
    assert report.sample_size == 60
    assert report.roc_auc >= 0.50
    assert labeler.is_fitted

    pred_prob = labeler.predict_trade_probability({
        "is_long": 1.0,
        "is_crypto": 0.0,
        "stop_dist_pct": 0.02,
        "t1_dist_pct": 0.03,
        "rr_ratio": 1.5,
        "t2_expansion": 2.0,
        "entry_hour": 14.0,
    })
    assert 0.0 <= pred_prob <= 1.0
