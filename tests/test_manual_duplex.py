from contextlib import ExitStack
import ctypes
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import struct
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('wrapper', str(ROOT / 'rastertospl-wrapper.py'))
spec = importlib.util.spec_from_loader(loader.name, loader)
w = importlib.util.module_from_spec(spec)
loader.exec_module(w)


class RasterTests(unittest.TestCase):
    def setUp(self):
        if w.libcups is None:
            self.skipTest('libcupsimage is required')
        environment = patch.dict(os.environ, {'PPD': ''})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def fixture(self, count=3):
        path = self.directory / 'input.ras'
        header = ctypes.create_string_buffer(1796)
        # cups_page_header2_t: width, height, media, bits/color,
        # bits/pixel, bytes/line, color order, color space.
        struct.pack_into('8I', header, 372, 8, 2, 0, 8, 8, 8, 0, 0)
        struct.pack_into('2I', header, 276, 300, 300)
        struct.pack_into('2I', header, 352, 612, 792)
        struct.pack_into('I', header, 420, 1)  # cupsNumColors
        with path.open('wb') as f:
            raster = w.libcups.cupsRasterOpen(f.fileno(), 1)
            try:
                for value in range(1, count + 1):
                    self.assertEqual(w.libcups.cupsRasterWriteHeader2(raster, header), 1)
                    pixels = ctypes.create_string_buffer(bytes([value]) * 16, 16)
                    self.assertEqual(w.libcups.cupsRasterWritePixels(raster, pixels, 16), 16)
            finally:
                w.libcups.cupsRasterClose(raster)
        return path

    def test_real_raster_round_trip_and_file_argument(self):
        source = self.fixture()
        fake = self.directory / 'filter'
        fake.write_text(
            '#!/usr/bin/python3\n'
            'import sys\n'
            'assert len(sys.argv) == 6\n'
            'assert "Duplex=None" in sys.argv[5]\n'
            'sys.stdout.buffer.write(sys.stdin.buffer.read())\n'
        )
        fake.chmod(0o755)
        with patch.object(w, 'DUPLEX_REQUEST_DIR', str(self.directory)):
            with source.open('rb') as f:
                pages = w.parse_raster_stream(f.fileno(), 'test')
            self.assertEqual([Path(p).read_bytes() for _, p in pages],
                             [bytes([i]) * 16 for i in (1, 2, 3)])
            output = self.directory / 'output.ras'
            argv = ['wrapper', '1', 'user', 'title', '1',
                    'Duplex=DuplexNoTumble', str(source)]
            with ExitStack() as stack:
                f = stack.enter_context(output.open('wb'))
                stack.enter_context(patch.object(w, 'REAL_FILTER', str(fake)))
                stack.enter_context(patch.object(
                    w.sys, 'stdout', types.SimpleNamespace(buffer=f)
                ))
                self.assertEqual(w.run_real_filter(argv, pages, False), 0)
            with output.open('rb') as f:
                reread = w.parse_raster_stream(f.fileno(), 'output')
            self.assertEqual([Path(p).read_bytes() for _, p in reread],
                             [bytes([i]) * 16 for i in (1, 2, 3)])

    def test_odd_job_padding_order_and_cleanup(self):
        source = self.fixture()
        passes = []
        directories = []

        def capture(argv, pages, last, extra_options=None):
            directories.append(w.DUPLEX_REQUEST_DIR)
            passes.append(([Path(p).read_bytes() for _, p in pages], extra_options))
            return 0

        argv = ['wrapper', '1', 'user', 'title', '1', 'sides=two-sided-long-edge', str(source)]
        with patch.object(w.sys, 'argv', argv), patch.object(w, 'run_real_filter', capture):
            self.assertEqual(w.main(), 0)
        self.assertEqual(passes, [([b'\x01' * 16, b'\x03' * 16], None),
                                 ([b'\xff' * 16], 'InputSlot=Manual'),
                                 ([b'\x02' * 16], 'InputSlot=Auto')])
        self.assertTrue(all(not os.path.exists(p) for p in directories))

    def test_truncated_pixels_fail(self):
        source = self.fixture(1)
        source.write_bytes(source.read_bytes()[:-8])
        with patch.object(w, 'DUPLEX_REQUEST_DIR', str(self.directory)), source.open('rb') as f:
            with self.assertRaisesRegex(ValueError, 'Truncated'):
                w.parse_raster_stream(f.fileno(), 'truncated')

    def test_native_duplex_bypasses_wrapper(self):
        ppd = self.directory / 'native.ppd'
        ppd.write_text('*Duplex DuplexNoTumble: "<</Duplex true>>setpagedevice"\n')
        with patch.dict(os.environ, {'PPD': str(ppd)}):
            self.assertFalse(w.is_manual_duplex_requested({'Duplex': 'DuplexNoTumble'}))

    def test_multiple_copies_fail_before_reading(self):
        argv = ['wrapper', '1', 'user', 'title', '2', 'sides=two-sided-long-edge']
        with patch.object(w.sys, 'argv', argv), \
             patch.object(w, 'parse_raster_stream') as parse:
            self.assertEqual(w.main(), 1)
            parse.assert_not_called()

    def page(self, width, height, bits, stride, data, name='pixels'):
        header = bytearray(1796)
        struct.pack_into('2I', header, 372, width, height)
        struct.pack_into('2I', header, 388, bits, stride)
        source = self.directory / name
        source.write_bytes(data)
        return bytes(header), str(source)

    def test_portrait_long_and_short_edge(self):
        page = self.page(2, 3, 8, 2, bytes([1, 2, 3, 4, 5, 6]))
        long = w.orient_back_pages([page], 'long')[0]
        short = w.orient_back_pages([page], 'short')[0]
        self.assertEqual(Path(long[1]).read_bytes(), bytes([6, 5, 4, 3, 2, 1]))
        self.assertEqual(Path(short[1]).read_bytes(), bytes([1, 2, 3, 4, 5, 6]))

    def test_landscape_binding(self):
        page = self.page(3, 2, 8, 3, bytes([1, 2, 3, 4, 5, 6]))
        self.assertEqual(w.orient_back_pages([page], 'long'), [page])
        short = w.orient_back_pages([page], 'short')[0]
        self.assertEqual(Path(short[1]).read_bytes(), bytes([6, 5, 4, 3, 2, 1]))

    def test_rgb_rotation_preserves_channels_and_stride(self):
        page = self.page(2, 2, 24, 7, b'ABCDEFxGHIJKLy')
        rotated = w.rotate_page_180(page)
        self.assertEqual(Path(rotated[1]).read_bytes(), b'JKLGHIyDEFABCx')

    def test_packed_rotation_preserves_padding(self):
        for bits, first, second, expected in [
                (1, 0b10100000, 0b01100000, bytes([0b11000000, 0xdd, 0b10100000, 0xee])),
                (2, 0b01101100, 0b11000100, bytes([0b01001100, 0xdd, 0b11100100, 0xee]))]:
            with self.subTest(bits=bits):
                page = self.page(3, 2, bits, 2, bytes([first, 0xee, second, 0xdd]))
                rotated = w.rotate_page_180(page)
                self.assertEqual(Path(rotated[1]).read_bytes(), expected)

    def test_four_bit_rotation_preserves_unused_nibble(self):
        page = self.page(3, 2, 4, 3, bytes.fromhex('12 3a ee 45 6b dd'))
        rotated = w.rotate_page_180(page)
        self.assertEqual(
            Path(rotated[1]).read_bytes(), bytes.fromhex('65 4b dd 32 1a ee')
        )

    def test_both_binding_modes_through_complete_job(self):
        for mode, options in [('long', 'Duplex=DuplexNoTumble'),
                              ('short', 'sides=two-sided-short-edge')]:
            with self.subTest(mode=mode):
                pages = [self.page(2, 3, 8, 2, bytes(range(i, i + 6)), str(i))
                         for i in (0, 10, 20, 30)]
                passes = []

                def capture(argv, selected, last, extra_options=None):
                    passes.append(([Path(p).read_bytes() for _, p in selected],
                                   extra_options))
                    return 0

                argv = ['wrapper', '1', 'user', 'title', '1', options]
                with patch.object(w.sys, 'argv', argv), \
                     patch.object(w, 'parse_raster_stream', return_value=pages), \
                     patch.object(w, 'run_real_filter', capture):
                    self.assertEqual(w.main(), 0)
                reverse = mode == 'long'
                backs = [bytes(range(i, i + 6))[::-1 if reverse else 1]
                         for i in (30, 10)]
                self.assertEqual(passes, [([bytes(range(6)), bytes(range(20, 26))], None),
                                         ([backs[0]], 'InputSlot=Manual'),
                                         ([backs[1]], 'InputSlot=Auto')])

    def test_failed_filter_removes_spooled_pages(self):
        source = self.fixture()
        directories = []

        def fail(*args, **kwargs):
            directories.append(w.DUPLEX_REQUEST_DIR)
            return 7

        argv = ['wrapper', '1', 'user', 'title', '1',
                'sides=two-sided-long-edge', str(source)]
        with patch.object(w.sys, 'argv', argv), \
             patch.object(w, 'run_real_filter', fail):
            self.assertEqual(w.main(), 7)
        self.assertTrue(all(not os.path.exists(p) for p in directories))


if __name__ == '__main__':
    unittest.main()
