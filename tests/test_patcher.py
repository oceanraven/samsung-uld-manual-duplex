import importlib.util
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('patcher', ROOT / 'patch_uld.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class PatcherTests(unittest.TestCase):
    def test_installer_patch_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'noarch/printer.pkg'
            path.parent.mkdir()
            path.write_text(
                'install_lns_p "${DIST_DIR}/${ARCH_SUBDIR}/rastertospl" '
                '"$INSTDIR_COMMON_PRINTER_BIN" "$INSTDIR_CUPS_FILTERS"\n'
            )
            p.patch_installer(directory)
            once = path.read_bytes()
            p.patch_installer(directory)
            self.assertEqual(path.read_bytes(), once)
            self.assertEqual(once.count(b'/rastertospl-real"'), 1)

    def test_only_matching_filters_are_patched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'printer.ppd'
            original = (
                '*PPD-Adobe: "4.3"\n'
                '*LanguageVersion: English\n'
                '*cupsFilter: "application/vnd.cups-raster 0 rastertospl"\n'
            )
            path.write_text(original)
            self.assertTrue(p.patch_ppd_file(path))
            self.assertIn('DuplexTumble', path.read_text())
            once = path.read_bytes()
            self.assertFalse(p.patch_ppd_file(path))
            self.assertEqual(path.read_bytes(), once)
            path.write_text(original.replace('rastertospl', 'pstosecps'))
            self.assertFalse(p.patch_ppd_file(path))

    def test_upgrade_long_edge_only_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'printer.ppd'
            path.write_text(
                '*cupsFilter: "application/vnd.cups-raster 0 rastertospl"\n'
                '*OpenUI *Duplex/Duplex: PickOne\n'
                '*Duplex DuplexNoTumble/Long Edge (Standard): ""\n'
                '*CloseUI: *Duplex\n'
            )
            self.assertTrue(p.patch_ppd_file(path))
            self.assertIn('DuplexTumble', path.read_text())
            self.assertFalse(p.patch_ppd_file(path))

    def test_native_duplex_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'printer.ppd'
            original = (
                '*cupsFilter: "application/vnd.cups-raster 0 rastertospl"\n'
                '*OpenUI *Duplex/Duplex: PickOne\n'
                '*Duplex DuplexNoTumble: "<</Duplex true>>setpagedevice"\n'
            )
            path.write_text(original)
            self.assertFalse(p.patch_ppd_file(path))
            self.assertEqual(path.read_text(), original)

    def test_cli_preserves_original_binary_on_repeated_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / 'noarch/share/ppd').mkdir(parents=True)
            (target / 'noarch/printer.pkg').write_text(
                'install_lns_p "${DIST_DIR}/${ARCH_SUBDIR}/rastertospl" '
                '"$INSTDIR_COMMON_PRINTER_BIN" "$INSTDIR_CUPS_FILTERS"\n'
            )
            original = b'\x7fELFvendor-filter'
            for arch in ('i386', 'x86_64'):
                (target / arch).mkdir()
                (target / arch / 'rastertospl').write_bytes(original)
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'patch_uld.py'), directory],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                for arch in ('i386', 'x86_64'):
                    self.assertEqual(
                        (target / arch / 'rastertospl-real').read_bytes(), original
                    )
                    self.assertEqual(
                        (target / arch / 'rastertospl').read_bytes(),
                        (ROOT / 'rastertospl-wrapper.py').read_bytes(),
                    )
            self.assertEqual(
                (target / 'noarch/printer.pkg').read_text().count('/rastertospl-real"'), 1
            )
