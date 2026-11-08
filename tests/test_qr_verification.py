"""Synthetic QR decoding only; no tickets, rendering, Sheets, or SMTP."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np
import qrcode

from event_checkin.controlled_render import ControlledTestBlocked, verify_qr


class QRVerificationTests(unittest.TestCase):
    def setUp(self):
        self.token = 'synthetic-qr-verification-token'
        qr = qrcode.QRCode(box_size=6, border=0)
        qr.add_data(self.token)
        qr.make(fit=True)
        pixels = np.asarray(qr.make_image().convert('RGB'))
        size = pixels.shape[0]
        self.settings = SimpleNamespace(qr_x=25, qr_y=30, qr_size=size)
        self.image = np.zeros((size + 60, size + 60, 3), dtype=np.uint8)
        self.image[30:30 + size, 25:25 + size] = pixels
        self.real_detector = cv2.QRCodeDetector()

    def fallback_detector(self):
        # Force a missed full-image scan; actually decode the resulting crop.
        detector = Mock()
        def decode(image):
            if image.shape == self.image.shape:
                return ('', None, None)
            return self.real_detector.detectAndDecode(image)
        detector.detectAndDecode.side_effect = decode
        return detector

    def test_fallback_decodes_exact_token_without_modifying_pixels(self):
        before = self.image.copy()
        detector = self.fallback_detector()
        with patch('cv2.QRCodeDetector', return_value=detector):
            verify_qr(self.settings, self.image, self.token)
        self.assertEqual(detector.detectAndDecode.call_count, 2)
        padded = detector.detectAndDecode.call_args.args[0]
        self.assertTrue(np.all(padded[0] == 255))
        np.testing.assert_array_equal(before, self.image)

    def test_wrong_full_image_token_fails_without_fallback(self):
        detector = Mock()
        detector.detectAndDecode.return_value = ('wrong-synthetic-token', None, None)
        with patch('cv2.QRCodeDetector', return_value=detector):
            with self.assertRaises(ControlledTestBlocked):
                verify_qr(self.settings, self.image, self.token)
        self.assertEqual(detector.detectAndDecode.call_count, 1)

    def test_wrong_crop_token_fails(self):
        with patch('cv2.QRCodeDetector', return_value=self.fallback_detector()):
            with self.assertRaises(ControlledTestBlocked):
                verify_qr(self.settings, self.image, 'different-synthetic-token')

    def test_unreadable_crop_fails(self):
        self.image[:] = 0
        with self.assertRaises(ControlledTestBlocked):
            verify_qr(self.settings, self.image, self.token)

    def test_invalid_crop_bounds_fail(self):
        for x, y, size in [(-1, 0, 10), (0, -1, 10), (0, 0, 0),
                            (0, 0, 10000), (0.5, 0, 10)]:
            with self.subTest(x=x, y=y, size=size):
                settings = SimpleNamespace(qr_x=x, qr_y=y, qr_size=size)
                detector = Mock()
                detector.detectAndDecode.return_value = ('', None, None)
                with patch('cv2.QRCodeDetector', return_value=detector):
                    with self.assertRaises(ControlledTestBlocked):
                        verify_qr(settings, self.image, self.token)
                self.assertEqual(detector.detectAndDecode.call_count, 1)

    def test_missing_image_fails(self):
        with self.assertRaises(ControlledTestBlocked):
            verify_qr(self.settings, None, self.token)
