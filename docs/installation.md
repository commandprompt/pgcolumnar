# Installation

pgColumnar builds with PGXS against an installed PostgreSQL server. The supported
versions are 15 through 18. The project validates version 19 against 19beta2. Validation
against the final 19 release is not done yet. PostgreSQL 13 and 14 build, but
they are not in the tested matrix.

## Requirements

- A PostgreSQL server and its development headers. The build finds them through
  `pg_config`.
- A C compiler and `make`.
- `pkg-config`. The build uses it to find the optional compression libraries.
- Optional: the `liblz4` and `libzstd` development libraries. If they are
  available, the build includes the `lz4` and `zstd` codecs. If they are not
  available, the build removes those codecs. A columnar table that requests a
  removed codec then uses a codec that is available.
- Optional: the `zlib` development library. If it is available, the Parquet
  reader decodes GZIP-compressed pages. The native table format does not use
  `zlib`.
- A little-endian host, for the Arrow and Parquet functions only. These functions
  are `read_parquet`, `parquet_schema`, the import and export functions, and the
  `pgcolumnar_parquet` foreign-data wrapper. All other parts of the extension
  operate on each host that PostgreSQL supports.

The replacement of a removed codec applies to the native table format only. If an
external Parquet file contains a page that uses a codec that the build removed,
the read fails with a decode error. The read does not use a different codec. For
the codecs that the reader supports, refer to [limitations.md](limitations.md).

## Build and install

Set the build to the `pg_config` of the target server:

```sh
make PG_CONFIG=/path/to/pg_config
make install PG_CONFIG=/path/to/pg_config
```

`make install` copies `pgcolumnar.so`, the control file, and the SQL script. It
puts them in the library directory and the extension directory of the server.

### Servers built from source

Build the extension with the compiler that configured the server.

PGXS gives the extension the compiler flags the server was configured with, and
PostgreSQL's `configure` adapts those flags to its compiler. A server configured
with a newer GCC therefore records flags an older GCC does not accept. GCC 15
records `-Wmissing-variable-declarations`, which GCC 13 rejects. The build then
stops on a flag you did not set:

```
gcc-13: error: unrecognized command-line option '-Wmissing-variable-declarations'
```

Name the compiler explicitly when the server was configured with one that is not
your default:

```sh
make CC=gcc-14 PG_CONFIG=/path/to/pg_config
```

This does not apply to packaged servers. Their flags come from the package
build, and the distribution compiler accepts them.

## Load the library

pgColumnar installs planner hooks and executor hooks when the server loads the
library. Add the extension to `shared_preload_libraries`, then start the server
again:

```
shared_preload_libraries = 'pgcolumnar'
```

Set this parameter in `postgresql.conf`. As an alternative, use `ALTER SYSTEM SET
shared_preload_libraries = 'pgcolumnar'`. Then start the server again. If the
parameter contains other libraries, add `pgcolumnar` to the list. Commas divide
the items in the list.

## Create the extension

Do this in each database that will contain columnar tables:

```sql
CREATE EXTENSION pgcolumnar;
```

This command creates the `pgcolumnar` schema, the `pgcolumnar` table access
method, the catalog tables, and the `pgcolumnar.*` functions. The extension is
not relocatable. Its objects stay in the `pgcolumnar` schema.

## Verify

```sql
-- the access method is registered
SELECT amname FROM pg_am WHERE amname = 'pgcolumnar';

-- you can create a columnar table and read it
CREATE TABLE install_check (id int, v text) USING pgcolumnar;
INSERT INTO install_check VALUES (1, 'ok');
SELECT * FROM install_check;
DROP TABLE install_check;
```

## Upgrade

To install a new build of the extension:

1. Run `make install` with the same `PG_CONFIG`.
2. Start the server again, so that it loads the new library.
3. Run `ALTER EXTENSION pgcolumnar UPDATE;` **in every database that has the
   extension**.

Step 3 is not optional, and it is easy to miss because nothing prompts for it.
The first two steps replace the shared library. The third updates the catalog to
match it.

