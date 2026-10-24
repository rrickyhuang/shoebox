# shoebox

Tools for keeping a personal photo library in order on an external drive, and
for getting photos off SD cards without losing any.

Written for one specific library, so the conventions below are baked in. The
parts worth stealing are probably the deduplication and the verification.

## The tools

| | |
|---|---|
| `sdcard.py` | import from SD cards: index, scan, ingest, verify, file, sync-trips |
| `integrity.py` | find silently corrupted raws by checking their embedded JPEG blocks (`--decode` for a slower, stricter pass) |
| `exfat_scan.py` | recover exFAT directory entries from a damaged card |
| `validate_entries.py` | discard recovered entries whose data is no longer there |
| `exfat_extract.py` | extract files from a damaged card using those entries |
| `test_sdcard.py` | the test suite |

## Setup

The library root defaults to `E:\Photos`. Point it somewhere else with an
environment variable:

```
set PHOTO_LIBRARY=D:\MyPhotos        # Windows
export PHOTO_LIBRARY=/mnt/photos     # elsewhere
```

Everything else -- `Library/`, `Inbox/`, `trips.json`, the index -- is derived
from that.

Python 3.8+. `sdcard.py` and `integrity.py` need nothing else and run
anywhere. The three `exfat_*` tools are **Windows-only** -- they open raw
devices through `CreateFileW`, which is the whole point of them -- and
`exfat_scan.py` additionally needs **numpy**. `integrity.py --decode` wants
**Pillow**.

## Importing a card

```
python sdcard.py index                  refresh the content index
python sdcard.py scan   F:              read-only: what is new, where it would go
python sdcard.py ingest F: --execute    copy only NEW files into Inbox, hash-verified
python sdcard.py verify F:              re-check staged files, quarantining failures
python sdcard.py file   --execute       move Inbox into the library
python sdcard.py sync-trips --execute   make trips.json match the folder names on disk
```

`--skip-dir NAME` ignores a folder a card is carrying that is not photographs.

**Nothing is ever written to a card.** Every device handle is opened read-only.

Commands that write to the *library*: `ingest --execute`, `file --execute`,
`sync-trips --execute`, `index` (maintains its database), and `verify` --
which, unlike the others, has no dry run and moves failures as soon as you
run it.

## Before you point this at photographs you care about

These tools **move and delete files**:

- `file --execute` **moves** originals out of `Inbox` into the library. It
  does not copy them.
- `verify` **moves** files that fail their check into `Inbox/_suspect`. It
  never deletes them, but it has no dry run.
- `ingest --execute` deletes exactly one thing: a copy it has just made that
  failed its hash check. The card original is never touched.
- `index` and `sync-trips --execute` rewrite `.photo-index.db` and
  `trips.json` respectively.

Everything destructive is dry-run by default except `verify`. `scan` and
`integrity.py` never write at all. Even so: have a backup before running
anything with `--execute` against a library you cannot replace.

## How files are filed

```
Library/<year>/<YYYY-MM-DD>_<trip-name>/   trips and named events
Library/<year>/<year>_Diary/               everyday-carry frames
Library/Film/<roll-id>/                    lab film scans, grouped by roll
```

1. A JPEG named `RRRRRRRRFFFF.jpg` is a lab film scan and goes to `Film/<roll>/`.
   Scans have no capture date at all, so dating them by file time would file a
   whole roll under the day it was downloaded.
2. Otherwise, if the capture date falls inside a window in `trips.json`, it goes
   to that trip.
3. Otherwise, if that day has 20+ frames from a camera that is not the
   everyday-carry one, the whole day gets its own folder -- both cameras
   together, because a day with both out is one shoot.
4. Otherwise it is diary.

## Deduplication

By **content**, never by filename. Cameras reuse filenames, and editors rename
files on import, so a name proves nothing. Identity is size plus a 128 KB quick
hash, always confirmed with a full MD5 before anything is called a duplicate.

Each ingest adds what it copied to the index immediately, so card #2 is
deduplicated against card #1 as well as against the library.

## Things learned the hard way

Every cheap identity check failed at least once on real data:

- **name + size** matched corrupted card files to clean library copies, and
  matched stale directory entries to real files
- **content hashing** reported 2,436 corrupted duplicates as new photos, because
  a corrupted copy can never hash-match its clean twin. Right method, bad input.
- **exFAT directory entries** from a whole-device sweep include stale ones from
  earlier uses of the card -- 51% of those found on one failing card pointed at
  clusters since reused

So: quarantine, never delete, on a diagnosis you cannot substantiate. Validate a
corruption detector against a known-bad control before trusting a clean result --
a detector that reports "all clean" is indistinguishable from a broken one.

## Tests

```
python test_sdcard.py
```

Every test here exists because something was actually wrong. They are
mutation-tested: each bug was deliberately reintroduced to confirm the suite
catches it. Twice a test passed while the thing it named was broken, so a green
run on its own is not evidence.

## Recovering a damaged card

The three `exfat_*` tools are a pipeline and must run in order. Each writes its
output next to the script, and the next one reads it:

```
python exfat_scan.py H              -> recovered_entries.json
python validate_entries.py H        -> validated_entries.json
python exfat_extract.py H <dest>    -> the files
```

`exfat_scan.py` sweeps the whole device, so it also finds *stale* directory
entries from earlier uses of the card. `validate_entries.py` exists to throw
those away: it checks that the data at each entry's first cluster actually
carries the magic number its extension implies. On one real card, 51% of
recovered entries were stale.

## Licence

MIT. See `LICENSE`.
