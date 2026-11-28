import unittest
from datetime import datetime
from ogamaru_updater import *

NOW = datetime(2026, 11, 28, tzinfo=JST)
class RegressionTests(unittest.TestCase):
    def test_legacy_atom(self):
        xml = '<feed xmlns="http://purl.org/atom/ns#"><entry><title>運航変更のお知らせ</title><link rel="alternate" href="https://example.org/"/></entry></feed>'
        items, err = parse_ogamaru_blog_atom(xml)
        self.assertIsNone(err)
        self.assertTrue(items[0]['is_alert'])
    def test_modern_atom(self):
        items, err = parse_ogamaru_blog_atom('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>お知らせ</title></entry></feed>')
        self.assertIsNone(err)
        self.assertEqual(len(items), 1)
    def test_tokyo_not_chichi(self):
        self.assertIsNone(extract_schedule_override('12月1日の東京出港時刻を11時00分から13時00分に変更します。', NOW))
    def test_multi_date(self):
        data = extract_schedule_updates('次のおがさわら丸の運航は、11/27(金) 東京 11時00分発、12/1(火) 父島 15時00分発 の予定です。', NOW)
        self.assertEqual(data, [{'target_date':'2026-12-01','type':'dep','new_time':'15:00'}])
    def test_restore(self):
        old = extract_schedule_updates('12月1日の父島出港時刻を15時00分から13時00分に変更します。', NOW)
        new = extract_schedule_updates('12月1日の父島出港時刻を13時00分から15時00分に変更します。', NOW)
        self.assertEqual(merge_persisted_overrides(old,new,'2026-11-28')[0]['new_time'],'15:00')
    def test_ambiguous(self):
        self.assertEqual(extract_schedule_updates('12月1日の父島出港時刻を13時00分に変更する可能性があります。', NOW),[])
    def test_cancel(self):
        self.assertEqual(extract_cancellations('12月1日の父島出港便は欠航です。', NOW),[{'target_date':'2026-12-01','type':'dep'}])
        self.assertEqual(extract_cancellations('12月1日の東京出港便は欠航です。', NOW),[])
        self.assertEqual(extract_cancellations('12月1日の父島出港便は欠航の可能性があります。', NOW),[])
    def test_no_service(self):
        self.assertEqual(parse_hahajima_status('<div>母島 沖港 <img alt="本日の運航なし"></div>')['status'],'運航なし')
    def test_year_boundary(self):
        self.assertEqual(extract_schedule_updates('1月2日の父島出港時刻を13時00分に変更します。', NOW)[0]['target_date'],'2027-01-02')
    def test_source_failure(self):
        from unittest.mock import patch
        from tempfile import TemporaryDirectory
        import ogamaru_updater as updater
        import sys
        with TemporaryDirectory() as tmp:
            output = Path(tmp)/'schedule.json'
            with patch.object(updater,'fetch_html',side_effect=URLError('test')), patch.object(updater,'HISTORY_CSV',Path(tmp)/'history.csv'), patch.object(sys,'argv',['updater','--output',str(output)]), patch.dict(os.environ, {'LINE_CHANNEL_ACCESS_TOKEN':'','WEBHOOK_URL':''}):
                self.assertEqual(updater.main(),0)
            data=json.loads(output.read_text())
            self.assertNotEqual(data['source_status']['ogamaru_next'],'ok')
            self.assertEqual(data['hahajima_status']['status'],'確認不能')
            self.assertEqual(data['persisted_overrides'],[])

if __name__ == '__main__': unittest.main()
