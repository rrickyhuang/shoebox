"""SD card triage for the photo library.  Read-only on every card.

    python sdcard.py index                  build/refresh the library content index
    python sdcard.py scan F:                report what is on a card (changes nothing)
    python sdcard.py ingest F: --execute    copy only the NEW files into Inbox
    python sdcard.py verify F:              re-check staged files against the card
    python sdcard.py file --execute         move Inbox -> Library by date / trip
    python sdcard.py sync-trips             make trips.json match folder names on disk

Why an index: a card is deduplicated against the *content* of everything already
in the library, not against filenames.  Ricoh and Canon both wrap their frame
counters (R0000002.DNG exists on several cards as different photos), and the same
photo can exist under different names after a rename-on-import, so names prove
nothing.  Identity here is size + a 128 KB quick hash, always confirmed with a
full MD5 before anything is called a duplicate.

Each ingest adds the newly copied files to the index immediately, so card #2 is
deduplicated against card #1 as well as against the library.  That matters: the
one dedup gap found in this library came from comparing cards only against
Lightroom and never against each other.

Nothing is ever written to the card, ever. Commands that write to the LIBRARY
are: ingest --execute, file --execute, sync-trips --execute, index (maintains
its database) and verify (moves failures into Inbox/_suspect -- note that one
has no dry run).
"""
import os, re, sys, json, struct, sqlite3, hashlib, shutil, datetime, collections

# Where the library lives. Override with the PHOTO_LIBRARY environment
# variable; everything else is derived from it.
BASE = os.environ.get("PHOTO_LIBRARY", r"E:\Photos")
LIB = os.path.join(BASE, "Library")
INBOX = os.path.join(BASE, "Inbox")
INDEX = os.path.join(BASE, ".photo-index.db")
QUARANTINE = "_suspect"      # Inbox subfolder for files that failed verify
TRIPS_JSON = os.path.join(BASE, "trips.json")

RAW = {".cr2", ".cr3", ".dng", ".nef", ".arw", ".raf", ".orf", ".rw2", ".pef", ".srw"}
MEDIA = RAW | {".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".png",
               ".mov", ".mp4", ".avi", ".m4v"}
SKIP_DIRS = {"archive", "inbox", "$recycle.bin", "system volume information",
             ".bzvol", "tools",
             # derived JPEGs, moved out of Library on 2026-10-17 so that Library
             # holds originals only. They must stay out of the index too, or
             # they count as library content and inflate every total.
             "exports",
             # pre-reorganisation name of what is now Archive\: 133k preview
             # JPEGs salvaged from a dead Lightroom account, not originals
             "_rescued from old lightroom catalog"}
QUICK = 64 * 1024
# Lab film scans: this lab names every frame RRRRRRRRFFFF.jpg -- 8-digit order
# number then a 4-digit frame number -- and the Noritsu scanner leaves
# DateTimeOriginal BLANK, writing only the scan date. Dating them by file mtime
# would file a whole roll under the day it was downloaded, which is meaningless,
# so they are grouped by roll instead and dated later by hand in darktable.
FILM_RE = re.compile(r"^(\d{8})(\d{4})$")
FILM_DIR = "Film"
# A day with at least this many frames from a camera that is NOT the
# everyday-carry Ricoh gets its own dated folder rather than going to Diary.
# Diary means the Ricoh; the Canon comes out for a reason. Measured on the real
# library the split is unambiguous -- stray Canon days ran 1-13 frames, actual
# shoots 37-353 -- so anything in between is unlikely to be a hard call.
NON_DIARY_CAMERA_DAY = 20
TRIP_DENSITY = 75
MERGE_GAP = 3      # quiet days this close stay inside a trip


def quickhash(path, size):
    """size + first 64 KB + last 64 KB.  Cheap, and unique enough to shortlist."""
    h = hashlib.md5()
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(QUICK))
        if size > 2 * QUICK:
            f.seek(-QUICK, os.SEEK_END)
            h.update(f.read(QUICK))
    return h.hexdigest()


def fullhash(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1048576), b""):
            h.update(b)
    return h.hexdigest()


def _tiff_date(d, base=0):
    if len(d) < base + 16 or d[base:base + 2] not in (b"II", b"MM"):
        return None
    en = "<" if d[base:base + 2] == b"II" else ">"
    try:
        off = base + struct.unpack_from(en + "I", d, base + 4)[0]
        found, ptr = {}, None
        for _ in range(2):
            if off <= base or off + 2 > len(d):
                break
            n = struct.unpack_from(en + "H", d, off)[0]
            for i in range(n):
                e = off + 2 + i * 12
                if e + 12 > len(d):
                    break
                tag, typ, cnt = struct.unpack_from(en + "HHI", d, e)
                vo = e + 8
                if tag in (0x9003, 0x0132) and typ == 2 and cnt >= 19:
                    p = base + struct.unpack_from(en + "I", d, vo)[0] if cnt > 4 else vo
                    if p + 19 <= len(d):
                        found[tag] = d[p:p + 19].decode("ascii", "ignore")
                elif tag == 0x8769:
                    ptr = base + struct.unpack_from(en + "I", d, vo)[0]
            if 0x9003 in found:
                break
            off = ptr or 0
        s = found.get(0x9003) or found.get(0x0132)
        m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})", s) if s else None
        if not m:
            return None
        # A camera with an unset clock writes 0000:00:00, which this regex
        # happily matches. It would file into Library/0000/ and then crash the
        # trip clustering on datetime.date(0, ...). Reject it so the caller
        # falls back to mtime, or reports the file as undated.
        try:
            datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        return "-".join(m.groups())
    except Exception:
        return None


