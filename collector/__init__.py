"""Garmin data collector.

Two-layer store:
- data/raw/      exactly what Garmin returned (immutable, reprocessable)
- data/derived/  regenerable, coach-friendly rollups (the coach reads this)
"""
