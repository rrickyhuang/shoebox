"""Regression tests for the bugs an independent review found on 2026-10-11.

Each test reproduces the original failure, so if someone "simplifies" one of the
fixes later these go red. Runs entirely in a temp sandbox: nothing in the real
library is read or written.

    python test_sdcard.py
"""
import os
import sys
import shutil
import sqlite3
import tempfile
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sdcard


def sandbox():
    root = tempfile.mkdtemp(prefix="sdtest_")
    sdcard.BASE = root
    sdcard.LIB = os.path.join(root, "Library")
    sdcard.INBOX = os.path.join(root, "Inbox")
    sdcard.INDEX = os.path.join(root, "index.db")
    sdcard.TRIPS_JSON = os.path.join(root, "trips.json")
    os.makedirs(sdcard.LIB)
    os.makedirs(sdcard.INBOX)
    return root


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def allfiles(root):
    return [os.path.join(dp, f) for dp, dn, fn in os.walk(root) for f in fn]


results = []


def check(name, passed, detail=""):
    results.append((name, passed, detail))
    print("  [%s] %s%s" % ("PASS" if passed else "FAIL", name,
                           ("  -- " + detail) if detail else ""))


# ---- finding 2: file --execute must not file quarantined files -------------
root = sandbox()
write(os.path.join(sdcard.INBOX, "card1", "GOOD.CR2"), b"good-photo")
write(os.path.join(sdcard.INBOX, sdcard.QUARANTINE, "CORRUPT.CR2"), b"corrupt")
sdcard.cmd_file(["--execute"])
lib = [os.path.basename(p) for p in allfiles(sdcard.LIB)]
check("finding 2: quarantined file stays out of the Library",
      "CORRUPT.CR2" not in lib and "GOOD.CR2" in lib,
      "Library now holds %s" % lib)
check("finding 2: quarantine folder survives the empty-dir sweep",
      os.path.isdir(os.path.join(sdcard.INBOX, sdcard.QUARANTINE)))
shutil.rmtree(root, ignore_errors=True)

# ---- finding 3: verify must not bless an Inbox it never checked ------------
root = sandbox()
card = os.path.join(root, "fakecard")
write(os.path.join(card, "DCIM", "OTHER.CR2"), b"unrelated")
write(os.path.join(sdcard.INBOX, "card1", "STAGED.CR2"), b"staged-photo")
import io
import contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    sdcard.cmd_verify([card])
out = buf.getvalue()
check("finding 3: no false all-clear when nothing could be matched",
      "all staged files are byte-identical" not in out and "NOT CLEAR" in out)
check("finding 3: a good-but-unmatched file is not quarantined",
      not os.path.isdir(os.path.join(sdcard.INBOX, sdcard.QUARANTINE)))
shutil.rmtree(root, ignore_errors=True)

# ---- finding 4: index must keep staged Inbox rows --------------------------
root = sandbox()
libf = write(os.path.join(sdcard.LIB, "2026", "LIBPHOTO.CR2"), b"in-library")
inbf = write(os.path.join(sdcard.INBOX, "card1", "STAGED.CR2"), b"staged-photo")
c = sdcard.db()
for p in (libf, inbf):
    st = os.stat(p)
    c.execute("insert or replace into files values(?,?,?,?,?)",
              (p, st.st_size, st.st_mtime, sdcard.quickhash(p, st.st_size), None))
c.commit()
c.close()
with contextlib.redirect_stdout(io.StringIO()):
    sdcard.cmd_index([])
c = sdcard.db()
rows = [r[0] for r in c.execute("select path from files")]
c.close()
check("finding 4: staged Inbox row survives an index refresh",
      inbf in rows, "%d rows: %s" % (len(rows), [os.path.basename(r) for r in rows]))
shutil.rmtree(root, ignore_errors=True)

# ---- finding 5: three same-named card files must all survive --------------
root = sandbox()
card = os.path.join(root, "fakecard")
for sub, body in (("BACKUP", b"AAA"), ("DCIM", b"BBB"), ("OLD", b"CCC")):
    write(os.path.join(card, sub, "100CANON", "IMG_1.CR2"), body)
