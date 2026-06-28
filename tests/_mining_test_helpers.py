from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path


def trading_days(start: str, count: int) -> list[str]:
    year, month, day = [int(part) for part in start.split("-")]
    current = date(year, month, day)
    days: list[str] = []
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def create_sample_market_dbs(base_dir: Path) -> dict[str, str]:
    stock_db = base_dir / "a_share_mvp.db"
    concept_db = base_dir / "ths_concept.db"
    mining_db = base_dir / "mining_mvp.db"

    stock_conn = sqlite3.connect(stock_db)
    try:
        stock_conn.executescript(
            """
            CREATE TABLE stock_info (
              sec_code TEXT PRIMARY KEY,
              bs_code TEXT,
              name TEXT,
              trade_status TEXT,
              updated_at TEXT
            );

            CREATE TABLE kline_daily (
              sec_type TEXT NOT NULL,
              sec_code TEXT NOT NULL,
              trade_date TEXT NOT NULL,
              open REAL,
              high REAL,
              low REAL,
              close REAL,
              pre_close REAL,
              change REAL,
              change_pct REAL,
              volume REAL,
              amount REAL,
              turnover_ratio REAL,
              source TEXT,
              updated_at TEXT,
              PRIMARY KEY (sec_type, sec_code, trade_date)
            );
            """
        )
        stock_conn.executemany(
            "INSERT INTO stock_info VALUES (?, ?, ?, ?, ?)",
            [
                ("600001", "sh.600001", "Alpha", "正常交易", "2026-04-10 00:00:00"),
                ("300001", "sz.300001", "Beta", "正常交易", "2026-04-10 00:00:00"),
                ("600003", "sh.600003", "Gamma", "正常交易", "2026-04-10 00:00:00"),
                ("600002", "sh.600002", "ST Bad", "正常交易", "2026-04-10 00:00:00"),
                ("830001", "bj.830001", "BJ Test", "正常交易", "2026-04-10 00:00:00"),
            ],
        )

        days = trading_days("2026-02-20", 40)
        alpha_closes = [10 + i * 0.2 for i in range(40)]
        beta_closes = [20 + i * 0.15 for i in range(40)]
        gamma_closes = [30 + i * 0.03 for i in range(40)]

        def insert_stock_rows(code: str, closes: list[float], amount_base: float) -> None:
            for idx, trade_day in enumerate(days):
                close = round(closes[idx], 2)
                pre_close = round(closes[idx - 1], 2) if idx else round(close * 0.99, 2)
                change = round(close - pre_close, 2)
                change_pct = round((change / pre_close) * 100, 2)
                open_price = round(pre_close * 1.002, 2)
                high = round(max(open_price, close) * 1.01, 2)
                low = round(min(open_price, close) * 0.99, 2)
                volume = 10_000_000 + idx * 5_000
                amount = amount_base + idx * 5_000_000
                stock_conn.execute(
                    """
                    INSERT INTO kline_daily (
                      sec_type, sec_code, trade_date, open, high, low, close, pre_close,
                      change, change_pct, volume, amount, turnover_ratio, source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "stock",
                        code,
                        trade_day,
                        open_price,
                        high,
                        low,
                        close,
                        pre_close,
                        change,
                        change_pct,
                        volume,
                        amount,
                        3.0,
                        "unit-test",
                        "2026-04-10 00:00:00",
                    ),
                )

        insert_stock_rows("600001", alpha_closes, 1_100_000_000)
        insert_stock_rows("300001", beta_closes, 1_200_000_000)
        insert_stock_rows("600003", gamma_closes, 900_000_000)

        def insert_index_rows(code: str, base_close: float) -> None:
            previous = base_close
            for idx, trade_day in enumerate(days):
                close = round(previous * (0.996 if idx % 5 == 0 else 1.003), 2)
                pre_close = round(previous, 2)
                change = round(close - pre_close, 2)
                change_pct = round((change / pre_close) * 100, 2) if pre_close else 0.0
                stock_conn.execute(
                    """
                    INSERT INTO kline_daily (
                      sec_type, sec_code, trade_date, open, high, low, close, pre_close,
                      change, change_pct, volume, amount, turnover_ratio, source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "index",
                        code,
                        trade_day,
                        pre_close,
                        max(pre_close, close),
                        min(pre_close, close),
                        close,
                        pre_close,
                        change,
                        change_pct,
                        100_000_000 + idx * 10_000,
                        100_000_000_000 + idx * 100_000_000,
                        None,
                        "unit-test",
                        "2026-04-10 00:00:00",
                    ),
                )
                previous = close

        for index_code, base_close in [
            ("000001", 3000.0),
            ("000300", 4000.0),
            ("000852", 6000.0),
            ("399001", 10000.0),
        ]:
            insert_index_rows(index_code, base_close)

        target_day = days[34]
        previous_day = days[33]
        lookback_start_day = days[28]
        short_lookback_day = days[24]

        stock_conn.execute(
            """
            UPDATE kline_daily
            SET open=?, high=?, low=?, close=?, pre_close=?, change=?, change_pct=?, amount=?
            WHERE sec_type='stock' AND sec_code='600001' AND trade_date=?
            """,
            (
                15.45,
                15.65,
                15.2,
                15.6,
                14.45,
                1.15,
                round((1.15 / 14.45) * 100, 2),
                1_800_000_000,
                target_day,
            ),
        )
        stock_conn.execute(
            """
            UPDATE kline_daily
            SET close=?, open=?, high=?, low=?, pre_close=?, change=?, change_pct=?
            WHERE sec_type='stock' AND sec_code='600001' AND trade_date=?
            """,
            (
                14.45,
                14.2,
                14.6,
                14.0,
                14.1,
                0.35,
                round((0.35 / 14.1) * 100, 2),
                previous_day,
            ),
        )
        stock_conn.execute(
            """
            UPDATE kline_daily
            SET close=?, open=?, high=?, low=?, pre_close=?, change=?, change_pct=?
            WHERE sec_type='stock' AND sec_code='600001' AND trade_date=?
            """,
            (
                13.6,
                13.4,
                13.7,
                13.3,
                13.3,
                0.3,
                round((0.3 / 13.3) * 100, 2),
                lookback_start_day,
            ),
        )
        stock_conn.execute(
            """
            UPDATE kline_daily
            SET close=?, open=?, high=?, low=?, pre_close=?, change=?, change_pct=?
            WHERE sec_type='stock' AND sec_code='600001' AND trade_date=?
            """,
            (
                14.4,
                14.2,
                14.5,
                14.1,
                14.1,
                0.3,
                round((0.3 / 14.1) * 100, 2),
                short_lookback_day,
            ),
        )
        stock_conn.execute(
            """
            UPDATE kline_daily
            SET open=?, high=?, low=?, close=?, pre_close=?, change=?, change_pct=?, amount=?
            WHERE sec_type='stock' AND sec_code='300001' AND trade_date=?
            """,
            (
                25.1,
                26.7,
                24.9,
                25.3,
                25.0,
                0.3,
                1.2,
                1_650_000_000,
                target_day,
            ),
        )
        stock_conn.execute(
            """
            UPDATE kline_daily
            SET close=?, open=?, high=?, low=?, pre_close=?, change=?, change_pct=?
            WHERE sec_type='stock' AND sec_code='300001' AND trade_date=?
            """,
            (
                25.0,
                24.8,
                25.2,
                24.7,
                24.6,
                0.4,
                round((0.4 / 24.6) * 100, 2),
                previous_day,
            ),
        )
        stock_conn.execute(
            """
            UPDATE kline_daily
            SET close=?, open=?, high=?, low=?, pre_close=?, change=?, change_pct=?
            WHERE sec_type='stock' AND sec_code='300001' AND trade_date=?
            """,
            (
                23.5,
                23.3,
                23.7,
                23.2,
                23.1,
                0.4,
                round((0.4 / 23.1) * 100, 2),
                lookback_start_day,
            ),
        )

        stock_conn.execute(
            """
            INSERT INTO kline_daily VALUES
            ('stock', '600002', ?, 5, 5.1, 4.9, 5.0, 4.9, 0.1, 2.04, 100000, 12000000, 1.0, 'unit-test', '2026-04-10 00:00:00')
            """,
            (target_day,),
        )
        stock_conn.execute(
            """
            INSERT INTO kline_daily VALUES
            ('stock', '830001', ?, 6, 6.1, 5.9, 6.0, 5.9, 0.1, 1.69, 100000, 12000000, 1.0, 'unit-test', '2026-04-10 00:00:00')
            """,
            (target_day,),
        )
        stock_conn.commit()
    finally:
        stock_conn.close()

    concept_conn = sqlite3.connect(concept_db)
    try:
        concept_conn.executescript(
            """
            CREATE TABLE concept_kline (
              trade_date TEXT NOT NULL,
              concept_code TEXT NOT NULL,
              close REAL,
              pre_close REAL,
              change_pct REAL,
              volume REAL,
              amount REAL,
              PRIMARY KEY (trade_date, concept_code)
            );

            CREATE TABLE concept_master (
              index_code TEXT PRIMARY KEY,
              name TEXT,
              concept_code TEXT,
              source TEXT,
              updated_at TEXT
            );
            """
        )
        concept_conn.executemany(
            "INSERT INTO concept_master VALUES (?, ?, ?, ?, ?)",
            [
                ("880001", "AI", "880001", "unit-test", "2026-04-10 00:00:00"),
                ("880002", "Robotics", "880002", "unit-test", "2026-04-10 00:00:00"),
            ],
        )
        days = trading_days("2026-02-20", 40)
        for idx, trade_day in enumerate(days):
            ai_close = round(100 + idx * 1.2, 2)
            robo_close = round(80 + idx * 0.4, 2)
            ai_pre = round(99 + idx * 1.2, 2) if idx else 99.0
            robo_pre = round(79.6 + idx * 0.4, 2) if idx else 79.6
            concept_conn.execute(
                "INSERT INTO concept_kline VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_day,
                    "880001",
                    ai_close,
                    ai_pre,
                    round(((ai_close - ai_pre) / ai_pre) * 100, 2),
                    100_000_000 + idx * 10_000,
                    9_000_000_000 + idx * 10_000_000,
                ),
            )
            concept_conn.execute(
                "INSERT INTO concept_kline VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_day,
                    "880002",
                    robo_close,
                    robo_pre,
                    round(((robo_close - robo_pre) / robo_pre) * 100, 2),
                    90_000_000 + idx * 10_000,
                    7_000_000_000 + idx * 8_000_000,
                ),
            )
        concept_conn.commit()
    finally:
        concept_conn.close()

    mining_conn = sqlite3.connect(mining_db)
    try:
        mining_conn.executescript(
            """
            CREATE TABLE stock_market_cap (
              sec_code TEXT PRIMARY KEY,
              total_mv REAL,
              snapshot_date TEXT,
              updated_at TEXT
            );

            CREATE TABLE stock_listing (
              sec_code TEXT PRIMARY KEY,
              list_date TEXT,
              snapshot_date TEXT,
              updated_at TEXT
            );
            """
        )
        target_trade_date = trading_days("2026-02-20", 40)[34]
        basics_rows = [
            ("600001", 4_000_000_000, "2025-01-01"),
            ("300001", 3_000_000_000, "2025-01-01"),
            ("600003", 4_900_000_000, "2025-01-01"),
            ("600002", 4_000_000_000, "2025-01-01"),
            ("830001", 4_000_000_000, "2025-01-01"),
        ]
        mining_conn.executemany(
            "INSERT INTO stock_market_cap VALUES (?, ?, ?, ?)",
            [
                (code, mv, target_trade_date, "2026-04-10 00:00:00")
                for code, mv, _ in basics_rows
            ],
        )
        mining_conn.executemany(
            "INSERT INTO stock_listing VALUES (?, ?, ?, ?)",
            [
                (code, list_date, target_trade_date, "2026-04-10 00:00:00")
                for code, _, list_date in basics_rows
            ],
        )
        mining_conn.commit()
    finally:
        mining_conn.close()

    return {
        "target_trade_date": trading_days("2026-02-20", 40)[34],
        "next_trade_date": trading_days("2026-02-20", 40)[35],
        "t_plus_2": trading_days("2026-02-20", 40)[36],
        "t_plus_5": trading_days("2026-02-20", 40)[39],
    }
