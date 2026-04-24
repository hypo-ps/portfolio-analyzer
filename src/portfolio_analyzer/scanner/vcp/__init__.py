"""VCP scanner (Volatility Contraction Pattern).

Implements the three-stage pipeline described in decisions D-S16+:
- Stage 1 hard filters (liquidity, trend, price strength, near-highs)
- Stage 2 fundamentals (reject + score)
- Stage 3 VCP detection (6 sub-scores + readiness + final compose)
"""
