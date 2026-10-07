import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import monitor


class ReportsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = monitor.open_buffer(pathlib.Path(self.temp.name) / 'samples.db')
        self.server = dict(name='vm1', kind='vm')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def resource(self, stamp, busy, interval=600):
        value = {'timestamp': {'interval': interval},
                 'cpu-load': [{'cpu': 'all', 'idle': 100-busy-3, 'iowait': 2, 'steal': 1}],
                 'memory': {'avail': 400, 'swpused': 10}, 'memory_total_bytes': 1024*1000}
        self.db.execute('INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?)',
                        ('vm1', 'resources', stamp, json.dumps(value)))

    def test_japan_day_boundary_weighted_cpu_and_missing_samples(self):
        self.resource('2026-10-03T14:59:00+00:00', 99)  # outside Oct 4 JST
        self.resource('2026-10-03T15:10:00+00:00', 10, 600)
        self.resource('2026-10-03T15:30:00+00:00', 40, 1200)
        self.resource('2026-10-04T15:10:00+00:00', 99)  # next day's first interval
        result = monitor.daily_report(self.db, self.server, dt.date(2026, 10, 4), ZoneInfo('Asia/Tokyo'))
        self.assertEqual(result['samples'], 2)
        self.assertEqual(result['cpu']['average_percent'], 30)
        self.assertEqual(result['cpu']['max_percent'], 40)
        self.assertEqual(result['coverage_percent'], 2.1)
        self.assertEqual(result['memory']['max_used_bytes'], 600*1024)

    def test_interval_ending_at_midnight_belongs_to_previous_day(self):
        self.resource('2026-10-03T15:00:00+00:00', 90)
        self.resource('2026-10-04T15:00:00+00:00', 20)
        result = monitor.daily_report(self.db, self.server, dt.date(2026, 10, 4), ZoneInfo('Asia/Tokyo'))
        self.assertEqual(result['samples'], 1)
        self.assertEqual(result['cpu']['average_percent'], 20)

    def test_missing_data_is_null_and_duplicates_do_not_double_count(self):
        result = monitor.daily_report(self.db, self.server, dt.date(2026, 10, 4), ZoneInfo('Asia/Tokyo'))
        self.assertIsNone(result['cpu']['average_percent'])
        self.assertEqual(result['storage'], [])
        self.resource('2026-10-04T00:00:00+00:00', 10)
        self.resource('2026-10-04T00:00:00+00:00', 10)
        result = monitor.daily_report(self.db, self.server, dt.date(2026, 10, 4), ZoneInfo('Asia/Tokyo'))
        self.assertEqual(result['samples'], 1)

    def test_disk_latest_minimum_and_prior_day_delta(self):
        for stamp, free in [('2026-10-03T14:50:00+00:00', 100),
                            ('2026-10-03T15:10:00+00:00', 30),
                            ('2026-10-04T14:50:00+00:00', 80)]:
            entry = dict(device='/dev/vda1', mount='/', free_bytes=free, used_percent=100-free)
            self.db.execute('INSERT INTO samples VALUES (?, ?, ?, ?)',
                            ('vm1', 'storage', stamp, json.dumps([entry])))
        result = monitor.daily_report(self.db, self.server, dt.date(2026, 10, 4), ZoneInfo('Asia/Tokyo'))
        disk = result['storage'][0]
        self.assertEqual(disk['free_bytes'], 80)
        self.assertEqual(disk['min_free_bytes'], 30)
        self.assertEqual(disk['free_change_bytes'], -20)

    def test_failed_publish_remains_retryable_and_success_is_idempotent(self):
        timezone = ZoneInfo('Asia/Tokyo')
        day = dt.datetime.now(timezone).date() - dt.timedelta(days=1)
        stamp = dt.datetime.combine(day, dt.time(12), timezone).astimezone(dt.timezone.utc).isoformat()
        self.resource(stamp, 10)
        config = dict(servers=[self.server], timezone='Asia/Tokyo')
        with patch('monitor.ysql', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                monitor.report(config, self.db, day)
        self.assertEqual(self.db.execute('SELECT count(*) FROM published').fetchone()[0], 0)
        with patch('monitor.ysql') as execute:
            monitor.report(config, self.db, day)
            self.assertIn('ON CONFLICT', execute.call_args_list[0].args[1])
            cutoff = (day - dt.timedelta(days=6)).isoformat()
            self.assertIn(cutoff, execute.call_args_list[0].args[1])
            execute.reset_mock()
            monitor.report(config, self.db, day)
            self.assertEqual(execute.call_count, 1)  # cleanup only, no duplicate report


if __name__ == '__main__':
    unittest.main()
