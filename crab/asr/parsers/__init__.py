"""Voice transcript parsers used by the controller / channel driver.

These translate free-form ASR output into structured intents:
  - :mod:`yes_no` — parses permission-relay yes/no answers
  - :mod:`menu_select` — parses ``ask_menu`` answers (positional, label, LLM fallback)
"""
