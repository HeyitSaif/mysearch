#!/usr/bin/env python3
"""Compares rendered snapshots with the goldens (Pillow). Used by scripts/snapshot_check.sh.

    compare_snapshots.py <rendered-dir> <golden-dir> [--update] <state> [<state> ...]

For each state, <rendered-dir>/<state>.png is compared with <golden-dir>/<state>.png. The check passes when the sizes match
and no more than MAX_DIFF_FRACTION of the pixels differ by more than CHANNEL_TOLERANCE in any colour channel (small
anti-aliasing differences between runs are tolerated, a changed layout or a changed label is not).

With --update the rendered PNGs replace the goldens, but never as copies: each golden is written by save_golden(), which
drops the chunks in PRIVATE_CHUNKS (a device colour profile, Exif, text, the modification time) and refuses to write when
the pixels would change. Look at every new golden before you keep it.
Exit status: 0 when every state passes (or was updated), 1 otherwise, 2 on a usage error.
"""

import io
import os
import struct
import sys
import zlib

import numpy as np
from PIL import Image, ImageChops

CHANNEL_TOLERANCE = 8          # per channel, 0 to 255
MAX_DIFF_FRACTION = 0.0005     # 0.05 % of the pixels

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Never kept in a golden. iCCP: a device colour profile (a display profile can carry the display's serial number).
# eXIf: Exif metadata. tEXt, zTXt, iTXt: text. tIME: when the file was last changed.
PRIVATE_CHUNKS = (b"iCCP", b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME")


def strip_png_chunks(data, drop=PRIVATE_CHUNKS):
    """The PNG bytes without the chunks named in drop. Every other chunk is copied byte for byte, so its CRC stays valid.

    Fails closed: raises ValueError for a bad signature, a truncated chunk, a chunk whose CRC does not match (corruption is
    not carried through), or bytes after IEND. Error messages name chunk types only, never their contents.
    """
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG file")
    out = [PNG_SIGNATURE]
    i = 8
    while True:
        if i + 12 > len(data):
            raise ValueError("truncated PNG: the file ends before IEND")
        length = struct.unpack(">I", data[i:i + 4])[0]
        end = i + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG: a chunk runs past the end of the file")
        kind = data[i + 4:i + 8]
        if zlib.crc32(data[i + 4:end - 4]) & 0xFFFFFFFF != struct.unpack(">I", data[end - 4:end])[0]:
            raise ValueError("bad CRC in a %s chunk" % kind.decode("latin-1"))
        if kind not in drop:
            out.append(data[i:end])
        i = end
        if kind == b"IEND":
            break
    if i != len(data):
        raise ValueError("bytes after IEND")
    return b"".join(out)


def same_pixels(a, b):
    """True when two PNG byte strings decode (Pillow, no colour management) to the same size and the same RGBA pixels."""
    ia = Image.open(io.BytesIO(a))
    ib = Image.open(io.BytesIO(b))
    if ia.size != ib.size:
        return False
    return np.array_equal(np.asarray(ia.convert("RGBA")), np.asarray(ib.convert("RGBA")))


def save_golden(rendered, golden):
    """Write the golden from a rendered PNG: the private chunks are dropped, and the pixels are checked first.

    Raises ValueError when the source is malformed and RuntimeError when stripping would change a pixel; in both cases
    nothing is written.
    """
    with open(rendered, "rb") as fh:
        original = fh.read()
    stripped = strip_png_chunks(original)
    if not same_pixels(original, stripped):
        raise RuntimeError("stripping would change the pixels; the golden was not written")
    folder = os.path.dirname(golden)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(golden, "wb") as fh:
        fh.write(stripped)


def compare(rendered, golden):
    a = Image.open(rendered).convert("RGB")
    b = Image.open(golden).convert("RGB")
    if a.size != b.size:
        return False, "size %dx%d, golden is %dx%d" % (a.size + b.size)
    diff = ImageChops.difference(a, b).convert("L")
    mask = diff.point(lambda v: 255 if v > CHANNEL_TOLERANCE else 0)
    changed = mask.histogram()[255]
    total = a.size[0] * a.size[1]
    fraction = changed / float(total)
    ok = fraction <= MAX_DIFF_FRACTION
    return ok, "%.4f%% of %d pixels differ" % (fraction * 100, total)


def main(argv):
    update = "--update" in argv
    args = [a for a in argv if a != "--update"]
    if len(args) < 3:
        sys.stderr.write("usage: compare_snapshots.py <rendered-dir> <golden-dir> [--update] <state> ...\n")
        return 2
    rendered_dir, golden_dir, states = args[0], args[1], args[2:]
    failed = []
    for state in states:
        rendered = os.path.join(rendered_dir, state + ".png")
        golden = os.path.join(golden_dir, state + ".png")
        if not os.path.isfile(rendered):
            print("FAIL %s: no render at %s" % (state, rendered))
            failed.append(state)
            continue
        if update:
            try:
                save_golden(rendered, golden)
            except (ValueError, RuntimeError) as ex:
                print("FAIL %s: %s" % (state, ex))
                failed.append(state)
                continue
            print("updated %s -> %s" % (state, golden))
            continue
        if not os.path.isfile(golden):
            print("FAIL %s: no golden at %s (run with --update after looking at the render)" % (state, golden))
            failed.append(state)
            continue
        ok, detail = compare(rendered, golden)
        print("%s %s: %s" % ("ok  " if ok else "FAIL", state, detail))
        if not ok:
            failed.append(state)
    if failed:
        print("failed: " + " ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