def _tiff_model(d, base=0):
    """EXIF tag 0x0110 (Model) out of an already-read buffer."""
    if len(d) < base + 16 or d[base:base + 2] not in (b"II", b"MM"):
        return None
    en = "<" if d[base:base + 2] == b"II" else ">"
    try:
        off = base + struct.unpack_from(en + "I", d, base + 4)[0]
        if off <= base or off + 2 > len(d):
            return None
        n = struct.unpack_from(en + "H", d, off)[0]
        for i in range(n):
            e = off + 2 + i * 12
            if e + 12 > len(d):
                break
            tag, typ, cnt = struct.unpack_from(en + "HHI", d, e)
            if tag == 0x0110 and typ == 2:
                p = base + struct.unpack_from(en + "I", d, e + 8)[0] if cnt > 4 else e + 8
                if p + cnt <= len(d):
                    return d[p:p + cnt].split(b"\x00")[0].decode("ascii", "ignore").strip()
    except Exception:
        pass
    return None


def capture_info(path, cap=3145728):
    """(date, how, camera model) from ONE read of the file.

    Reading the head twice to get the date and then the model would double the
    I/O over tens of thousands of files, so both come out of the same buffer.
    """
    try:
        with open(path, "rb") as f:
            d = f.read(cap)
    except Exception:
        d = b""
    base = 0 if d[:2] in (b"II", b"MM") else None
    if base is None:
        i = d.find(b"Exif\x00\x00")
        base = i + 6 if i >= 0 else None
    got = _tiff_date(d, base) if base is not None else None
    model = _tiff_model(d, base) if base is not None else None
    if got:
        return got, "exif", model
    try:
        return (datetime.datetime.fromtimestamp(
                os.path.getmtime(path)).strftime("%Y-%m-%d"), "mtime", model)
    except Exception:
        return None, "unknown", model


def capture_date(path, cap=3145728):
    """DateTimeOriginal if we can find it, else file mtime."""
    d, how, _ = capture_info(path, cap)
    return d, how


SCANNER_HINTS = ("EZ CONTROLLER", "NORITSU", "FRONTIER", "FLEXTIGHT", "EPSON",
                 "COOLSCAN", "SCANNER", "PLUSTEK", "SILVERFAST")


def is_scanner(model):
    """A film scanner is not a camera, so it must not vote on shoot days.

    The lab's Noritsu writes Model = "EZ Controller". Film scans are normally
    caught by filename, but the collision renamer can append a suffix and break
    that match -- 72 such files already exist in this library. Without this
    check they counted as non-Ricoh frames and a whole roll of film, dated by
    its DOWNLOAD day, would fabricate a dated folder for a day nothing happened.
    """
    if not model:
        return False
    u = model.upper()
    return any(h in u for h in SCANNER_HINTS)


def is_diary_camera(model):
    """True for the everyday-carry Ricoh, and for anything unidentifiable.

    Unknown models count as diary deliberately: the fallback should be to leave
    a file where it would otherwise have gone, not to invent a folder for it.
    """
    if not model:
        return True
    u = model.upper()
    return "RICOH" in u or u.startswith("GR")


def load_trips():
    if not os.path.exists(TRIPS_JSON):
        return []
    return json.load(open(TRIPS_JSON, encoding="utf-8")).get("trips", [])


def sidecars_for(path):
    """Sidecars that must travel with a media file.

    darktable writes "file.ext.xmp"; Lightroom/ACR wrote "file.xmp". These hold
    the capture date, GPS, rating and the whole edit history, so moving a file
    without its sidecar silently discards all of that.
    """
    out = []
    for cand in (path + ".xmp", os.path.splitext(path)[0] + ".xmp"):
        if cand != path and os.path.exists(cand) and cand not in out:
            out.append(cand)
    return out


def film_roll(path):
    """Return the lab roll id if this looks like a lab film scan, else None."""
    stem, ext = os.path.splitext(os.path.basename(path))
    if ext.lower() not in (".jpg", ".jpeg"):
        return None
    m = FILM_RE.match(stem)
    return m.group(1) if m else None


