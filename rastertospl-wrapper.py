#!/usr/bin/env python3
"""
rastertospl - Manual Duplex Wrapper for Samsung ULD CUPS Filter

This script wraps the original rastertospl-real binary to provide
manual double-sided (duplex) printing for printers that lack
hardware duplex support.

It uses libcupsimage to properly parse CUPS Raster data (handling v1, v2, v3
and PackBits compression natively).
"""

import os
import sys
import struct
import subprocess
import time
import signal
import ctypes
from ctypes.util import find_library

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
REAL_FILTER = os.path.join(SCRIPT_DIR, "rastertospl-real")

if not os.path.isfile(REAL_FILTER):
    for candidate in [
        "/usr/lib/cups/filter/rastertospl-real",
        "/usr/lib64/cups/filter/rastertospl-real",
        "/opt/smfp-common/printer/bin/rastertospl-real",
        "/opt/samsung/printer/bin/rastertospl-real",
    ]:
        if os.path.isfile(candidate):
            REAL_FILTER = candidate
            break

DUPLEX_REQUEST_DIR = "/tmp/manual-duplex"
DUPLEX_FIFO = os.path.join(DUPLEX_REQUEST_DIR, "request")
DUPLEX_RESPONSE_FIFO = os.path.join(DUPLEX_REQUEST_DIR, "response")

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------


def log(msg):
    print(f"DEBUG: rastertospl-wrapper: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# libcups bindings
# ---------------------------------------------------------------------------

try:
    lib_name = find_library("cupsimage") or "libcupsimage.so.2"
    libcups = ctypes.CDLL(lib_name)

    libcups.cupsRasterOpen.restype = ctypes.c_void_p
    libcups.cupsRasterOpen.argtypes = [ctypes.c_int, ctypes.c_int]

    libcups.cupsRasterClose.argtypes = [ctypes.c_void_p]

    libcups.cupsRasterReadHeader2.restype = ctypes.c_uint
    libcups.cupsRasterReadHeader2.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    libcups.cupsRasterReadPixels.restype = ctypes.c_uint
    libcups.cupsRasterReadPixels.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]

    libcups.cupsRasterWriteHeader2.restype = ctypes.c_uint
    libcups.cupsRasterWriteHeader2.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    libcups.cupsRasterWritePixels.restype = ctypes.c_uint
    libcups.cupsRasterWritePixels.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
except Exception as e:
    log(f"Failed to load libcupsimage: {e}")
    libcups = None

# ---------------------------------------------------------------------------
# Option parsing
# ---------------------------------------------------------------------------


def parse_options(options_str):
    opts = {}
    if not options_str:
        return opts
    for token in options_str.split():
        if "=" in token:
            key, val = token.split("=", 1)
            opts[key] = val
        else:
            opts[token] = "true"
    return opts


def is_manual_duplex_requested(options):
    duplex = options.get("Duplex", "None")
    if duplex in ("DuplexNoTumble", "DuplexTumble"):
        return True
    sides = options.get("sides", "one-sided")
    if sides in ("two-sided-long-edge", "two-sided-short-edge"):
        return True
    return False


def get_duplex_mode(options):
    duplex = options.get("Duplex", "None")
    if duplex == "DuplexTumble":
        return "short"
    sides = options.get("sides", "one-sided")
    if sides == "two-sided-short-edge":
        return "short"
    return "long"


# ---------------------------------------------------------------------------
# Raster processing
# ---------------------------------------------------------------------------


