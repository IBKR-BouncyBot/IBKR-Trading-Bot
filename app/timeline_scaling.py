"""Pure helpers for completed-cycle audit timeline scaling.

These functions normalize imported timestamps/prices, select robust display
bounds, and position path/marker/event rows without importing PySide6. They are
visualization-only and do not affect strategy, order, storage, or broker state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import isfinite
from typing import Any

DEFAULT_TIMELINE_OUTLIER_PADDING_FRACTION = 0.08


TIMELINE_PATH_START_FRACTION = 0.08
TIMELINE_PATH_END_FRACTION = 0.98



def finite_float(value: Any) -> float | None:
    """Return a finite float, or None for blanks, NaN, infinities, and non-numbers."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return number if isfinite(number) else None


def positive_price(value: Any) -> float | None:
    """Return a finite positive price, ignoring zero/negative API placeholders."""
    number = finite_float(value)
    if number is None or number <= 0:
        return None
    return number




def parse_timeline_timestamp(value: Any) -> float | None:
    """Parse a persisted/captured timestamp to POSIX seconds for audit scaling."""
    if value is None or value == "":
        return None
    number = finite_float(value)
    if number is not None and abs(number) > 100000.0:
        return number
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(str(value).strip(), fmt).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue
    return None




def choose_timestamp_for_display(
    preferred: Any,
    fallback: Any = None,
    *,
    reference_values: Sequence[Any] = (),
    max_reference_offset_seconds: float = 90 * 60,
) -> float | None:
    """Choose the timestamp that best aligns GUI audit rows on one UTC axis.

    IB execution callbacks can carry broker/local-session timestamps that are
    correct as broker records but not always in the same timezone basis as the
    app's market-capture rows.  The audit timeline is a GUI reconstruction, so
    it should prefer app-confirmed cycle/order/decision timestamps when they
    exist and use broker execution time only when it is compatible with the
    surrounding app/capture timestamps.
    """
    preferred_ts = parse_timeline_timestamp(preferred)
    fallback_ts = parse_timeline_timestamp(fallback)
    if preferred_ts is None:
        return fallback_ts
    if fallback_ts is None:
        return preferred_ts
    references = [
        ts for ts in (parse_timeline_timestamp(value) for value in reference_values)
        if ts is not None
    ]
    if not references:
        return preferred_ts
    tolerance = max(0.0, float(max_reference_offset_seconds))
    preferred_distance = min(abs(preferred_ts - ts) for ts in references)
    fallback_distance = min(abs(fallback_ts - ts) for ts in references)
    if fallback_distance <= tolerance and fallback_distance + 1.0 < preferred_distance:
        return fallback_ts
    return preferred_ts



def preferred_timeline_timestamp(
    candidates: Sequence[Any],
    reference_window: tuple[float, float] | None = None,
    *,
    tolerance_seconds: float = 3600.0,
) -> float | None:
    """Return the first parsed candidate that is coherent with the capture axis.

    Imported IB execution rows can occasionally carry a broker/exchange-local
    timestamp that is hours away from the app's UTC market-capture timestamps.
    For timeline drawing, prefer app-observed cycle/order timestamps that fall
    near the captured price window, and suppress out-of-window timestamps when a capture window is available. This is a GUI/audit display helper only.
    """
    parsed = [ts for ts in (parse_timeline_timestamp(value) for value in candidates) if ts is not None]
    if not parsed:
        return None
    if reference_window is None:
        return parsed[0]
    low, high = reference_window
    if high < low:
        low, high = high, low
    pad = max(0.0, float(tolerance_seconds))
    for ts in parsed:
        if (low - pad) <= ts <= (high + pad):
            return ts
    return None

