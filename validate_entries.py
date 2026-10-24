"""Does each recovered directory entry actually point at the file it names?

A whole-device sweep finds directory entries wherever they survive, including in
stale directory clusters left over from earlier uses of the card. Such an entry
names a real former file but its FirstCluster now points at data that has since
been reused. The cheap, decisive test: read the first bytes at FirstCluster and
check they are the magic number the extension implies.
"""
import json, os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exfat_extract as X

if len(sys.argv) < 2:
    print("usage: python validate_entries.py <drive letter>")
    print("  e.g. python validate_entries.py H")
    print("  Refusing to guess: this opens a raw device for reading.")
    sys.exit(2)

here = os.path.dirname(os.path.abspath(__file__))
IN = os.path.join(here, "recovered_entries.json")
if not os.path.exists(IN):
    print("no recovered_entries.json beside this script.")
    print("  Run exfat_scan.py first: it produces the file this reads.")
    sys.exit(2)
meta = json.load(open(IN, encoding="utf-8"))
cb, heap = meta["cluster_bytes"], meta["heap"]
recs = [r for r in meta["records"] if not r["dir"] and r["len"] > 0]

letter = sys.argv[1].strip().rstrip(chr(92) + "/").rstrip(":").upper()
d = X.Dev(letter)
print("  reading device %s:" % letter)
tally = collections.Counter()
good = []
for i, r in enumerate(recs, 1):
    ext = os.path.splitext(r["name"])[1].lower()
    d.seek(heap + (r["cluster"] - 2) * cb)
    b = d.read(512)
    if len(b) < 16:
        tally["unreadable"] += 1
        continue
    okhdr = False
    if ext in (".cr2", ".dng", ".nef", ".arw", ".tif", ".tiff"):
        okhdr = b[:4] in (b"II*\x00", b"MM\x00*")
        if okhdr and ext == ".cr2":
            okhdr = b[8:10] == b"CR"
    elif ext in (".jpg", ".jpeg"):
        okhdr = b[:3] == b"\xff\xd8\xff"
    elif ext in (".mp4", ".mov", ".avi"):
        okhdr = b[4:8] in (b"ftyp", b"moov", b"mdat") or b[:4] == b"RIFF"
    else:
        okhdr = None
    # Keep anything not positively REFUTED. The job is to drop stale entries,
    # not to drop everything unconfirmable: .cr3/.orf/.rw2/.heic/.png have no
    # branch above, and on a dying card those are files you still want to try.
    if okhdr is False:
        tally["header WRONG (stale entry)"] += 1
    else:
        tally["header matches extension" if okhdr else "not checkable, kept anyway"] += 1
        good.append(r)
    if i % 1000 == 0:
        print("    checked %d / %d" % (i, len(recs)))
d.close()

print("")
for k, v in tally.most_common():
    print("  %-30s %5d  (%.1f%%)" % (k, v, 100.0 * v / max(1, len(recs))))
print("")
print("  entries whose data really is there : %d  (%.1f GB)"
      % (len(good), sum(r["len"] for r in good) / 1e9))
json.dump({"cluster_bytes": cb, "heap": heap,
           "cluster_count": meta.get("cluster_count"), "records": good},
          open(os.path.join(here, "validated_entries.json"), "w", encoding="utf-8"), indent=1)
print("  -> validated_entries.json")
