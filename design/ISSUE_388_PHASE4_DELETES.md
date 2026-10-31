# #388 phase 4 - deletes

Phase 4 of the #388 plan: apply row-level deletes instead of refusing tables
that carry them. Phases 1-3 (Avro decode, field-id projection, filesystem
catalog + current-snapshot scan) are merged; `iceberg_scan` today refuses any
snapshot with delete files (`0A000`). This phase reads them correctly.

"A reader that ignores deletes silently returns rows the table says are gone" --
the plan's worst failure mode. So correctness, not breadth, is the bar: every
increment either applies its delete kind correctly or keeps refusing the kinds it
does not yet handle. We never silently drop a delete.

## The Iceberg v2 delete model (from the spec)

Two merge-on-read delete kinds, both tracked in **delete manifests**
(`manifest_file.content = 1`), each entry's `data_file.content` distinguishing:

- **Position deletes** (`content = 1`). A Parquet file with two columns:
  `file_path` (string, field id 2147483546) and `pos` (long, field id
  2147483545). Each row names a (data file, row ordinal) to drop. A position
  delete may also be "path-scoped" (applies to one data file) or global.
- **Equality deletes** (`content = 2`). A Parquet file carrying the columns named
  by the manifest entry's `equality_ids`. Any data row equal to a delete row on
  those columns is dropped.

**Sequence-number ordering is the correctness crux, and the rule differs by
delete kind** (Iceberg spec, Scan Planning):

- a **position** delete applies when `datafile.sequence_number <=
  delete.sequence_number` -- i.e. `delete.sequence_number >=
  datafile.sequence_number`. The `<=` (not strict `<`) is deliberate: a position
  delete affects data written in the **same commit** or earlier, so a delete and
  the rows it removes may share a sequence number (a single-commit upsert).
- an **equality** delete applies when `datafile.sequence_number <
  delete.sequence_number` -- strictly lower (it targets pre-existing data only).

Get this wrong and the table reads subtly wrong -- deletes bleed onto newer data,
or fail to apply to same-commit or older data. Gate **per kind**, never one
shared operator (position deletes also match `file_path`). Getting this backwards
-- a strict `>` for position deletes -- silently dropped same-commit deletes and
was the blocking bug of the first 4a review; do not repeat it in 4b.

## Fixture tooling (the gating constraint)

pyiceberg 0.11.1 -- the only available writer, and the current release -- CANNOT
write merge-on-read deletes (it warns and falls back to copy-on-write). No Spark
in the containers. So delete fixtures are **constructed**, not written by an
independent engine:

1. pyiceberg writes the data (a normal append) -- the data files are real.
2. A generator hand-crafts the delete Parquet (via pyarrow) and the delete
   manifest (Avro OCF, the same crafting the delete-refusal arm already uses),
   wires them into a new manifest-list + metadata.json snapshot, and sets the
   sequence numbers.
3. The oracle is computed: appended rows **minus** the positions/values the
   generator deleted. The independence we keep is pyarrow-for-data and an
   explicit hand-computed expected set; document that the delete wiring is ours
   (no independent delete writer exists), and cross-check the crafted manifest by
   decoding it back through `read_manifest_list` / `read_avro_manifest`.

A sequence-number arm is essential precisely because the oracle is ours, and it
must sit on the boundary: craft the "apply" delete at a sequence number EQUAL to
the data file's (a position delete must apply -- the case a strict `>` wrongly
excludes) and the "no-apply" delete at a strictly LOWER one (must not apply), so
the `>=` rule is load-bearing, not decorative.

## Architecture

The scan must, per data file, know each row's **ordinal position** to apply
position deletes, and must evaluate **equality** on the delete columns. Open
question for the reader (investigate before coding): does `pq_read_rows` expose a
per-row ordinal, and can a custom sink filter rows? Likely shape:

- The walk (`ice_walk_data_files`) stops refusing deletes and instead **collects**
  the delete entries (path, content, sequence_number, equality_ids) alongside the
  data entries, so the scan sees both.
