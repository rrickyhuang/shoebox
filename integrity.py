"""Read-only integrity sweep of the photo library.

A file whose tail was overwritten with stale sectors keeps its correct size and
its correct IFD structure (those live near the start), so only the DATA proves
damage. Nearly every CR2/DNG embeds a full-size JPEG preview near the end of the
file, so checking that preview is a direct test of the tail.

Pass 1 (cheap): locate each raw's data blocks by walking IFD0 + SubIFDs, then
read only the first and last 2 bytes of the embedded JPEG preview and confirm
the SOI/EOI markers. Two tiny seeks per file.
Pass 2 (--decode): fully decode each preview that passed, which also catches
corruption in the middle of the block.
"""
import os, sys, struct, collections

# Override the library root with the PHOTO_LIBRARY environment variable.
LIB = os.path.join(os.environ.get("PHOTO_LIBRARY", r"E:\Photos"), "Library")
RAW = (".cr2", ".cr3", ".dng", ".nef", ".arw", ".orf", ".rw2")
DECODE = "--decode" in sys.argv


def ifd_blocks(fh, size):
    """Return [(offset, length, kind)] for strips/tiles/previews, or None."""
    fh.seek(0)
    head = fh.read(1048576)
    if head[:2] not in (b"II", b"MM"):
        return None
    en = "<" if head[:2] == b"II" else ">"

    def u32(off):
        if off + 4 > len(head):
            return None
        return struct.unpack_from(en + "I", head, off)[0]

    def u16(off):
        if off + 2 > len(head):
            return None
        return struct.unpack_from(en + "H", head, off)[0]

    blocks = []
    todo, seen = [u32(4)], set()
    while todo:
        off = todo.pop()
        if not off or off in seen or off + 2 > len(head):
            continue
        seen.add(off)
        n = u16(off)
        if not n or n > 512:
            continue
        info = {}
        for i in range(n):
            e = off + 2 + i * 12
            if e + 12 > len(head):
                break
            tag, typ, cnt = struct.unpack_from(en + "HHI", head, e)
            esz = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 11: 4, 12: 8}.get(typ, 1)
            total = esz * cnt
            po = u32(e + 8) if total > 4 else e + 8
            if typ in (3, 4) and po is not None and po + total <= len(head):
                f = "H" if typ == 3 else "I"
                info[tag] = [struct.unpack_from(en + f, head, po + k * esz)[0]
                             for k in range(min(cnt, 2048))]
            if tag == 0x014A and tag in info:
                todo.extend(info[tag])
        for ot, ct, kind in ((0x0201, 0x0202, "preview"), (0x0111, 0x0117, "strip"),
                             (0x0144, 0x0145, "tile")):
            if info.get(ot) and info.get(ct):
                for o, c in zip(info[ot], info[ct]):
                    if c > 0:
                        blocks.append((o, c, kind))
        nx = u32(off + 2 + n * 12)
        if nx:
            todo.append(nx)
    return blocks


def check(path):
    """-> (verdict, detail).  verdict in ok / damaged / unchecked"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            ext = os.path.splitext(path)[1].lower()
            if ext in (".jpg", ".jpeg"):
                fh.seek(0)
                if fh.read(2) != b"\xff\xd8":
                    return "damaged", "no JPEG SOI"
                # Lab film scanners pad a few bytes after EOI, so the marker is
                # not always the final two bytes. Look in the tail instead.
                fh.seek(max(0, size - 1024))
                if b"\xff\xd9" not in fh.read():
                    return "damaged", "no JPEG EOI in final 1 KB"
                return "ok", "jpeg markers"
            if ext not in RAW:
                return "unchecked", "not a raw"
            blocks = ifd_blocks(fh, size)
            if not blocks:
                return "unchecked", "no TIFF structure (CR3/HEIF?)"
            bad = [(o, c) for o, c in [(o, c) for o, c, k in blocks] if o + c > size]
            if bad:
                return "damaged", "data block runs past EOF"
            # Check EVERY JPEG-coded block, largest first -- NOT just the one
            # tagged 0x0201. On a Canon CR2 that tag is a 14 KB thumbnail
            # sitting 0.2% into the file, so testing only it passes a file
            # whose entire tail has been destroyed. Verified against a control:
            # 32.7 MB of a 32.8 MB file overwritten, old code said "ok".
            # The largest block is the raw image data (lossless JPEG, so it
            # does start FFD8) and it ends at EOF, which is what makes this a
            # real tail test.
            checked = 0
            # last_end must come from blocks we actually VERIFIED, not from
            # every block. Taking the max over all of them let an unverified
            # strip satisfy the reaches-EOF test while the only block really
            # checked was a thumbnail near the start -- true for CR2 by luck,
            # false for uncompressed DNG, NEF, ARW, ORF and RW2.
            last_end = 0
            for o, c, kind in sorted(blocks, key=lambda b: -b[1]):
                fh.seek(o)
                if fh.read(2) != bytes((0xFF, 0xD8)):
                    continue                     # not a JPEG-coded block
                fh.seek(o + c - 2)
                if fh.read(2) != bytes((0xFF, 0xD9)):
                    return "damaged", "JPEG block at %d+%d does not terminate" % (o, c)
                checked += 1
                last_end = max(last_end, o + c)
                if DECODE:
                    fh.seek(o)
                    blob = fh.read(c)
                    try:
                        import io
                        from PIL import Image
                        im = Image.open(io.BytesIO(blob))
                        im.load()
                    except Exception as e:
                        return "damaged", ("JPEG block at %d will not decode: %s"
                                           % (o, str(e)[:50]))
            if checked == 0:
                return "unchecked", "no JPEG-coded block found"
            if last_end < size - 65536:
                return "unchecked", ("data ends %d bytes before EOF"
                                     % (size - last_end))
            return "ok", "%d JPEG block(s) intact, data reaches EOF" % checked
    except Exception as e:
        return "unchecked", "read error: %s" % str(e)[:60]


def main():
    files = []
    for dp, dn, fn in os.walk(LIB):
        for f in fn:
            if os.path.splitext(f)[1].lower() in RAW + (".jpg", ".jpeg"):
                files.append(os.path.join(dp, f))
    print("  sweeping %d files%s" % (len(files), "  (with full preview decode)" if DECODE else ""))

    tally = collections.Counter()
    damaged, unchecked = [], collections.Counter()
    for i, p in enumerate(files, 1):
        v, d = check(p)
        tally[v] += 1
        if v == "damaged":
            damaged.append((p, d))
        elif v == "unchecked":
            unchecked[d] += 1
        if i % 2500 == 0:
            print("    %d / %d   damaged so far: %d" % (i, len(files), len(damaged)))

    print("")
    print("  ok        : %d" % tally["ok"])
    print("  DAMAGED   : %d" % tally["damaged"])
    print("  unchecked : %d" % tally["unchecked"])
    for k, v in unchecked.most_common():
        print("      %-44s %d" % (k, v))
    if damaged:
        print("")
        print("  DAMAGED FILES:")
        for p, d in damaged:
            print("    %s" % p)
            print("        %s" % d)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "damaged.txt"), "w", encoding="utf-8") as fh:
            for p, d in damaged:
                fh.write("%s\t%s\n" % (p, d))
        print("")
        print("  -> damaged.txt")
    else:
        print("")
        print("  no damage found.")


if __name__ == "__main__":
    main()
