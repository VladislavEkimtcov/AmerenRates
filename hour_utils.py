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


def _parse_price(price_value):
    try:
        return float(price_value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid price value: {price_value}")


def shift_hours_if_last_zero(hourly_price_details):
    """
    If the last hour entry has a zero price, rotate the list forward by one hour so
    midnight (00) gets the zero price and all other hours move ahead by one.
    Hours are reassigned sequentially to keep labels aligned (00-23).
    """
    if not hourly_price_details:
        return hourly_price_details

    try:
        last_price = _parse_price(hourly_price_details[-1].get("price"))
    except ValueError:
        return hourly_price_details

    if last_price != 0:
        return hourly_price_details

    rotated = [hourly_price_details[-1]] + hourly_price_details[:-1]
    adjusted = []
    for idx, item in enumerate(rotated):
        new_item = dict(item)
        new_item["hour"] = f"{idx:02d}"
        adjusted.append(new_item)

    return adjusted
