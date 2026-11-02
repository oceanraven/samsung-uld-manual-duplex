#!/usr/bin/env python3
"""
rastertospl - Manual Duplex Wrapper for Samsung ULD CUPS Filter

This script wraps the original rastertospl-real binary to provide
manual double-sided (duplex) printing for printers that lack
hardware duplex support.

It uses libcupsimage to properly parse CUPS Raster data (handling v1, v2, v3
and PackBits compression natively).
"""

import ctypes
import os
import re
import struct
import subprocess
import sys
import tempfile
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

DUPLEX_REQUEST_DIR = None

# Native cups_page_header2_t layout (unsigned 32-bit fields).
HEADER_SIZE = 1796
DUPLEX_OFFSET = 272
TUMBLE_OFFSET = 368
WIDTH_OFFSET = 372
HEIGHT_OFFSET = 376
BITS_PER_PIXEL_OFFSET = 388
BYTES_PER_LINE_OFFSET = 392
COLOR_ORDER_OFFSET = 396
COLOR_SPACE_OFFSET = 400
CHUNK_SIZE = 4 * 1024 * 1024

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------


def log(msg, level="DEBUG"):
    print(f"{level}: rastertospl-wrapper: {msg}", file=sys.stderr)

# ---------------------------------------------------------------------------
# libcups bindings
# ---------------------------------------------------------------------------

try:
    lib_name = find_library("cupsimage") or "libcupsimage.so.2"
    libcups = ctypes.CDLL(lib_name)

    libcups.cupsRasterOpen.restype = ctypes.c_void_p
    libcups.cupsRasterOpen.argtypes = [ctypes.c_int, ctypes.c_int]

    libcups.cupsRasterClose.restype = None
    libcups.cupsRasterClose.argtypes = [ctypes.c_void_p]

    libcups.cupsRasterReadHeader2.restype = ctypes.c_uint
    libcups.cupsRasterReadHeader2.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    libcups.cupsRasterReadPixels.restype = ctypes.c_uint
    libcups.cupsRasterReadPixels.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]

    libcups.cupsRasterWriteHeader2.restype = ctypes.c_uint
    libcups.cupsRasterWriteHeader2.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    libcups.cupsRasterWritePixels.restype = ctypes.c_uint
    libcups.cupsRasterWritePixels.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
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
    # Existing hardware-duplex PPDs must continue to use the vendor filter.
    ppd_path = os.environ.get("PPD")
    if ppd_path:
        with open(ppd_path, "rb") as ppd:
            if re.search(rb'^\*Duplex\s+[^:]+:\s*"[^"\r\n]+"', ppd.read(), re.MULTILINE):
                return False
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
    ras_in = libcups.cupsRasterOpen(fd_in, 0)  # CUPS_RASTER_READ
    if not ras_in:
        raise OSError("cupsRasterOpen failed")

    pages = []
    page_idx = 0

    try:
        while True:
            header = ctypes.create_string_buffer(HEADER_SIZE)
            if not libcups.cupsRasterReadHeader2(ras_in, header):
                break

            height = struct.unpack_from("I", header, HEIGHT_OFFSET)[0]
            bpl = struct.unpack_from("I", header, BYTES_PER_LINE_OFFSET)[0]
            color_order = struct.unpack_from("I", header, COLOR_ORDER_OFFSET)[0]
            if color_order != 0:
                raise ValueError("Only chunky raster color order is supported")
            if not height or not bpl:
                raise ValueError("Invalid raster dimensions")
            page_size = height * bpl

            log(f"Parsed page {page_idx + 1} header: {height}x{bpl} ({page_size} bytes)")

            tmp_file = os.path.join(DUPLEX_REQUEST_DIR, f"job-{job_id}-page-{page_idx}.raw")

            # Read in chunks to avoid allocating huge string buffers for very large pages
            bytes_read = 0

            with open(tmp_file, "wb") as f:
                while bytes_read < page_size:
                    to_read = min(CHUNK_SIZE, page_size - bytes_read)
                    pixel_data = ctypes.create_string_buffer(to_read)
                    res = libcups.cupsRasterReadPixels(ras_in, pixel_data, to_read)
                    if res == 0:
                        raise ValueError("Truncated raster page")
                    f.write(pixel_data.raw[:res])
                    bytes_read += res

            pages.append((header.raw, tmp_file))
            page_idx += 1

    finally:
        libcups.cupsRasterClose(ras_in)
    return pages


def rotate_page_180(page):
    """Reverse rows and whole pixels, preserving channel order and row padding."""
    header, source = page
    width, height = struct.unpack_from("2I", header, WIDTH_OFFSET)
    bits, stride = struct.unpack_from("2I", header, BITS_PER_PIXEL_OFFSET)
    active = (width * bits + 7) // 8
    if not width or not height or bits <= 0 or active > stride:
        raise ValueError("Invalid raster geometry for rotation")
    if bits not in (1, 2, 4) and bits % 8:
        raise ValueError(f"Unsupported packed pixel size: {bits}")
    destination = source + ".rotated"
    table = None
    if bits < 8:
        mask = (1 << bits) - 1
        table = bytes(sum(((value >> offset) & mask) << (8 - bits - offset)
                          for offset in range(0, 8, bits)) for value in range(256))
    with open(source, "rb") as inp, open(destination, "wb") as out:
        for y in range(height - 1, -1, -1):
            inp.seek(y * stride)
            row = inp.read(stride)
            if len(row) != stride:
                raise ValueError("Truncated raster page during rotation")
            pixels = row[:active]
            if bits < 8:
                reversed_bytes = pixels[::-1].translate(table)
                padding = active * 8 - width * bits
                value = int.from_bytes(reversed_bytes, "big") << padding
                value &= (1 << (active * 8)) - 1
                value |= pixels[-1] & ((1 << padding) - 1)
                rotated = value.to_bytes(active, "big")
            elif bits == 8:
                rotated = pixels[::-1]
            else:
                pixel_bytes = bits // 8
                rotated = b"".join(pixels[x:x + pixel_bytes]
                                   for x in range(active - pixel_bytes, -1, -pixel_bytes))
            out.write(rotated + row[active:])
    return header, destination


