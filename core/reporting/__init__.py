"""Scheduled Telegram summary reports (daily + weekly), shared by both models.

Model-agnostic by construction: the builders take a list of trade dicts and
the caller's own status vocabulary, never a model's `config`. Livne and Biton
have genuinely different lifecycles (Livne has PARTIAL_TP1 / LOSS_SL_BE /
EMERGENCY_EXIT and never emits WIN_TP1; Biton is the reverse), so every status
set is passed in rather than assumed here.
"""