def existing_day_folder(date):
    """The library folder for this date if one already exists, else None.

    Matches both a bare date and a renamed one (2026-04-02_whatever).
    """
    yp = os.path.join(LIB, date[:4])
    if not os.path.isdir(yp):
        return None
    for f in sorted(os.listdir(yp)):
        if f == date or f.startswith(date + "_"):
            full = os.path.join(yp, f)
            if os.path.isdir(full):
                return full
    return None


def record_shoot_day(date):
    """Persist a rule-3 decision into trips.json.

    Without this the decision is forgotten the moment the run ends: a shoot
    split across two cards and filed in two sittings would put the first card
    in its own folder and the second in Diary, splitting one day across two
    places. Writing it down means destination() routes everything later for
    that date to the same folder.
    """
    try:
        doc = json.load(open(TRIPS_JSON, encoding="utf-8"))
    except Exception:
        doc = {"trips": []}
    trips = doc.setdefault("trips", [])
    if any(t["start"] == date for t in trips):
        return False
    trips.append({"start": date, "end": date, "name": ""})
    trips.sort(key=lambda t: t["start"])
    tmp = TRIPS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, TRIPS_JSON)
    return True


def destination(date, trips):
    """Library sub-path for a capture date."""
    for t in trips:
        if t["start"] <= date <= t["end"]:
            y = t["start"][:4]
            folder = t["start"] + ("_" + t["name"] if t.get("name") else "")
            return os.path.join(LIB, y, folder)
    y = date[:4]
    return os.path.join(LIB, y, y + "_Diary")


def db():
    c = sqlite3.connect(INDEX)
    c.execute("create table if not exists files(path text primary key, "
              "size integer, mtime real, quick text, full text)")
    c.execute("create index if not exists i_sq on files(size, quick)")
    c.commit()
    return c


class WalkFailed(Exception):
    """A directory could not be enumerated; the caller must not treat the
    result as a complete listing."""


def _raise(err):
    raise WalkFailed(err)


def walk_media(root, skip=()):
    # os.walk defaults to swallowing every OSError and silently omitting the
    # unreadable subtree. In cmd_index that made an I/O error indistinguishable
    # from a deletion, so the rows were purged and the next card re-ingested
    # every one of those photos as "new". Fail loudly instead.
    for dp, dn, fn in os.walk(root, onerror=_raise):
        dn[:] = [x for x in dn if x.lower() not in skip]
        for f in fn:
            # a name beginning "._" is a macOS AppleDouble stub, not an image:
            # 4 KB of resource-fork metadata a Mac leaves on FAT/exFAT cards.
            # Four rode in on an earlier dump; darktable would have shown them
            # as four broken raws.
            if f.startswith("._"):
                continue
            if os.path.splitext(f)[1].lower() in MEDIA:
                yield os.path.join(dp, f)


def cmd_index(args):
    c = db()
    if "--rebuild" in args:
        c.execute("delete from files")
        c.commit()
        print("  index cleared")
    known = {p: (s, m) for p, s, m in c.execute("select path, size, mtime from files")}
    seen, added, refreshed = set(), 0, 0
    walk_ok = True
    try:
        listing = list(walk_media(BASE, SKIP_DIRS))
    except WalkFailed as e:
        walk_ok = False
        listing = []
        print("  !! could not read part of the library: %s" % e)
        print("     refusing to purge index rows from an incomplete scan.")
    for p in listing:
        try:
            st = os.stat(p)
        except OSError:
            continue
        seen.add(p)
        prev = known.get(p)
        if prev and prev[0] == st.st_size and abs(prev[1] - st.st_mtime) < 2:
            continue
        try:
            q = quickhash(p, st.st_size)
        except OSError:
            continue
        c.execute("insert or replace into files values(?,?,?,?,?)",
                  (p, st.st_size, st.st_mtime, q, None))
        if prev:
            refreshed += 1
        else:
            added += 1
        if (added + refreshed) % 2000 == 0:
            c.commit()
            print("    %d indexed..." % (added + refreshed))
    # Inbox is in SKIP_DIRS so staged files never appear in `seen`. Deleting
    # rows for them would discard exactly the card-to-card dedup that ingest
    # inserts them for, so only forget paths OUTSIDE the Inbox.
    inbox_prefix = INBOX.rstrip(os.sep) + os.sep
    stale = ([] if not walk_ok else
             [p for p in known
              if p not in seen and not p.startswith(inbox_prefix)])
    for p in stale:
        c.execute("delete from files where path=?", (p,))
    c.commit()
    n = c.execute("select count(*) from files").fetchone()[0]
    print("  added %d   refreshed %d   removed %d (gone from disk)"
          % (added, refreshed, len(stale)))
    print("  index now holds %d library files -> %s" % (n, INDEX))
    c.close()


