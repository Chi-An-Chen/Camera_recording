import errno
import os
import signal
import struct
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import recording_archive as archive


def box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


FTYP = box(b"ftyp", b"isom\0\0\0\0isom")
MOOV = box(b"moov", box(b"mvhd", b"metadata"))
MDAT = box(b"mdat", b"video")
MP4 = FTYP + MOOV + MDAT
CAM = "192_0_2_3"
OLD = "20260101_090000.mp4"
LAST = "20260101_091000.mp4"


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.camera = self.root / CAM
        self.camera.mkdir()
        if sys.platform.startswith("linux"):
            self.addCleanup(signal.signal, signal.SIGIO, signal.getsignal(signal.SIGIO))
        archive.prepare_writer_checks()

    def inventory(self):
        return {f["name"]: f for f in archive.list_files(str(self.root), CAM)}

    def delete(self, names, expected=None):
        return archive.delete_files(str(self.root), CAM, names, expected)

    def test_inventory_handles_stopped_last_clip_and_incomplete_older_files(self):
        (self.camera / LAST).write_bytes(MP4)
        (self.camera / OLD).write_bytes(FTYP + box(b"mdat", b"moov in video data"))
        entries = archive.list_files(str(self.root), CAM)
        self.assertEqual([f["name"] for f in entries], [LAST, OLD])
        self.assertEqual((entries[0]["day"], entries[0]["size"]), ("2026-01-01", len(MP4)))
        self.assertTrue(entries[0]["closed"])
        self.assertFalse(entries[0]["recording"])
        self.assertEqual(entries[1]["reason"], "incomplete")
        self.assertTrue(entries[1]["recording"])
        self.assertEqual(self.delete([OLD])["skipped"], [OLD])

    def test_stalled_writer_survives_single_and_day_delete_after_closed_snapshot(self):
        old, last = self.camera / OLD, self.camera / LAST
        old.write_bytes(MP4)
        last.write_bytes(MP4)
        stale = time.time() - 25
        os.utime(str(last), (stale, stale))
        expected = {name: row["identity"] for name, row in self.inventory().items()}
        with last.open("ab"):
            self.assertEqual(self.inventory()[LAST]["reason"], "open")
            one = self.delete([LAST], expected)
            self.assertEqual((one["deleted"], one["reasons"]), ([], {LAST: "open"}))
            day = self.delete([OLD, LAST], expected)
            self.assertEqual((day["deleted"], day["skipped"]), ([OLD], [LAST]))
            self.assertEqual(last.read_bytes(), MP4)
        self.assertEqual(self.delete([LAST], expected)["deleted"], [LAST])

    def test_delete_rechecks_selected_camera_name_and_confirmation_identity(self):
        last = self.camera / LAST
        last.write_bytes(MP4)
        expected = {LAST: self.inventory()[LAST]["identity"]}
        other = self.root / "192_0_2_4"
        other.mkdir()
        (other / LAST).write_bytes(MP4)
        new = self.camera / "new"
        new.write_bytes(MP4)
        os.replace(str(new), str(last))
        self.assertEqual(self.delete([LAST], expected)["reasons"], {LAST: "changed"})
        self.assertEqual(self.delete([LAST], {})["reasons"], {LAST: "changed"})
        with self.assertRaises(FileNotFoundError):
            archive.open_clip(str(self.root), CAM, LAST, expected=expected[LAST])
        with archive.open_clip(str(self.root), CAM, LAST) as clip:
            self.assertEqual(clip.read(), MP4)
        self.assertTrue(last.exists())
        self.assertTrue((other / LAST).exists())
        self.assertEqual(self.delete([LAST, LAST])["deleted"], [LAST])
        self.assertTrue((other / LAST).exists())

    def test_unsafe_paths_missing_files_and_non_regular_entries(self):
        outside = self.root / "original.mp4"
        outside.write_bytes(MP4)
        (self.camera / LAST).symlink_to(outside)
        os.mkfifo(str(self.camera / OLD))
        self.assertEqual(self.inventory(), {})
        for name in (LAST, OLD, "../original.mp4"):
            with self.assertRaises((OSError, ValueError)):
                archive.open_clip(str(self.root), CAM, name)
        missing = "20260101_080000.mp4"
        names = [LAST, OLD, "../original.mp4", missing]
        self.assertEqual(self.delete(names)["reasons"],
                         {LAST: "invalid", OLD: "invalid", "../original.mp4": "invalid", missing: "missing"})
        alias = "192_0_2_5"
        (self.root / alias).symlink_to(self.camera, target_is_directory=True)
        self.assertEqual(archive.list_files(str(self.root), alias), [])
        self.assertEqual(archive.delete_files(str(self.root), alias, [LAST])["deleted"], [])
        self.assertEqual(outside.read_bytes(), MP4)

    def test_truncated_fragmented_and_unsized_boxes_stay_protected(self):
        path = self.camera / LAST
        large_mdat = struct.pack(">I4sQ", 1, b"mdat", 17) + b"x"
        for content, complete in (
                (FTYP + large_mdat + MOOV, True),
                (FTYP + MOOV + MDAT[:-1], False),
                (FTYP + MOOV + struct.pack(">I4s", 0, b"mdat") + b"data", False),
                (FTYP + MOOV + box(b"moof", b"x") + MDAT, False)):
            with self.subTest(content=content):
                path.write_bytes(content)
                with path.open("rb") as clip:
                    self.assertEqual(archive._has_trailer(clip.fileno(), len(content)), complete)

    def test_list_caches_only_trailer_evidence_and_delete_reads_it_again(self):
        path = self.camera / LAST
        path.write_bytes(MP4)
        with patch.object(archive, "_has_trailer", wraps=archive._has_trailer) as trailer:
            self.assertTrue(self.inventory()[LAST]["closed"])
            self.assertTrue(self.inventory()[LAST]["closed"])
            self.assertEqual(trailer.call_count, 1)
            self.assertEqual(self.delete([LAST])["deleted"], [LAST])
            self.assertEqual(trailer.call_count, 2)

    def test_path_replaced_during_delete_is_retained(self):
        path = self.camera / LAST
        path.write_bytes(MP4)
        new = self.camera / "replacement"
        new.write_bytes(MP4)

        def complete(fd, info, fresh):
            os.replace(str(new), str(path))
            return True

        with patch.object(archive, "_complete", side_effect=complete):
            self.assertEqual(self.delete([LAST])["reasons"], {LAST: "changed"})
        self.assertTrue(path.exists())

    def test_unavailable_writer_check_and_pending_linux_lease_break_skip(self):
        path = self.camera / LAST
        path.write_bytes(MP4)
        with patch.object(sys, "platform", "darwin"), \
                patch.object(archive, "_mac_writers", return_value=None):
            self.assertEqual(self.delete([LAST])["reasons"], {LAST: "unverified"})
        with patch.object(sys, "platform", "linux"), \
                patch.object(signal, "getsignal", return_value=signal.SIG_IGN), \
                patch.object(archive.fcntl, "F_SETLEASE", 1024, create=True), \
                patch.object(archive.fcntl, "F_GETLEASE", 1025, create=True), \
                patch.object(archive.fcntl, "fcntl") as lease:
            lease.side_effect = PermissionError(errno.EPERM, "not allowed")
            self.assertEqual(self.delete([LAST])["reasons"], {LAST: "unverified"})
            lease.side_effect = [0, archive.fcntl.F_UNLCK]
            self.assertEqual(self.delete([LAST])["reasons"], {LAST: "open"})
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
