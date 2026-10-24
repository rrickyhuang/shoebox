"""Read-only recovery of exFAT directory entries from a raw device.

The card's DCIM directory lost its contents, but every file's metadata lives in a
32-byte directory-entry SET stored inside its own subdirectory cluster, which is
very likely still intact on the media. One set is:

    0x85  File Directory Entry -- attributes, timestamps, SecondaryCount
    0xC0  Stream Extension     -- NameLength, FirstCluster, DataLength, flags
    0xC1  File Name Entry (xN) -- 15 UTF-16LE characters each

Recovering these yields exact filenames, exact byte lengths and the starting
cluster, so files can be extracted byte-perfect instead of carved and guessed.
Camera-written files are nearly always contiguous (NoFatChain set), which makes
extraction a plain read of DataLength bytes from FirstCluster.

The source device is opened read-only and shared. Nothing is ever written to it.

usage:  python exfat_scan.py H [--limit-gb N]     (bare drive letter)
"""
import ctypes
import ctypes.wintypes as w
import struct
import sys
import os
import json
import collections
import numpy as np

GENERIC_READ = 0x80000000
SHARE_RW = 0x03
OPEN_EXISTING = 3
MEDIA = (".cr2", ".cr3", ".dng", ".nef", ".arw", ".orf", ".rw2", ".jpg", ".jpeg",
         ".heic", ".tif", ".tiff", ".png", ".mov", ".mp4", ".avi")

k32 = ctypes.windll.kernel32
k32.CreateFileW.restype = w.HANDLE
k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                            w.DWORD, w.DWORD, w.HANDLE]
k32.SetFilePointerEx.argtypes = [w.HANDLE, ctypes.c_longlong,
                                 ctypes.POINTER(ctypes.c_longlong), w.DWORD]


class Dev(object):
    def __init__(self, path):
        self.h = k32.CreateFileW(path, GENERIC_READ, SHARE_RW, None,
                                 OPEN_EXISTING, 0, None)
        if self.h in (None, w.HANDLE(-1).value, 0xFFFFFFFFFFFFFFFF):
            raise OSError("cannot open %s" % path)

    def seek(self, off):
        newp = ctypes.c_longlong(0)
        if not k32.SetFilePointerEx(self.h, ctypes.c_longlong(off),
                                    ctypes.byref(newp), 0):
            raise OSError("seek to %d failed" % off)

    def read(self, n):
        buf = ctypes.create_string_buffer(n)
        got = w.DWORD(0)
        if not k32.ReadFile(self.h, buf, w.DWORD(n), ctypes.byref(got), None):
            return b""
        return buf.raw[:got.value]

    def close(self):
        k32.CloseHandle(self.h)


def parse_set(blk, o, cluster_count):
    """Return a record dict for the entry set at offset o, or None."""
    if o + 64 > len(blk):
        return None
    sec = blk[o + 1]
    if sec < 2 or sec > 18:
        return None
    attrs = struct.unpack_from("<H", blk, o + 4)[0]
    mtime = struct.unpack_from("<I", blk, o + 12)[0]
    c0 = o + 32
    flags = blk[c0 + 1]
    name_len = blk[c0 + 3]
    first_cluster = struct.unpack_from("<I", blk, c0 + 20)[0]
    data_len = struct.unpack_from("<Q", blk, c0 + 24)[0]
    if not (1 <= name_len <= 255):
        return None
    if first_cluster < 2 or first_cluster >= cluster_count + 2:
        return None
    need = (name_len + 14) // 15
    chars = []
    for j in range(2, 2 + need):
        e = o + 32 * j
        if e + 32 > len(blk) or blk[e] != 0xC1:
            return None
        chars.append(blk[e + 2:e + 32].decode("utf-16-le", "replace"))
    name = "".join(chars)[:name_len]
    if not name or any(ch in name for ch in '\/:*?"<>|'):
        return None
    return {"name": name, "len": data_len, "cluster": first_cluster,
            "contig": bool(flags & 0x02), "dir": bool(attrs & 0x10),
            "mtime": mtime}