def triage(drive, skip_dirs=()):
    """Return (new, dupes) for a card.  Reads the card, writes nothing to it.

    skip_dirs names folders on the card to ignore entirely -- for foreign files
    a card may be carrying. One card arrived with a Test folder of instructional
    videos that had no EXIF at all, so they would have been dated by mtime and
    filed as if they were photographs.
    """
    root = drive if drive.endswith((os.sep, "/")) else drive + os.sep
    if not os.path.isdir(root):
        sys.exit("no such drive: %s" % root)
    c = db()
    if not c.execute("select count(*) from files").fetchone()[0]:
        sys.exit("index is empty -- run: python sdcard.py index")

    ignore = {"$recycle.bin", "system volume information"} | {d.lower() for d in skip_dirs}
    try:
        files = list(walk_media(root, ignore))
    except WalkFailed as e:
        # A card that is starting to fail is exactly when these tools get used,
        # so turn an unreadable directory into a clear stop rather than a
        # traceback -- and never let a partial listing look like a full one.
        c.close()
        sys.exit("could not read all of %s: %s\n"
                 "  Refusing to continue: a partial listing would report files "
                 "as absent\n  that are simply unreadable. Re-seat the card and "
                 "try again." % (root, e))
    print("  %d media files on %s" % (len(files), root))
    if skip_dirs:
        print("  (ignoring folders: %s)" % ", ".join(sorted(skip_dirs)))
    new, dupes, cache = [], [], {}
    for i, p in enumerate(files, 1):
        try:
            st = os.stat(p)
            q = quickhash(p, st.st_size)
        except OSError:
            continue
        cands = [r[0] for r in c.execute(
            "select path from files where size=? and quick=?", (st.st_size, q))]
        if not cands:
            new.append((p, st.st_size))
        else:
            mine = fullhash(p)
            hit = None
            for lp in cands:
                if lp not in cache:
                    row = c.execute(
                        "select full, size, mtime from files where path=?",
                        (lp,)).fetchone()
                    fh = row[0] if row and row[0] else None
                    # A cached hash is only evidence if the file is still
                    # there AND unchanged. Checking existence alone let a file
                    # that had been replaced in place -- restored from backup,
                    # overwritten -- still vouch for content it no longer holds,
                    # so a card photo would be skipped as a duplicate of
                    # content that is no longer there.
                    # NOTE: stat into lib_st, never `st` -- that name holds the
                    # CARD file's stat and the enclosing loop still needs it.
                    if fh is not None:
                        try:
                            lib_st = os.stat(lp)
                        except OSError:
                            c.execute("delete from files where path=?", (lp,))
                            continue
                        if (row[1] != lib_st.st_size
                                or abs(row[2] - lib_st.st_mtime) >= 2):
                            fh = None          # stale: recompute below
                    if fh is None:
                        try:
                            fh = fullhash(lp)
                        except OSError:
                            continue
                        c.execute("update files set full=? where path=?", (fh, lp))
                    cache[lp] = fh
                if cache[lp] == mine:
                    hit = lp
                    break
            if hit:
                dupes.append((p, hit))
            else:
                new.append((p, st.st_size))
        if i % 500 == 0:
            c.commit()
            print("    checked %d / %d" % (i, len(files)))
    c.commit()
    c.close()
    return new, dupes


def shoot_days_for(records, trips):
    """Dates that rule 3 will lift out of Diary. records: (date, model, how)."""
    tally = collections.Counter()
    for d, model, how in records:
        if how == "exif" and not is_scanner(model) and not is_diary_camera(model):
            tally[d] += 1
    out = {d for d, n in tally.items() if n >= NON_DIARY_CAMERA_DAY
           and not any(t["start"] <= d <= t["end"] for t in trips)}
    # A date that already has its own folder is a shoot day regardless of how
    # few frames this particular card holds -- otherwise the second card of a
    # split shoot goes to Diary while the first sits in its own folder.
    for d in set(dd for dd, _, _ in records):
        if not any(t["start"] <= d <= t["end"] for t in trips) \
                and existing_day_folder(d):
            out.add(d)
    return out


