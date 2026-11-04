import base64
import hashlib
import unittest

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from lingxing_dashboard.demo import DEMO_APP_ID, DEMO_SECRET, FakeLingxing
from lingxing_dashboard.lingxing import LingxingClient, LingxingError, make_sign
from lingxing_dashboard.lingxing.client import canonical_string


class SignTest(unittest.TestCase):
    def test_canonical_string_sorts_and_skips_empty(self):
        s = canonical_string({"b": 2, "a": "x", "empty": "", "none": None, "list": [3, 1], "obj": {"z": 1, "a": 2}})
        self.assertEqual(s, 'a=x&b=2&list=[3,1]&obj={"a":2,"z":1}')

    def test_sign_is_aes_of_upper_md5(self):
        params = {"sid": 1, "access_token": "t", "app_key": DEMO_APP_ID, "timestamp": "1700000000"}
        sign = make_sign(DEMO_APP_ID, params)
        raw = base64.b64decode(sign)
        dec = Cipher(algorithms.AES(DEMO_APP_ID.encode()), modes.ECB()).decryptor()
        unpadder = padding.PKCS7(128).unpadder()
        plain = unpadder.update(dec.update(raw) + dec.finalize()) + unpadder.finalize()
        expected = hashlib.md5(b"access_token=t&app_key=demo_app_id_0001&sid=1&timestamp=1700000000").hexdigest().upper()
        self.assertEqual(plain.decode(), expected)


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLingxing()
        self.sleeps = []
        self.client = LingxingClient(DEMO_APP_ID, DEMO_SECRET, "http://fake", transport=self.fake,
                                     sleep=self.sleeps.append)

    def test_signed_request_accepted(self):
        payload = self.client.request("GET", "/erp/sc/data/seller/lists", {})
        self.assertEqual(payload["code"], 0)
        self.assertEqual(len(payload["data"]), 5)

    def test_rate_limit_waits_and_retries(self):
        self.fake.rate_limit_next = 2
        payload = self.client.request("POST", "/erp/sc/data/mws/listing", {"sid": "101", "offset": 0, "length": 10})
        self.assertEqual(payload["code"], 0)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_paged_follows_offset(self):
        rows = self.client.paged("/erp/sc/data/mws/listing", {"sid": "101"}, length=2)
        self.assertEqual(len(rows), 6)
        offsets = [p["offset"] for path, p in self.fake.calls if path == "/erp/sc/data/mws/listing"]
        self.assertEqual(offsets, [0, 2, 4])

    def test_bad_credentials(self):
        bad = LingxingClient(DEMO_APP_ID, "wrong", "http://fake", transport=self.fake)
        with self.assertRaises(LingxingError):
            bad.request("GET", "/erp/sc/data/seller/lists", {})


if __name__ == "__main__":
    unittest.main()
