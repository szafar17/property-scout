"""Property Scout — daily real-estate deal-flow pipeline for a licensed NJ agent.

The package ingests active MLS listings (Bergen / Passaic / Hudson counties),
normalizes and dedupes them across boards, enriches them with appreciation,
safety, and convenience signals, scores each 0-100 from config-driven weights,
and upserts the results into a Notion pipeline database.
"""

__version__ = "0.1.0"
