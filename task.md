Let's add support for checking tomorrow's rates in 3cols_combo.py.
Every hour, check if tomorrow's rates are available by filing a request to the same RATES_URL, difference being that "SelectedDate" in the posted payload will be tomorrow's date. An example of a successful response is:
```json
{
    "chartBytes": "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",
    "selectedDate": "May 28, 2026",
    "isNextDay": true,
    "hourlyPriceDetails": [
        {
            "hour": "01",
            "date": "2026-05-28T00:00:00",
            "price": 0.02884
        },
        {
            "hour": "02",
            "date": "2026-05-28T00:00:00",
            "price": 0.02454
        },
        {
            "hour": "03",
            "date": "2026-05-28T00:00:00",
            "price": 0.02319
        },
        {
            "hour": "04",
            "date": "2026-05-28T00:00:00",
            "price": 0.0233
        },
        {
            "hour": "05",
            "date": "2026-05-28T00:00:00",
            "price": 0.02454
        },
        {
            "hour": "06",
            "date": "2026-05-28T00:00:00",
            "price": 0.02639
        },
        {
            "hour": "07",
            "date": "2026-05-28T00:00:00",
            "price": 0.02641
        },
        {
            "hour": "08",
            "date": "2026-05-28T00:00:00",
            "price": 0.02444
        },
        {
            "hour": "09",
            "date": "2026-05-28T00:00:00",
            "price": 0.02249
        },
        {
            "hour": "10",
            "date": "2026-05-28T00:00:00",
            "price": 0.0225
        },
        {
            "hour": "11",
            "date": "2026-05-28T00:00:00",
            "price": 0.02431
        },
        {
            "hour": "12",
            "date": "2026-05-28T00:00:00",
            "price": 0.02616
        },
        {
            "hour": "13",
            "date": "2026-05-28T00:00:00",
            "price": 0.0269
        },
        {
            "hour": "14",
            "date": "2026-05-28T00:00:00",
            "price": 0.0285
        },
        {
            "hour": "15",
            "date": "2026-05-28T00:00:00",
            "price": 0.0309
        },
        {
            "hour": "16",
            "date": "2026-05-28T00:00:00",
            "price": 0.03292
        },
        {
            "hour": "17",
            "date": "2026-05-28T00:00:00",
            "price": 0.03629
        },
        {
            "hour": "18",
            "date": "2026-05-28T00:00:00",
            "price": 0.04637
        },
        {
            "hour": "19",
            "date": "2026-05-28T00:00:00",
            "price": 0.07334
        },
        {
            "hour": "20",
            "date": "2026-05-28T00:00:00",
            "price": 0.08382
        },
        {
            "hour": "21",
            "date": "2026-05-28T00:00:00",
            "price": 0.06402
        },
        {
            "hour": "22",
            "date": "2026-05-28T00:00:00",
            "price": 0.03506
        },
        {
            "hour": "23",
            "date": "2026-05-28T00:00:00",
            "price": 0.02812
        },
        {
            "hour": "24",
            "date": "2026-05-28T00:00:00",
            "price": 0.02733
        }
    ],
    "isErrorFetchingData": false
}
```

As you can see, "isNextDay": true is the indicator that it's tomorrow's rates we are looking at, and all hours are present.

When the rates for tomorrow are fetched, store them in tomorrow_cached.json. No need to attempt the tomorrow fetch till the cache grows stale, indicative of the date in the tomorrow cache no longer being tomorrow.

When the dates for tomorrow become available, they are run through the PROCESS_RATE_PROMPT.md in "daily" mode. No need for special handling, as the rates info for today and tomorrow is not much different.

At this point, we might have up to three outstanding prompts for the endpoint. To not overload it, please add a batching system to the LLM analysis, so that prompts are passed to the endpoint only one at a time.

Let's talk about the UX part of this. Right now, the only two screens are today's rates and analysis. Change the following:
1. On today's rates screen, it is now possible to press the "t" button to view the graph for tomorrow.
2. The bottom bar now reads "[i] AI [t] Tomorrow" on the today graph and "[i] AI [t] Today" on the tomorrow graph. The backwards seconds stay where they are.
3. The new "Tomorrow outlook" section is now made available on the reports page.