c = sdcard.db()
seed = write(os.path.join(sdcard.LIB, "seed.CR2"), b"seed")
st = os.stat(seed)
c.execute("insert or replace into files values(?,?,?,?,?)",
          (seed, st.st_size, st.st_mtime, sdcard.quickhash(seed, st.st_size), None))
c.commit()
c.close()
with contextlib.redirect_stdout(io.StringIO()):
    sdcard.cmd_ingest([card, "--execute"])
staged = [p for p in allfiles(sdcard.INBOX)]
bodies = sorted(open(p, "rb").read() for p in staged)
check("finding 5: all three same-named card files land separately",
      bodies == [b"AAA", b"BBB", b"CCC"],
      "%d staged, contents %s" % (len(staged), bodies))
shutil.rmtree(root, ignore_errors=True)

# ---- finding 8: a cached hash for a deleted library file must not count ---
root = sandbox()
card = os.path.join(root, "fakecard")
photo = write(os.path.join(card, "DCIM", "IMG_9.CR2"), b"the-only-copy")
libf = write(os.path.join(sdcard.LIB, "IMG_9.CR2"), b"the-only-copy")
c = sdcard.db()
st = os.stat(libf)
full = hashlib.md5(open(libf, "rb").read()).hexdigest()
c.execute("insert or replace into files values(?,?,?,?,?)",
          (libf, st.st_size, st.st_mtime, sdcard.quickhash(libf, st.st_size), full))
c.commit()
c.close()
os.remove(libf)                       # library file gone, index not refreshed
with contextlib.redirect_stdout(io.StringIO()):
    new, dupes = sdcard.triage(card)
check("finding 8: card photo is NOT skipped as a duplicate of a deleted file",
      len(new) == 1 and len(dupes) == 0,
      "new=%d dupes=%d" % (len(new), len(dupes)))
shutil.rmtree(root, ignore_errors=True)

# ---- sidecars must travel with the file they belong to -------------------
root = sandbox()
raw = write(os.path.join(sdcard.INBOX, "card1", "IMG_7.CR2"), b"raw-data")
write(raw + ".xmp", b"<x>darktable sidecar: date, gps, edits</x>")
with contextlib.redirect_stdout(io.StringIO()):
    sdcard.cmd_file(["--execute"])
landed = sorted(os.path.basename(p) for p in allfiles(sdcard.LIB))
check("sidecar travels with its raw when filed",
      landed == ["IMG_7.CR2", "IMG_7.CR2.xmp"], "Library holds %s" % landed)
check("no sidecar left stranded in the Inbox",
      allfiles(sdcard.INBOX) == [], "Inbox holds %s" % allfiles(sdcard.INBOX))
shutil.rmtree(root, ignore_errors=True)

print("")

# =====================================================================
# Regression tests for the second independent review (2026-10-18).
# Each FAILS against the code as it stood before that review; a green run
# here only means something because these were seen red first.
# Each block is guarded so one failure cannot abort the remaining tests.
# =====================================================================

def _guard(label, fn):
    try:
        fn()
    except Exception as e:
        check(label + " [raised]", False, "%s: %s" % (type(e).__name__, e))


