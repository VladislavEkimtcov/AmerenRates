## Project Overview
AmerenRates is a terminal-based utility written in Python 3.8.x. It fetches real-time hourly electricity rates from the Ameren API, visualizes them using an ANSI-colored bar chart TUI, and optionally performs background LLM analysis on the data.

## Key Files
* `3cols_combo.py`: The main execution script containing the TUI render loop, API fetching, and background LLM threading.
* `PROCESS_RATE_PROMPT.md`: The markdown template used to generate prompts for the LLM rate analysis.
* `README.md`: Project documentation and quickstart instructions.

## Architecture & Constraints
1. **Python Version**: Target Python 3.8.x compatibility.
2. **Concurrency**: The main TUI runs on a synchronous refresh loop. LLM analysis (`ensure_rate_analysis_background`) is spawned in background threads (`threading.Thread`) to avoid blocking the UI.
4. **Thread Safety**: Any modification or reading of the `rate_thoughts.json` state MUST be protected by the `RATE_ANALYSIS_LOCK`.
5. **Caching**: Aggressively cache network calls to prevent API bans and excessive LLM use (`cached_rates.json` and `rate_thoughts.json`).