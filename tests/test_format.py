import os
import tempfile
import unittest
from io import BytesIO, RawIOBase
from pathlib import Path

from tests.support import GOLDEN_CATALOG, GOLDEN_ENTRIES, GOLDEN_HEADER


class CodecTests(unittest.TestCase):
    def test_path_encoding(self):
        from sourcediff_py.format import decode_bytes, encode_bytes

        raw = b"sub/a%\t\n\r\xff"
        self.assertEqual(encode_bytes(raw), "sub/a%25%09%0A%0D%FF")
        self.assertEqual(decode_bytes(encode_bytes(raw)), raw)

    def test_codec_round_trips_all_byte_values(self):
        from sourcediff_py.format import decode_bytes, encode_bytes

        raw = bytes(range(256))
        self.assertEqual(decode_bytes(encode_bytes(raw)), raw)

    def test_codec_rejects_malformed_or_noncanonical_values(self):
        from sourcediff_py.format import FormatError, decode_bytes

        for value in ("%", "%0", "%GG", "%0a", "%41", "%2F", "%20", "a\tb", "é", "\udcff"):
            with self.subTest(value=value), self.assertRaises(FormatError):
                decode_bytes(value)

    def test_directory_child_frame(self):
        from sourcediff_py.format import directory_child_frame

        digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        self.assertEqual(
            directory_child_frame("file", b"a\n%\xff", digest),
            b"file\x00a%0A%25%FF\x00"
            b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\x00",
        )


class CatalogTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.work = Path(workspace.name)
        self.source = self.work / "baseline.txt"

    def read(self, data, *, require_complete=False):
        from sourcediff_py.format import read_catalog

        self.source.write_bytes(data)
        return read_catalog(self.source, self.work, require_complete=require_complete)

    def test_golden_catalog_is_parsed_as_byte_paths_and_ordered_settings(self):
        from sourcediff_py.format import iter_entries

        settings, entry_spool, error_spool, complete = self.read(
            GOLDEN_CATALOG, require_complete=True
        )
        self.assertTrue(complete)
        self.assertEqual(settings["hash"], "sha256")
        self.assertEqual(settings["exclude"], ["first\t\udcff", "last"])
        self.assertEqual(settings["exclude-dir"], [])
        self.assertEqual(settings["exclude-ext"], [])
        self.assertEqual(entry_spool.read_bytes(), GOLDEN_ENTRIES)
        self.assertEqual(error_spool.read_bytes(), b"")
        entries = list(iter_entries(entry_spool))
        self.assertEqual([entry["path"] for entry in entries], [
            b"$(touch sentinel)", b".", b"link", b"sub", b"sub/a%\t\n\r\xff", b"\xff"
        ])
        self.assertEqual(entries[0]["mtime_ns"], -2)
        self.assertIsNone(entries[1]["size"])
        self.assertEqual(entries[2]["link_target"], b"-")
        self.assertIsNone(entries[3]["precision_ns"])
        self.assertEqual(entries[4]["precision_ns"], 1000000000)

    def test_catalog_stream_uses_current_position_without_closing_input(self):
        from sourcediff_py.format import read_catalog_stream

        with BytesIO(b"prefix" + GOLDEN_CATALOG) as source:
            source.seek(len(b"prefix"))
            settings, entry_spool, error_spool, complete = read_catalog_stream(
                source, self.work, require_complete=True
            )
            self.assertFalse(source.closed)
        self.assertTrue(complete)
        self.assertEqual(settings["exclude"], ["first\t\udcff", "last"])
        self.assertEqual(entry_spool.read_bytes(), GOLDEN_ENTRIES)
        self.assertEqual(error_spool.read_bytes(), b"")

    def test_invalid_header_does_not_read_a_newline_free_prefix(self):
        from sourcediff_py.format import FormatError, read_catalog_stream

        with BytesIO(b"SOURCEDIFF\t1" + b"x" * (1024 * 1024)) as source:
            with self.assertRaises(FormatError):
                read_catalog_stream(source, self.work)
            self.assertLessEqual(source.tell(), len(b"SOURCEDIFF\t1\n"))
        self.assertEqual(list(self.work.iterdir()), [])

    def test_catalog_stream_accepts_short_raw_reads(self):
        from sourcediff_py.format import read_catalog_stream

        class ShortReader(RawIOBase):
            def __init__(self, data):
                self.data = BytesIO(data)

            def readable(self):
                return True

            def readinto(self, buffer):
                block = self.data.read(min(len(buffer), 1))
                buffer[:len(block)] = block
                return len(block)

        with ShortReader(GOLDEN_CATALOG) as source:
            settings, entries, errors, complete = read_catalog_stream(
                source, self.work, require_complete=True
            )
            self.assertFalse(source.closed)
        self.assertTrue(complete)
        self.assertEqual(settings["hash"], "sha256")
        self.assertEqual(entries.read_bytes(), GOLDEN_ENTRIES)
        self.assertEqual(errors.read_bytes(), b"")

    def test_unknown_setting_diagnostic_escapes_control_characters(self):
        from sourcediff_py.format import FormatError, write_catalog

        key = "unrecognized\x1b[2J\r\x7f"
        with self.assertRaises(FormatError) as read_error:
            self.read(GOLDEN_CATALOG.replace(b"S\thash\t", b"S\t" + key.encode() + b"\t"))
        settings, entries, errors, complete = self.read(GOLDEN_CATALOG)
        with self.assertRaises(FormatError) as write_error:
            write_catalog(self.work / "written", {**settings, key: "value"}, entries, errors, complete)
        for error in (read_error.exception, write_error.exception):
            with self.subTest(error=error):
                self.assertTrue(str(error).isprintable())

    def test_shell_looking_path_and_setting_are_data(self):
        previous = Path.cwd()
        os.chdir(self.work)
        try:
            data = GOLDEN_CATALOG.replace(b"S\texclude\tlast", b"S\texclude\t$(touch sentinel)")
            settings, *_ = self.read(data, require_complete=True)
            self.assertEqual(settings["exclude"][-1], "$(touch sentinel)")
            self.assertFalse(Path("sentinel").exists())
        finally:
            os.chdir(previous)

    def test_writer_reproduces_golden_catalog_from_spools(self):
        from sourcediff_py.format import encode_entry, iter_entries, write_catalog

        settings, entry_spool, error_spool, complete = self.read(GOLDEN_CATALOG)
        recreated = self.work / "entries.txt"
        with recreated.open("wb") as stream:
            for entry in iter_entries(entry_spool):
                stream.write(encode_entry(entry))
        destination = self.work / "written.txt"
        write_catalog(destination, settings, recreated, error_spool, complete)
        self.assertEqual(destination.read_bytes(), GOLDEN_CATALOG)

    def test_entry_serializer_keeps_missing_and_literal_dash_targets_distinct(self):
        from sourcediff_py.format import FormatError, encode_entry, iter_entries

        _, entry_spool, _, _ = self.read(GOLDEN_CATALOG)
        file, _, link, *_ = iter_entries(entry_spool)
        with self.assertRaises(FormatError):
            encode_entry(dict(file, link_target=b"-"))
        with self.assertRaises(FormatError):
            encode_entry(dict(link, link_target=None))

    def test_incomplete_error_catalog_can_be_read_for_diagnostics(self):
        from sourcediff_py.format import FormatError, encode_error, iter_errors, write_catalog

        header = GOLDEN_HEADER.replace(b"S\tcomplete\tcomplete", b"S\tcomplete\tincomplete")
        expected = b"X\tbad%0A\tread link\tvanished%0D%FF\tsubtree\n"
        data = header + expected + b"END\tincomplete\t0\t1\n"
        settings, entry_spool, error_spool, complete = self.read(data)
        self.assertFalse(complete)
        self.assertEqual(settings["complete"], "incomplete")
        self.assertEqual(entry_spool.read_bytes(), b"")
        self.assertEqual(error_spool.read_bytes(), expected)
        error, = iter_errors(error_spool)
        self.assertEqual(
            error, {"path": b"bad\n", "operation": "read link", "message": b"vanished\r\xff",
                    "scope": "subtree"}
        )
        self.assertEqual(encode_error(error), expected)
        destination = self.work / "written.txt"
        write_catalog(destination, settings, entry_spool, error_spool, complete)
        self.assertEqual(destination.read_bytes(), data)
        with self.assertRaises(FormatError):
            self.read(data, require_complete=True)

    def test_malformed_catalogs_leave_no_partial_spools(self):
        from sourcediff_py.format import FormatError

        failures = {
            "unsupported version": GOLDEN_CATALOG.replace(b"SOURCEDIFF\t1", b"SOURCEDIFF\t2"),
            "extra record": GOLDEN_CATALOG + b"\n",
            "CRLF": GOLDEN_CATALOG.replace(b"\n", b"\r\n"),
            "missing trailer": GOLDEN_CATALOG.rsplit(b"END", 1)[0],
            "missing final LF": GOLDEN_CATALOG[:-1],
            "incorrect count": GOLDEN_CATALOG.replace(b"END\tcomplete\t6", b"END\tcomplete\t5"),
            "inconsistent status": GOLDEN_CATALOG.replace(b"END\tcomplete", b"END\tincomplete"),
            "signed count": GOLDEN_CATALOG.replace(b"\t6\t0\n", b"\t+6\t0\n"),
            "oversized count": GOLDEN_CATALOG.replace(b"\t6\t0\n", b"\t9223372036854775808\t0\n"),
            "missing setting": GOLDEN_CATALOG.replace(b"S\thash\tsha256\n", b""),
            "unknown setting": GOLDEN_CATALOG.replace(b"S\thash", b"S\tvariable"),
            "duplicate scalar": GOLDEN_CATALOG.replace(
                b"S\thash\tsha256\n", b"S\thash\tsha256\nS\thash\tblake3\n"
            ),
            "invalid boolean": GOLDEN_CATALOG.replace(b"S\trecursive\ttrue", b"S\trecursive\t1"),
            "invalid depth": GOLDEN_CATALOG.replace(b"S\tmax-depth\t-", b"S\tmax-depth\t01"),
            "invalid hash algorithm": GOLDEN_CATALOG.replace(b"sha256\n", b"sha1\n"),
            "invalid symlink mode": GOLDEN_CATALOG.replace(b"\trecord\n", b"\tresolve\n"),
            "invalid engine": GOLDEN_CATALOG.replace(b"\tpython\n", b"\tunknown\n"),
            "empty pattern": GOLDEN_CATALOG.replace(b"\tlast\n", b"\t\n"),
            "NUL in pattern": GOLDEN_CATALOG.replace(b"\tlast\n", b"\t%00\n"),
            "unknown record": GOLDEN_CATALOG.replace(b"E\t.\t", b"Q\t.\t"),
            "record shape": GOLDEN_CATALOG.replace(b"E\t.\tdirectory", b"E\t.\tdirectory\textra"),
            "setting after entries": GOLDEN_CATALOG.replace(b"E\tsub\t", b"S\texclude\tx\nE\tsub\t"),
            "duplicate path": GOLDEN_CATALOG.replace(b"E\t%FF\t", b"E\tsub/a%25%09%0A%0D%FF\t"),
            "missing root": GOLDEN_CATALOG.replace(b"E\t.\tdirectory\t-\t-\t-2\t1\t-\tunverified\n", b"")
                .replace(b"END\tcomplete\t6", b"END\tcomplete\t5"),
            "file root": GOLDEN_CATALOG.replace(b"E\t.\tdirectory", b"E\t.\tfile"),
            "out of order": GOLDEN_CATALOG.replace(b"E\t%FF\t", b"E\t!\t"),
            "malformed escape": GOLDEN_CATALOG.replace(b"E\t%FF\t", b"E\t%\t"),
            "noncanonical escape": GOLDEN_CATALOG.replace(b"E\t%FF\t", b"E\t%ff\t"),
            "decoded NUL": GOLDEN_CATALOG.replace(b"E\t%FF\t", b"E\t%FF%00\t"),
            "absolute path": GOLDEN_CATALOG.replace(b"E\t%FF\t", b"E\t/%FF\t"),
            "empty path": GOLDEN_CATALOG.replace(b"E\t%FF\t", b"E\t\t"),
            "dot component": GOLDEN_CATALOG.replace(b"E\t%FF\t", b"E\t%FF/.\t"),
            "parent component": GOLDEN_CATALOG.replace(b"E\t%FF\t", b"E\t%FF/..\t"),
            "trailing slash": GOLDEN_CATALOG.replace(b"E\t%FF\t", b"E\t%FF/\t"),
            "empty component": GOLDEN_CATALOG.replace(b"E\t%FF\t", b"E\t%FF//a\t"),
            "wrong digest length": GOLDEN_CATALOG.replace(b"ba7816bf", b"ba7816b"),
            "uppercase digest": GOLDEN_CATALOG.replace(b"ba7816bf", b"BA7816BF"),
            "missing complete digest": GOLDEN_CATALOG.replace(
                b"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", b"-"
            ).replace(b"\t3\t-2\t1\t-\tverified", b"\t3\t-2\t1\t-\tunverified"),
            "missing complete directory digest": GOLDEN_CATALOG.replace(
                b"S\tdirectory-hashes\tfalse", b"S\tdirectory-hashes\ttrue"
            ),
            "unknown kind": GOLDEN_CATALOG.replace(b"E\t%FF\tfile", b"E\t%FF\tsocket"),
            "unknown verification": GOLDEN_CATALOG.replace(b"\tunverified\n", b"\tdifferent\n"),
            "invalid size": GOLDEN_CATALOG.replace(b"\t3\t-2\t", b"\t-1\t-2\t"),
            "oversized mtime": GOLDEN_CATALOG.replace(b"\t-2\t1\t", b"\t9223372036854775808\t1\t"),
            "invalid precision": GOLDEN_CATALOG.replace(b"\t-2\t1\t", b"\t-2\t0\t"),
            "precision without mtime": GOLDEN_CATALOG.replace(b"\t-2\t1\t", b"\t-\t1\t"),
            "link target on file": GOLDEN_CATALOG.replace(b"\t3\t-2\t1\t-\t", b"\t3\t-2\t1\ta\t"),
            "link policy mismatch": GOLDEN_CATALOG.replace(b"S\tsymlinks\trecord", b"S\tsymlinks\tfollow"),
            "error scope": GOLDEN_HEADER.replace(b"S\tcomplete\tcomplete", b"S\tcomplete\tincomplete")
                + b"X\tx\top\tmessage\tother\nEND\tincomplete\t0\t1\n",
            "errors in complete catalog": GOLDEN_HEADER + GOLDEN_ENTRIES
                + b"X\tx\top\tmessage\tentry\nEND\tcomplete\t6\t1\n",
        }
        for name, data in failures.items():
            with self.subTest(name=name):
                with self.assertRaises(FormatError):
                    self.read(data)
                self.assertEqual(list(self.work.iterdir()), [self.source])

    def test_writer_rejects_bad_spool_without_emitting_success_trailer(self):
        from sourcediff_py.format import FormatError, write_catalog

        settings, entry_spool, error_spool, complete = self.read(GOLDEN_CATALOG)
        entry_spool.write_bytes(GOLDEN_ENTRIES + GOLDEN_ENTRIES)
        destination = self.work / "written.txt"
        with self.assertRaises(FormatError):
            write_catalog(destination, settings, entry_spool, error_spool, complete)
        self.assertNotIn(b"END\tcomplete", destination.read_bytes())

    def test_incomplete_directory_hashes_may_be_absent(self):
        catalog = GOLDEN_CATALOG.replace(
            b"S\tdirectory-hashes\tfalse", b"S\tdirectory-hashes\ttrue"
        ).replace(
            b"S\tcomplete\tcomplete", b"S\tcomplete\tincomplete"
        ).replace(b"END\tcomplete", b"END\tincomplete")
        _, entry_spool, _, complete = self.read(catalog)
        self.assertFalse(complete)
        self.assertEqual(entry_spool.read_bytes(), GOLDEN_ENTRIES)
