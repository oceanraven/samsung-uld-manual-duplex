# Samsung ULD Manual Duplex Patcher

Add manual double-sided printing to Samsung Unified Linux Driver (ULD) printers
that lack hardware duplex, such as the Samsung M2070 series.

The patcher adds Long Edge and Short Edge options to compatible PPDs, wraps the
vendor `rastertospl` filter, and prints odd pages before printing the backs in
reverse order. The first back page requests Manual Feed so the printer can pause
for reloading. This pause depends on the printer model.

## Requirements

- Linux with CUPS and support for PPD-based printer drivers
- Python 3
- `libcupsimage` available to the CUPS filter
- An extracted official Samsung ULD archive

## Install or update

1. Download and extract the official ULD archive from
   [HP/Samsung support](https://support.hp.com/ie-en/drivers/samsung-xpress-sl-m2070-laser-multifunction-printer-series/model/16450385).
2. From this repository, patch the extracted driver directory:

   ```bash
   python3 patch_uld.py /absolute/path/to/uld
   ```

3. Install the patched printer driver:

   ```bash
   cd /absolute/path/to/uld
   sudo ./install-printer.sh
   ```

   Use `sudo ./install.sh` instead to install both printer and scanner drivers.

4. For an existing printer queue, explicitly update its PPD. Installing the
   driver files alone may leave the old queue settings in place. For an M2070:

   ```bash
   sudo lpadmin -p M2070-Series -P /absolute/path/to/uld/noarch/share/ppd/Samsung_M2070_Series.ppd
   lpoptions -p M2070-Series -l | grep -i duplex
   ```

   Replace the directory, queue name, and PPD filename with your own. The expected
   options are `None`, `DuplexNoTumble` (Long Edge), and `DuplexTumble` (Short Edge).
   Restart the printing application after updating the queue.

The patcher preserves the original binary as `rastertospl-real` and updates
`noarch/printer.pkg` so the installer copies it alongside the wrapper. Re-running
the patcher preserves that binary and does not duplicate PPD options or installer
entries. Existing hardware-duplex PPDs continue to use the vendor filter.

## Print double-sided

First test with a numbered four-page document and one copy.

1. Select the updated printer queue and **Long Edge** or **Short Edge**.
2. Let the odd pages print. On supported printers, Manual Feed then requests
   paper or an OK/Start button press.
3. Reload the stack so the last printed sheet feeds first and the blank sides
   receive the even pages. Refeed orientation depends on the printer.
4. Press the printer's OK/Start button when prompted.
5. Check that pages 1/2 and 3/4 share sheets and that both sides have the intended
   orientation when turned along the selected binding edge.

Use the same refeed orientation for both binding modes. For the reported M2070
refeed orientation, portrait Long Edge rotates back pages 180 degrees and Short
Edge leaves them unchanged. Landscape pages use the opposite rotation. An odd
page count adds a blank back so sheets remain aligned.

## Troubleshooting

- **Double-sided printing is unavailable:** check the queue's PPD with
  `lpoptions -p YOUR_QUEUE -l`. Update it as shown above, select that queue in the
  application, and restart the application.
- **Unable to open PPD:** use the full path to the extracted driver. A relative
  path such as `$PWD/noarch/...` works only from that driver directory.
- **Printer drivers are deprecated:** this is a CUPS warning about future driver
  support, not an installation error by itself.
- **No pause before the backs:** Manual Feed behavior varies by model. Verify it
  with a small job before printing a larger document. A filter finishing means
  data was sent; it does not confirm that printing or reloading has completed.

## Limits and verification

Manual duplex supports one copy per job. Multiple copies and non-chunky raster
color orders fail explicitly. Rotation supports packed 1-, 2-, and 4-bit pixels
and byte-aligned pixels, preserving channel order and row padding.

Page data is stored in a private temporary directory per invocation and removed
on normal completion or Python exceptions. Truncated pixel data and raster write
failures fail the job.

Run the regression tests from this repository:

```bash
python3 -m unittest discover -s tests -v
```

Tests cover generated CUPS raster round trips, both binding modes, packed and RGB
rotation, odd-page padding, cleanup on failure, native duplex passthrough, and
repeatable patching with original binary preservation. Raster tests require
`libcupsimage`; they are skipped if it is unavailable. Physical printer behavior
still requires a print test on your model.
