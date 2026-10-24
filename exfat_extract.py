"""Extract files from a damaged exFAT card using recovered directory entries.

Read-only on the card. Writes each recovered file to a destination folder, then
checks it: exact expected length, a plausible raw header, and for CR2/DNG that
the embedded JPEG preview still terminates correctly (the same tail test that
caught the one genuinely corrupt file in the library).

Files are pulled in CLUSTER ORDER, which keeps reads sequential across the
media -- worth having on a card that may not survive many passes. There is
deliberately no "irreplaceable first" ordering: the name+size test that would
decide it proved unreliable here (11 of 12 sampled matches were stale directory
entries, not duplicates), so everything is extracted and deduplicated afterwards
by sdcard.py, which compares content.

Contiguous files (NoFatChain) are a straight read. Fragmented ones follow the
FAT cluster chain, read from the first FAT.

usage: python exfat_extract.py H <dest> [--limit N]
"""
import ctypes
import ctypes.wintypes as w
import struct
import sys
import os
import json
import sqlite3

GENERIC_READ = 0x80000000
SHARE_RW = 0x03
OPEN_EXISTING = 3

k32 = ctypes.windll.kernel32
k32.CreateFileW.restype = w.HANDLE
k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                            w.DWORD, w.DWORD, w.HANDLE]
k32.SetFilePointerEx.argtypes = [w.HANDLE, ctypes.c_longlong,
                                 ctypes.POINTER(ctypes.c_longlong), w.DWORD]


class Dev(object):
    def __init__(self, letter):
        path = chr(92) * 2 + "." + chr(92) + letter + ":"
        self.h = k32.CreateFileW(path, GENERIC_READ, SHARE_RW, None,
                                 OPEN_EXISTING, 0, None)
        if self.h in (None, 0, w.HANDLE(-1).value, 0xFFFFFFFFFFFFFFFF):
            raise OSError("cannot open " + path)

    def seek(self, off):
        n = ctypes.c_longlong(0)
        if not k32.SetFilePointerEx(self.h, ctypes.c_longlong(off),
                                    ctypes.byref(n), 0):
            raise OSError("seek failed")

    def read(self, n):
        buf = ctypes.create_string_buffer(n)
        got = w.DWORD(0)
        if not k32.ReadFile(self.h, buf, w.DWORD(n), ctypes.byref(got), None):
            return b""
        return buf.raw[:got.value]

    def close(self):
        k32.CloseHandle(self.h)