def report(new, dupes, trips):
    print("")
    print("  already in library : %d" % len(dupes))
    print("  NEW                : %d  (%.1f GB)"
          % (len(new), sum(s for _, s in new) / 1e9))
    if not new:
        print("")
        print("  nothing to import from this card.")
        return {}
    dates, how = {}, collections.Counter()
    film = collections.Counter()
    recs = []
    for p, s in new:
        roll = film_roll(p)
        if roll:
            film[roll] += 1
            continue
        d, h, model = capture_info(p)
        dates[p] = d
        how[h] += 1
        if d:
            recs.append((d, model, h))
    if film:
        print("  lab film scans     : %d in %d roll(s) -- grouped by roll, not date"
              % (sum(film.values()), len(film)))
        for r in sorted(film):
            print("      roll %s  %d frames  ->  %s"
                  % (r, film[r], os.path.join(FILM_DIR, r)))
    # A file can yield no date at all -- unreadable header AND unreadable mtime.
    # Keep those out of the sorted date list (None does not compare to str) and
    # report them separately so they are never silently filed somewhere wrong.
    undated = [p for p, d in dates.items() if not d]
    per_day = collections.Counter(d for d in dates.values() if d)
    print("  dates from EXIF    : %d    from file mtime: %d" % (how["exif"], how["mtime"]))
    if undated:
        print("  NO DATE AT ALL     : %d  (will NOT be filed; listed below)" % len(undated))
        for p in undated[:20]:
            print("      %s" % p)
        if len(undated) > 20:
            print("      ... and %d more" % (len(undated) - 20))
    print("")
    print("  new files by date:")
    shoots = shoot_days_for(recs, trips)
    for d in sorted(per_day):
        dest = (os.path.join(LIB, d[:4], d) if d in shoots
                else destination(d, trips))
        inside = any(t["start"] <= d <= t["end"] for t in trips) or d in shoots
        flag = ""
        if not inside and per_day[d] >= TRIP_DENSITY:
            flag = "   <-- dense day, may be an unlisted trip"
        print("    %s  %5d  ->  %s%s"
              % (d, per_day[d], os.path.relpath(dest, BASE), flag))
    # Cluster the dense days instead of spanning first..last, which would
    # propose one absurd four-month "trip". Same rule the library was built
    # with: dense days seed a trip, and quiet days within MERGE_GAP stay inside
    # it, so a rest day in the middle of a trip does not split it in two.
    D = datetime.date.fromisoformat
    hot = [d for d in sorted(per_day) if per_day[d] >= TRIP_DENSITY
           and not any(t["start"] <= d <= t["end"] for t in trips)]
    clusters, cur = [], []
    for d in hot:
        if cur and (D(d) - D(cur[-1])).days <= MERGE_GAP:
            cur.append(d)
        else:
            if cur:
                clusters.append(cur)
            cur = [d]
    if cur:
        clusters.append(cur)
    if clusters:
        print("")
        print("  unlisted dense periods -- each may deserve a trip in trips.json:")
        for c in clusters:
            a, b = c[0], c[-1]
            span = (D(b) - D(a)).days + 1
            days = [(D(a) + datetime.timedelta(days=k)).isoformat() for k in range(span)]
            n = sum(per_day.get(x, 0) for x in days)
            prof = " ".join(str(per_day.get(x, 0)) if per_day.get(x) else "." for x in days)
            rng = a if a == b else "%s .. %s" % (a, b)
            print("    %-26s %2dd  %5d files   daily: %s" % (rng, span, n, prof))
    return dates


def skip_opt(args):
    """--skip-dir NAME (repeatable): folders on the card to ignore."""
    out = []
    for i, a in enumerate(args):
        if a == "--skip-dir" and i + 1 < len(args):
            out.append(args[i + 1])
    return out