def time_window_from_values(values: Sequence[Any], *, pad_seconds: float = 3600.0) -> tuple[float, float] | None:
    """Return a padded time window for a cycle from known cycle/order/fill times."""
    parsed = [ts for ts in (parse_timeline_timestamp(value) for value in values) if ts is not None]
    if not parsed:
        return None
    pad = max(0.0, float(pad_seconds))
    return min(parsed) - pad, max(parsed) + pad




def row_time_in_window(
    row: Mapping[str, Any],
    window: tuple[float, float] | None,
    *,
    time_keys: Sequence[str] = ("captured_at_utc", "event_time_utc", "timestamp", "time", "created_at", "updated_at"),
) -> bool | None:
    """Return whether a captured row belongs to a window, or None when undated."""
    if window is None:
        return True
    for key in time_keys:
        if key not in row:
            continue
        ts = parse_timeline_timestamp(row.get(key))
        if ts is None:
            continue
        low, high = window
        return low <= ts <= high
    return None


def filter_rows_to_time_window(
    rows: Sequence[Mapping[str, Any]],
    window: tuple[float, float] | None,
    *,
    keep_undated: bool = False,
) -> list[dict[str, Any]]:
    """Filter loose imported capture rows so unrelated cycles do not distort scaling."""
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        result = row_time_in_window(row, window)
        if result is True or (result is None and keep_undated):
            filtered.append(dict(row))
    return filtered


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        raise ValueError("sorted_values must not be empty")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    fraction = max(0.0, min(1.0, float(fraction)))
    pos = fraction * (len(sorted_values) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = pos - lower
    return float(sorted_values[lower]) * (1.0 - weight) + float(sorted_values[upper]) * weight


def _median(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    return _percentile(ordered, 0.50)


def _iqr_filtered(values: Sequence[float]) -> list[float]:
    """Remove extreme one-off values while preserving normal trading movement."""
    if len(values) < 3:
        return list(values)
    ordered = sorted(values)
    center = _percentile(ordered, 0.50)
    if center > 0:
        # Imported debug-capture folders may contain unrelated cycles or stale API
        # placeholders. A 10x move within one completed cycle is treated as a
        # capture-selection/scaling outlier, not normal price action.
        ratio_low = center / 10.0
        ratio_high = center * 10.0
        ratio_filtered = [value for value in ordered if ratio_low <= value <= ratio_high]
        if len(ratio_filtered) >= max(2, len(values) // 2):
            ordered = ratio_filtered
    if len(ordered) < 8:
        # Small marker-only fallback timelines do not have enough samples for a
        # stable IQR. Keep wide real movement visible but reject impossible stale
        # values that are several multiples away from the median marker.
        center = _percentile(ordered, 0.50)
        small_pad = max(abs(center) * 0.75, 0.01)
        filtered = [value for value in ordered if (center - small_pad) <= value <= (center + small_pad)]
        return filtered if len(filtered) >= 2 else list(ordered)
    center = _percentile(ordered, 0.50)
    q1 = _percentile(ordered, 0.25)
    q3 = _percentile(ordered, 0.75)
    iqr = q3 - q1
    minimum_iqr = max(abs(center) * 0.002, 0.01)
    spread = max(iqr, minimum_iqr)
    low_fence = q1 - 4.0 * spread
    high_fence = q3 + 4.0 * spread
    filtered = [value for value in values if low_fence <= value <= high_fence and (center <= 0 or center / 10.0 <= value <= center * 10.0)]
    return filtered if len(filtered) >= max(4, len(values) // 2) else list(ordered)


def marker_centered_price_window(important_prices: Sequence[Any]) -> tuple[float, float] | None:
    """Return a tight marker-centered window for a completed cycle timeline.

    BUY/SELL/anchor/drop markers are the audit anchors. Imported debug capture
    folders can contain extra rows from neighbouring cycles, so the timeline must
    not let unrelated path prices decide the primary Y-axis when real trade
    markers are available.
    """
    clean = [price for price in (positive_price(value) for value in important_prices) if price is not None]
    if not clean:
        return None
    core = _iqr_filtered(clean)
    if not core:
        core = clean
    low = min(core)
    high = max(core)
    center = (low + high) / 2.0
    span = max(high - low, abs(center) * 0.004, 0.01)
    padding = max(span * 0.65, abs(center) * 0.01, 0.02)
    return max(0.0, low - padding), high + padding


def _path_prices_near_markers(path_prices: Sequence[float], important_prices: Sequence[float]) -> list[float]:
    """Keep captured prices near the cycle's trade-marker range.

    v2.24 deliberately retains a tighter marker corridor than the old broad filter.
    The release reference identifies the regression baseline for this still-current
    rule. If the audit view has anchor/drop/BUY/SELL markers, those markers define
    the scale; imported capture rows far outside that corridor are disclosed as
    hidden rows instead of flattening the visual chart.
    """
    if not path_prices or not important_prices:
        return list(path_prices)
    window = marker_centered_price_window(important_prices)
    if window is None:
        return list(path_prices)
    low, high = window
    span = max(high - low, 0.01)
    center = (low + high) / 2.0
    extra = max(span * 1.5, abs(center) * 0.015, 0.02)
    corridor_low = max(0.0, low - extra)
    corridor_high = high + extra
    nearby = [price for price in path_prices if corridor_low <= price <= corridor_high]
    if not nearby:
        return []
    # Keep the nearby subset whenever markers exist. A full unrelated capture is
    # less useful than a clear marker-centered timeline with an explicit hidden
    # row count.
    return nearby


def display_price_bounds(
    path_prices: Sequence[Any],
    important_prices: Sequence[Any] = (),
    *,
    lower_percentile: float = 0.02,
    upper_percentile: float = 0.98,
) -> tuple[float, float] | None:
    """Return robust Y-axis bounds for a cycle timeline.

    The scale is marker-first. When BUY/SELL/anchor/drop markers are available,
    they define the Y-axis and capture rows outside the marker corridor are
    treated as imported/off-scale diagnostics. Without markers, the function
    falls back to percentile-based capture scaling.
    """
    clean_path = [price for price in (positive_price(value) for value in path_prices) if price is not None]
    clean_important = [price for price in (positive_price(value) for value in important_prices) if price is not None]
    important_core = _iqr_filtered(clean_important) if clean_important else []

    if important_core:
        # Marker-first scaling: completed cycle markers are the authoritative
        # scale for this audit view. Imported debug-capture folders may contain
        # valid positive rows from another time/cycle; those rows are disclosed
        # as hidden instead of flattening the BUY/SELL markers.
        marker_low = min(important_core)
        marker_high = max(important_core)
        marker_center = (marker_low + marker_high) / 2.0
        marker_span = marker_high - marker_low
        minimum_span = max(abs(marker_center) * 0.006, 0.01)
        span = max(marker_span, minimum_span)

        # Include nearby path rows only when they fit the marker-focused range.
        # The window is intentionally moderate; it should show the captured price
        # path around the trade, but not a full unrelated imported capture.
        nearby_pad = max(span * 1.5, abs(marker_center) * 0.0125, 0.02)
        nearby_path = [price for price in clean_path if (marker_low - nearby_pad) <= price <= (marker_high + nearby_pad)]
        if nearby_path:
            nearby_core = _iqr_filtered(nearby_path)
            marker_low = min(marker_low, min(nearby_core))
            marker_high = max(marker_high, max(nearby_core))
            marker_center = (marker_low + marker_high) / 2.0
            marker_span = marker_high - marker_low
            minimum_span = max(abs(marker_center) * 0.006, 0.01)
            span = max(marker_span, minimum_span)

        padding = max(span * 0.22, abs(marker_center) * 0.0025, 0.02)
        return max(0.0, marker_low - padding), marker_high + padding

    base_source = clean_path
    if not base_source:
        return None

    base = _iqr_filtered(base_source)
    ordered = sorted(base)
    if len(ordered) >= 20:
        low = _percentile(ordered, lower_percentile)
        high = _percentile(ordered, upper_percentile)
    elif len(ordered) >= 4:
        low = _percentile(ordered, 0.05)
        high = _percentile(ordered, 0.95)
    else:
        low = min(ordered)
        high = max(ordered)

    if high < low:
        low, high = high, low
    center = (low + high) / 2.0 if high != low else float(ordered[0])
    minimum_span = max(abs(center) * 0.006, 0.01)
    if (high - low) < minimum_span:
        low = center - minimum_span / 2.0
        high = center + minimum_span / 2.0

    span = max(high - low, minimum_span)
    center = (low + high) / 2.0
    padding = max(span * 0.12, abs(center) * 0.001, 0.01)
    return max(0.0, low - padding), high + padding


def price_within_bounds(
    value: Any,
    bounds: tuple[float, float] | None,
    *,
    pad_fraction: float = DEFAULT_TIMELINE_OUTLIER_PADDING_FRACTION,
) -> bool:
    """Return whether a path price belongs on the visible timeline axis.

    The cycle timeline is an audit view. Imported historical captures can
    contain rows from neighbouring cycles or stale API values that are far away
    from the actual BUY/SELL markers. Those rows should remain counted and
    disclosed, but not flatten the plotted trade path.
    """
    price = positive_price(value)
    if price is None or bounds is None:
        return False
    low, high = bounds
    if high < low:
        low, high = high, low
    span = max(high - low, 0.01)
    padding = max(span * max(0.0, float(pad_fraction)), 0.01)
    return (low - padding) <= price <= (high + padding)


def filter_path_points_for_display(
    points: Sequence[Mapping[str, Any]],
    important_prices: Sequence[Any] = (),
    *,
    price_key: str = "price",
) -> tuple[list[dict[str, Any]], int, tuple[float, float] | None]:
    """Filter off-scale imported capture rows before GUI timeline painting.

    Returns ``(visible_points, hidden_count, bounds)``. The bounds are computed
    from robust price scaling before the path is filtered, then the path rows
    outside those bounds are hidden from the drawn line. Important BUY/SELL and
    trigger markers still participate in the axis calculation so the displayed
    cycle remains coherent.
    """
    copied: list[dict[str, Any]] = []
    for point in points:
        if not isinstance(point, Mapping):
            continue
        price = positive_price(point.get(price_key))
        if price is None:
            continue
        item = dict(point)
        item[price_key] = price
        copied.append(item)
    path_prices = [item[price_key] for item in copied]
    bounds = display_price_bounds(path_prices, important_prices)
    if bounds is None:
        return copied, 0, None

    visible = [item for item in copied if price_within_bounds(item.get(price_key), bounds)]
    hidden = len(copied) - len(visible)
    if not visible and copied:
        # Never make the audit widget blank solely because all imported capture
        # rows were off-scale. Keep a compact fallback around the marker/bounds
        # center so the operator sees that data existed but was suspect.
        low, high = bounds
        center = (low + high) / 2.0
        visible = sorted(copied, key=lambda item: abs(float(item[price_key]) - center))[: min(2, len(copied))]
        hidden = len(copied) - len(visible)
    return visible, hidden, bounds

def timeline_path_time_window(
    points: Sequence[Mapping[str, Any]],
    *,
    time_key: str = "time",
) -> tuple[float, float] | None:
    """Return the timestamp span for plotted path rows when it is usable.

    The audit chart should use real marker times only when they overlap the
    capture rows shown in the plot. Imported debug captures often have separate
    BUY and SELL windows or stale cycle-level timestamps; in that case markers
    fall back to semantic stage positions instead of being crushed at the left
    edge.
    """
    times = [finite_float(item.get(time_key)) for item in points if isinstance(item, Mapping)]
    clean = [value for value in times if value is not None]
    if len(clean) < 2:
        return None
    low = min(clean)
    high = max(clean)
    if high <= low:
        return None
    return low, high


def timeline_path_position(
    index: int,
    count: int,
    *,
    start: float = TIMELINE_PATH_START_FRACTION,
    end: float = TIMELINE_PATH_END_FRACTION,
) -> float:
    """Return a stable left-to-right position for a captured price row.

    The position is based on row order, not elapsed wall-clock time. That avoids
    a common imported-capture problem where a large gap between capture ZIPs
    compresses all useful cycle movement into a tiny area of the chart.
    """
    if count <= 1:
        return clamp_fraction((start + end) / 2.0)
    start = clamp_fraction(start)
    end = clamp_fraction(end)
    if end < start:
        start, end = end, start
    frac = max(0.0, min(1.0, float(index) / float(max(1, count - 1))))
    return start + (end - start) * frac


def timeline_item_position(
    item: Mapping[str, Any],
    path_time_window: tuple[float, float] | None,
    *,
    fallback_position: float = 0.5,
    time_key: str = "time",
    start: float = TIMELINE_PATH_START_FRACTION,
    end: float = TIMELINE_PATH_END_FRACTION,
) -> float:
    """Return an x-position for a marker/transition/guard on the audit chart.

    If the item timestamp falls inside the plotted capture window, the marker is
    placed at the matching capture position. If the timestamp is missing or
    outside that window, the function uses the semantic stage hint supplied by
    the GUI. This keeps anchor/drop/BUY/SELL labels readable for imported
    historical captures whose timestamps do not line up with the plotted rows.
    """
    if path_time_window is not None:
        low, high = path_time_window
        if high > low:
            ts = finite_float(item.get(time_key))
            if ts is not None and low <= ts <= high:
                start = clamp_fraction(start)
                end = clamp_fraction(end)
                if end < start:
                    start, end = end, start
                frac = (ts - low) / (high - low)
                return start + (end - start) * clamp_fraction(frac)
    hint = finite_float(item.get("position_hint"))
    if hint is None:
        hint = finite_float(item.get("position"))
    if hint is not None:
        return clamp_fraction(hint)
    return clamp_fraction(fallback_position)


def clamp_fraction(value: Any) -> float:
    """Clamp a normalized coordinate to the drawable 0..1 range."""
    number = finite_float(value)
    if number is None:
        return 0.0
    return max(0.0, min(1.0, float(number)))


def _event_sort_value(item: Mapping[str, Any], timed_min: float | None, timed_max: float | None, fallback_order: float) -> tuple[Any, ...]:
    t = finite_float(item.get("time"))
    order = finite_float(item.get("order"))
    order_value = order if order is not None else fallback_order
    if t is not None:
        return (0, t, order_value)
    hint = finite_float(item.get("position_hint"))
    if hint is None:
        hint = finite_float(item.get("position"))
    if hint is not None:
        hint = clamp_fraction(hint)
        if timed_min is not None and timed_max is not None and timed_max > timed_min:
            synthetic_time = timed_min + (timed_max - timed_min) * hint
            return (0, synthetic_time, order_value)
        return (0, hint, order_value)
    return (1, order_value)


def normalized_axis_positions(buckets: Sequence[Sequence[Mapping[str, Any]]]) -> dict[tuple[int, int], float]:
    """Return ranked 0..1 positions for path, marker, transition, and guard buckets.

    Real elapsed time can be misleading for this audit view because completed
    cycles may have separate BUY/SELL capture windows with large gaps. The GUI
    sorts known events chronologically, uses semantic position hints for missing
    marker timestamps, then assigns ranked positions. This preserves order while
    keeping every important event visible.
    """
    timed_values: list[float] = []
    for bucket in buckets:
        for item in bucket:
            t = finite_float(item.get("time"))
            if t is not None:
                timed_values.append(t)
    timed_min = min(timed_values) if timed_values else None
    timed_max = max(timed_values) if timed_values else None

    records: list[tuple[tuple[Any, ...], tuple[int, int]]] = []
    for bucket_index, bucket in enumerate(buckets):
        for item_index, item in enumerate(bucket):
            sort_key = _event_sort_value(item, timed_min, timed_max, float(item_index)) + (bucket_index, item_index)
            records.append((sort_key, (bucket_index, item_index)))
    if not records:
        return {}
    if len(records) == 1:
        return {records[0][1]: 0.5}
    records.sort(key=lambda item: item[0])
    denominator = float(len(records) - 1)
    return {item_key: index / denominator for index, (_sort_key, item_key) in enumerate(records)}



def evenly_spaced_positions(count: int, *, start: float = 0.0, end: float = 1.0) -> list[float]:
    """Return stable 0..1 positions for drawing a dense timeline path.

    Audit timelines should not compress BUY/SELL markers because one capture
    window has many more rows than the marker/event buckets. This helper spaces
    a single dense bucket inside a semantic span while preserving source order.
    """
    try:
        count_int = int(count)
    except Exception:
        count_int = 0
    if count_int <= 0:
        return []
    left = clamp_fraction(start)
    right = clamp_fraction(end)
    if right < left:
        left, right = right, left
    if count_int == 1:
        return [(left + right) / 2.0]
    span = right - left
    denominator = float(count_int - 1)
    return [left + span * (idx / denominator) for idx in range(count_int)]

def _path_time_position(
    timestamp: Any,
    path_records: Sequence[tuple[float, float]],
) -> float | None:
    """Map an event timestamp to the order-spread price path position.

    The displayed audit path is intentionally spread by row order, not raw wall
    clock elapsed time. This helper aligns BUY/SELL markers to the nearest
    captured path row when their timestamp falls inside the plotted capture
    window, so markers and the blue path describe the same event sequence.
    """
    ts = finite_float(timestamp)
    if ts is None or not path_records:
        return None
    records = sorted(path_records, key=lambda item: item[0])
    if ts < records[0][0] or ts > records[-1][0]:
        return None
    if len(records) == 1:
        return records[0][1]
    previous_time, previous_position = records[0]
    if ts <= previous_time:
        return previous_position
    for current_time, current_position in records[1:]:
        if ts <= current_time:
            span = current_time - previous_time
            if span <= 0:
                return current_position
            fraction = (ts - previous_time) / span
            return previous_position + (current_position - previous_position) * clamp_fraction(fraction)
        previous_time, previous_position = current_time, current_position
    return records[-1][1]


def _path_price_position(
    price: Any,
    path_records: Sequence[tuple[float, float]],
    *,
    fallback_position: float | None = None,
    max_relative_difference: float = 0.0125,
) -> float | None:
    """Fallback-map an untimed event price to the nearest plotted path row.

    Timed rows use UTC-normalized timestamps. This helper remains for the
    fallback path when imported records lack usable event times but do contain
    prices close to the captured path.
    """
    target = positive_price(price)
    if target is None or not path_records:
        return None
    fallback = clamp_fraction(fallback_position) if fallback_position is not None else None
    best: tuple[float, float] | None = None
    for row_price, row_position in path_records:
        candidate = positive_price(row_price)
        if candidate is None:
            continue
        relative = abs(candidate - target) / max(abs(target), 0.01)
        position_penalty = abs(clamp_fraction(row_position) - fallback) * 0.001 if fallback is not None else 0.0
        score = relative + position_penalty
        if best is None or score < best[0]:
            best = (score, clamp_fraction(row_position))
    if best is None:
        return None
    if best[0] <= max(0.0, float(max_relative_difference)) + 0.002:
        return best[1]
    return None


def staged_axis_positions(
    buckets: Sequence[Sequence[Mapping[str, Any]]],
    *,
    path_start: float = 0.08,
    path_end: float = 0.96,
) -> dict[tuple[int, int], float]:
    """Return readable fallback X positions for the audit timeline.

    The primary chart uses :func:`true_time_axis_positions`. This staged
    helper remains the fallback for imported rows without reliable timestamps.
    """
    result: dict[tuple[int, int], float] = {}
    path_start = clamp_fraction(path_start)
    path_end = clamp_fraction(path_end)
    if path_end <= path_start:
        path_start, path_end = 0.10, 0.94

    path_bucket = buckets[0] if buckets else []
    path_time_records: list[tuple[float, float]] = []
    path_price_records: list[tuple[float, float]] = []
    path_count = len(path_bucket)
    for item_index, item in enumerate(path_bucket):
        if path_count == 1:
            position = (path_start + path_end) / 2.0
        else:
            fraction = item_index / float(max(1, path_count - 1))
            position = path_start + (path_end - path_start) * fraction
        position = clamp_fraction(position)
        result[(0, item_index)] = position
        if isinstance(item, Mapping):
            ts = finite_float(item.get("time"))
            if ts is not None:
                path_time_records.append((ts, position))
            price = positive_price(item.get("price"))
            if price is not None:
                path_price_records.append((price, position))

    for bucket_index, bucket in enumerate(buckets[1:], start=1):
        count = len(bucket)
        if count <= 0:
            continue
        for item_index, item in enumerate(bucket):
            if bucket_index == 1:
                hint = finite_float(item.get("position_hint"))
                if hint is None:
                    hint = finite_float(item.get("position"))
                if hint is not None:
                    fallback_position = clamp_fraction(hint)
                elif count == 1:
                    fallback_position = 0.5
                else:
                    fallback_position = 0.06 + 0.88 * (item_index / float(max(1, count - 1)))
            elif bucket_index == 2:
                fallback_position = 0.5 if count == 1 else 0.08 + 0.84 * (item_index / float(max(1, count - 1)))
            else:
                fallback_position = 0.58 if count == 1 else path_start + (path_end - path_start) * (item_index / float(max(1, count - 1)))

            aligned = _path_time_position(item.get("time"), path_time_records)
            if aligned is None and bucket_index == 1:
                aligned = _path_price_position(item.get("price"), path_price_records, fallback_position=fallback_position)
            position = aligned if aligned is not None else fallback_position
            result[(bucket_index, item_index)] = clamp_fraction(position)

    # Nudge marker labels apart just enough to keep them readable, but keep the
    # nudge small so timestamp/price alignment with the blue path remains clear.
    marker_keys = sorted([key for key in result if key[0] == 1], key=lambda key: result[key])
    minimum_marker_gap = 0.055
    for i in range(1, len(marker_keys)):
        prev_key = marker_keys[i - 1]
        key = marker_keys[i]
        if result[key] - result[prev_key] < minimum_marker_gap:
            result[key] = min(0.98, result[prev_key] + minimum_marker_gap)
    for i in range(len(marker_keys) - 2, -1, -1):
        key = marker_keys[i]
        next_key = marker_keys[i + 1]
        if result[next_key] - result[key] < minimum_marker_gap:
            result[key] = max(0.02, result[next_key] - minimum_marker_gap)
    return result


def true_time_axis_positions(
    buckets: Sequence[Sequence[Mapping[str, Any]]],
    *,
    path_start: float = 0.08,
    path_end: float = 0.96,
    time_key: str = "time",
    reference_window: tuple[float, float] | None = None,
) -> dict[tuple[int, int], float]:
    """Return X positions on one real timestamp axis for audit timelines.

    Unlike :func:`staged_axis_positions`, this helper does not stretch capture
    rows by row order and does not move BUY/SELL markers to semantic cycle
    positions when real timestamps are available. Every item with a valid
    timestamp is placed on the same linear time scale. When a plotted market
    path supplies ``reference_window``, that window is authoritative: unrelated
    or older action timestamps cannot compress the market path, and action rows
    outside the captured window are pinned to the nearest edge. Items without
    usable timestamps fall back to staged/readable positions so imported
    historical rows remain visible without pretending to have precise timing.

    The function is intentionally pure so Windows/Linux tests can validate the
    timeline alignment without importing PySide6.
    """
    fallback = staged_axis_positions(buckets, path_start=path_start, path_end=path_end)
    low: float
    high: float
    if reference_window is not None:
        window_low = finite_float(reference_window[0])
        window_high = finite_float(reference_window[1])
        if window_low is None or window_high is None or window_high <= window_low:
            reference_window = None
        else:
            low, high = window_low, window_high
    if reference_window is None:
        timed: list[float] = []
        for bucket in buckets:
            for item in bucket:
                if not isinstance(item, Mapping):
                    continue
                value = finite_float(item.get(time_key))
                if value is not None:
                    timed.append(value)
        if len(timed) < 2:
            return fallback
        low = min(timed)
        high = max(timed)
    if high <= low:
        return fallback

    path_start = clamp_fraction(path_start)
    path_end = clamp_fraction(path_end)
    if path_end <= path_start:
        path_start, path_end = 0.08, 0.96
    span = high - low
    result: dict[tuple[int, int], float] = {}
    for bucket_index, bucket in enumerate(buckets):
        for item_index, item in enumerate(bucket):
            position = fallback.get((bucket_index, item_index), 0.5)
            if isinstance(item, Mapping):
                value = finite_float(item.get(time_key))
                if value is not None:
                    fraction = (value - low) / span
                    position = path_start + (path_end - path_start) * clamp_fraction(fraction)
            result[(bucket_index, item_index)] = clamp_fraction(position)
    return result


def downsample_timeline_points(points: Sequence[Mapping[str, Any]], max_points: int = 480) -> list[dict[str, Any]]:
    """Return an evenly sampled copy of timeline path points for stable painting.

    The source capture can contain thousands of rows. Painting every row is not
    useful in the small audit widget and can make labels appear compressed. The
    downsample keeps the first/last point and local high/low prices within each
    bucket so the visible path still represents the cycle movement.
    """
    if max_points <= 0:
        return []
    copied = [dict(item) for item in points]
    if len(copied) <= max_points:
        return copied
    if max_points < 4:
        return [copied[0], copied[-1]][:max_points]

    bucket_count = max(1, (max_points - 2) // 2)
    middle = copied[1:-1]
    bucket_size = max(1, len(middle) // bucket_count)
    selected: list[dict[str, Any]] = [copied[0]]
    for start in range(0, len(middle), bucket_size):
        chunk = middle[start:start + bucket_size]
        if not chunk:
            continue
        priced = [(positive_price(item.get("price")), idx, item) for idx, item in enumerate(chunk)]
        priced = [(price, idx, item) for price, idx, item in priced if price is not None]
        if not priced:
            selected.append(chunk[len(chunk) // 2])
        else:
            low = min(priced, key=lambda item: (item[0], item[1]))[2]
            high = max(priced, key=lambda item: (item[0], -item[1]))[2]
            if low is high:
                selected.append(low)
            else:
                # Preserve source order inside the bucket.
                ordered = sorted({id(low): low, id(high): high}.values(), key=lambda item: chunk.index(item))
                selected.extend(ordered)
        if len(selected) >= max_points - 1:
            break
    selected.append(copied[-1])

    # Final guard in case buckets selected slightly too many points.
    if len(selected) > max_points:
        step = (len(selected) - 1) / float(max_points - 1)
        compacted = [selected[round(i * step)] for i in range(max_points)]
        compacted[0] = selected[0]
        compacted[-1] = selected[-1]
        return [dict(item) for item in compacted]
    return [dict(item) for item in selected]
