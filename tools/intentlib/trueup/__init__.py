"""Intent true-up: reconcile a repository's recorded intent with what its history shows.

Like a financial true-up, the goal is to make the books reflect reality after the fact, without
rewriting earlier entries. Everything here is optional and non-authoritative. Nothing in this
package is imported by canonical validation, and nothing it writes is read as canonical intent.
Reconstructed material lives in a separate state directory and reaches `.intent/` only through
`trueup promote`, an explicit reviewed transaction.
"""

STATE_FORMAT = 1
ANALYSIS_VERSION = "1"
PROMPT_VERSION = "1"

NON_AUTHORITATIVE_LABEL = (
    "TRUE-UP (RETROSPECTIVE, NON-AUTHORITATIVE): reconstructed after the fact from historical evidence, "
    "partly by an AI model. Not captured when the commit was made, not human-approved, not independently "
    "verified, and not a canonical constraint. Historical decisions may have been reversed; present "
    "applicability is unconfirmed."
)
