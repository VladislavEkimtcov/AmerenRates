from datetime import datetime, timedelta


def detect_hour_offset(hourly_price_details):
    hours = []
    for item in hourly_price_details or []:
        hour_value = item.get("hour")
        try:
            hours.append(int(hour_value))
        except (TypeError, ValueError):
            continue
    if not hours:
        return 0
    return min(hours) % 24


def normalize_hour(hour_str, offset=0):
    try:
        hour = int(hour_str)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid hour value: {hour_str}")
    return (hour - offset) % 24


def hour_to_time(hour_str, offset=0):
    hour = normalize_hour(hour_str, offset)
    time_obj = (datetime.min + timedelta(hours=hour)).strftime("%-I:%M")
    return time_obj
