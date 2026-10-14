"""Regressions for real-source failures with offline source responses."""
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import backfill_stocks as stocks
from tests.synthetic_db import make_stock_db, trade_days


class StockSourceTests(unittest.TestCase):
    def test_multipage_iterator_works_without_dataframe_append(self):
        from baostock.data.resultset import ResultData
        rs = ResultData()
        rs.error_code = '0'
        rs.fields = ['code', 'name']
        rs.data = [['sh.600000', '浦发银行']]
        def next_page():
            if rs.cur_row_num < len(rs.data):
                return True
            if rs.data[0][0] == 'sh.600000':
                rs.data = [['sz.000001', '平安银行']]
                rs.cur_row_num = 0
                return True
            return False
        with mock.patch.object(rs, 'next', side_effect=next_page):
            result = stocks.read_baostock_result(rs)
        self.assertEqual(result.code.tolist(), ['sh.600000', 'sz.000001'])

    def test_pagination_error_is_not_partial_success(self):
        rs = mock.Mock(error_code='0', fields=['code'])
        def fail():
            rs.error_code, rs.error_msg = '100', 'page failed'
            return False
        rs.next.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'page failed'):
            stocks.read_baostock_result(rs)

    def test_empty_filtered_stock_list_fails(self):
        rs = mock.Mock(error_code='0')
        with mock.patch.object(stocks.bs, 'query_all_stock', return_value=rs), \
             mock.patch.object(stocks, 'read_baostock_result', return_value=pd.DataFrame({'code':['bj.920001'], 'code_name':['北交所'], 'tradeStatus':['1']})):
            with self.assertRaisesRegex(RuntimeError, 'no HS A shares'):
                stocks.fetch_all_hs_a_stock_list('2026-10-13')

    def test_all_empty_stock_fetches_fail_even_with_existing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'stock.db'
            days = trade_days('2026-10-13', 5)
            make_stock_db(db, days, n_stocks=1)
            listing = pd.DataFrame({'sec_code':['600000'], 'bs_code':['sh.600000'], 'name':['浦发银行'], 'trade_status':['1']})
            with mock.patch('sys.argv', ['backfill_stocks.py','--db',str(db)]), \
                 mock.patch.object(stocks,'bs_login'), mock.patch.object(stocks,'bs_logout'), \
                 mock.patch.object(stocks,'get_last_trading_day',return_value=days[-1]), \
                 mock.patch.object(stocks,'last_n_trading_days',return_value=days), \
                 mock.patch.object(stocks,'fetch_all_hs_a_stock_list',return_value=listing), \
                 mock.patch.object(stocks,'fetch_kline_baostock',return_value=pd.DataFrame()), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'no stock kline saved'):
                    stocks.main()
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM kline_daily').fetchone()[0],25)