- For each data file D (sequence S_d): gather applicable position-delete files
  (matching D's path, sequence > S_d) -> a sorted set of positions; and
  applicable equality-delete files (sequence > S_d) -> their rows.
- Read D through a **position-aware sink**: track the row ordinal, skip a row
  whose ordinal is in the position set or whose equality-column tuple matches an
  equality-delete row.

This needs the reader to either expose row ordinals to a sink, or accept a
skip-set. Reuse `ice_open_path` to open each delete file (a delete-file path is
opened here too -- same content-leak surface as data files).

## Sub-increments (each its own PR, each provable)

- **4a - position deletes. DONE.** `pq_read_rows` gained a sorted `skipPos` set
  (dropping rows by file ordinal, computed as sum-of-prior-row-group-rows + r);
  `PgColumnarReadParquetByFieldId` threads it. The walk grew a `collect_deletes`
  mode: the lister still refuses deletes, the scan collects position-delete
  entries (still refusing equality) and resolves each entry's data sequence
  number (inheriting the manifest's when null). `iceberg_scan` reads the
  position-delete Parquet files (by their reserved field ids 2147483546/545),
  and for each data file drops the ordinals its deletes name whose sequence
  number is `>=` the data file's (position-delete rule; equal = same commit).
  Fixture is hand-crafted (`warehouse_del`, generator `gen_delete_fixture.py`)
  with `apply` (equal seq) / `noapply` (strictly older) / `inherit` / `equality`
  / `wrongpath` / `badseq` variants. Removal proofs: defeating the reader skip
  keeps the deleted rows; reverting the gate to strict `>` reds the equal-seq
  apply; dropping the path match applies a wrong-file delete.
  PG17/18/19 (PG19 needed `TupleDescFinalize` on the manual pos-delete tupdesc)
  + ASAN; the Parquet family and 3a/3b/3c suites are unregressed.
- **4b - equality deletes.** Add equality evaluation on `equality_ids` columns.
  Designed below.
- **4c - v3 deletion vectors (Puffin).** A new container format (Puffin) holding
  a roaring bitmap of deleted positions -- a decoder like the Avro one. Largest.
  Designed below.

Held out: pruning/partition transforms (phase 5), object storage + REST (phase
6). Multiple delete files, and delete + data in one snapshot, are covered within
4a/4b.

## 4b design - equality deletes

Spec rules (iceberg.apache.org/spec, Scan Planning + Equality Delete Files;
verified against the spec text 2026-08-14, local copy in the session scratchpad):

- **Sequence gate is STRICT `<`**: an equality delete applies to a data file iff
  `datafile.sequence_number < delete.sequence_number`. At EQUAL sequence numbers
  it does NOT apply -- the deliberate opposite of the position-delete `<=` (no
  same-commit carve-out: this is how "delete old row by key + insert new row" in
  one commit avoids the delete eating the new row). Gate per kind, never shared.
- **Matching**: a data row is deleted if, for ANY row of an applicable equality
  delete file, the values of ALL `equality_ids` columns are equal. Null matches
  null (`IS NULL` semantics, not SQL `=`). Matching is by FIELD ID; column names
  are irrelevant. A delete file may carry extra columns beyond `equality_ids`;
  they are payload and MUST be ignored for matching.
- **`equality_ids`** (data_file field 135, `list<int>`): required when
  `content = 2` (null/empty there is corrupt metadata). Restricted to primitive,
  non-float/double fields (never inside maps/lists).
- **Partition scoping**: a delete file written with an UNPARTITIONED spec is
  global; one written with a partitioned spec applies only to data files with the
  same (spec id, partition tuple). We do not decode typed partitions until phase
  5, so: resolve the delete manifest's `partition_spec_id` against metadata.json
  `partition-specs`; empty `fields` = global (apply); non-empty = refuse `0A000`
  (partition-scoped equality deletes are phase-5 work; applying them globally
  would over-delete); unresolvable spec id = `XX001`. Position deletes never
  consult this (their per-file path match subsumes partition scope).

### Architecture: probe pass reusing skipPos (no reader changes)

Everything an equality delete needs already exists as of 4a; 4b composes it:

1. **Decode `equality_ids`** in `av_decode_data_file` (nullable `array<int>` per
   the Avro block protocol already implemented in `av_skip`), onto
   `PgColumnarAvroManifestEntry`. Thread the enclosing manifest's
   `partition_spec_id` to the walk callback (the entry itself does not carry it).
2. **Collect** content=2 entries in the scan walk (drop the 0A000 at the old
   refusal point; the lister still refuses all deletes). Validate: PARQUET only
   (0A000), non-empty equality_ids (XX001), unpartitioned spec (0A000 above).
3. **Read each equality-delete file once, upfront** (they are global): a manual
   tupdesc built from the file's `equality_ids`, with Postgres types derived from
   the table's CURRENT SCHEMA in metadata.json (`type` per field id): int->int4,
   long->int8, string->text, boolean->bool, date->date; any other type is
   refused 0A000 (never silently unapplied). Types come from the schema, not the
   user's AS clause, so a delete on an unprojected column still applies. Rows are
   kept as Datum arrays (per-file scratch context reset, survivors datumCopy'd
   out -- the 4a `ice_read_pos_deletes` memory pattern).
4. **Per data file D**: filter delete files to `E.seq > D.seq`. If any, run a
   PROBE pass: read ONLY the union of their equality columns from D via
   `PgColumnarReadParquetByFieldId` subset projection (a whole-file read, so the
   tuplestore row index IS the file ordinal -- the same ordinal skipPos uses).
   Test each row against each delete row (AND over that file's ids, null==null
   via each column's type-cache equality operator); matching ordinals join the
   position-delete ordinals in one sorted, deduplicated skipPos for the full
   read. Cost: applicable equality deletes read D's key columns twice (footer
   parse + key decode); acceptable for v1, noted in docs.
5. **Cross-check**: the full read returns `probe_rows - |skip ordinals <
   probe_rows|` rows; a mismatch is XX001 (catches ordinal-accounting drift
   between the two passes).

Known refusals / loud errors, all provable by fixture arm:
- partitioned-spec equality delete: 0A000 (over-deleting is the alternative);
- equality column of an unmapped type (e.g. timestamp): 0A000, before any file
  is opened;
- equality column in the schema but ABSENT from an older data file: the reader's
  existing 22023 (`no Parquet column has field id`); the spec says such a column
  reads as null -- a later increment can add missing-id->null projection; until
  then the error is loud, never wrong;
- content=2 with null/empty equality_ids: XX001;
- delete-file paths go through `ice_open_path` like every opened file (the
  boundary arm reads a real file outside the root and asserts the boundary's
  SQLSTATE, per the 4a lesson).

### 4b fixture arms (generator `gen_delete_fixture.py`, additive only)

Data stays `data.parquet` (ids 1..5, region eu/eu/us/us/us, amount 10..50, seq
5). New: `eqdel-*.parquet` delete files, `data2.parquet` (id 6 region NULL, id 7
region 'eu') for the null arm. Arms, each its own metadata variant:

| arm | delete | seq | expect |
|---|---|---|---|
| eqapply | ids=[1], rows id 2,4 | 6 | survivors 1,3,5 |
| eqboundary | same file | 5 (== data) | ALL rows survive (strict `<`) |
| eqmulti | ids=[1,2], rows (3,eu),(4,us) | 6 | (3,eu) no match: AND, not OR; survivors 1,2,3,5 |
| eqextra | file has id+amount cols, ids=[1], amount values wrong | 6 | extra col ignored; survivors 1,3,4,5 |
| eqnull | ids=[2], row region=NULL, over data+data2 | 6 | id 6 dropped (null==null), id 7 kept |
| eqtwo | two files: ids=[1] row 5; ids=[2] row eu | 6 | union of files; survivors 3,4 |
| eqmixed | posdel (ords 1,3) seq 5 + eq ids=[1] row 5 | 6 | pos+eq union; survivors 1,3 |
| eqnoids | content=2, equality_ids null | 6 | XX001 |
| eqpart | delete manifest spec-id 1 (partitioned) | 6 | 0A000 |
| eqtype | ids=[8] (schema field "created" timestamp) | 6 | 0A000 |
| eqmissing | ids=[9] (schema field "extra" long, absent from parquet) | 6 | 22023 |
| eqescape | delete file path outside the table root | 6 | boundary SQLSTATE |

Removal proofs: flip `<` to `<=` -> eqboundary reds (the arm that distinguishes
the two rules); skip the null-equality branch -> eqnull reds; match on all file
columns instead of equality_ids -> eqextra reds; drop the union across files ->
eqtwo reds; drop the pos+eq merge -> eqmixed reds; derive types from the AS
clause -> the unprojected-column arm reds (eqapply is run projecting a column
subset). The equality metadata variants carry `partition-specs`
(`[{"spec-id":0,"fields":[]}]`, plus a partitioned spec 1 for eqpart); existing
4a variants are untouched (the scoping lookup runs only for content=2).

### 4b adversarial audit (3 lenses, 11 findings verified, all confirmed)

The 4a-style audit (spec / memory / matching lenses, each finding adversarially
verified against the code) ran after GREEN. Fixed in this increment, each with
a fixture arm and a removal proof:

- **equality_ids int64->int32 silent truncation** (`av_read_int_array`): a
  value beyond int32 aliased onto a real field id and keyed the delete on a
  column the manifest never named -- silent wrong rows. Now a decode error
  (XX001); arm `eqbigid` (value 2^32+2, which truncated to the real field 2 and
  demonstrably deleted the 'eu' rows before the fix).
- **Missing manifest-list `partition_spec_id` defaulted to 0** and sailed past
  the partition-scope guard (a scoped delete applied globally). Presence is now
  tracked through the decoder and required for equality deletes (XX001); arm
  `eqnospec` (a manifest list schema without the field, which silently
  globalized before the fix).
- **A matched partition spec without a `fields` array read as unpartitioned**,
  the opposite posture from the sibling undefined-spec-id XX001. Now XX001; arm
  `eqnofields`.
- **Never-applicable equality deletes were validated (and could refuse) before
  the eligibility filter ran.** A delete whose sequence number exceeds no data
  file's has no effect per the strict-< rule and is now skipped unread; arm
  `eqstaletype` (an unsupported-type delete at the data's own sequence number
  must NOT refuse). Consequence: the pre-4b `equality` fixture (equal seq, no
  equality_ids) is now skipped rather than XX001; `eqnoids` (seq 6) keeps the
  XX001 arm.
- **v1-shaped manifests (no sequence_number column) were refused as corrupt on
  EXISTING entries**; the spec defaults every file to 0 when the column is
  absent. The decoder now distinguishes "column absent from the schema" (v1,
  default 0, any status) from "v2 explicit null" (ADDED inherits, others
  XX001 -- the badseq arm still holds); arm `v1seq`.
- The dropped-column refusal message no longer blames a drop for nested-field
  ids ("not a top-level field ... dropped or nested columns").

Confirmed and DEFERRED, all loud errors (never silent wrongness), recorded here
as the 4b follow-up backlog:

- **Missing-id -> null projection**: an equality column added after a data file
  was written errors 22023 in the probe (arm `eqmissing`); the spec projects it
  as null. Needs a reader mode binding an absent field id to a constant null
  column.
- **Dropped delete columns**: the spec says a dropped column must still be
  applied (via the historical schema); we refuse 0A000. Needs schema-history
  resolution.
- **int->long / float->double type promotion**: a pre-promotion file's physical
  INT32 vs the current schema's `long` errors in the binder; a reader-wide
  promotion feature, not 4b-specific. Done in #1789: the field-id binder widens
  both, so the probe and the delete-file read bind an old file too.

## 4c design - v3 deletion vectors (Puffin)

Spec rules (Iceberg table spec + Puffin spec, verified 2026-08-15 against the
spec texts; local copies in the session scratchpad; DV sections of spec.md at
lines 710-754, 1050-1095, 1353-1386, 1933-1945):

- A DV is the v3 ENCODING of position deletes: a delete-manifest entry with
  `content = 1`, `file_format = "puffin"`, `file_path` naming the CONTAINING
  Puffin file (several DVs may share one), and three v3 `data_file` fields:
  `referenced_data_file` (135/143: the ONE data file all its deletes target;
  required for DVs), `content_offset` (144) and `content_size_in_bytes` (145),
  which are required and "must exactly match the `offset` and `length` stored
  in the Puffin footer" for the blob. `record_count` is the DV's cardinality.
- **Sequence gate `<=`**, identical to position-delete files (same-commit
  deletes count). Partition scoping is subsumed by the exact
  `referenced_data_file` path match, the same argument as 4a's path match.
- **A DV SUPERSEDES position-delete files.** The position-delete-file scope
  rule gains a fourth condition in v3: it applies only when "there is no
  deletion vector that must be applied to the data file". A writer adding a DV
  must fold all existing position deletes for that file into it, so a reader
  ignores Parquet position-delete rows for any data file an applicable DV
  covers. Union instead of supersede is wrong BY SPEC, not just redundant --
  the fixture arm sits exactly on that distinction.
- **At most one DV per data file per snapshot** (writer guarantee); on
  violation "results of the scan are undefined ... implementations may raise an
  error". We raise (XX001), the safe choice.
- **DVs are v3-only**: "not supported in v2 or earlier". The reader gains its
  first use of the `format-version` VALUE: a puffin-format delete entry in a
  table whose metadata says version < 3 is refused (0A000, naming the version).
- Puffin container: `PFA1` magic at start, footer = `PFA1 | payload JSON |
  payload-size (4, LE) | flags (4) | PFA1`. Flags bit 0 = compressed footer;
  we refuse a compressed footer loudly (0A000) rather than adding lz4.
  Blob metadata: type `deletion-vector-v1`, `offset`/`length`,
  `properties["referenced-data-file"]`; a DV blob must NOT declare a
  `compression-codec` (declaring one is refused).
- DV blob bytes: `length (4, BE, = 4 + vector bytes) | magic D1 D3 39 64 |
  portable 64-bit roaring bitmap | CRC-32 (4, BE, over magic+vector, zlib
  polynomial)`. Note the endianness split: prefix and CRC big-endian (Delta
  compatibility), roaring little-endian.
- Portable roaring64: uint64 LE bucket count; per bucket (ascending key)
  uint32 LE high-32 key + a full 32-bit roaring bitmap. 32-bit format: cookie
  12346 (no runs; + uint32 container count) or 12347 (runs; count in the high
  16 bits + a run-marker bitset); descriptive header of (key, cardinality-1)
  pairs; an offset header (present for cookie 12346, or 12347 with >= 4
  containers -- a decoder can ignore it); containers in order: array (sorted
  uint16s), bitset (8192 bytes), run (uint16 count + (start, length-1) pairs).
  Any other cookie aborts the decode.

### Architecture

A new decoder file, `src/columnar_puffin.c` (+ .h), mirroring the Avro
decoder's role: `PgColumnarPuffinReadDeletionVector(buf, len, blob_offset,
blob_size, referenced_path, &positions, &npos)` parses the footer (payload JSON
through the server's jsonb reader, like metadata.json), locates the
`deletion-vector-v1` blob whose footer offset/length EQUAL the manifest's
`content_offset`/`content_size_in_bytes` (the spec's cross-check), validates
the blob (length prefix consistency, magic, CRC-32 via zlib's crc32 -- already
linked for Avro deflate), decodes the roaring bitmap with the Avro decoder's
bounded-cursor discipline, and returns the positions ascending (the natural
roaring iteration order -- exactly the sorted skipPos contract).

The scan side rides 4a's machinery whole:

- The Avro decoder gains the three v3 fields (nullable unions, existing
  helpers); `ice_scan_cb` copies them onto `IceEntry`.
- `ice_read_pos_deletes` branches where the "only PARQUET" refusal fires
  today: puffin entries are validated (v3 gate, referenced/offset/size present,
  size capped), the Puffin file is opened through `ice_open_path` and slurped
  (whole file, 64 MB cap -- DV blobs are KBs to MBs), and each decoded position
  becomes an `IcePosDel {dpath = referenced_data_file, pos, seq}` in the same
  snapshot-global list, flagged `from_dv`. Duplicate DV entries for one
  referenced file are detected here (XX001).
- The per-data-file merge applies supersede: if any applicable (`seq >=`,
  path-matched) DV row exists for data file D, non-DV position rows for D are
  ignored; DV rows merge into skipPos as position rows always did. Equality
  deletes (4b) are orthogonal and unaffected.
- Positions beyond the file's row count never match an ordinal and are
  harmless by construction; positions above 2^32 exercise the second roaring
  bucket and flow through uint64 untouched.
- Validation: decoded cardinality must equal the entry's `record_count`
  (the spec defines it so), else XX001.

### 4c fixture arms (generator additions, additive only; oracle-rich)

Unlike 4b, BOTH pyiceberg 0.11.1 and DuckDB's iceberg extension read DVs, so
the hand-built fixtures get two independent oracles (proven live during
research: a hand-built Puffin file decoded identically by pyiceberg and by the
recipe DuckDB implements). pyroaring's `BitMap64.serialize()` IS the portable
serialization (verified byte-by-byte against the format spec); the blob wrapper
is `struct.pack(">I") + D1D33964 + bytes + crc32 BE`.

| arm | shape | expect |
|---|---|---|
| dvapply | DV ordinals {1,3}, seq == data seq 5 | THE `<=` boundary; survivors ids 1,3,5 |
| dvnoapply | same DV, seq 4 < data 5 | all 5 rows |
| dvsupersede | DV {1} + applicable Parquet posdel {3}, same data file | posdel IGNORED: survivors 1,3,4,5 (union would also drop id 4) |
| dvother | DV {1} on data.parquet + posdel {0} on data2.parquet | supersede is per-file: posdel still applies to data2 |
| dvwide | DV {1, 65536..70000, 2^32+5} | second bucket + bitset-scale container; survivors minus id 2 |
| dvrun | run-optimized DV {0,1,2} (cookie 12347) | run containers decode; survivors ids 4,5 |
| dvtwo | one Puffin file, two DV blobs for two data files | multi-blob footer; both apply |
| dvdup | two DV entries referencing one data file | XX001 |
| dvbadcrc | CRC word corrupted | XX001 |
| dvbadmagic | blob magic wrong | XX001 |
| dvoffmismatch | manifest content_offset != footer offset | XX001 |
| dvnoref | referenced_data_file null on a puffin entry | XX001 |
| dvbadcount | record_count != decoded cardinality | XX001 |
| dvcompressed | blob metadata declares compression-codec | XX001 |
| dvflags | footer flags bit 0 set (compressed footer) | 0A000 |
| dvv2 | puffin entry in a format-version 2 table | 0A000 naming the version |
| dvescape | Puffin path outside the table root | 22023 (boundary) |

Removal proofs: `<=` -> `<` reds dvapply; drop supersede -> dvsupersede reds
(id 4 wrongly deleted); global instead of per-file supersede -> dvother reds;
skip CRC -> dvbadcrc; skip the offset cross-check -> dvoffmismatch; skip the
cardinality check -> dvbadcount; drop the v3 gate -> dvv2; drop the dup check
-> dvdup. Value arms cross-checked against pyiceberg (which reads DVs) and
DuckDB, extending the 4b crosscheck script.

### 4c adversarial audit (3 lenses, 9 findings -> 4 distinct defects, all fixed)

The audit (spec / memory / hostile-bytes-decode lenses, each finding verified
against the code) ran after GREEN; the spec and decode lenses independently
found three of the four, and zero findings were refuted. Fixed here, each with
a fixture arm and a removal proof:

- **BLOCKING: int64 overflow in the Puffin blob bounds check.**
  `blob_offset + blob_size > paystart - 4` wrapped negative for attacker-sized
  operands (both come from the manifest, cross-checked only against a footer
  the same author wrote), passing the check into `bp = buf + blob_offset`, a
  wild pointer `pf_be32` dereferenced -- a real SIGSEGV (the pre-fix build
  crashes on the `dvbigoff` fixture). Fixed by testing each operand against the
  file bound without adding them.
- **IMPORTANT: the parsed Puffin footer (payload copy + jsonb, up to the 64 MB
  cap) was retained in the query context per DV entry**, because the reader was
  called after switching back to the outer context. A snapshot with many DVs,
  or many DVs sharing one large-footer Puffin file, retained O(entries x
  footer). Fixed by decoding inside the per-file scratch context and copying
  only the ordinal array out before the reset.
- **IMPORTANT: a roaring run container with start + length > 0xFFFF** carried
  into the container-key bits and fabricated ordinals in a neighboring
  container -- silently wrong rows, CRC-valid. Fixed with a bounds check
  (`dvrunoverflow`, XX001).
- **MINOR: the one-DV-per-data-file check compared raw strings** while the
  per-file merge strips the URI scheme, so an aliased pair (one `file://`, one
  not) evaded the check and was unioned. Fixed by stripping the scheme on both
  sides (`dvdupscheme`, XX001).

Removal proofs: restoring the additive bounds check crashes `dvbigoff` again;
deleting the run-range check reds `dvrunoverflow`; reverting the dedup to a raw
strcmp reds `dvdupscheme`. PG17/18/19 + ASAN clean over all arms including the
three formerly-crashing ones.