def orient_back_pages(pages, mode):
    # With the tested refeed path, unrotated portrait backs bind at the short edge.
    oriented = []
    for page in pages:
        width, height = struct.unpack_from("2I", page[0], WIDTH_OFFSET)
        rotate = (mode == "long") == (width <= height)
        oriented.append(rotate_page_180(page) if rotate else page)
    return oriented


def run_real_filter(argv, pages, is_last_pass, extra_options=None):
    """
    Spawns rastertospl-real and writes the selected pages via a new CUPS raster stream.
    """
    cmd = [REAL_FILTER] + argv[1:6]
    if len(cmd) >= 6:
        opts = cmd[5]
        cleaned = []
        for token in opts.split():
            if token.startswith(("Duplex=", "sides=", "InputSlot=")):
                continue
            cleaned.append(token)
        cleaned.extend(["Duplex=None", "sides=one-sided"])
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

    try:
        for header_bytes, tmp_file in pages:
            header = ctypes.create_string_buffer(header_bytes, len(header_bytes))
            # Suppress hardware duplex in the raster header as well as options.
            struct.pack_into("I", header, DUPLEX_OFFSET, 0)
            struct.pack_into("I", header, TUMBLE_OFFSET, 0)
            if not libcups.cupsRasterWriteHeader2(ras_out, header):
                raise IOError("Failed to write raster header")

            with open(tmp_file, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    pixel_buf = ctypes.create_string_buffer(chunk, len(chunk))
                    if libcups.cupsRasterWritePixels(ras_out, pixel_buf, len(chunk)) != len(chunk):
                        raise IOError("Failed to write raster pixels")

            if is_last_pass:
                try:
                    os.unlink(tmp_file)
                except FileNotFoundError:
                    pass

    finally:
        libcups.cupsRasterClose(ras_out)
        proc.stdin.close()
        proc.wait()
    return proc.returncode

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _main():
    argv = sys.argv
    if len(argv) not in (6, 7):
        log("Expected job-id user title copies options [raster-file]", "ERROR")
        return 1
    log(f"Wrapper invoked with {len(argv)} arguments")

    options_str = argv[5] if len(argv) > 5 else ""
    options = parse_options(options_str)

    if not is_manual_duplex_requested(options):
        log("No duplex requested - passing through to real filter")
        os.execv(REAL_FILTER, [REAL_FILTER] + argv[1:])

    if libcups is None:
        log("Manual duplex requires libcupsimage", "ERROR")
        return 1
    if len(argv) > 4 and argv[4] != "1":
        log("Manual duplex requires one copy per job", "ERROR")
        return 1

    log("Manual duplex requested - intercepting raster stream")

    job_id = "raster"

    fd_in = 0
    if len(argv) > 6 and argv[6]:
        fd_in = os.open(argv[6], os.O_RDONLY)

    try:
        pages = parse_raster_stream(fd_in, job_id)
    except Exception as e:
        log(f"Error parsing raster stream: {e}")
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
        # Total pages is odd (2n + 1). The last odd page has no corresponding even page.
        # When the stack is flipped and fed back, this last odd page will be the first
        # sheet pulled. We must pad the even pages with a blank page so that this sheet
        # passes through blank, and the actual even pages align correctly with their fronts.

        # We duplicate the header of the last odd page
        last_header = odd_pages[-1][0]
        height = struct.unpack_from("I", last_header, HEIGHT_OFFSET)[0]
        bpl = struct.unpack_from("I", last_header, BYTES_PER_LINE_OFFSET)[0]
        cs = struct.unpack_from("I", last_header, COLOR_SPACE_OFFSET)[0]

        # Additive colorspaces (W, RGB, SW, etc) use 0xFF for white.
        # Subtractive (K, CMYK) use 0x00 for white.
        if cs in (0, 1, 2, 18, 19, 20):
            fill_byte = b"\xFF"
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

    even_pages = orient_back_pages(even_pages, get_duplex_mode(options))

    log(f"Odd pages: {len(odd_pages)}, Even pages (padded): {len(even_pages)}")

    log("=== Printing odd pages ===")
    ret = run_real_filter(argv, odd_pages, False)
    if ret != 0:
        log(f"Error printing odd pages (exit code {ret})")
        return ret

    log("=== Sending even pages ===")
    even_pages_reversed = list(reversed(even_pages))

    # We bypass the computer GUI wait and instead use the printer's Manual Feed.
    # To avoid requiring the user to press OK for *every* page, we split the even
    # pages into two jobs:
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


def main():
    global DUPLEX_REQUEST_DIR
    options = parse_options(sys.argv[5] if len(sys.argv) > 5 else "")
    if not is_manual_duplex_requested(options):
        return _main()
    with tempfile.TemporaryDirectory(prefix="manual-duplex-") as directory:
        previous_directory = DUPLEX_REQUEST_DIR
        DUPLEX_REQUEST_DIR = directory
        try:
            return _main()
        finally:
            DUPLEX_REQUEST_DIR = previous_directory


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"Fatal error: {e}", "ERROR")
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
