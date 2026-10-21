"""Machine Learning Meta-Labeling Pipeline for Trade Quality Classification.

Implements Marcos López de Prado's Meta-Labeling architecture:
The primary trading strategy determines direction and entry levels.
The secondary Machine Learning model predicts the probability of the trade
achieving positive outcome (TP1/TP2 before SL), filtering low-confidence trades.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.metrics import accuracy_score, brier_score_loss, precision_score, recall_score, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit


@dataclass
class MetaLabelingReport:
    sample_size: int
    train_size: int
    test_size: int
    baseline_win_rate: float
    filtered_win_rate: float
    roc_auc: float
    accuracy: float
    precision: float
    recall: float
    brier_score: float
    feature_importances: dict[str, float]
    sharpe_before: float
    sharpe_after: float
    total_pnl_before: float
    total_pnl_after: float


class SignalMetaLabeler:
    """Trains and predicts trade success probability based on entry features."""

    def __init__(self, n_estimators: int = 150, max_depth: int = 4, random_state: int = 42):
        self.model = GradientBoostingClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            learning_rate=0.05,
            subsample=0.8,
            random_state=random_state,
        )
        self.feature_names: list[str] = []
        self.is_fitted: bool = False

    def extract_features(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series]:
        """Extracts engineered tabular features from trade records."""
        records = []
        labels = []

        for _, row in df.iterrows():
            # Skip unclosed or invalid trades
            pnl = row.get("pnl_pct")
            status = row.get("current_status")
            if pd.isna(pnl) or status in ("PENDING", "ACTIVE"):
                continue

            # Binary label: 1 if profitable trade, 0 otherwise
            label = 1 if float(pnl) > 0 else 0

            # Price levels
            entry = None
            if "entry_zone" in row and isinstance(row["entry_zone"], (list, tuple)) and len(row["entry_zone"]) >= 2:
                entry = (float(row["entry_zone"][0]) + float(row["entry_zone"][1])) / 2.0
            elif "entry_price" in row and pd.notna(row["entry_price"]):
                entry = float(row["entry_price"])

            stop = float(row.get("stop_loss") or row.get("initial_stop_loss") or 0.0)
            t1 = float(row.get("target1") or 0.0)
            t2 = float(row.get("target2") or 0.0)

            if not entry or entry <= 0 or stop <= 0:
                continue

            direction = str(row.get("direction", "LONG")).upper()
            is_long = 1.0 if direction == "LONG" else 0.0

            # Feature Engineering:
            # 1. Stop distance in percent (volatility / risk proxy)
            stop_dist_pct = abs(entry - stop) / entry

            # 2. Target 1 distance in percent
            t1_dist_pct = abs(t1 - entry) / entry if t1 > 0 else stop_dist_pct

            # 3. Structural R:R ratio
            rr_ratio = t1_dist_pct / stop_dist_pct if stop_dist_pct > 0 else 1.0

            # 4. Target 2 expansion factor
            t2_expansion = (abs(t2 - entry) / entry) / t1_dist_pct if (t1_dist_pct > 0 and t2 > 0) else 1.5

            # 5. Asset class encoding
            asset_class = str(row.get("asset_class", "stocks")).lower()
            is_crypto = 1.0 if asset_class == "crypto" else 0.0

            # 6. Timing feature if available
            entry_hour = 12.0
            if "entry_date" in row and pd.notna(row["entry_date"]):
                dt_val = pd.to_datetime(row["entry_date"])
                entry_hour = float(dt_val.hour)

            feat = {
                "is_long": is_long,
                "is_crypto": is_crypto,
                "stop_dist_pct": stop_dist_pct,
                "t1_dist_pct": t1_dist_pct,
                "rr_ratio": rr_ratio,
                "t2_expansion": t2_expansion,
                "entry_hour": entry_hour,
                "pnl_pct": float(pnl),  # Kept for performance benchmarking
            }
            records.append(feat)
            labels.append(label)

        if not records:
            return pd.DataFrame(), pd.Series(dtype=int)

        full_df = pd.DataFrame(records)
        y = pd.Series(labels, name="label")
        return full_df, y

    def train_and_evaluate(
        self,
        features_df: pd.DataFrame,
        y: pd.Series,
        confidence_threshold: float = 0.55,
    ) -> MetaLabelingReport:
        """Trains model with time-series cross validation to prevent lookahead bias."""
        feature_cols = [c for c in features_df.columns if c != "pnl_pct"]
        self.feature_names = feature_cols
        X = features_df[feature_cols].copy()

        n_samples = len(X)
        if n_samples < 30:
            raise ValueError(f"Insufficient sample size for meta-labeling: {n_samples}")

        # Out-of-sample Time-Series Split (80% train, 20% holdout test)
        split_idx = int(n_samples * 0.75)
        X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
        pnl_test = features_df["pnl_pct"].iloc[split_idx:]

        self.model.fit(X_train, y_train)
        self.is_fitted = True

        y_prob = self.model.predict_proba(X_test)[:, 1]
        y_pred = (y_prob >= 0.50).astype(int)

        # Baseline vs Filtered metrics
        baseline_win_rate = float(y_test.mean())
        filter_mask = y_prob >= confidence_threshold
        filtered_win_rate = float(y_test[filter_mask].mean()) if filter_mask.sum() > 0 else 0.0

        roc_auc = float(roc_auc_score(y_test, y_prob)) if len(np.unique(y_test)) > 1 else 0.5
        acc = float(accuracy_score(y_test, y_pred))
        prec = float(precision_score(y_test, y_pred, zero_division=0))
        rec = float(recall_score(y_test, y_pred, zero_division=0))
        brier = float(brier_score_loss(y_test, y_prob))

        # Financial impact calculation on test set
        test_returns_all = pnl_test.values / 100.0
        test_returns_filtered = pnl_test[filter_mask].values / 100.0 if filter_mask.sum() > 0 else np.array([0.0])

        sharpe_before = float((np.mean(test_returns_all) / (np.std(test_returns_all) + 1e-6)) * np.sqrt(252))
        sharpe_after = float((np.mean(test_returns_filtered) / (np.std(test_returns_filtered) + 1e-6)) * np.sqrt(252))

        importances = dict(zip(feature_cols, self.model.feature_importances_))
        sorted_imp = dict(sorted(importances.items(), key=lambda item: item[1], reverse=True))

        return MetaLabelingReport(
            sample_size=n_samples,
            train_size=len(X_train),
            test_size=len(X_test),
            baseline_win_rate=baseline_win_rate,
            filtered_win_rate=filtered_win_rate,
            roc_auc=roc_auc,
            accuracy=acc,
            precision=prec,
            recall=rec,
            brier_score=brier,
            feature_importances=sorted_imp,
            sharpe_before=sharpe_before,
            sharpe_after=sharpe_after,
            total_pnl_before=float(np.sum(test_returns_all)),
            total_pnl_after=float(np.sum(test_returns_filtered)),
        )

    def predict_trade_probability(self, trade_features: dict[str, float]) -> float:
        """Predicts probability of success for an incoming candidate trade setup."""
        if not self.is_fitted:
            raise RuntimeError("Model must be fitted before calling predict_trade_probability")
        df_row = pd.DataFrame([trade_features])[self.feature_names]
        prob = self.model.predict_proba(df_row)[0, 1]
        return float(prob)
