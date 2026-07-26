BEGIN TRANSACTION;
CREATE TABLE app_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
INSERT INTO "app_settings" VALUES('strategy','{"ticker": "SYNTH", "investment_amount": 12345.0, "initial_drop_pct": 2.0, "buy_rebound_trail_pct": 1.0, "rise_trigger_pct": 3.0, "sell_trailing_stop_pct": 1.0, "atr_adaptive_enabled": false, "atr_adapt_minimum_profit_enabled": true, "atr_block_new_buy_until_ready": false, "atr_adapt_protective_sell_enabled": false, "atr_protective_sell_multiplier": 3.0, "atr_period": 14, "atr_bar_seconds": 60, "atr_initial_drop_multiplier": 1.5, "atr_buy_rebound_multiplier": 0.75, "atr_minimum_profit_multiplier": 1.0, "atr_sell_trail_multiplier": 1.0, "atr_min_pct": 0.1, "atr_max_pct": 20.0, "protective_sell_enabled": false, "protective_sell_trailing_stop_pct": 3.0, "slippage_buffer_enabled": false, "slippage_buffer_pct": 0.5, "hard_risk_limits_enabled": false, "max_daily_loss_ticker": 0.0, "max_daily_loss_total": 0.0, "max_cycles_per_ticker_day": 0, "max_consecutive_losses": 0, "max_spread_pct": 1.0, "min_trade_price": 0.0, "max_gap_from_prev_close_pct": 0.0, "block_delayed_data_in_live": true, "what_if_check_enabled": true, "stale_data_guard_enabled": true, "max_selected_price_age_seconds": 3.0, "max_bid_ask_age_seconds": 3.0, "max_rth_status_age_seconds": 60.0, "volatility_filter_enabled": false, "volatility_window_seconds": 300, "max_recent_price_move_pct": 5.0, "session_timing_guard_enabled": true, "no_new_buy_first_minutes": 5, "no_new_buy_last_minutes": 15, "cancel_buy_before_close_minutes": 5, "cancel_sell_and_liquidate_before_close_enabled": false, "liquidate_before_close_minutes": 5, "reinvest_profits": true, "auto_repeat": true, "rth_only": true, "exchange": "SMART", "primary_exchange": "NASDAQ", "contract_con_id": 424242, "currency": "USD", "sec_type": "STK", "tif": "GTC"}','2026-07-24T18:46:30+00:00');
CREATE TABLE broker_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    ticker TEXT,
                    cycle_id TEXT,
                    order_ref TEXT,
                    order_id INTEGER,
                    perm_id INTEGER,
                    execution_id TEXT,
                    raw_json TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(id) ON DELETE SET NULL
                );