class ConceptSourceTests(unittest.TestCase):
    def test_small_concept_backfill_preserves_history_outside_requested_window(self):
        import backfill_concepts as concepts
        from tests.synthetic_db import make_concept_db
        days = trade_days('2026-10-13', 80)
        for window in (5, 45, 60):
            with self.subTest(window=window):
                with tempfile.TemporaryDirectory() as tmp:
                    db = Path(tmp) / 'concept.db'
                    make_concept_db(db, days[:-window], n_concepts=1)
                    with sqlite3.connect(db) as con:
                        original = con.execute('SELECT * FROM concept_kline ORDER BY trade_date').fetchall()
                    adata = mock.Mock()
                    # THS returns more history than requested. Older local rows must stay intact.
                    adata.stock.market.get_market_concept_ths.return_value = pd.DataFrame({
                        'trade_date': days, 'close': [999.0] * len(days),
                        'change_pct': [1.0] * len(days), 'amount': [1e9] * len(days),
                    })
                    listing = pd.DataFrame({'index_code': ['885000'], 'name': ['概念']})
                    with mock.patch('sys.argv', ['backfill_concepts.py', '--db', str(db), '--days', str(window), '--asof', days[-1]]), \
                         mock.patch.object(concepts, 'try_import_adata', return_value=adata), \
                         mock.patch.object(concepts, 'get_ths_concept_list', return_value=listing), \
                         mock.patch.object(concepts, 'load_filtered_concepts', return_value=(set(), None)), \
                         contextlib.redirect_stdout(io.StringIO()):
                        concepts.main()
                    with sqlite3.connect(db) as con:
                        rows = con.execute('SELECT * FROM concept_kline ORDER BY trade_date').fetchall()
                    self.assertEqual(rows[:-window], original)
                    self.assertEqual([r[0].replace('/', '-') for r in rows], days)
                    self.assertEqual([r[2] for r in rows[-window:]], [999.0] * window)

    def test_missing_index_code_is_resolved_from_ths_detail(self):
        import backfill_concepts as concepts
        adata = mock.Mock()
        adata.stock.info.all_concept_code_ths.return_value = pd.DataFrame({'index_code':[float('nan')], 'concept_code':['301558'], 'name':['阿里巴巴概念']})
        response = mock.Mock(content=b'<input id="clid" value="885611">')
        with mock.patch.object(concepts.requests, 'get', return_value=response) as get:
            result = concepts.get_ths_concept_list(adata)
        self.assertEqual(result.index_code.tolist(), ['885611'])
        self.assertEqual(get.call_args.kwargs['timeout'], 20)

    def test_empty_source_list_fails(self):
        import backfill_concepts as concepts
        adata = mock.Mock()
        adata.stock.info.all_concept_code_ths.return_value = pd.DataFrame()
        with self.assertRaisesRegex(RuntimeError, 'list returned empty'):
            concepts.get_ths_concept_list(adata)

    def test_detail_timeout_retries_at_most_three_times(self):
        import backfill_concepts as concepts
        adata = mock.Mock()
        adata.stock.info.all_concept_code_ths.return_value = pd.DataFrame({
            'index_code':[None], 'concept_code':['309120'], 'name':['概念'],
        })
        response = mock.Mock(content=b'<input id="clid" value="885611">')
        with mock.patch.object(concepts.requests, 'get', side_effect=[
            concepts.requests.exceptions.ReadTimeout('timeout'), response,
        ]) as get, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(concepts.get_ths_concept_list(adata).index_code.tolist(), ['885611'])
        self.assertEqual(get.call_count, 2)
        with mock.patch.object(concepts.requests, 'get', side_effect=concepts.requests.exceptions.ReadTimeout('timeout')) as get, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, '1 unresolved index codes'):
                concepts.get_ths_concept_list(adata)
        self.assertEqual(get.call_count, 3)

    def test_concept_history_excludes_intraday_and_preserves_units(self):
        import datetime as dt
        import backfill_concepts as concepts
        adata = mock.Mock()
        adata.stock.market.get_market_concept_ths.return_value = pd.DataFrame({
            'trade_date':['2026-10-12','2026-10-13','2026-10-14'],
            'close':['100.00','101.00','109.00'], 'change':[0.,1.,8.],
            'change_pct':[0.,1.,7.92], 'volume':['200','220','100'],
            'amount':['30000','33000','10000'], 'index_code':['885611']*3,
        })
        result = concepts.fetch_concept_kline(adata, '885611', dt.date(2026,10,12), dt.date(2026,10,13))
        self.assertEqual(result.trade_date.tolist(), [dt.date(2026,10,12),dt.date(2026,10,13)])
        self.assertEqual(result.iloc[-1].change_pct, 1.0)
        self.assertEqual(result.iloc[-1].amount, 33000.0)

    def test_default_asof_is_completed_day_and_all_empty_fails(self):
        import datetime as dt
        import backfill_concepts as concepts
        from tests.synthetic_db import make_concept_db
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'concept.db'
            make_concept_db(db, ['2026-10-13'], n_concepts=1)
            listing = pd.DataFrame({'index_code':['885611'], 'name':['阿里巴巴概念']})
            with mock.patch('sys.argv',['backfill_concepts.py','--db',str(db)]), \
                 mock.patch.object(concepts,'try_import_adata',return_value=mock.Mock()), \
                 mock.patch.object(stocks,'bs_login'), mock.patch.object(stocks,'bs_logout'), \
                 mock.patch.object(stocks,'get_last_trading_day',return_value='2026-10-13'), \
                 mock.patch.object(concepts,'get_ths_concept_list',return_value=listing), \
                 mock.patch.object(concepts,'load_filtered_concepts',return_value=(set(),None)), \
                 mock.patch.object(concepts,'fetch_concept_kline',return_value=pd.DataFrame()) as fetch, \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'no concept kline saved'):
                    concepts.main()
            self.assertEqual(fetch.call_args.args[-1],dt.date(2026,10,13))
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute('SELECT MAX(trade_date) FROM concept_kline').fetchone()[0],'2026/10/13')


