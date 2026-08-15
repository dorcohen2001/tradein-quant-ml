"""Model-agnostic paper-trading plumbing.

Strictly the MECHANISM of persisting and journalling trades -- never the
lifecycle. That distinction is the whole design here, and it is deliberate:

  SHARED (this package)     atomic JSON store I/O, the corrupt-store policy,
                            the Excel upsert-by-ID mechanism, P&L arithmetic.
                            These were byte-identical duplicates in both
                            models, down to the comments.

  PER-MODEL (models/*)      the state machine, the status vocabulary, the
                            schema, the migration, and every threshold. Livne
                            runs PENDING -> ACTIVE -> PARTIAL_TP1 -> WIN_TP2 /
                            LOSS_SL_BE / EMERGENCY_EXIT with a breakeven move
                            and blended two-leg P&L; Biton runs a single-shot
                            PENDING -> ACTIVE -> WIN_TP1 / WIN_TP2 / LOSS_SL.

Unifying those state machines was explicitly NOT attempted. They are genuinely
different strategies, they are tracking real open positions (133 Livne and 36
Biton trades when this was written), and a subtle merge bug would corrupt live
history rather than fail loudly. What is shared here is only the code that was
already identical.
"""