def cmd_verify(args):
    """Independently re-check every staged Inbox file against its card original.

    The ingest already hash-checks each file right after copying, but on an
    interrupted run that guarantee proved untrustworthy once (2026-10-11: 253 of
    296 staged files later compared as same-size-different-hash, cause never
    established, both devices testing clean afterwards). So this exists as a
    separate, after-the-fact check that does not rely on the ingest being right.

    Anything that fails is MOVED ASIDE, never deleted -- last time the files were
    deleted on a diagnosis that could not be substantiated.
    """
    drive = args[0]
    root = drive if drive.endswith((os.sep, "/")) else drive + os.sep
    if not os.path.isdir(root):
        sys.exit("no such drive: %s" % root)
    # Skip the quarantine, exactly as cmd_file does. Without this, once
    # anything has been quarantined every later verify re-checks it: the files
    # no longer match the card, so they either land in `unmatched` (and the
    # run can never come back clear) or get quarantined again under _2, _3,
    # accumulating copies of the same bad file.
    staged = [os.path.join(dp, f)
              for dp, dn, fn in os.walk(INBOX)
              if QUARANTINE not in dp.split(os.sep)
              for f in fn if not f.startswith("._")]
    if not staged:
        sys.exit("Inbox is empty -- nothing to verify")
    card = {}
    for q in walk_media(root, {"$recycle.bin", "system volume information"}):
        card.setdefault(os.path.basename(q), q)
    dupnames = {}
    for q in walk_media(root, {"$recycle.bin", "system volume information"}):
        dupnames[os.path.basename(q)] = dupnames.get(os.path.basename(q), 0) + 1
    ambiguous = {k for k, v in dupnames.items() if v > 1}
    if ambiguous:
        print("  NOTE: %d basenames occur more than once on this card; those files"
              % len(ambiguous))
        print("        cannot be matched by name alone and are reported, not judged.")

    print("  verifying %d staged files against %s" % (len(staged), root))
    good, bad, unmatched = 0, [], []
    for i, f in enumerate(staged, 1):
        base = os.path.basename(f)
        if base in ambiguous or base not in card:
            unmatched.append(f)
            continue
        src = card[base]
        try:
            if os.path.getsize(f) != os.path.getsize(src):
                bad.append((f, "size %d vs card %d"
                            % (os.path.getsize(f), os.path.getsize(src))))
            elif fullhash(f) == fullhash(src):
                good += 1
            else:
                bad.append((f, "same size, different content"))
        except OSError as e:
            bad.append((f, "read error: %s" % e))
        if i % 250 == 0:
            print("    %d / %d" % (i, len(staged)))

    print("")
    print("  verified identical to the card : %d" % good)
    print("  FAILED                         : %d" % len(bad))
    print("  could not be matched by name   : %d" % len(unmatched))
    for f, why in bad[:25]:
        print("      %s  <- %s" % (os.path.basename(f), why))
    if len(bad) > 25:
        print("      ... and %d more" % (len(bad) - 25))

    if not bad and (good != len(staged) or unmatched):
        print("")
        print("  NOT CLEAR: %d of %d staged files were actually verified."
              % (good, len(staged)))
        print("  %d could not be matched to a file on this card. Wrong drive, wrong"
              % len(unmatched))
        print("  card, already ejected, or renamed during ingest. Do not file yet.")
        return
    if bad:
        q = os.path.join(INBOX, QUARANTINE)
        os.makedirs(q, exist_ok=True)
        # The Inbox is a tree (one folder per card) but the quarantine is flat,
        # so two staged files can share a basename. os.replace overwrites
        # silently, which destroyed a quarantined file while the output still
        # claimed "NOT deleted" -- the worst possible moment to lose evidence.
        moved_side = 0
        for f, why in bad:
            base = os.path.basename(f)
            tgt = os.path.join(q, base)
            stem, ext = os.path.splitext(base)
            k = 2
            while os.path.exists(tgt):
                tgt = os.path.join(q, "%s_%d%s" % (stem, k, ext))
                k += 1
            try:
                sides = sidecars_for(f)
                os.replace(f, tgt)
                for sc in sides:          # the sidecar goes with it, or it is stranded
                    if sc.startswith(f):
                        sdst = tgt + sc[len(f):]
                    else:
                        sdst = os.path.splitext(tgt)[0] + sc[len(os.path.splitext(f)[0]):]
                    if not os.path.exists(sdst):
                        os.replace(sc, sdst)
                        moved_side += 1
            except OSError as e:
                print("      could not quarantine %s: %s" % (f, e))
        print("")
        print("  moved %d file(s)%s to %s -- NOT deleted."
              % (len(bad), (" and %d sidecar(s)" % moved_side) if moved_side else "", q))
        print("  the card still holds the originals; inspect before removing anything.")
    else:
        print("")
        print("  all staged files are byte-identical to the card. safe to file.")


def cmd_sync_trips(args):
    """Make trips.json agree with the folder names actually on disk.

    Folders get renamed by hand once you remember what a day actually was: a
    bare 2024-10-05 becomes 2024-10-05_some-hike. trips.json does not notice,
    so a later card holding photos from that date would be filed under the
    stale name, creating a second folder for one day. The folders on disk are
    the source of truth; this copies their names back into trips.json.
    """
    execute = "--execute" in args
    on_disk = {}
    for y in sorted(os.listdir(LIB)):
        yp = os.path.join(LIB, y)
        if not (os.path.isdir(yp) and y.isdigit()):
            continue
        for f in sorted(os.listdir(yp)):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:_(.+))?$", f)
            if m and os.path.isdir(os.path.join(yp, f)):
                on_disk[m.group(1)] = m.group(2) or ""

    doc = json.load(open(TRIPS_JSON, encoding="utf-8"))
    trips = doc.get("trips", [])
    changed, orphan = [], []
    for t in trips:
        start = t["start"]
        if start in on_disk:
            if t.get("name", "") != on_disk[start]:
                changed.append((start, t.get("name", "") or "(unnamed)",
                                on_disk[start] or "(unnamed)"))
                t["name"] = on_disk[start]
        elif t["start"] == t["end"]:
            orphan.append(start)

    known = {t["start"] for t in trips}
    newfolders = [d for d in on_disk if d not in known]

    print("  trips.json entries        : %d" % len(trips))
    print("  dated folders on disk     : %d" % len(on_disk))
    print("  names that disagree       : %d" % len(changed))
    for d, was, now in changed:
        print("      %s   %-24s -> %s" % (d, was, now))
    if orphan:
        print("  single-day entries with no folder: %s" % ", ".join(sorted(orphan)))
    if newfolders:
        print("  folders with no trips.json entry : %s" % ", ".join(sorted(newfolders)))
        print("      (a later card from those dates would go to Diary instead)")
    if not changed and not newfolders:
        print("  already in sync.")
        return
    if not execute:
        print("")
        print("  dry run. add --execute to write the corrected names back.")
        return
    for d in newfolders:
        trips.append({"start": d, "end": d, "name": on_disk[d]})
    trips.sort(key=lambda t: t["start"])
    doc["trips"] = trips
    # Write via a temp file in the same directory and rename over the original.
    # open(...,"w") truncates before json.dump writes a byte, so any failure
    # mid-write left trips.json empty -- and there is no other copy of the trip
    # windows anywhere.
    tmp = TRIPS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, TRIPS_JSON)
    print("")
    print("  trips.json updated: %d renamed, %d added" % (len(changed), len(newfolders)))