class AdditionalEmptySourceTests(unittest.TestCase):
    def test_all_empty_indexes_fail_after_valid_stocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'stocks.db'
            days = trade_days('2026-10-13', 5)
            make_stock_db(db, days, n_stocks=1)
            listing = pd.DataFrame({'sec_code':['600000'], 'bs_code':['sh.600000'], 'name':['浦发银行'], 'trade_status':['1']})
            with sqlite3.connect(db) as con:
                valid = pd.read_sql_query('SELECT * FROM kline_daily WHERE sec_type="stock"',con)
            with mock.patch('sys.argv', ['backfill_stocks.py','--db',str(db),'--with-indexes']), \
                 mock.patch.object(stocks,'bs_login'), mock.patch.object(stocks,'bs_logout'), \
                 mock.patch.object(stocks,'get_last_trading_day',return_value=days[-1]), \
                 mock.patch.object(stocks,'last_n_trading_days',return_value=days), \
                 mock.patch.object(stocks,'fetch_all_hs_a_stock_list',return_value=listing), \
                 mock.patch.object(stocks,'fetch_kline_baostock',return_value=valid), \
                 mock.patch.object(stocks,'fetch_indexes_baostock',return_value=pd.DataFrame()), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'no requested index kline saved'):
                    stocks.main()

    def test_unresolved_concept_code_cannot_silently_shrink_master(self):
        import backfill_concepts as concepts
        adata = mock.Mock()
        adata.stock.info.all_concept_code_ths.return_value = pd.DataFrame({'index_code':['885001',None], 'concept_code':['300001','300002'], 'name':['有效','无法解析']})
        response = mock.Mock(content=b'<html>no clid</html>')
        with mock.patch.object(concepts.requests,'get',return_value=response), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'1 unresolved index codes'):
                concepts.get_ths_concept_list(adata)


class StockFailureBudgetTests(unittest.TestCase):
    def test_late_session_failure_stops_after_three_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'stocks.db'
            days = trade_days('2026-10-13',5)
            make_stock_db(db,days,n_stocks=1)
            with sqlite3.connect(db) as con:
                valid = pd.read_sql_query('SELECT * FROM kline_daily WHERE sec_type="stock"',con)
            listing = pd.DataFrame({'sec_code':[f'{600000+i}' for i in range(10)],
                'bs_code':[f'sh.{600000+i}' for i in range(10)], 'name':['股票']*10,'trade_status':['1']*10})
            responses = [valid]*6 + [RuntimeError('10001001 用户未登录')]*4
            captured = io.StringIO()
            with mock.patch('sys.argv',['backfill_stocks.py','--db',str(db)]), \
                 mock.patch.object(stocks,'bs_login'), mock.patch.object(stocks,'bs_logout'), \
                 mock.patch.object(stocks,'get_last_trading_day',return_value=days[-1]), \
                 mock.patch.object(stocks,'last_n_trading_days',return_value=days), \
                 mock.patch.object(stocks,'fetch_all_hs_a_stock_list',return_value=listing), \
                 mock.patch.object(stocks,'fetch_kline_baostock',side_effect=responses) as fetch, \
                 contextlib.redirect_stdout(captured):
                with self.assertRaisesRegex(RuntimeError,'3 consecutive times'):
                    stocks.main()
            self.assertEqual(fetch.call_count,9)
            self.assertIn('10001001 用户未登录',captured.getvalue())
            self.assertNotIn('[RUN] DONE',captured.getvalue())