CREATE TABLE cycles (
                    id TEXT PRIMARY KEY,
                    cycle_number INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    account TEXT,
                    con_id INTEGER,
                    exchange TEXT,
                    primary_exchange TEXT DEFAULT '',
                    currency TEXT,
                    rth_only INTEGER NOT NULL DEFAULT 1,
                    investment_amount REAL NOT NULL,
                    budget REAL NOT NULL,
                    reinvest_profits INTEGER NOT NULL,
                    reinvested_profit REAL NOT NULL,
                    initial_drop_pct REAL NOT NULL,
                    buy_rebound_trail_pct REAL NOT NULL,
                    rise_trigger_pct REAL NOT NULL,
                    sell_trailing_stop_pct REAL NOT NULL,
                    anchor_price REAL,
                    last_price REAL,
                    drop_trigger_price REAL,
                    buy_initial_trail_stop_price REAL,
                    rise_trigger_price REAL,
                    sell_initial_trail_stop_price REAL,
                    quantity INTEGER NOT NULL,
                    buy_order_id INTEGER,
                    buy_perm_id INTEGER,
                    buy_order_ref TEXT,
                    buy_status TEXT,
                    buy_filled_qty INTEGER NOT NULL,
                    avg_buy_price REAL,
                    buy_commission REAL NOT NULL,
                    buy_filled_at TEXT,
                    sell_order_id INTEGER,
                    sell_perm_id INTEGER,
                    sell_order_ref TEXT,
                    sell_status TEXT,
                    sell_filled_qty INTEGER NOT NULL,
                    avg_sell_price REAL,
                    sell_commission REAL NOT NULL,
                    sell_filled_at TEXT,
                    gross_pnl REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    stop_after_current_cycle INTEGER NOT NULL,
                    error_message TEXT
                , atr_adaptive_enabled INTEGER NOT NULL DEFAULT 1, atr_adapt_minimum_profit_enabled INTEGER NOT NULL DEFAULT 1, atr_block_new_buy_until_ready INTEGER NOT NULL DEFAULT 1, atr_adapt_protective_sell_enabled INTEGER NOT NULL DEFAULT 0, atr_protective_sell_multiplier REAL NOT NULL DEFAULT 3.0, atr_period INTEGER NOT NULL DEFAULT 14, atr_bar_seconds INTEGER NOT NULL DEFAULT 60, atr_initial_drop_multiplier REAL NOT NULL DEFAULT 1.5, atr_buy_rebound_multiplier REAL NOT NULL DEFAULT 0.75, atr_minimum_profit_multiplier REAL NOT NULL DEFAULT 1.0, atr_sell_trail_multiplier REAL NOT NULL DEFAULT 1.0, atr_min_pct REAL NOT NULL DEFAULT 0.10, atr_max_pct REAL NOT NULL DEFAULT 20.0, protective_sell_enabled INTEGER NOT NULL DEFAULT 0, protective_sell_trailing_stop_pct REAL NOT NULL DEFAULT 0, slippage_buffer_enabled INTEGER NOT NULL DEFAULT 0, slippage_buffer_pct REAL NOT NULL DEFAULT 0, hard_risk_limits_enabled INTEGER NOT NULL DEFAULT 0, max_daily_loss_ticker REAL NOT NULL DEFAULT 0, max_daily_loss_total REAL NOT NULL DEFAULT 0, max_cycles_per_ticker_day INTEGER NOT NULL DEFAULT 0, max_consecutive_losses INTEGER NOT NULL DEFAULT 0, max_spread_pct REAL NOT NULL DEFAULT 0, min_trade_price REAL NOT NULL DEFAULT 0, max_gap_from_prev_close_pct REAL NOT NULL DEFAULT 0, block_delayed_data_in_live INTEGER NOT NULL DEFAULT 1, what_if_check_enabled INTEGER NOT NULL DEFAULT 1, stale_data_guard_enabled INTEGER NOT NULL DEFAULT 1, max_selected_price_age_seconds REAL NOT NULL DEFAULT 3, max_bid_ask_age_seconds REAL NOT NULL DEFAULT 3, max_rth_status_age_seconds REAL NOT NULL DEFAULT 60, volatility_filter_enabled INTEGER NOT NULL DEFAULT 0, volatility_window_seconds INTEGER NOT NULL DEFAULT 300, max_recent_price_move_pct REAL NOT NULL DEFAULT 5, session_timing_guard_enabled INTEGER NOT NULL DEFAULT 1, no_new_buy_first_minutes INTEGER NOT NULL DEFAULT 5, no_new_buy_last_minutes INTEGER NOT NULL DEFAULT 15, cancel_buy_before_close_minutes INTEGER NOT NULL DEFAULT 5, cancel_sell_and_liquidate_before_close_enabled INTEGER NOT NULL DEFAULT 0, liquidate_before_close_minutes INTEGER NOT NULL DEFAULT 5, recovery_required INTEGER NOT NULL DEFAULT 0, close_position_market_requested INTEGER NOT NULL DEFAULT 0, close_before_rth_liquidation_requested INTEGER NOT NULL DEFAULT 0, close_before_rth_cancel_requested INTEGER NOT NULL DEFAULT 0, protective_sell_order_id INTEGER, protective_sell_perm_id INTEGER, protective_sell_order_ref TEXT, protective_sell_status TEXT, protective_sell_initial_stop_price REAL, protective_sell_cancel_requested INTEGER NOT NULL DEFAULT 0, protective_sell_filled_qty INTEGER NOT NULL DEFAULT 0, protective_avg_sell_price REAL, protective_sell_commission REAL NOT NULL DEFAULT 0, protective_sell_filled_at TEXT);
INSERT INTO "cycles" VALUES('v311-synthetic-cycle',1,'SYNTH','3_WAIT_RISE_TRIGGER','2026-01-15T15:00:00+00:00','2026-01-15T15:35:00+00:00','SIM',424242,'SMART','NASDAQ','USD',1,12345.0,12345.0,1,0.0,2.0,1.0,3.0,1.0,100.0,100.0,98.0,NULL,NULL,NULL,10,1001,2001,'IBKRBOT|SYNTH|CYCLE-000001|SYNTH001|BUY_TRAIL','Filled',10,99.5,0.5,NULL,NULL,NULL,NULL,NULL,0,NULL,0.0,NULL,0.0,0.0,0,NULL,0,1,0,0,3.0,14,60,1.5,0.75,1.0,1.0,0.1,20.0,0,3.0,0,0.5,0,0.0,0.0,0,0,1.0,0.0,0.0,1,1,1,3.0,3.0,60.0,0,300,5.0,1,5,15,5,0,5,0,0,0,0,NULL,NULL,NULL,NULL,NULL,0,0,NULL,0.0,NULL);
CREATE TABLE decision_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    ticker TEXT,
                    cycle_id TEXT,
                    stage_before TEXT,
                    stage_after TEXT,
                    decision_result TEXT,
                    message TEXT NOT NULL,
                    broker_order_id INTEGER,
                    perm_id INTEGER,
                    raw_json TEXT,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(id) ON DELETE SET NULL
                );
CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    ticker TEXT,
                    cycle_id TEXT,
                    message TEXT NOT NULL,
                    raw_json TEXT,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(id) ON DELETE SET NULL
                );
INSERT INTO "events" VALUES(1,'2026-01-15T15:30:00+00:00','INFO','SYNTH','v311-synthetic-cycle','Synthetic migration fixture','{"fixture": true}');
CREATE TABLE executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT,
                    ticker TEXT NOT NULL,
                    order_ref TEXT,
                    order_id INTEGER,
                    perm_id INTEGER,
                    execution_id TEXT,
                    side TEXT NOT NULL,
                    shares REAL NOT NULL,
                    price REAL NOT NULL,
                    avg_price REAL,
                    commission REAL NOT NULL DEFAULT 0,
                    currency TEXT DEFAULT 'USD',
                    executed_at TEXT NOT NULL,
                    raw_json TEXT,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(id) ON DELETE SET NULL
                );
INSERT INTO "executions" VALUES(1,'v311-synthetic-cycle','SYNTH','IBKRBOT|SYNTH|CYCLE-000001|SYNTH001|BUY_TRAIL',1001,2001,'SYNTH-EXEC-1','BUY',10.0,99.5,99.5,0.5,'USD','2026-01-15T15:30:00+00:00','{"source": "synthetic migration fixture"}');
CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    action TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    order_id INTEGER,
                    perm_id INTEGER,
                    order_ref TEXT,
                    quantity INTEGER NOT NULL,
                    trailing_percent REAL,
                    initial_stop_price REAL,
                    status TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    raw_json TEXT,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(id) ON DELETE CASCADE
                );
INSERT INTO "orders" VALUES(1,'v311-synthetic-cycle','SYNTH','BUY','TRAIL',1001,2001,'IBKRBOT|SYNTH|CYCLE-000001|SYNTH001|BUY_TRAIL',10,1.0,100.0,'Filled','2026-01-15T15:30:00+00:00','2026-01-15T15:31:00+00:00','{}');
CREATE INDEX idx_cycles_ticker_stage ON cycles(ticker, stage);
CREATE INDEX idx_cycles_updated_at ON cycles(updated_at);
CREATE INDEX idx_cycles_stage_ticker_updated ON cycles(stage, ticker, updated_at);
CREATE INDEX idx_cycles_stage_sell_updated ON cycles(stage, sell_filled_at, updated_at);
CREATE INDEX idx_cycles_stage_ticker_sell_updated ON cycles(stage, ticker, sell_filled_at, updated_at);
CREATE INDEX idx_orders_ref ON orders(order_ref);
CREATE INDEX idx_orders_cycle ON orders(cycle_id);
CREATE INDEX idx_orders_cycle_status_ref ON orders(cycle_id, status, order_ref);
CREATE INDEX idx_exec_cycle ON executions(cycle_id);
CREATE INDEX idx_exec_order_ref ON executions(order_ref);
CREATE INDEX idx_exec_execution_id ON executions(execution_id);
CREATE INDEX idx_exec_cycle_time ON executions(cycle_id, executed_at);
CREATE INDEX idx_events_created_at ON events(created_at);
CREATE INDEX idx_events_cycle_created ON events(cycle_id, created_at, id);
CREATE INDEX idx_decision_events_cycle ON decision_events(cycle_id);
CREATE INDEX idx_decision_events_created_at ON decision_events(created_at);
CREATE INDEX idx_decision_events_cycle_created ON decision_events(cycle_id, created_at, id);
CREATE INDEX idx_broker_events_created_at ON broker_events(created_at);
CREATE INDEX idx_broker_events_order_ref ON broker_events(order_ref);
CREATE INDEX idx_broker_events_execution_id ON broker_events(execution_id);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('orders',1);
INSERT INTO "sqlite_sequence" VALUES('executions',1);
INSERT INTO "sqlite_sequence" VALUES('events',1);
COMMIT;