def _t_sidecar_collision():
    root = sandbox()
    try:
        dest = os.path.join(sdcard.LIB, "2026", "2026_Diary")
        write(os.path.join(dest, "IMG_7.CR2"), b"OLD-LIBRARY-PHOTO")
        write(os.path.join(sdcard.INBOX, "c1", "IMG_7.CR2"), b"NEW-PHOTO")
        write(os.path.join(sdcard.INBOX, "c1", "IMG_7.CR2.xmp"), b"NEW-SIDECAR")
        with contextlib.redirect_stdout(io.StringIO()):
            sdcard.cmd_file(["--execute"])
        body = open(os.path.join(dest, "IMG_7.CR2"), "rb").read()
        check("rev2 f1: pre-existing photo untouched by an incoming same-name file",
              body == b"OLD-LIBRARY-PHOTO", "it now reads %r" % body[:24])
        check("rev2 f1: no foreign sidecar attached to the pre-existing photo",
              not os.path.exists(os.path.join(dest, "IMG_7.CR2.xmp")))
        got = {os.path.basename(p): open(p, "rb").read() for p in allfiles(sdcard.LIB)}
        raw = [n for n, b in got.items() if b == b"NEW-PHOTO"]
        check("rev2 f1: the new photo kept its own sidecar",
              bool(raw) and got.get(raw[0] + ".xmp") == b"NEW-SIDECAR",
              "library holds %s" % sorted(got))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _t_quarantine():
    root = sandbox()
    try:
        card = os.path.join(root, "card")
        write(os.path.join(card, "DCIM", "IMG_1.CR2"), b"CARD-ORIGINAL")
        write(os.path.join(sdcard.INBOX, "c1", "IMG_1.CR2"), b"staged-WRONG-1")
        write(os.path.join(sdcard.INBOX, "c1", "IMG_1.CR2.xmp"), b"SIDECAR-1")
        write(os.path.join(sdcard.INBOX, "c2", "IMG_1.CR2"), b"staged-WRONG-2")
        with contextlib.redirect_stdout(io.StringIO()):
            sdcard.cmd_verify([card])
        q = os.path.join(sdcard.INBOX, sdcard.QUARANTINE)
        got = sorted(os.listdir(q)) if os.path.isdir(q) else []
        bodies = sorted(open(os.path.join(q, f), "rb").read()
                        for f in got if not f.endswith(".xmp"))
        check("rev2 f2: both failing files survive quarantine",
              bodies == [b"staged-WRONG-1", b"staged-WRONG-2"],
              "quarantine holds %s" % got)
        check("rev2 f2: the sidecar was quarantined, not stranded in the Inbox",
              any(f.endswith(".xmp") for f in got), "quarantine holds %s" % got)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _t_trips_atomic():
    import json as J
    root = sandbox()
    try:
        J.dump({"trips": [{"start": "2025-01-01", "end": "2025-01-02",
                           "name": "keep-me"}]},
               open(sdcard.TRIPS_JSON, "w", encoding="utf-8"))
        before = open(sdcard.TRIPS_JSON, encoding="utf-8").read()
        os.makedirs(os.path.join(sdcard.LIB, "2025", "2025-01-01_renamed"))
        real = J.dump

        def boom(*a, **k):
            raise IOError("simulated failure mid-write")

        J.dump = boom
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                sdcard.cmd_sync_trips(["--execute"])
        except Exception:
            pass
        finally:
            J.dump = real
        check("rev2 f3: trips.json intact after a write that failed partway",
              open(sdcard.TRIPS_JSON, encoding="utf-8").read() == before,
              "file is now %d bytes" % os.path.getsize(sdcard.TRIPS_JSON))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _t_camera_rules():
    check("rev2 f4: a lab scanner is not a camera",
          sdcard.is_scanner("EZ Controller")
          and not sdcard.is_scanner("Canon EOS 6D Mark II"))
    check("rev2 f4: Ricoh counts as diary, Canon does not",
          sdcard.is_diary_camera("RICOH GR III")
          and not sdcard.is_diary_camera("Canon EOS 6D Mark II"))
    check("rev2 f4: an unknown model defaults to diary, not to a new folder",
          sdcard.is_diary_camera(None) and sdcard.is_diary_camera(""))
    n = sdcard.NON_DIARY_CAMERA_DAY
    canon = [("2026-04-02", "Canon EOS 6D Mark II", "exif")] * n
    check("rev2 f6: %d EXIF-dated Canon frames make a shoot day" % n,
          sdcard.shoot_days_for(canon, []) == {"2026-04-02"})
    check("rev2 f6: one frame short does not",
          sdcard.shoot_days_for(canon[:-1], []) == set())
    check("rev2 f6: the same frames dated only by mtime do not",
          sdcard.shoot_days_for(
              [("2026-04-02", "Canon EOS 6D Mark II", "mtime")] * n, []) == set())
    check("rev2 f6: film scans do not make a shoot day",
          sdcard.shoot_days_for(
              [("2026-04-02", "EZ Controller", "exif")] * n, []) == set())
    check("rev2 f6: a day already inside a trip window is left alone",
          sdcard.shoot_days_for(canon, [{"start": "2026-04-01",
                                         "end": "2026-04-03", "name": "x"}]) == set())


