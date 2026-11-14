"""helper: job application automation.

Deterministic Python does the work (Playwright, SQLite, Notion REST, LaTeX). Every judgment step is
a file handoff to the interactive Claude Code session: helper writes an input file, Claude writes the
output file, helper validates it. helper never calls an AI API.
"""

__version__ = "0.1.0"
