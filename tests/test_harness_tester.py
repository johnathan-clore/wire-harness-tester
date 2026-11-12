"""Run with:  python3 -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import harness_tester as ht  # noqa: E402

WIRES = [("1", "A8"), ("2", "A7"), ("3", "A6"), ("4", "A5")]


def statuses(results):
    return {a: s for a, _, s, _ in results}


class TestContinuity(unittest.TestCase):
    def test_good_harness_passes(self):
        res = ht.run_test(WIRES, ht.SimulatedHarness(WIRES))
        self.assertTrue(all(s == "PASS" for s in statuses(res).values()))

    def test_open(self):
        hw = ht.SimulatedHarness(WIRES)
        hw.links.discard(frozenset([("A", "1"), ("B", "A8")]))
        self.assertEqual(statuses(ht.run_test(WIRES, hw))["1"], "OPEN")

    def test_miswire(self):
        hw = ht.SimulatedHarness(WIRES)
        hw.links -= {frozenset([("A", "1"), ("B", "A8")]), frozenset([("A", "2"), ("B", "A7")])}
        hw.links |= {frozenset([("A", "1"), ("B", "A7")]), frozenset([("A", "2"), ("B", "A8")])}
        s = statuses(ht.run_test(WIRES, hw))
        self.assertEqual((s["1"], s["2"]), ("MISWIRE", "MISWIRE"))

    def test_short(self):
        hw = ht.SimulatedHarness(WIRES)
        hw.links.add(frozenset([("A", "3"), ("A", "4")]))
        s = statuses(ht.run_test(WIRES, hw))
        self.assertEqual((s["3"], s["4"]), ("SHORT", "SHORT"))

    def test_splice_is_not_a_short(self):
        wires = [("1", "A1"), ("1", "A2")]  # one pin spliced to two
        res = ht.run_test(wires, ht.SimulatedHarness(wires))
        self.assertTrue(all(s == "PASS" for s in statuses(res).values()))


class TestA620Classes(unittest.TestCase):
    def test_class1_needs_no_hv(self):
        r = ht.build_requirements(1)
        self.assertIsNone(r["dwv"])
        self.assertIsNone(r["ir"])
        self.assertIsNone(r["continuity_max_ohms"])
        self.assertTrue(r["shorts_required"])

    def test_class2_no_hv_with_wide_clearance(self):
        r = ht.build_requirements(2, clearance_under_2mm=False, has_coax=False)
        self.assertIsNone(r["dwv"])
        self.assertIsNone(r["ir"])

    def test_class2_hv_when_tight_clearance(self):
        r = ht.build_requirements(2, clearance_under_2mm=True)
        self.assertEqual(r["dwv"]["volts_dc"], 1000)
        self.assertEqual(r["ir"]["min_mohm"], 100)
        self.assertFalse(r["shorts_required"])

    def test_class3_limits(self):
        r = ht.build_requirements(3, length_m=2.0)
        self.assertEqual(r["continuity_max_ohms"], 2.0)
        self.assertEqual(r["dwv"]["volts_dc"], 1500)
        self.assertEqual(r["dwv"]["min_dwell_s"], 1.0)
        self.assertEqual(r["ir"]["min_mohm"], 100)

    def test_class3_long_harness_and_wire_resistance(self):
        r = ht.build_requirements(3, length_m=10.0, wire_max_ohms=1.5)
        self.assertEqual(r["continuity_max_ohms"], 2.5)
        self.assertEqual(r["ir"]["min_mohm"], 10)

    def test_class3_coax(self):
        self.assertEqual(ht.build_requirements(3, has_coax=True)["ir"]["min_mohm"], 500)

    def test_verdicts(self):
        good = ht.run_test(WIRES, ht.SimulatedHarness(WIRES))
        self.assertEqual(ht.evaluate(good, ht.build_requirements(1))[1], "PASS")
        self.assertEqual(ht.evaluate(good, ht.build_requirements(3))[1], "PENDING")
        hw = ht.SimulatedHarness(WIRES)
        hw.links.discard(frozenset([("A", "1"), ("B", "A8")]))
        bad = ht.run_test(WIRES, hw)
        self.assertEqual(ht.evaluate(bad, ht.build_requirements(3))[1], "FAIL")


if __name__ == "__main__":
    unittest.main()
