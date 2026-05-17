# AmerenRates
Quick and dirty python 3.8.x script to get electricity rates from Ameren

## 3cols_combo AI analysis

`3cols_combo.py` can generate background LLM analysis for the rate day and
current hour. Copy `rate_analysis.env.example` to `.env`, set
`RATE_OPENAI_MODEL_ID`, and point `RATE_OPENAI_ENDPOINT` at an OpenAI-compatible
API.

```bash
cp rate_analysis.env.example .env
python 3cols_combo.py
```

The app refreshes itself every minute. Press `i` to toggle the cached analysis
view. Daily and hourly results are stored in `rate_thoughts.json`.
