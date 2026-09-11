from app.timeline_scaling import display_price_bounds, filter_path_points_for_display, normalized_axis_positions, positive_price


def test_timeline_price_bounds_ignore_zero_and_extreme_capture_outlier():
    bounds = display_price_bounds(
        [0, None, 100.0, 100.1, 99.9, 100.2, 100.0, 99.8, 100.3, 100.1, 999999.0],
        [101.0],
    )
    assert bounds is not None
    low, high = bounds
    assert 90.0 < low < 101.0
    assert 101.0 < high < 125.0


def test_timeline_price_bounds_flat_prices_keep_nonzero_axis_span():
    bounds = display_price_bounds([218.70, 218.70, 218.70, 218.70], [218.70])
    assert bounds is not None
    low, high = bounds
    assert low < 218.70 < high
    assert high - low > 0.01


def test_positive_price_rejects_api_placeholders_and_bad_values():
    assert positive_price(0) is None
    assert positive_price(-1) is None
    assert positive_price("") is None
    assert positive_price("nan") is None
    assert positive_price("123.45") == 123.45


def test_timeline_axis_positions_use_rank_not_elapsed_time_gap():
    positions = normalized_axis_positions([
        [
            {"time": 0.0, "order": 0},
            {"time": 1.0, "order": 1},
            {"time": 1_000_000.0, "order": 2},
        ]
    ])
    assert positions[(0, 0)] == 0.0
    assert positions[(0, 1)] == 0.5
    assert positions[(0, 2)] == 1.0


def test_timeline_axis_positions_mix_path_markers_and_missing_times():
    positions = normalized_axis_positions([
        [{"time": 10.0, "order": 0}, {"time": 20.0, "order": 1}],
        [{"time": 15.0, "order": 0}, {"order": 1}],
    ])
    assert positions[(0, 0)] == 0.0
    assert round(positions[(1, 0)], 6) == round(1 / 3, 6)
    assert round(positions[(0, 1)], 6) == round(2 / 3, 6)
    assert positions[(1, 1)] == 1.0


def test_timeline_price_bounds_ignore_far_outlier_marker_when_path_exists():
    bounds = display_price_bounds(
        [0, 100.0, 100.4, 101.2, 102.0, None],
        [98.5, 100.8, 102.4, 9999.0],
    )
    assert bounds is not None
    low, high = bounds
    assert low < 98.5
    assert high > 102.4
    assert high < 150.0


def test_timeline_price_bounds_use_markers_when_no_price_path_exists():
    bounds = display_price_bounds([], [100.0, 105.0])
    assert bounds is not None
    low, high = bounds
    assert low < 100.0
    assert high > 105.0


def test_timeline_price_bounds_marker_only_fallback_ignores_extreme_outlier():
    bounds = display_price_bounds([], [100.0, 101.0, 102.0, 9999.0])
    assert bounds is not None
    low, high = bounds
    assert low < 100.0
    assert high > 102.0
    assert high < 200.0



def test_timeline_filter_hides_offscale_imported_path_rows_before_drawing():
    points = [
        {"price": 100.0, "time": 1},
        {"price": 100.2, "time": 2},
        {"price": 99.8, "time": 3},
        {"price": 9999.0, "time": 4},
        {"price": 100.1, "time": 5},
    ]
    visible, hidden, bounds = filter_path_points_for_display(points, [100.0, 101.0])
    assert bounds is not None
    assert hidden == 1
    assert [item["price"] for item in visible] == [100.0, 100.2, 99.8, 100.1]


def test_timeline_filter_keeps_all_normal_wide_but_plausible_cycle_rows():
    points = [{"price": price} for price in [95.0, 96.0, 100.0, 103.0, 105.0]]
    visible, hidden, bounds = filter_path_points_for_display(points, [95.0, 105.0])
    assert bounds is not None
    assert hidden == 0
    assert len(visible) == len(points)


def test_timeline_time_window_filters_loose_imported_capture_rows():
    from app.timeline_scaling import filter_rows_to_time_window, time_window_from_values

    window = time_window_from_values(["2026-01-01T14:30:00+00:00", "2026-01-01T15:00:00+00:00"], pad_seconds=60)
    assert window is not None
    rows = [
        {"captured_at_utc": "2026-01-01T14:29:30+00:00", "price": 100.0},
        {"captured_at_utc": "2026-01-01T15:00:30+00:00", "price": 101.0},
        {"captured_at_utc": "2026-01-02T14:30:00+00:00", "price": 999.0},
        {"price": 88.0},
    ]

    filtered = filter_rows_to_time_window(rows, window)

    assert [row["price"] for row in filtered] == [100.0, 101.0]


def test_timeline_price_bounds_ignore_unrelated_10x_imported_cycle_values():
    bounds = display_price_bounds(
        [100.0, 100.4, 100.8, 101.2, 100.9, 100.7, 1000.0, 1005.0],
        [99.5, 101.4],
    )
    assert bounds is not None
    low, high = bounds
    assert low < 99.5
    assert high > 101.4
    assert high < 160.0


def test_marker_focused_scaling_hides_imported_rows_twenty_percent_away():
    points = [
        {"price": 100.0, "time": 1},
        {"price": 100.2, "time": 2},
        {"price": 100.1, "time": 3},
        {"price": 121.0, "time": 4},
        {"price": 122.0, "time": 5},
        {"price": 100.3, "time": 6},
    ]

    visible, hidden, bounds = filter_path_points_for_display(points, [99.8, 100.4, 101.0])

    assert bounds is not None
    assert hidden == 2
    assert [item["price"] for item in visible] == [100.0, 100.2, 100.1, 100.3]
    assert bounds[0] < 99.8
    assert bounds[1] > 101.0
    assert bounds[1] < 115.0
