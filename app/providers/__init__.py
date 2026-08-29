"""Model providers. One protocol, and however many implementations are in use.

The call site in `app/plans/generate.py` never names a provider. That is not
future-proofing for its own sake: the planner runs behind a flag that ships off, falls
back to deterministic rules whenever anything goes wrong, and is expected to change
vendor — so the seam has to be real rather than aspirational.
"""
