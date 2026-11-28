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

class NoticeTests(unittest.TestCase):
    def item(self, title, body='', published='2026-11-09'):
        return {'title':title,'body':body,'published_date':published,'is_alert':True,'url':'https://example.org/'}
    def test_old_notice_current_summary_separate(self):
        old = self.item('台風32号による運航変更','11月10日の父島出港時刻を14時00分に変更します。')
        active, unresolved, past = classify_notices([old], NOW)
        self.assertEqual(active,[]); self.assertEqual(unresolved,[])
        self.assertEqual(past[0]['target_dates'],['2026-11-10'])
    def test_future_notice(self):
        a,u,p = classify_notices([self.item('12月1日の父島出港便は欠航です。')],NOW)
        self.assertEqual(a[0]['target_dates'],['2026-12-01']); self.assertEqual(u+p,[])
    def test_unknown_date(self):
        a,u,p = classify_notices([self.item('台風による運航変更')],NOW)
        self.assertEqual(a+p,[]); self.assertEqual(len(u),1)
    def test_open_ended(self):
        a,u,p = classify_notices([self.item('11月10日以降の運航は当面変更します')],NOW)
        self.assertEqual(a+p,[]); self.assertEqual(len(u),1)
    def test_without_publication_year(self):
        a,u,p = classify_notices([self.item('11月10日の父島出港便は欠航です。',published=None)],NOW)
        self.assertEqual(a+p,[]); self.assertEqual(len(u),1)
    def test_published_not_modified(self):
        xml = '<feed xmlns="http://purl.org/atom/ns#"><entry><title>運航変更</title><issued>2026-11-09T12:00:00+09:00</issued><modified>2026-11-28T12:00:00+09:00</modified><summary>11月10日の父島出港便は欠航です。</summary></entry></feed>'
        items,err = parse_ogamaru_blog_atom(xml)
        self.assertIsNone(err); self.assertEqual(items[0]['published_date'],'2026-11-09'); self.assertEqual(items[0]['modified_date'],'2026-11-28')
        self.assertEqual(len(classify_notices(items,NOW)[2]),1)
    def test_history_deduplicates(self):
        past = classify_notices([self.item('11月10日の父島出港便は欠航です。')],NOW)[2]
        self.assertEqual(len(merge_notice_history(past,past,'2026-11-28')),1)

if __name__ == '__main__': unittest.main()