def cmd_scan(args):
    drive = args[0]
    new, dupes = triage(drive, skip_opt(args))
    report(new, dupes, load_trips())
    print("")
    print("  scan only -- nothing copied.")
    print("  to import:  python sdcard.py ingest %s --execute" % drive)


def cmd_ingest(args):
    drive = args[0]
    execute = "--execute" in args
    trips = load_trips()
    new, dupes = triage(drive, skip_opt(args))
    report(new, dupes, trips)
    if not new:
        return
    label = "card"
    try:
        import subprocess
        out = subprocess.run(["cmd", "/c", "vol", drive.rstrip("\\/")],
                             capture_output=True, text=True, timeout=10).stdout
        m = re.search("is (.+)", out)
        if m:
            # keep the whole volume label, then make it filename-safe
            label = re.sub(r"[^A-Za-z0-9_.-]+", "-", m.group(1).strip()).strip("-") or "card"
    except Exception:
        pass
    stamp = datetime.date.today().isoformat()
    dest_dir = os.path.join(INBOX, "%s_%s" % (stamp, label))
    n = 1
    while os.path.exists(dest_dir):
        n += 1
        dest_dir = os.path.join(INBOX, "%s_%s_%d" % (stamp, label, n))

    print("")
    print("  -> %s" % dest_dir)
    if not execute:
        print("  dry run. add --execute to copy the %d new files." % len(new))
        return

    os.makedirs(dest_dir, exist_ok=True)
    c = db()
    copied = failed = 0
    errs = []
    for p, _ in new:
        name = os.path.basename(p)
        target = os.path.join(dest_dir, name)
        if os.path.exists(target):
            # Two card files can share a basename AND a parent-folder leaf name
            # (two bodies, a BACKUP tree, a re-imported card image), so the
            # __tag form is not unique either. Loop until it is: without this,
            # copy2 silently truncated the previous file and the hash check
            # compared the source against the copy it had just overwritten, so
            # the loss was invisible and still counted as copied.
            tag = os.path.basename(os.path.dirname(p))
            stem, ext = os.path.splitext(name)
            cand = os.path.join(dest_dir, "%s__%s%s" % (stem, tag, ext))
            k = 2
            while os.path.exists(cand):
                cand = os.path.join(dest_dir, "%s__%s_%d%s" % (stem, tag, k, ext))
                k += 1
            target = cand
        try:
            shutil.copy2(p, target)
            if fullhash(p) != fullhash(target):
                os.remove(target)
                raise IOError("hash mismatch after copy")
            st = os.stat(target)
            c.execute("insert or replace into files values(?,?,?,?,?)",
                      (target, st.st_size, st.st_mtime,
                       quickhash(target, st.st_size), None))
            copied += 1
        except Exception as e:
            failed += 1
            if len(errs) < 10:
                errs.append("%s: %s" % (p, e))
        if copied % 200 == 0:
            c.commit()
    c.commit()
    c.close()
    print("  copied %d, each verified by hash.  failed %d" % (copied, failed))
    for e in errs:
        print("    %s" % e)
    print("")
    print("  card untouched. safe to eject and insert the next one.")
    print("  when every card is done:  python sdcard.py file --execute")


