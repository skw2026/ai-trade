import json
import unittest
import urllib.parse
import review_eth_supply as r


class ReviewTests(unittest.TestCase):
    def test_public_only(self):
        for kind in ('community','full','definitions','sample'):
            u=urllib.parse.urlsplit(r.url_for(kind,'2023-01-01'))
            self.assertEqual(u.netloc,'community-api.coinmetrics.io')
            self.assertNotIn('api_key',u.query)

    def test_no_price_metric(self):
        u=r.url_for('sample','2023-01-01')
        self.assertNotIn('Price',u)
        self.assertNotIn('ROI',u)
        q=urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
        self.assertEqual(q['page_size'],['1'])
        self.assertEqual(q['start_time'],q['end_time'])

    def test_wrong_date(self):
        with self.assertRaisesRegex(ValueError,'SAMPLE_DATE'):
            r.url_for('sample','2024-01-01')

    def test_wrong_action(self):
        with self.assertRaisesRegex(ValueError,'UNKNOWN'):
            r.url_for('trade')

    def test_no_silent_pagination(self):
        with self.assertRaisesRegex(ValueError,'INCOMPLETE'):
            r.decode(json.dumps(dict(data=[],next_page_url='https://other.test')),200)

    def test_empty_is_not_data_ready(self):
        got=r.decode('{"data":[]}',200)
        self.assertEqual(got['data'],[])
        self.assertNotIn('qualified',got)

    def test_expected_denial_recorded(self):
        got=r.decode('{"error":{"type":"forbidden"}}',403)
        self.assertEqual(got['outcome'],'CAPABILITY_OR_ACCESS_DENIED')

    def test_server_error_not_capability(self):
        with self.assertRaisesRegex(ValueError,'UNEXPECTED'):
            r.decode('{"error":{"type":"internal"}}',500)

    def test_duplicate_key(self):
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):
            r.decode('{"data":[],"data":[]}',200)

    def test_13_old_gates(self):
        self.assertEqual(len(r.GATES),13)


if __name__=='__main__':
    unittest.main()