def preview_ok(path):
    """For a TIFF-based raw: does its largest embedded JPEG block end cleanly?"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(1048576)
            if head[:2] not in (b"II", b"MM"):
                return None
            en = "<" if head[:2] == b"II" else ">"
            off = struct.unpack_from(en + "I", head, 4)[0]
            if off + 2 > len(head):
                return None
            n = struct.unpack_from(en + "H", head, off)[0]
            if n > 512:
                return None
            info = {}
            for i in range(n):
                e = off + 2 + i * 12
                if e + 12 > len(head):
                    break
                tag, typ, cnt = struct.unpack_from(en + "HHI", head, e)
                esz = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8,
                       11: 4, 12: 8}.get(typ, 1)
                total = esz * cnt
                if total > 4:
                    po = struct.unpack_from(en + "I", head, e + 8)[0]
                else:
                    po = e + 8
                if typ in (3, 4) and po + total <= len(head):
                    f = "H" if typ == 3 else "I"
                    info[tag] = [struct.unpack_from(en + f, head, po + k * esz)[0]
                                 for k in range(min(cnt, 1024))]
            blocks = []
            for ot, ct in ((0x0111, 0x0117), (0x0201, 0x0202), (0x0144, 0x0145)):
                if info.get(ot) and info.get(ct):
                    for o, c in zip(info[ot], info[ct]):
                        if c > 0:
                            blocks.append((o, c))
            if not blocks:
                return None
            o, c = max(blocks, key=lambda b: b[1])
            if o + c > size:
                return False
            fh.seek(o)
            if fh.read(2) != b"\xff\xd8":
                return None
            fh.seek(o + c - 2)
            return fh.read(2) == b"\xff\xd9"
    except Exception:
        return None


def fat_chain(fat, start, need, cluster_count):
    """Cluster chain starting at `start`, following the FAT, at most `need` long.

    Pure so it can be tested without a device. Refuses to trust a damaged FAT:
    a wild cluster number that lands inside the volume would otherwise splice
    unrelated data into the middle of a file, which then has exactly the right
    length and is counted as a successful extraction. Cycles do the same with
    repeated garbage. Both stop the chain, so the caller reports the file short
    rather than silently wrong.
    """
    out = [start]
    seen = {start}
    cur = start
    for _ in range(max(0, need - 1)):
        o = cur * 4
        if o + 4 > len(fat):
            break
        nxt = struct.unpack_from("<I", fat, o)[0]
        if nxt < 2 or nxt >= cluster_count + 2:
            break                       # end-of-chain, bad-cluster marker, or wild
        if nxt in seen:
            break                       # FAT loop
        out.append(nxt)
        seen.add(nxt)
        cur = nxt
    return out


def unique_target(dest, name, first_cluster, claimed, length=None):
    """A destination path no other recovered entry has taken.

    Recovered entries routinely repeat a filename -- cameras reuse
    IMG_1234.CR2, and a whole-device sweep also picks up stale directory
    clusters. Writing them all to one name meant the second either overwrote
    the first or was skipped by the resume check, and BOTH were counted as
    extracted. On a failing card there is no second chance.

    Returns (path, already_done). already_done is True only when a previous
    run of this same entry left a complete file there.
    """
    target = os.path.join(dest, name)
    if target not in claimed and os.path.exists(target) and length is not None \
            and os.path.getsize(target) == length:
        return target, True
    if target in claimed or os.path.exists(target):
        stem, ext = os.path.splitext(name)
        target = os.path.join(dest, "%s__cl%d%s" % (stem, first_cluster, ext))
        k = 2
        while target in claimed or os.path.exists(target):
            target = os.path.join(dest, "%s__cl%d_%d%s"
                                  % (stem, first_cluster, k, ext))
            k += 1
    return target, False


def main():
    letter = sys.argv[1].strip().rstrip(chr(92) + "/").rstrip(":").upper()
    dest = sys.argv[2]
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    here = os.path.dirname(os.path.abspath(__file__))
    meta = json.load(open(os.path.join(here, "validated_entries.json"), encoding="utf-8"))
    cluster_bytes = meta["cluster_bytes"]
    heap = meta["heap"]
    cluster_count = meta.get("cluster_count") or (1 << 62)   # bound if known
    recs = [r for r in meta["records"] if not r["dir"] and r["len"] > 0]

    have = set()
    idx = os.path.join(os.environ.get("PHOTO_LIBRARY", "E:" + os.sep + "Photos"),
                       ".photo-index.db")
    if os.path.exists(idx):
        c = sqlite3.connect(idx)
        for p, s in c.execute("select path, size from files"):
            have.add((os.path.basename(p).lower(), s))
        c.close()
    print("  library index holds %d (name,size) pairs" % len(have))

    # NO name+size prefilter: on this card 11 of 12 entries it called "already
    # filed" were stale entries pointing at reused clusters, not duplicates.
    # Everything validated gets extracted; dedup is done afterwards by
    # sdcard.py, which compares content, not names.
    maybe_known = sum(1 for r in recs
                      if (r["name"].lower(), r["len"]) in have)
    print("  validated entries            : %d  (%.1f GB)"
          % (len(recs), sum(r["len"] for r in recs) / 1e9))
    print("    share a name+size with the library: %d (extracted anyway)"
          % maybe_known)
    order = sorted(recs, key=lambda r: r["cluster"])
    if limit:
        order = order[:limit]
    print("  extracting %d file(s) to %s" % (len(order), dest))

    os.makedirs(dest, exist_ok=True)
    d = Dev(letter)
    d.seek(0)
    boot = d.read(512)
    bps = 1 << boot[108]
    fat_off = struct.unpack_from("<I", boot, 80)[0] * bps
    fat_len = struct.unpack_from("<I", boot, 84)[0] * bps

    state = {"fat": None}

    def chain(start, need):
        if state["fat"] is None:
            print("    reading FAT (%.1f MB) for fragmented files..." % (fat_len / 1e6))
            d.seek(fat_off)
            state["fat"] = d.read(fat_len)
            if len(state["fat"]) < fat_len:
                print("    WARNING: FAT read short (%d of %d bytes); fragmented"
                      % (len(state["fat"]), fat_len))
                print("             files may come out incomplete.")
        return fat_chain(state["fat"], start, need, cluster_count)

    claimed = set()
    ok = 0
    short = 0
    badprev = 0
    failed = 0
    log = []
    for i, r in enumerate(order, 1):
        name = r["name"]
        length = r["len"]
        first = r["cluster"]
        target, already = unique_target(dest, name, first, claimed, length)
        if already:
            claimed.add(target)
            ok += 1
            continue
        claimed.add(target)
        ncl = (length + cluster_bytes - 1) // cluster_bytes
        if r["contig"]:
            clusters = [first + k for k in range(ncl)]
        else:
            clusters = chain(first, ncl)
        written = 0
        try:
            with open(target, "wb") as out:
                for cl in clusters:
                    off = heap + (cl - 2) * cluster_bytes
                    d.seek(off)
                    chunk = d.read(cluster_bytes)
                    if not chunk:
                        break
                    remain = length - written
                    if len(chunk) > remain:
                        chunk = chunk[:remain]
                    out.write(chunk)
                    written += len(chunk)
                    if written >= length:
                        break
        except Exception as e:
            failed += 1
            log.append((name, "write error: " + str(e)))
            continue
        if written != length:
            short += 1
            # rename it out of the way: left as-is it looks like a recovered
            # photo in the output folder, and only the log says otherwise
            try:
                os.replace(target, target + ".partial")
            except OSError:
                pass
            log.append((name, "short: %d of %d bytes (saved as .partial)"
                        % (written, length)))
            continue
        pv = preview_ok(target)
        if pv is False:
            badprev += 1
            log.append((name, "preview does not terminate, data suspect"))
            continue                        # counted once, as suspect, not as ok
        ok += 1
        if i % 100 == 0:
            print("    %d / %d   ok=%d short=%d suspect=%d"
                  % (i, len(order), ok, short, badprev))
    d.close()

    print("")
    print("  extracted              : %d" % ok)
    print("  short / incomplete     : %d" % short)
    print("  preview check failed   : %d" % badprev)
    print("  write failures         : %d" % failed)
    if log:
        print("")
        print("  problems (first 30):")
        for n, why in log[:30]:
            print("      %-22s %s" % (n, why))
        fh = open(os.path.join(here, "extract_problems.txt"), "w", encoding="utf-8")
        for n, why in log:
            fh.write(n + chr(9) + why + chr(10))
        fh.close()
        print("  -> extract_problems.txt")


if __name__ == "__main__":
    main()