def cmd_file(args):
    execute = "--execute" in args
    trips = load_trips()
    if not os.path.isdir(INBOX):
        sys.exit("no Inbox at %s" % INBOX)
    # NEVER walk _suspect: cmd_verify puts files there precisely because they
    # failed verification, and promises they will not be touched. Walking it
    # filed a quarantined corrupt file into the Library and then the empty-dir
    # sweep below deleted the quarantine folder, erasing the evidence.
    # Pass 1: read each file once for date AND camera.
    rows, undated, dated = [], [], []
    for p in walk_media(INBOX, {QUARANTINE}):
        roll = film_roll(p)
        if roll:
            rows.append((p, os.path.join(LIB, FILM_DIR, roll, os.path.basename(p))))
            continue
        d, how, model = capture_info(p)
        if not d:
            undated.append(p)          # no usable date: leave it in the Inbox
            continue
        dated.append((p, d, model, how))

    # Pass 2: a day with enough non-Ricoh frames is a shoot, not diary. Count
    # per day first, because the decision is about the DAY, not the file.
    shoot_days = shoot_days_for([(d, m, h) for _, d, m, h in dated], trips)
    if shoot_days:
        print("  %d day(s) have >=%d non-Ricoh frames and get their own folder"
              % (len(shoot_days), NON_DIARY_CAMERA_DAY))
        for d in sorted(shoot_days):
            tot = sum(1 for _, dd, _, _ in dated if dd == d)
            ex = existing_day_folder(d)
            where = os.path.relpath(ex, LIB) if ex else os.path.join(d[:4], d)
            print("      %s  %d of %d frames  ->  %s%s"
                  % (d, tot, tot, where, "  (joining the existing folder)" if ex else ""))
        print("      (whole day moves together: both cameras out means one shoot)")
        print("")

    # Pass 3: build the plan.
    for p, d, model, how in dated:
        if d in shoot_days:
            dest = existing_day_folder(d) or os.path.join(LIB, d[:4], d)
        else:
            dest = destination(d, trips)
        rows.append((p, os.path.join(dest, os.path.basename(p))))
    if undated:
        print("  %d file(s) have no readable date and are being LEFT in the Inbox:"
              % len(undated))
        for p in undated[:20]:
            print("      %s" % p)
        if len(undated) > 20:
            print("      ... and %d more" % (len(undated) - 20))
        print("")
    if not rows:
        sys.exit("Inbox holds no datable media")

    # Decide the media file's final name FIRST, then derive its sidecars from
    # THAT name. Planning them as independent rows let the raw get uniquified
    # while its sidecar kept the original name, so the sidecar landed on a
    # DIFFERENT, pre-existing library photo -- handing that photo someone
    # else's rating, GPS and edit history while the incoming photo arrived
    # with none. Verified before this fix; see test_sdcard.py.
    taken, final, n_side = collections.Counter(), [], 0

    def claim(path):
        """Reserve a destination, uniquifying if taken on disk or in this plan."""
        if not (os.path.exists(path) or taken[path.lower()]):
            taken[path.lower()] += 1
            return path
        stem, ext = os.path.splitext(path)
        k = 2
        while True:
            cand = "%s_%d%s" % (stem, k, ext)
            if not (os.path.exists(cand) or taken[cand.lower()]):
                taken[cand.lower()] += 1
                return cand
            k += 1

    for src, dst in rows:
        final_dst = claim(dst)
        final.append((src, final_dst))
        for sc in sidecars_for(src):
            if sc.startswith(src):
                # darktable style: IMG_1.CR2.xmp -> <resolved media name>.xmp
                sdst = final_dst + sc[len(src):]
            else:
                # Lightroom style: IMG_1.xmp -> <resolved media stem>.xmp
                sdst = os.path.splitext(final_dst)[0] + sc[len(os.path.splitext(src)[0]):]
            taken[sdst.lower()] += 1
            final.append((sc, sdst))
            n_side += 1

    tree = collections.Counter(os.path.relpath(os.path.dirname(d), BASE) for _, d in final)
    print("filing %d files from Inbox:" % len(final))
    for k in sorted(tree):
        print("  %-52s %5d" % (k, tree[k]))
    if n_side:
        print("  (includes %d sidecar file(s) travelling with their raws)" % n_side)
    renamed = sum(1 for s, d in final if os.path.basename(s) != os.path.basename(d))
    if renamed:
        print("  (%d renamed to avoid a name clash in the destination)" % renamed)
    if not execute:
        print("")
        print("dry run. add --execute to move them.")
        return

    for d in sorted(shoot_days):
        if record_shoot_day(d):
            print("  recorded %s in trips.json so later cards join the same folder" % d)

    c = db()
    done = 0
    for s, d in final:
        os.makedirs(os.path.dirname(d), exist_ok=True)
        try:
            os.replace(s, d)
            c.execute("delete from files where path=?", (s,))
            st = os.stat(d)
            c.execute("insert or replace into files values(?,?,?,?,?)",
                      (d, st.st_size, st.st_mtime, quickhash(d, st.st_size), None))
            done += 1
        except Exception as e:
            print("  FAILED %s: %s" % (s, e))
    c.commit()
    c.close()
    for dp, dn, fn in os.walk(INBOX, topdown=False):
        if not dn and not fn and dp != INBOX:
            try:
                os.rmdir(dp)
            except OSError:
                pass
    print("")
    print("  filed %d files. index updated." % done)
    print("  then point darktable at any folder that gained files.")


CMDS = {"index": cmd_index, "scan": cmd_scan, "ingest": cmd_ingest,
        "verify": cmd_verify, "file": cmd_file, "sync-trips": cmd_sync_trips}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    fn = CMDS[sys.argv[1]]
    rest = sys.argv[2:]
    if fn in (cmd_scan, cmd_ingest, cmd_verify) and not rest:
        sys.exit("which drive?  e.g.  python sdcard.py %s F:" % sys.argv[1])
    fn(rest)
