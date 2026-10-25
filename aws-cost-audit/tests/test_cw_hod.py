"""Hour-of-day aggregation (D5): the AWS CLI prints local-time timestamps; the profile must honour the offset, not shift twice."""
import contextlib
import datetime
import importlib.util
import io
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "cw_hod.py"
spec = importlib.util.spec_from_file_location("cw_hod", SCRIPT)
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)


def run_profile(points, tz):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cw.profile(points, "t", "%.1f", tz, 1)
    return buf.getvalue()


class Profile(unittest.TestCase):
    def test_local_offset_timestamp_is_not_shifted_again(self):
        # 18:00-03:00 is 18h local; the table row for 18 must hold the value, row 15 must be empty.
        t = datetime.datetime.fromisoformat("2026-10-23T18:00:00-03:00")
        out = run_profile([(t, 9.0)], -3)
        rows = {line[:2]: line for line in out.splitlines() if line[:2].isdigit()}
        self.assertIn("9.0", rows["18"])
        self.assertNotIn("9.0", rows["15"])

    def test_utc_timestamp_is_converted_to_local(self):
        t = datetime.datetime.fromisoformat("2026-10-23T21:00:00+00:00")  # 18h at UTC-3
        out = run_profile([(t, 4.0)], -3)
        rows = {line[:2]: line for line in out.splitlines() if line[:2].isdigit()}
        self.assertIn("4.0", rows["18"])

    def test_weekday_and_weekend_columns(self):
        fri = datetime.datetime.fromisoformat("2026-10-23T12:00:00+00:00")  # Friday
        sat = datetime.datetime.fromisoformat("2026-10-24T12:00:00+00:00")  # Saturday
        out = run_profile([(fri, 2.0), (sat, 6.0)], 0)
        row = [line for line in out.splitlines() if line.startswith("12")][0]
        cols = row.split()
        self.assertEqual(cols[1:], ["4.0", "2.0", "6.0"])  # all, weekday, weekend

    def test_no_points(self):
        out = run_profile([], -3)
        self.assertIn("no datapoints", out)


if __name__ == "__main__":
    unittest.main()
