"""CLI script to train and evaluate the Machine Learning Meta-Labeler on live trade logs.

Usage:
    python analysis/train_meta_model.py --model livne --threshold 0.55
"""
from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from analysis.quant_performance import load_trades_df
from core.ml.meta_labeler import SignalMetaLabeler


def main():
    parser = argparse.ArgumentParser(description="Train ML Signal Meta-Labeler")
    parser.add_argument("--model", default="livne", help="Model key (default: livne)")
    parser.add_argument("--threshold", type=float, default=0.55, help="Confidence threshold (default: 0.55)")
    args = parser.parse_args()

    print(f"Loading trades for model: {args.model}...")
    df = load_trades_df(args.model)

    labeler = SignalMetaLabeler()
    features_df, y = labeler.extract_features(df)

    if features_df.empty:
        print("No valid features could be extracted from trade store.")
        return

    print(f"Extracted {len(features_df)} trade feature rows. Training Meta-Labeler...")
    rep = labeler.train_and_evaluate(features_df, y, confidence_threshold=args.threshold)

    print("\n" + "=" * 80)
    print(f"MACHINE LEARNING META-LABELING RESULTS [{args.model.upper()}]")
    print("=" * 80)
    print(f"Dataset Size          : {rep.sample_size} total trades ({rep.train_size} train, {rep.test_size} out-of-sample test)")
    print(f"Out-of-Sample ROC-AUC : {rep.roc_auc:.4f}")
    print(f"Accuracy / Brier Score: {rep.accuracy:.2%} / {rep.brier_score:.4f}")
    print(f"Precision / Recall    : {rep.precision:.2%} / {rep.recall:.2%}")
    print("-" * 80)
    print("QUANTITATIVE STRATEGY ENHANCEMENT (TEST SET):")
    print(f"  Baseline Win Rate   : {rep.baseline_win_rate:.2%}")
    print(f"  ML-Filtered Win Rate: {rep.filtered_win_rate:.2%} (Threshold >= {args.threshold:.2f})")
    print(f"  Sharpe Ratio Before : {rep.sharpe_before:.2f}")
    print(f"  Sharpe Ratio After  : {rep.sharpe_after:.2f}")
    print(f"  Cumulative PnL      : {rep.total_pnl_before:+.2%} -> {rep.total_pnl_after:+.2%}")
    print("-" * 80)
    print("FEATURE IMPORTANCES (GINI):")
    for feat, imp in rep.feature_importances.items():
        bar = "#" * int(imp * 40)
        print(f"  {feat:20}: {imp:6.2%} {bar}")
    print("=" * 80)


if __name__ == "__main__":
    main()
