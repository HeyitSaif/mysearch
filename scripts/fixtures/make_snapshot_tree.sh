#!/bin/sh
# Builds the fictional folder tree that the golden snapshots render (scripts/snapshot_check.sh, through --fixture).
#   scripts/fixtures/make_snapshot_tree.sh <new-dir>
# The folder must be absolute and must not exist, or must be empty. Nothing else is touched and nothing is deleted.
# Only fictional names and generated content go in: a PNG and a minimal PDF generated here, and short text files.
# Modification times are fixed in 2025, so the list views sort the same way on every run.
set -eu
if [ $# -ne 1 ]; then
  echo "usage: make_snapshot_tree.sh <new-dir>" >&2
  exit 2
fi
root="$1"
case "$root" in
  /*) ;;
  *) echo "use an absolute path: $root" >&2; exit 2 ;;
esac
if [ -e "$root" ]; then
  if [ ! -d "$root" ] || [ -n "$(ls -A "$root")" ]; then
    echo "refusing to write into a folder that exists and is not empty: $root" >&2
    exit 2
  fi
fi
mkdir -p "$root"
exec python3 - "$root" <<'PY'
import datetime
import os
import struct
import sys
import zlib

root = sys.argv[1]


def write(rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def png(width, height, pixel):
    """A deterministic RGB PNG; pixel(x, y) gives (r, g, b)."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)                       # filter type: none
        for x in range(width):
            rows.extend(pixel(x, y))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")


def harbour(x, y):
    # a sky gradient, a sea band and a sun: plain shapes, nothing photographic
    if y > 130:
        return (30, 90 + y // 4, 130)
    sun = (x - 230) ** 2 + (y - 60) ** 2 < 900
    if sun:
        return (250, 205, 90)
    return (120 + y // 2, 170 + y // 4, 235)


def diagram(x, y):
    if x % 40 == 0 or y % 40 == 0:
        return (60, 60, 60)
    return (240, 244, 248)


def pdf(text):
    stream = ("BT /F1 24 Tf 72 720 Td (" + text + ") Tj ET").encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(number).encode("ascii") + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)).encode("ascii")
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode("ascii")
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)).encode("ascii")
    return bytes(out)


TEXT = {
    "home/Downloads/Sample download.txt": "A sample download for the snapshot fixture.\n",
    "home/Documents/Notes/Meeting notes.md": "# Meeting notes\n\n- Sample agenda item one.\n- Sample agenda item two.\n",
    "home/Documents/Notes/Weekly plan.txt": "Sample weekly plan: review, draft, publish.\n",
    "home/Documents/Studio/Projects/Alpha/README.md": "# Alpha\n\nA fictional sample project.\n",
    "home/Documents/Studio/Projects/Alpha/app/Model.swift":
        "// Sample model for the snapshot fixture. Not real code.\n"
        "struct SampleItem {\n    let name: String\n    let quantity: Int\n}\n",
    "home/Documents/Studio/Projects/Alpha/app/Navigator.swift":
        "// Sample navigator for the snapshot fixture.\nstruct SampleNavigator {\n    var current: String\n}\n",
    "home/Documents/Studio/Projects/Alpha/docs/Guide.md": "# Alpha guide\n\nSample text for the snapshot fixture.\n",
    "home/Documents/Studio/Projects/Beta/README.md": "# Beta\n\nAnother fictional sample project.\n",
}

for rel, text in TEXT.items():
    write(rel, text.encode("utf-8"))
write("home/Photos/Harbour.png", png(320, 200, harbour))
write("home/Documents/Studio/Projects/Alpha/docs/Diagram.png", png(320, 200, diagram))
write("home/Documents/Invoices/2025/Acme invoice 0001.pdf", pdf("Acme invoice 0001 (fictional sample)"))
write("home/Documents/Invoices/2025/Acme invoice 0002.pdf", pdf("Acme invoice 0002 (fictional sample)"))
write(".mse-snapshot-fixture", b"Fictional fixture for MySearch snapshots. It holds no real data.\n")

# the folders the states open, and the stand-ins for the boot volume, a cloud drive and an external drive
for rel in ("home/Desktop", "home/Documents/Notes", "home/Documents/Studio/Projects/Alpha/app",
            "home/Documents/Studio/Projects/Alpha/docs", "home/Documents/Studio/Projects/Beta",
            "home/Photos", "disk", "cloud", "drive"):
    os.makedirs(os.path.join(root, rel), exist_ok=True)

FILE_TIMES = {
    "home/Downloads/Sample download.txt": "2025-02-10 09:00",
    "home/Photos/Harbour.png": "2025-02-20 09:00",
    "home/Documents/Notes/Meeting notes.md": "2025-03-02 09:00",
    "home/Documents/Notes/Weekly plan.txt": "2025-03-09 09:00",
    "home/Documents/Invoices/2025/Acme invoice 0001.pdf": "2025-05-14 09:00",
    "home/Documents/Invoices/2025/Acme invoice 0002.pdf": "2025-06-02 09:00",
    "home/Documents/Studio/Projects/Alpha/README.md": "2025-07-01 09:00",
    "home/Documents/Studio/Projects/Alpha/app/Model.swift": "2025-07-18 09:00",
    "home/Documents/Studio/Projects/Alpha/app/Navigator.swift": "2025-07-19 09:00",
    "home/Documents/Studio/Projects/Alpha/docs/Guide.md": "2025-07-22 09:00",
    "home/Documents/Studio/Projects/Alpha/docs/Diagram.png": "2025-07-23 09:00",
    "home/Documents/Studio/Projects/Beta/README.md": "2025-08-01 09:00",
}
DIR_TIME = "2025-09-01 09:00"


def epoch(text):
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=datetime.timezone.utc).timestamp()


# Fixed modification times, set after everything is created (creating an entry changes its folder's time).
for rel, when in FILE_TIMES.items():
    os.utime(os.path.join(root, rel), (epoch(when), epoch(when)))
dirs = []
for dirpath, dirnames, _ in os.walk(root):
    for d in dirnames:
        dirs.append(os.path.join(dirpath, d))
for path in sorted(dirs, key=lambda p: p.count(os.sep), reverse=True):
    os.utime(path, (epoch(DIR_TIME), epoch(DIR_TIME)))
os.utime(root, (epoch(DIR_TIME), epoch(DIR_TIME)))
print(root)
PY