def parse_raster_stream(fd_in, job_id):
    """
    Parses the raster stream using libcupsimage and saves each page's
    uncompressed pixels to a temporary file to save memory.
    Returns a list of (header_bytes, tmp_file_path)
    """
    ras_in = libcups.cupsRasterOpen(fd_in, 0)  # 0 = CUPS_RASTER_READ
    if not ras_in:
        raise Exception("cupsRasterOpen failed")

    pages = []
    page_idx = 0

    os.makedirs(DUPLEX_REQUEST_DIR, mode=0o777, exist_ok=True)

    while True:
        header = ctypes.create_string_buffer(1796)
        if not libcups.cupsRasterReadHeader2(ras_in, header):
            break

        height = struct.unpack_from("I", header, 376)[0]
        bpl = struct.unpack_from("I", header, 392)[0]
        page_size = height * bpl

        log(f"Parsed page {page_idx + 1} header: {height}x{bpl} ({page_size} bytes)")

        tmp_file = os.path.join(DUPLEX_REQUEST_DIR, f"job-{job_id}-page-{page_idx}.raw")

        # Read in chunks to avoid allocating huge string buffers for very large pages
        chunk_size = 1024 * 1024 * 4  # 4MB
        bytes_read = 0

        with open(tmp_file, "wb") as f:
            while bytes_read < page_size:
                to_read = min(chunk_size, page_size - bytes_read)
                pixel_data = ctypes.create_string_buffer(to_read)
                res = libcups.cupsRasterReadPixels(ras_in, pixel_data, to_read)
                if res == 0:
                    break
                f.write(pixel_data.raw)
                bytes_read += to_read

        pages.append((header.raw, tmp_file))
        page_idx += 1

    libcups.cupsRasterClose(ras_in)
    return pages