def main():
    # take a bare drive letter and build the device path here: the shell mangles
    # a literal device path into a filesystem path on the way in
    arg = sys.argv[1].strip().rstrip(chr(92) + "/").rstrip(":").upper()
    dev_path = chr(92) * 2 + "." + chr(92) + arg + ":"
    limit_gb = None
    if "--limit-gb" in sys.argv:
        limit_gb = float(sys.argv[sys.argv.index("--limit-gb") + 1])

    d = Dev(dev_path)
    d.seek(0)
    boot = d.read(512)
    bps = 1 << boot[108]
    spc = 1 << boot[109]
    cluster_bytes = bps * spc
    heap_sector = struct.unpack_from("<I", boot, 88)[0]
    cluster_count = struct.unpack_from("<I", boot, 92)[0]
    total_sectors = struct.unpack_from("<Q", boot, 72)[0]
    heap = heap_sector * bps
    total_bytes = total_sectors * bps
    print("  device        : %s" % dev_path)
    print("  cluster size  : %d KB" % (cluster_bytes // 1024))
    print("  clusters      : %d" % cluster_count)
    print("  size          : %.1f GB" % (total_bytes / 1e9))
    print("  data area at  : %d" % heap)

    end = total_bytes
    if limit_gb:
        end = min(end, heap + int(limit_gb * 1e9))
    BLK = 16 * 1024 * 1024
    OVER = 4096

    found = {}
    pos = heap
    scanned = 0
    while pos < end:
        d.seek(pos)
        blk = d.read(BLK)
        if not blk:
            # a bad region: skip a cluster and carry on rather than aborting
            pos += cluster_bytes
            continue
        scanned += len(blk)
        arr = np.frombuffer(blk, dtype=np.uint8)
        n32 = len(blk) // 32
        if n32 >= 2:
            types = arr[:n32 * 32].reshape(n32, 32)[:, 0]
            cand = np.nonzero((types[:-1] == 0x85) & (types[1:] == 0xC0))[0]
            for ci in cand:
                rec = parse_set(blk, int(ci) * 32, cluster_count)
                if rec:
                    key = (rec["name"], rec["cluster"], rec["len"])
                    found.setdefault(key, rec)
        if len(blk) < BLK:
            pos += len(blk)
        else:
            pos += BLK - OVER
        if scanned % (2 * 1024 * 1024 * 1024) < BLK:
            print("    scanned %.0f GB, %d entries so far"
                  % (scanned / 1e9, len(found)))
    d.close()

    recs = list(found.values())
    files = [r for r in recs if not r["dir"]]
    dirs = [r for r in recs if r["dir"]]
    media = [r for r in files if os.path.splitext(r["name"])[1].lower() in MEDIA]
    print("")
    print("  directory entry sets recovered : %d" % len(recs))
    print("    directories                  : %d" % len(dirs))
    print("    files                        : %d" % len(files))
    print("    media files                  : %d" % len(media))
    if media:
        print("    media bytes                  : %.1f GB"
              % (sum(r["len"] for r in media) / 1e9))
        print("    contiguous (easy extract)    : %d of %d"
              % (sum(1 for r in media if r["contig"]), len(media)))
        ext = collections.Counter(os.path.splitext(r["name"])[1].lower() for r in media)
        print("    by type                      : %s" % dict(ext))
    if dirs:
        print("    directory names              : %s"
              % sorted(set(r["name"] for r in dirs))[:20])

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recovered_entries.json")
    json.dump({"cluster_bytes": cluster_bytes, "heap": heap,
               "cluster_count": cluster_count, "records": recs},
              open(out, "w", encoding="utf-8"), indent=1)
    print("")
    print("  -> %s" % out)


if __name__ == "__main__":
    main()
