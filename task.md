We got a note that while the OpenAI-compatible endpoint used by 3cols_combo.py is free, reducing use is always welcome. We have a new strategy to reduce use. Right now, the logic to look at the current hour and compare it to what's coming is run hourly. This has two problems: frequent reconsiderations of pretty much the same data and no look-back/reflection earlier in the day.

We propose the following change to the hourly analysis: instead of running it hourly, run it at the start (or cache miss) of the day, prompting to get a short, single-sentence summary for EVERY hour of the day relative to all other hours. Then, the per-hour info shower will draw from this cache.

Please update the analysis logic and prompt template to reflect this change.