def _t_walk_errors():
    root = sandbox()
    try:
        libf = write(os.path.join(sdcard.LIB, "2026", "A.CR2"), b"library-photo")
        c = sdcard.db()
        st = os.stat(libf)
        c.execute("insert or replace into files values(?,?,?,?,?)",
                  (libf, st.st_size, st.st_mtime,
                   sdcard.quickhash(libf, st.st_size), None))
        c.commit()
        c.close()
        real = os.walk

        def broken(top, **kw):
            onerror = kw.get("onerror")
            if onerror:
                onerror(OSError("simulated I/O error"))
            return iter([])

        os.walk = broken
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                sdcard.cmd_index([])
        except Exception:
            pass
        finally:
            os.walk = real
        c = sdcard.db()
        rows = [r[0] for r in c.execute("select path from files")]
        c.close()
        check("rev2 f10: index does not purge rows after an unreadable walk",
              libf in rows, "%d row(s) survived" % len(rows))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _t_stale_hash():
    import hashlib, time
    root = sandbox()
    try:
        card = os.path.join(root, "card")
        write(os.path.join(card, "DCIM", "IMG_9.CR2"), b"the-only-copy")
        libf = write(os.path.join(sdcard.LIB, "IMG_9.CR2"), b"the-only-copy")
        st = os.stat(libf)
        full = hashlib.md5(open(libf, "rb").read()).hexdigest()
        c = sdcard.db()
        c.execute("insert or replace into files values(?,?,?,?,?)",
                  (libf, st.st_size, st.st_mtime,
                   sdcard.quickhash(libf, st.st_size), full))
        c.commit()
        c.close()
        time.sleep(1.1)
        write(libf, b"COMPLETELY-DIFFERENT-CONTENT")
        with contextlib.redirect_stdout(io.StringIO()):
            new, dupes = sdcard.triage(card)
        check("rev2 f11: a replaced library file no longer vouches for old content",
              len(new) == 1 and len(dupes) == 0,
              "new=%d dupes=%d" % (len(new), len(dupes)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


for _lbl, _fn in (("sidecar collision", _t_sidecar_collision),
                  ("quarantine", _t_quarantine),
                  ("atomic trips.json", _t_trips_atomic),
                  ("camera rules", _t_camera_rules),
                  ("walk errors", _t_walk_errors),
                  ("stale hash", _t_stale_hash)):
    _guard(_lbl, _fn)


def _t_split_shoot():
    """A shoot spanning two cards, filed in two separate sittings, must end up
    in ONE folder. Before the fix the first card created the folder and the
    second went to Diary, splitting one day across two places."""
    root = sandbox()
    try:
        import json as J
        J.dump({"trips": []}, open(sdcard.TRIPS_JSON, "w", encoding="utf-8"))
        n = sdcard.NON_DIARY_CAMERA_DAY

        def stage(count, tag):
            for i in range(count):
                write(os.path.join(sdcard.INBOX, tag, "IMG_%s_%d.CR2" % (tag, i)),
                      b"x" * 40)

        # make every staged file look like an EXIF-dated Canon frame
        real = sdcard.capture_info
        sdcard.capture_info = lambda p, cap=0: ("2026-04-02", "exif",
                                                "Canon EOS 6D Mark II")
        try:
            stage(n + 2, "card1")          # over the threshold: fires the rule
            with contextlib.redirect_stdout(io.StringIO()):
                sdcard.cmd_file(["--execute"])
            stage(8, "card2")              # well under it, same day, later sitting
            with contextlib.redirect_stdout(io.StringIO()):
                sdcard.cmd_file(["--execute"])
        finally:
            sdcard.capture_info = real

        folders = {}
        for p in allfiles(sdcard.LIB):
            folders.setdefault(os.path.basename(os.path.dirname(p)), 0)
            folders[os.path.basename(os.path.dirname(p))] += 1
        check("rev2 f5: a shoot split across two sittings lands in ONE folder",
              len(folders) == 1 and sum(folders.values()) == n + 10,
              "ended up as %s" % folders)
        check("rev2 f5: and nothing leaked into Diary",
              not any("Diary" in k for k in folders), "folders: %s" % sorted(folders))
        doc = J.load(open(sdcard.TRIPS_JSON, encoding="utf-8"))
        check("rev2 f5: the decision was recorded in trips.json",
              any(t["start"] == "2026-04-02" for t in doc["trips"]),
              "trips.json holds %s" % doc["trips"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


_guard("split shoot", _t_split_shoot)

def _t_existing_folder_no_trip_entry():
    """A dated folder can exist with no trips.json entry -- folders made by
    hand, or before rule 3 recorded its decisions. Frames for that date must
    join it even when this card holds far too few to fire the rule."""
    root = sandbox()
    try:
        import json as J
        J.dump({"trips": []}, open(sdcard.TRIPS_JSON, "w", encoding="utf-8"))
        existing = os.path.join(sdcard.LIB, "2026", "2026-04-02_some-hike")
        os.makedirs(existing)
        write(os.path.join(existing, "IMG_old.CR2"), b"already-filed")
        for i in range(5):                     # 5 frames: far below the threshold
            write(os.path.join(sdcard.INBOX, "c3", "IMG_new_%d.CR2" % i), b"y" * 40)
        real = sdcard.capture_info
        sdcard.capture_info = lambda p, cap=0: ("2026-04-02", "exif",
                                                "Canon EOS 6D Mark II")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                sdcard.cmd_file(["--execute"])
        finally:
            sdcard.capture_info = real
        landed = sorted(os.path.basename(os.path.dirname(p)) for p in allfiles(sdcard.LIB))
        check("rev2 f5b: frames join an existing dated folder with no trips entry",
              set(landed) == {"2026-04-02_some-hike"},
              "they landed in %s" % sorted(set(landed)))
        check("rev2 f5b: the existing folder kept its name, no bare-date twin",
              not os.path.isdir(os.path.join(sdcard.LIB, "2026", "2026-04-02")),
              "bare-date twin present: %s"
              % os.path.isdir(os.path.join(sdcard.LIB, "2026", "2026-04-02")))
    finally:
        shutil.rmtree(root, ignore_errors=True)


_guard("existing folder, no trip entry", _t_existing_folder_no_trip_entry)

# ---------------------------------------------------------------------
# Recovery-path helpers (exfat_extract). These run against a FAILING card,
# one shot, so a silent wrong answer is unrecoverable.
# ---------------------------------------------------------------------
import struct as _struct
import exfat_extract as X


def _fat(pairs, size=256):
    """Build a FAT: {cluster: next_cluster}."""
    buf = bytearray(size * 4)
    for a, b in pairs.items():
        _struct.pack_into("<I", buf, a * 4, b)
    return bytes(buf)


def _t_fat_chain():
    CC = 100                                   # clusters 2..101 are valid
    f = _fat({10: 11, 11: 12, 12: 0xFFFFFFFF})
    check("rev2 f12: a healthy chain is followed",
          X.fat_chain(f, 10, 4, CC) == [10, 11, 12], "%s" % X.fat_chain(f, 10, 4, CC))
    check("rev2 f12: the chain stops at `need`, never overruns",
          X.fat_chain(f, 10, 2, CC) == [10, 11])

    wild = _fat({10: 11, 11: 999999})          # points far outside the volume
    check("rev2 f12: a wild cluster number stops the chain",
          X.fat_chain(wild, 10, 8, CC) == [10, 11],
          "got %s" % X.fat_chain(wild, 10, 8, CC))

    inside = _fat({10: 11, 11: 95})            # inside the volume but wrong
    check("rev2 f12: an in-range pointer is still followed (cannot detect that)",
          X.fat_chain(inside, 10, 8, CC) == [10, 11, 95])

    loop = _fat({10: 11, 11: 12, 12: 10})      # cycles back
    got = X.fat_chain(loop, 10, 50, CC)
    check("rev2 f12: a FAT cycle terminates instead of spinning",
          got == [10, 11, 12] and len(got) == 3, "got %s" % got)

    short = _fat({10: 11}, size=11)            # FAT truncated mid-walk
    check("rev2 f12: a truncated FAT stops rather than reading past the end",
          X.fat_chain(short, 10, 8, CC) == [10, 11],
          "got %s" % X.fat_chain(short, 10, 8, CC))

    bad = _fat({10: 0xFFFFFFF7})               # bad-cluster marker
    check("rev2 f12: the bad-cluster marker ends the chain",
          X.fat_chain(bad, 10, 8, CC) == [10])


def _t_unique_target():
    root = sandbox()
    try:
        dest = os.path.join(root, "out")
        os.makedirs(dest)
        claimed = set()

        t1, done = X.unique_target(dest, "IMG_1.CR2", 500, claimed, 100)
        check("rev2 f8: first entry gets the plain name",
              os.path.basename(t1) == "IMG_1.CR2" and not done)
        claimed.add(t1)

        t2, done = X.unique_target(dest, "IMG_1.CR2", 900, claimed, 100)
        check("rev2 f8: a second entry with the same name gets its own path",
              t2 != t1 and not done, "second went to %s" % os.path.basename(t2))
        claimed.add(t2)

        t3, done = X.unique_target(dest, "IMG_1.CR2", 1300, claimed, 100)
        check("rev2 f8: a third one too -- none of them collide",
              len({t1, t2, t3}) == 3, "paths: %s" % [os.path.basename(x) for x in (t1, t2, t3)])

        # a previous run already wrote this exact entry, complete
        write(os.path.join(dest, "IMG_9.CR2"), b"z" * 64)
        t4, done = X.unique_target(dest, "IMG_9.CR2", 77, set(), 64)
        check("rev2 f8: a complete file from a previous run is recognised, not redone",
              done and os.path.basename(t4) == "IMG_9.CR2")

        # same name on disk but the WRONG length: must not be reused or clobbered
        t5, done = X.unique_target(dest, "IMG_9.CR2", 77, set(), 999)
        check("rev2 f8: a same-name file of different length is not overwritten",
              not done and os.path.basename(t5) != "IMG_9.CR2",
              "would have written to %s" % os.path.basename(t5))
    finally:
        shutil.rmtree(root, ignore_errors=True)


_guard("fat chain", _t_fat_chain)
_guard("unique target", _t_unique_target)

# ---------------------------------------------------------------------
# capture_info's EXIF parser. Every other fixture in this file is a few
# ASCII bytes, so it falls through to the mtime branch and the parser never
# runs -- yet it decides the folder every photo lands in. These build real
# TIFF/JPEG structures so the parser is actually exercised.
# ---------------------------------------------------------------------

def _tiff(model, datestr, big_endian=False):
    """A minimal but valid TIFF: IFD0 with Model + a pointer to an ExifIFD
    holding DateTimeOriginal. Offsets are computed, not hand-counted."""
    import struct as st
    en = ">" if big_endian else "<"
    magic = b"MM" if big_endian else b"II"
    model_b = model.encode("ascii") + b"\x00"
    date_b = datestr.encode("ascii") + b"\x00"

    ifd0_off = 8
    ifd0_len = 2 + 2 * 12 + 4              # count + 2 entries + next-IFD
    model_off = ifd0_off + ifd0_len
    exif_off = model_off + len(model_b)
    exif_len = 2 + 1 * 12 + 4
    date_off = exif_off + exif_len

    out = bytearray()
    out += magic + st.pack(en + "H", 42) + st.pack(en + "I", ifd0_off)
    out += st.pack(en + "H", 2)
    out += st.pack(en + "HHI", 0x0110, 2, len(model_b)) + st.pack(en + "I", model_off)
    out += st.pack(en + "HHI", 0x8769, 4, 1) + st.pack(en + "I", exif_off)
    out += st.pack(en + "I", 0)
    out += model_b
    out += st.pack(en + "H", 1)
    out += st.pack(en + "HHI", 0x9003, 2, len(date_b)) + st.pack(en + "I", date_off)
    out += st.pack(en + "I", 0)
    out += date_b
    return bytes(out)


def _jpeg_with_exif(tiff):
    """Wrap a TIFF in a JPEG APP1 segment, the way a camera JPEG carries it."""
    import struct as st
    payload = b"Exif\x00\x00" + tiff
    return (bytes([0xFF, 0xD8])
            + bytes([0xFF, 0xE1]) + st.pack(">H", len(payload) + 2) + payload
            + bytes([0xFF, 0xD9]))


def _t_capture_info():
    root = sandbox()
    try:
        p1 = write(os.path.join(root, "a.dng"),
                   _tiff("RICOH GR III", "2026:04:02 10:11:12"))
        d, how, model = sdcard.capture_info(p1)
        check("rev2 exif: date parsed from a TIFF raw",
              (d, how) == ("2026-04-02", "exif"), "got %s (%s)" % (d, how))
        check("rev2 exif: camera model parsed from the same single read",
              model == "RICOH GR III", "got %r" % model)
        check("rev2 exif: that model routes to diary",
              sdcard.is_diary_camera(model))

        p2 = write(os.path.join(root, "b.jpg"),
                   _jpeg_with_exif(_tiff("Canon EOS 6D Mark II",
                                         "2024:10:05 07:13:00")))
        d, how, model = sdcard.capture_info(p2)
        check("rev2 exif: the JPEG APP1 path works too",
              (d, how, model) == ("2024-10-05", "exif", "Canon EOS 6D Mark II"),
              "got %s / %s / %r" % (d, how, model))
        check("rev2 exif: a Canon is not a diary camera",
              not sdcard.is_diary_camera(model))

        p3 = write(os.path.join(root, "c.dng"),
                   _tiff("NIKON D850", "2025:01:02 03:04:05", big_endian=True))
        d, how, model = sdcard.capture_info(p3)
        check("rev2 exif: big-endian (MM) TIFF is handled",
              (d, how, model) == ("2025-01-02", "exif", "NIKON D850"),
              "got %s / %s / %r" % (d, how, model))

        p4 = write(os.path.join(root, "d.dng"),
                   _tiff("Canon EOS 6D Mark II", "0000:00:00 00:00:00"))
        d, how, model = sdcard.capture_info(p4)
        check("rev2 exif: an unset camera clock is rejected, falls back to mtime",
              how == "mtime" and d and not d.startswith("0000"),
              "got %s (%s)" % (d, how))
        check("rev2 exif: the model is still recovered from a clock-less file",
              model == "Canon EOS 6D Mark II", "got %r" % model)

        p5 = write(os.path.join(root, "e.jpg"), b"not an image at all")
        d, how, model = sdcard.capture_info(p5)
        check("rev2 exif: a file with no EXIF falls back to mtime with no model",
              how == "mtime" and model is None, "got %s / %r" % (how, model))

        check("rev2 exif: capture_date still agrees with capture_info",
              sdcard.capture_date(p1) == ("2026-04-02", "exif"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


_guard("capture_info EXIF parser", _t_capture_info)
bad = [n for n, ok, _ in results if not ok]
print("  %d/%d passed" % (len(results) - len(bad), len(results)))
if bad:
    print("  FAILING: " + ", ".join(bad))
    sys.exit(1)