def run_real_filter(argv, pages, is_last_pass, extra_options=None):
    """
    Spawns rastertospl-real and writes the selected pages via a new CUPS raster stream.
    """
    cmd = [REAL_FILTER] + argv[1:]
    if len(cmd) >= 6:
        opts = cmd[5]
        cleaned = []
        for token in opts.split():
            if (
                token.startswith("Duplex=")
                or token.startswith("sides=")
                or token.startswith("InputSlot=")
            ):
                continue
            cleaned.append(token)
        if extra_options:
            cleaned.append(extra_options)
        cmd[5] = " ".join(cleaned) if cleaned else "none"

    log(f"Running real filter with {len(pages)} pages: {cmd[0]}")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=sys.stdout.buffer,
        stderr=sys.stderr.buffer,
    )

    # 2 = CUPS_RASTER_WRITE_COMPRESSED (v2). Safe for modern Samsung drivers.
    # 1 = CUPS_RASTER_WRITE (v1 uncompressed). Safest for all drivers.
    ras_out = libcups.cupsRasterOpen(proc.stdin.fileno(), 1)

    if not ras_out:
        log("Failed to open output raster stream")
        proc.stdin.close()
        proc.wait()
        return 1

    for header_bytes, tmp_file in pages:
        header = ctypes.create_string_buffer(header_bytes, len(header_bytes))
        libcups.cupsRasterWriteHeader2(ras_out, header)

        page_size = os.path.getsize(tmp_file)
        chunk_size = 1024 * 1024 * 4

        with open(tmp_file, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                pixel_buf = ctypes.create_string_buffer(chunk, len(chunk))
                libcups.cupsRasterWritePixels(ras_out, pixel_buf, len(chunk))

        if is_last_pass:
            try:
                os.unlink(tmp_file)
            except:
                pass

    libcups.cupsRasterClose(ras_out)
    proc.stdin.close()
    proc.wait()
    return proc.returncode


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    argv = sys.argv
    log(f"Wrapper invoked with {len(argv)} arguments")

    if libcups is None:
        log("libcupsimage could not be loaded - falling back to direct passthrough")
        os.execv(REAL_FILTER, [REAL_FILTER] + argv[1:])

    options_str = argv[5] if len(argv) > 5 else ""
    options = parse_options(options_str)

    if not is_manual_duplex_requested(options):
        log("No duplex requested - passing through to real filter")
        os.execv(REAL_FILTER, [REAL_FILTER] + argv[1:])

    log("Manual duplex requested - intercepting raster stream")

    duplex_mode = get_duplex_mode(options)
    job_id = argv[1] if len(argv) > 1 else "0"
    title = argv[3] if len(argv) > 3 else "Unknown"

    fd_in = 0
    if len(argv) > 6 and argv[6]:
        fd_in = os.open(argv[6], os.O_RDONLY)

    try:
        pages = parse_raster_stream(fd_in, job_id)
    except Exception as e:
        log(f"Error parsing raster stream: {e}")
        log("Falling back to direct passthrough")
        if fd_in != 0:
            os.close(fd_in)
        return 1
    finally:
        if fd_in != 0:
            os.close(fd_in)

    total_pages = len(pages)
    if total_pages == 0:
        log("No pages in document")
        return 0

    if total_pages == 1:
        log("Single page document - no duplex needed")
        return run_real_filter(argv, pages, True)

    odd_pages = [pages[i] for i in range(0, total_pages, 2)]
    even_pages = [pages[i] for i in range(1, total_pages, 2)]

    if total_pages % 2 != 0:
        # Total pages is odd. The last odd page has no corresponding even page.
        # When the stack is flipped and fed back, this last odd page will be the first
        # sheet pulled. We must pad the even pages with a blank page so that this sheet
        # passes through blank, and the actual even pages align correctly with their fronts.

        # We duplicate the header of the last odd page
        last_header = odd_pages[-1][0]
        height = struct.unpack_from("I", last_header, 376)[0]
        bpl = struct.unpack_from("I", last_header, 392)[0]
        cs = struct.unpack_from("I", last_header, 396)[0]

        # Additive colorspaces (W, RGB, SW, etc) use 0xFF for white.
        # Subtractive (K, CMYK) use 0x00 for white.
        if cs in (0, 1, 2, 15, 16, 17, 18, 19, 20):
            fill_byte = b"\xff"
        else:
            fill_byte = b"\x00"

        blank_file = os.path.join(DUPLEX_REQUEST_DIR, f"job-{job_id}-blank.raw")
        page_size = height * bpl
        chunk_size = 1024 * 1024

        with open(blank_file, "wb") as f:
            chunk = fill_byte * chunk_size
            written = 0
            while written < page_size:
                to_write = min(chunk_size, page_size - written)
                f.write(chunk[:to_write])
                written += to_write

        even_pages.append((last_header, blank_file))

    log(f"Odd pages: {len(odd_pages)}, Even pages (padded): {len(even_pages)}")

    log("=== Printing odd pages ===")
    ret = run_real_filter(argv, odd_pages, False)
    if ret != 0:
        log(f"Error printing odd pages (exit code {ret})")
        return ret

    log("=== Sending even pages ===")
    even_pages_reversed = list(reversed(even_pages))

    # We bypass the computer GUI wait and instead use the printer's Manual Feed.
    # To avoid requiring the user to press OK for every page, we split the even pages into two jobs:
    # 1. The first even page is sent with InputSlot=Manual (Printer pauses, user presses OK)
    # 2. The remaining even pages are sent with InputSlot=Auto (Prints continuously)

    first_even_page = [even_pages_reversed[0]]
    rest_even_pages = even_pages_reversed[1:]

    log("Sending first even page with Manual Feed (waiting for OK button)...")
    is_last = len(rest_even_pages) == 0
    ret = run_real_filter(argv, first_even_page, is_last, "InputSlot=Manual")
    if ret != 0:
        log(f"Error printing first even page (exit code {ret})")
        return ret

    if not is_last:
        log(f"Sending remaining {len(rest_even_pages)} even pages with Auto Feed...")
        ret = run_real_filter(argv, rest_even_pages, True, "InputSlot=Auto")
        if ret != 0:
            log(f"Error printing remaining even pages (exit code {ret})")
            return ret

    log("Manual duplex printing completed successfully")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"Fatal error: {e}")
        import traceback

        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

