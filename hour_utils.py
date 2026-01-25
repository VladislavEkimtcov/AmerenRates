from datetime import datetime, timedelta


def _parse_hour(hour_value):
    try:
        return int(hour_value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid hour value: {hour_value}")


def detect_hour_offset(hourly_price_details):
    hours = []
    for item in hourly_price_details or []:
        try:
            hours.append(_parse_hour(item.get("hour")))
        except ValueError:
            continue
    if not hours:
        return 0
    return min(hours) % 24


def normalize_hour(hour_str, offset=0):
    hour = _parse_hour(hour_str)
    return (hour - offset) % 24


def hour_to_time(hour_str, offset=0):
    hour = normalize_hour(hour_str, offset)
    time_obj = (datetime.min + timedelta(hours=hour)).strftime("%-I:%M")
    return time_obj
