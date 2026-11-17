"""Reconstruct project history directly into canonical .intent data.

Evidence and uncertainty remain explicit. Processing checkpoints are disposable;
ordinary intent queries consume the versioned records, without a second store.
"""

STATE_FORMAT = 1
ANALYSIS_VERSION = "2"
PROMPT_VERSION = "1"

RECONSTRUCTED_LABEL = (
    "RECONSTRUCTED INTENT: imported Git history with evidence, inference basis and uncertainty. "
    "Verification is pending; historical associations do not establish current applicability."
)