```sql
-- in each database that ran CREATE EXTENSION pgcolumnar
ALTER EXTENSION pgcolumnar UPDATE;
SELECT extversion FROM pg_extension WHERE extname = 'pgcolumnar';
```

To find the databases that need it, list the databases you can connect to:

```sql
SELECT datname FROM pg_database WHERE datallowconn;
```

Then connect to each one and check whether it has the extension. A query cannot
do this from one database, because `pg_extension` lists only the extensions of
the database you are connected to:

```sql
SELECT extversion FROM pg_extension WHERE extname = 'pgcolumnar';
```

### If you skipped step 3

Every function the extension installs records the name of a C symbol. When those
names change between builds, the recorded names no longer resolve, and the
extension stops working until the catalog is updated. Reading an existing
columnar table then fails:

```
ERROR:  could not find function "columnar_handler" in file ".../pgcolumnar.so"
```

The fix is step 3. Run `ALTER EXTENSION pgcolumnar UPDATE;` in that database and
the error goes away. **Your data is not affected.** The tables are intact and no
conversion happens. Only the catalog entry is stale.

Between releases that add a catalog table, the new library can also need a table
the old catalog does not have. Upgrading from `1.0-alpha4` to `1.0-alpha5`,
`DROP TABLE`, `TRUNCATE` and `pgcolumnar.vacuum()` on a columnar table fail until
step 3, and change nothing; reads and writes keep working:

```
ERROR:  columnar metadata table "pgcolumnar.retired_group" does not exist
HINT:  The pgcolumnar catalog in this database may be older than the installed library. Run ALTER EXTENSION pgcolumnar UPDATE in this database.
```

The fix is the same: step 3.

Do not use `DROP EXTENSION` instead. A plain `DROP EXTENSION pgcolumnar` fails
while columnar tables exist, because they depend on the access method. The
form that succeeds is `DROP EXTENSION pgcolumnar CASCADE`, and it drops every
columnar table with it.

### Upgrading to 1.0-alpha5

`1.0-alpha5`, recorded in `VERSION`, is what this source tree installs. The
latest published pre-release is `v1.0-alpha4`.
`ALTER EXTENSION pgcolumnar UPDATE` (step 3 above) reaches `1.0-alpha5` from every
previously published version: `1.0-dev`, which the `v1.0-alpha` tag installed,
`1.0-alpha`, `1.0-alpha2`, `1.0-alpha3`, and `1.0-alpha4`. PostgreSQL applies
the shipped upgrade scripts in sequence. One command therefore carries a
`1.0-dev` install through `1.0-alpha`, `1.0-alpha2`, `1.0-alpha3` and
`1.0-alpha4` to `1.0-alpha5`.

The `1.0-alpha` cycle renamed the extension's C symbols into the `pgcolumnar`
namespace, so that two extensions named `columnar` can be loaded without
colliding. That rename is why the update is mandatory rather than cosmetic. The
recorded symbol names must be rewritten to match the new library, and only
`ALTER EXTENSION UPDATE` does that. The SQL you write does not change. Function
names, settings and table syntax are all the same.

The source records the on-disk format version. The specification also records it,
in [../design/NATIVE_FORMAT_AND_INTERFACE_SPEC.md](https://github.com/commandprompt/pgcolumnar/blob/main/design/NATIVE_FORMAT_AND_INTERFACE_SPEC.md).
A build reads the tables that earlier builds wrote, from format version 1 up to the
version that it writes. A conversion is not necessary. The reverse is not true. A
table that a newer build has written to is refused by an older build. See
[limitations.md](limitations.md) for the details.

## Remove

First drop the extension from a database. Then remove the files, but only if no
database uses the extension:

```sql
DROP EXTENSION pgcolumnar;   -- fails if columnar tables still exist; drop them first
```

To unload the library, remove `pgcolumnar` from `shared_preload_libraries` and
start the server again. Do this only after you drop all columnar tables. A read
of a columnar table needs the access method, and the access method is not
available when the library is not loaded.
