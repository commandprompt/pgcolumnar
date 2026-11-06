"""A columnar table writes the native format, judged by the catalog rows it produces,
and DROP clears them (#1341). The pytest twin of `test/native_writer.sh`, every check
by the shell's name.

5000 rows, a quarter of them NULL in v, under a 1000-row stripe limit: one storage row
at format version 2 with 1024-value vectors, five row groups holding 5000 rows, and ten
column chunks (five groups, two columns) counting 10000 values. Dropping the table
deletes the storage, row-group and column-chunk rows for its storage id. Like the shell
it reads no data back; native_roundtrip does that.
"""


def _one(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def test_the_native_writer_by_its_catalog_rows(pgc_conn, expect):
    with pgc_conn.cursor() as cur:
        cur.execute("CREATE TABLE nw (id int, v text) USING pgcolumnar")
        cur.execute("SELECT pgcolumnar.set_options('nw', stripe_row_limit => 1000)")
        cur.execute("INSERT INTO nw SELECT g, CASE WHEN g % 4 = 0 THEN NULL ELSE 'v'||g END "
                    "FROM generate_series(1, 5000) g")
        sid = _one(cur, "SELECT pgcolumnar.get_storage_id('nw')")
        of = lambda sql: _one(cur, sql.format(sid=sid))  # noqa: E731
        expect.num(of("SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = {sid}"), 1,
                   "native storage row written")
        expect.num(of("SELECT format_version FROM pgcolumnar.storage WHERE storage_id = {sid}"), 2,
                   "native format version recorded")
        expect.num(of("SELECT vector_length FROM pgcolumnar.storage WHERE storage_id = {sid}"), 1024,
                   "native vector length recorded")
        expect.num(of("SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {sid}"), 5,
                   "native row groups (5000 rows / 1000 limit)")
        expect.num(int(of("SELECT sum(row_count) FROM pgcolumnar.row_group WHERE storage_id = {sid}")), 5000,
                   "native row-group row total")
        expect.num(of("SELECT count(*) FROM pgcolumnar.column_chunk WHERE storage_id = {sid}"), 10,
                   "native column chunks (5 groups x 2 cols)")
        expect.num(int(of("SELECT sum(value_count) FROM pgcolumnar.column_chunk WHERE storage_id = {sid}")), 10000,
                   "native column-chunk value total")

        cur.execute("DROP TABLE nw")
        expect.num(of("SELECT count(*) FROM pgcolumnar.storage WHERE storage_id = {sid}"), 0,
                   "drop clears native storage row")
        expect.num(of("SELECT count(*) FROM pgcolumnar.row_group WHERE storage_id = {sid}"), 0,
                   "drop clears native row groups")
        expect.num(of("SELECT count(*) FROM pgcolumnar.column_chunk WHERE storage_id = {sid}"), 0,
                   "drop clears native column chunks")
