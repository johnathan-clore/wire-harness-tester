#!/usr/bin/env python3
"""
Wire Harness Continuity Tester  (IPC/WHMA-A-620 class aware)
=============================================================

Checks a wire harness against its pinout diagram. It drives each pin on
connector A one at a time and listens on every other pin. That finds:
  * OPEN      - an expected wire doesn't conduct (broken wire, bad crimp)
  * MISWIRE   - a pin lands on the wrong pin at the other end
  * SHORT     - a pin is connected to pins it shouldn't touch

It also asks which IPC/WHMA-A-620 product class the harness is built to
(1, 2 or 3) and sets the acceptance requirements for that class:
continuity resistance limit, shorts test, dielectric withstanding voltage
(DWV / hipot) and insulation resistance (IR). The report then shows which
of those this tester performed and which still need a measured test on
dedicated equipment (4-wire ohmmeter, hipot tester, megohmmeter).

  NOTE: The class values in A620_CLASSES below are the commonly published
  A-620 defaults. Always confirm them against your copy of the current
  revision (A-620F, Oct 2025) and the customer's drawing / contract, which
  override the defaults. Edit A620_CLASSES if your requirements differ.

How the electronics work (Raspberry Pi backend):
  - Every harness pin is wired to its own GPIO through the test fixture.
  - All GPIOs start as inputs with internal pull-UP resistors (they read HIGH).
  - To test one pin, it becomes an output driven LOW.
  - Any other pin connected to it by a wire is pulled LOW too, so reading
    LOW means "connected".
  This only uses 3.3 V and a few microamps. It is safe for a PASSIVE,
  UNPOWERED harness. Never connect the fixture to a powered harness.

Usage
-----
  python3 harness_tester.py                                   # asks for everything
  python3 harness_tester.py --pinout examples/pinout_example.csv --sim --class 3
  python3 harness_tester.py --pinout examples/pinout_example.csv --sim --fault
  python3 harness_tester.py --pinout pinout.csv --fixture fixture.csv --class 3   # real test on a Pi
"""

import argparse
import csv
import datetime
import os
import random
import sys
import time

# ----------------------------------------------------------------------------
# IPC/WHMA-A-620 class settings
# ----------------------------------------------------------------------------
# None = not required for that class.
# "conditional" = Class 2 only: required when conductor clearance is under
#                 2 mm, or the assembly has coax/biaxial/triaxial cable.

A620_CLASSES = {
    1: {
        "name": "Class 1 - General Electronic Products",
        "description": "Function of the completed assembly is the main requirement.",
        "continuity": {"limit": "tester default"},
        "shorts": {"required": True},
        "dwv": None,
        "ir": None,
    },
    2: {
        "name": "Class 2 - Dedicated Service Electronic Products",
        "description": "Continued performance and extended life required; "
                       "uninterrupted service desired but not critical.",
        "continuity": {"limit": "tester default"},
        "shorts": {"required": True, "waived_if_dwv_or_ir": True},
        "dwv": {"conditional": True, "volts_dc": 1000, "max_leakage_ma": 1.0, "min_dwell_s": 0.1},
        "ir": {"conditional": True, "use_class": 3},
    },
    3: {
        "name": "Class 3 - High Performance Electronic Products",
        "description": "Performance critical, no downtime, possibly harsh "
                       "environment (e.g. military aircraft, life support).",
        "continuity": {"limit": "2 ohm, or 1 ohm + wire max resistance (greater)",
                       "base_ohms": 2.0, "plus_ohms": 1.0},
        "shorts": {"required": True, "waived_if_dwv_or_ir": True},
        "dwv": {"conditional": False, "volts_dc": 1500, "max_leakage_ma": 1.0, "min_dwell_s": 1.0},
        "ir": {"conditional": False, "min_mohm_short": 100, "min_mohm_long": 10,
               "min_mohm_coax": 500, "long_over_m": 3.0, "max_dwell_s": 10},
    },
}


def build_requirements(a620_class, clearance_under_2mm=False, has_coax=False,
                       length_m=1.0, wire_max_ohms=0.0):
    """Turn a class plus harness details into concrete test limits."""
    if a620_class not in A620_CLASSES:
        raise ValueError(f"Class must be 1, 2 or 3, got {a620_class!r}")
    spec = A620_CLASSES[a620_class]
    req = {"class": a620_class, "name": spec["name"], "description": spec["description"]}

    # Continuity resistance limit
    cont = spec["continuity"]
    if "base_ohms" in cont:
        req["continuity_max_ohms"] = max(cont["base_ohms"], cont["plus_ohms"] + wire_max_ohms)
    else:
        req["continuity_max_ohms"] = None  # tester default (go / no-go)

    # Is high-voltage testing triggered for this harness?
    hv_triggered = clearance_under_2mm or has_coax

    # DWV / hipot
    dwv = spec["dwv"]
    if dwv and (not dwv["conditional"] or hv_triggered):
        req["dwv"] = {k: dwv[k] for k in ("volts_dc", "max_leakage_ma", "min_dwell_s")}
    else:
        req["dwv"] = None

    # Insulation resistance
    ir = spec["ir"]
    if ir and (not ir["conditional"] or hv_triggered):
        src = A620_CLASSES[ir["use_class"]]["ir"] if "use_class" in ir else ir
        if has_coax:
            min_mohm = src["min_mohm_coax"]
        elif length_m > src["long_over_m"]:
            min_mohm = src["min_mohm_long"]
        else:
            min_mohm = src["min_mohm_short"]
        volts = req["dwv"]["volts_dc"] if req["dwv"] else "tester default"
        req["ir"] = {"min_mohm": min_mohm, "volts_dc": volts, "max_dwell_s": src["max_dwell_s"]}
    else:
        req["ir"] = None

    # Low-voltage shorts test. This tester always runs it; record whether
    # the class makes it mandatory or allows DWV/IR to replace it.
    sh = spec["shorts"]
    req["shorts_required"] = not (sh.get("waived_if_dwv_or_ir") and (req["dwv"] or req["ir"]))

    req["details"] = {"clearance_under_2mm": clearance_under_2mm, "has_coax": has_coax,
                      "length_m": length_m, "wire_max_ohms": wire_max_ohms}
    return req


def _ask_yes_no(prompt, default=False):
    suffix = " [Y/n] " if default else " [y/N] "
    ans = input(prompt + suffix).strip().lower()
    return default if not ans else ans.startswith("y")


def _ask_float(prompt, default):
    while True:
        ans = input(f"{prompt} [{default}]: ").strip()
        if not ans:
            return default
        try:
            return float(ans)
        except ValueError:
            print("    Please enter a number.")


def ask_class(args):
    """Ask for the A-620 class (and the harness details it depends on)."""
    a620_class = args.a620_class
    if a620_class is None:
        print("\nIPC/WHMA-A-620 product class:")
        for n, spec in A620_CLASSES.items():
            print(f"  {n}) {spec['name']}\n       {spec['description']}")
        while a620_class is None:
            ans = input("Class [3]: ").strip() or "3"
            if ans in ("1", "2", "3"):
                a620_class = int(ans)
            else:
                print("    Please enter 1, 2 or 3.")

    clearance = args.clearance_under_2mm
    coax = args.coax
    length = args.length
    wire_ohms = args.wire_ohms

    if a620_class >= 2:
        if coax is None:
            coax = _ask_yes_no("Does the harness include coax, biaxial or triaxial cable?")
        if clearance is None:
            clearance = _ask_yes_no("Is any conductor-to-conductor clearance under 2 mm "
                                    "(e.g. tightly pitched connector pins)?")
        if length is None:
            length = _ask_float("Harness length in metres", 1.0)
    if a620_class == 3 and wire_ohms is None:
        wire_ohms = _ask_float("Max specified resistance of the longest wire, in ohms", 0.0)

    return build_requirements(a620_class,
                              clearance_under_2mm=bool(clearance),
                              has_coax=bool(coax),
                              length_m=length if length is not None else 1.0,
                              wire_max_ohms=wire_ohms if wire_ohms is not None else 0.0)


def describe_requirements(req):
    print(f"\nSettings for {req['name']}:")
    if req["continuity_max_ohms"] is not None:
        print(f"   Continuity:  <= {req['continuity_max_ohms']:.2f} ohm per wire")
    else:
        print("   Continuity:  tester default (go / no-go)")
    print(f"   Shorts:      {'required' if req['shorts_required'] else 'optional (covered by DWV/IR)'}")
    if req["dwv"]:
        d = req["dwv"]
        print(f"   DWV/hipot:   {d['volts_dc']} VDC, <= {d['max_leakage_ma']} mA leakage, "
              f">= {d['min_dwell_s']} s dwell")
    else:
        print("   DWV/hipot:   not required")
    if req["ir"]:
        i = req["ir"]
        print(f"   Insulation:  >= {i['min_mohm']} Mohm at {i['volts_dc']} VDC, "
              f"<= {i['max_dwell_s']} s dwell")
    else:
        print("   Insulation:  not required")


# ----------------------------------------------------------------------------
# Pinout loading / entry
# ----------------------------------------------------------------------------

def load_pinout_csv(path):
    """Return a list of (a_pin, b_pin) tuples from a CSV file."""
    wires = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        for row_num, row in enumerate(reader, start=1):
            row = [c.strip() for c in row]
            if not row or not row[0] or row[0].startswith("#"):
                continue
            if row_num == 1 and row[0].lower() in ("side_a", "a", "from"):
                continue  # header
            if len(row) < 2 or not row[1]:
                raise ValueError(f"Line {row_num}: expected 'side_a,side_b', got {row}")
            wires.append((row[0], row[1]))
    if not wires:
        raise ValueError("Pinout file has no wires in it.")
    return wires


def ask_pinout():
    """Ask the user to type the pinout in by hand."""
    print("\nEnter the pinout one wire at a time.")
    print("Format:  <connector A pin> <connector B pin>   e.g.  1 A8")
    print("Press Enter on an empty line when finished.\n")
    wires = []
    while True:
        line = input(f"  Wire {len(wires) + 1}: ").strip()
        if not line:
            break
        parts = line.replace(",", " ").replace("->", " ").split()
        if len(parts) != 2:
            print("    Please enter exactly two pins, like:  1 A8")
            continue
        wires.append((parts[0], parts[1]))
    if not wires:
        sys.exit("No wires entered. Exiting.")
    if _ask_yes_no("\nSave this pinout to a CSV for next time?"):
        name = input("  File name [pinout.csv]: ").strip() or "pinout.csv"
        with open(name, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["side_a", "side_b"])
            w.writerows(wires)
        print(f"  Saved to {name}")
    return wires


def load_fixture_csv(path):
    """Return {('A','1'): gpio, ('B','A8'): gpio, ...}."""
    fixture = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            key = (row["connector"].strip().upper(), row["pin"].strip())
            fixture[key] = int(row["gpio"])
    return fixture


# ----------------------------------------------------------------------------
# Hardware backends
# ----------------------------------------------------------------------------
# Pins are identified as ("A", "1") or ("B", "A8") throughout.

class SimulatedHarness:
    """Pretends to be a harness so you can try the software with no hardware."""

    def __init__(self, wires, inject_fault=False):
        self.links = set()
        for a, b in wires:
            self.links.add(frozenset([("A", a), ("B", b)]))
        if inject_fault:
            self._inject_random_faults(wires)
        self.driven = None

    def _inject_random_faults(self, wires):
        wires = list(wires)
        random.shuffle(wires)
        a, b = wires[0]
        self.links.discard(frozenset([("A", a), ("B", b)]))
        print(f"  [sim] Injected OPEN on A{a} -> B{b}")
        if len(wires) >= 3:
            (a1, b1), (a2, b2) = wires[1], wires[2]
            self.links.discard(frozenset([("A", a1), ("B", b1)]))
            self.links.discard(frozenset([("A", a2), ("B", b2)]))
            self.links.add(frozenset([("A", a1), ("B", b2)]))
            self.links.add(frozenset([("A", a2), ("B", b1)]))
            print(f"  [sim] Injected SWAP: A{a1} and A{a2}")
        if len(wires) >= 5:
            a3, a4 = wires[3][0], wires[4][0]
            self.links.add(frozenset([("A", a3), ("A", a4)]))
            print(f"  [sim] Injected SHORT between A{a3} and A{a4}")

    def setup(self, all_pins):
        self.all_pins = all_pins

    def drive_low(self, pin):
        self.driven = pin

    def release(self, pin):
        self.driven = None

    def read_connected(self):
        """Return every pin electrically connected to the driven pin."""
        seen, stack = {self.driven}, [self.driven]
        while stack:  # follow chains so splices/shorts propagate like real copper
            cur = stack.pop()
            for link in self.links:
                if cur in link:
                    for p in link:
                        if p not in seen:
                            seen.add(p)
                            stack.append(p)
        seen.discard(self.driven)
        return seen

    def cleanup(self):
        pass


class RaspberryPiHarness:
    """Real hardware: each harness pin wired to its own Raspberry Pi GPIO."""

    SETTLE_TIME = 0.002  # seconds to let the line settle after switching

    def __init__(self, fixture):
        try:
            import RPi.GPIO as GPIO  # noqa: N814
        except ImportError:
            sys.exit("RPi.GPIO isn't installed. On the Pi run:  sudo apt install python3-rpi.gpio\n"
                     "Or use --sim to try the software without hardware.")
        self.GPIO = GPIO
        self.fixture = fixture

    def setup(self, all_pins):
        missing = [p for p in all_pins if p not in self.fixture]
        if missing:
            names = ", ".join(f"{c}{p}" for c, p in missing)
            sys.exit(f"These pins are in the pinout but not in the fixture file: {names}")
        self.GPIO.setwarnings(False)
        self.GPIO.setmode(self.GPIO.BCM)
        self.all_pins = all_pins
        for p in all_pins:
            self.GPIO.setup(self.fixture[p], self.GPIO.IN, pull_up_down=self.GPIO.PUD_UP)

    def drive_low(self, pin):
        self.driven = pin
        self.GPIO.setup(self.fixture[pin], self.GPIO.OUT, initial=self.GPIO.LOW)
        time.sleep(self.SETTLE_TIME)

    def release(self, pin):
        self.GPIO.setup(self.fixture[pin], self.GPIO.IN, pull_up_down=self.GPIO.PUD_UP)
        time.sleep(self.SETTLE_TIME)

    def read_connected(self):
        return {p for p in self.all_pins
                if p != self.driven and self.GPIO.input(self.fixture[p]) == self.GPIO.LOW}

    def cleanup(self):
        self.GPIO.cleanup()


# ----------------------------------------------------------------------------
# The test itself
# ----------------------------------------------------------------------------

def fmt(pin):
    return f"{pin[0]}:{pin[1]}"


def expected_nets(wires):
    """Map each pin to the set of other pins it should be connected to."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in wires:
        parent[find(("A", a))] = find(("B", b))
    nets = {}
    for p in list(parent):
        nets.setdefault(find(p), set()).add(p)
    return {p: net - {p} for net in nets.values() for p in net}


def run_test(wires, hw):
    """Drive every pin and compare what we measure to what the pinout says."""
    should_connect = expected_nets(wires)
    all_pins = sorted(should_connect, key=lambda p: (p[0], p[1].zfill(6)))
    hw.setup(all_pins)

    measured = {}
    try:
        for pin in all_pins:
            hw.drive_low(pin)
            measured[pin] = hw.read_connected()
            hw.release(pin)
    finally:
        hw.cleanup()

    results = []  # (a_pin, b_pin, status, detail)
    for a, b in wires:
        pa, pb = ("A", a), ("B", b)
        got = measured[pa]
        extra = got - should_connect[pa]
        if pb not in got:
            wrong_b = sorted(p for p in extra if p[0] == "B")
            if wrong_b:
                status = "MISWIRE"
                detail = "goes to " + ", ".join(fmt(p) for p in wrong_b) + " instead"
            elif not got:
                status, detail = "OPEN", "no continuity to anything"
            else:
                status = "OPEN"
                detail = "not reaching B; touches " + ", ".join(fmt(p) for p in sorted(got))
        elif extra:
            status = "SHORT"
            detail = "also connected to " + ", ".join(fmt(p) for p in sorted(extra))
        else:
            status, detail = "PASS", ""
        results.append((a, b, status, detail))
    return results


def evaluate(results, req):
    """Build the A-620 checklist. Returns (checklist, verdict).

    checklist: list of (test_name, requirement_text, status)
      status is PASS, FAIL, or PENDING (required but needs external equipment)
    verdict: FAIL, PASS, or PENDING
    """
    cont_fail = any(s in ("OPEN", "MISWIRE") for _, _, s, _ in results)
    short_fail = any(s == "SHORT" for _, _, s, _ in results)
    checklist = []

    checklist.append(("Pinout / continuity (go/no-go)", "every wire lands on the right pin",
                      "FAIL" if cont_fail else "PASS"))
    if req["continuity_max_ohms"] is not None:
        checklist.append(("Continuity resistance",
                          f"<= {req['continuity_max_ohms']:.2f} ohm per wire (4-wire ohmmeter)",
                          "PENDING"))
    checklist.append(("Shorts (low voltage)",
                      "no unintended connections" + ("" if req["shorts_required"]
                                                     else " (optional: covered by DWV/IR)"),
                      "FAIL" if short_fail else "PASS"))
    if req["dwv"]:
        d = req["dwv"]
        checklist.append(("DWV / hipot",
                          f"{d['volts_dc']} VDC, <= {d['max_leakage_ma']} mA, >= {d['min_dwell_s']} s",
                          "PENDING"))
    if req["ir"]:
        i = req["ir"]
        checklist.append(("Insulation resistance",
                          f">= {i['min_mohm']} Mohm at {i['volts_dc']} VDC",
                          "PENDING"))

    statuses = {s for _, _, s in checklist}
    verdict = "FAIL" if "FAIL" in statuses else ("PENDING" if "PENDING" in statuses else "PASS")
    return checklist, verdict


def print_report(results, req, label=""):
    width = max(len(a) for a, *_ in results) + max(len(b) for _, b, *_ in results) + 6
    print("\n" + "=" * 72)
    print(f" HARNESS TEST REPORT {label}".rstrip())
    print(f" {datetime.datetime.now():%Y-%m-%d %H:%M:%S}    IPC/WHMA-A-620 {req['name']}")
    print("=" * 72)
    for a, b, status, detail in results:
        wire = f"A:{a} -> B:{b}".ljust(width + 4)
        mark = "OK " if status == "PASS" else "XX "
        print(f" {mark} {wire} {status:<8} {detail}")

    checklist, verdict = evaluate(results, req)
    print("-" * 72)
    print(f" A-620 CLASS {req['class']} CHECKLIST")
    for name, requirement, status in checklist:
        tag = {"PASS": "OK ", "FAIL": "XX ", "PENDING": "-- "}[status]
        print(f" {tag} {name:<32} {status:<8} {requirement}")
    print("-" * 72)
    fails = sum(1 for r in results if r[2] != "PASS")
    if verdict == "FAIL":
        print(f" RESULT: FAIL  ({fails} of {len(results)} wires have problems)")
    elif verdict == "PENDING":
        print(" RESULT: WIRING PASS - NOT YET ACCEPTED")
        print("         Run the PENDING tests on measured equipment before accepting.")
    else:
        print(f" RESULT: PASS  (all {len(results)} wires correct, Class {req['class']} tests complete)")
    print("=" * 72)
    return verdict


def save_log(results, req, verdict, path, label):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "harness_id", "a620_class", "side_a", "side_b",
                        "status", "detail", "overall_verdict"])
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        for a, b, status, detail in results:
            w.writerow([ts, label, req["class"], a, b, status, detail, verdict])


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Wire harness continuity tester (IPC/WHMA-A-620 aware)")
    ap.add_argument("--pinout", help="CSV pinout file (side_a,side_b)")
    ap.add_argument("--fixture", help="CSV fixture file (connector,pin,gpio) for real hardware")
    ap.add_argument("--sim", action="store_true", help="simulate a harness, no hardware needed")
    ap.add_argument("--fault", action="store_true", help="with --sim, inject random faults")
    ap.add_argument("--log", default="harness_log.csv", help="CSV file to append results to")
    ap.add_argument("--class", dest="a620_class", type=int, choices=[1, 2, 3],
                    help="IPC/WHMA-A-620 product class (asked if omitted)")
    ap.add_argument("--length", type=float, help="harness length in metres (sets IR limit)")
    ap.add_argument("--wire-ohms", type=float,
                    help="max specified resistance of the longest wire (Class 3 continuity limit)")
    ap.add_argument("--coax", action=argparse.BooleanOptionalAction, default=None,
                    help="harness includes coax/biaxial/triaxial cable")
    ap.add_argument("--clearance-under-2mm", action=argparse.BooleanOptionalAction, default=None,
                    help="any conductor clearance under 2 mm")
    return ap.parse_args(argv)


def main():
    args = parse_args()

    print("Wire Harness Tester")
    print("-------------------")

    # 1) Get the pinout diagram
    pinout_path = args.pinout or input("Pinout CSV file (leave blank to type it in): ").strip()
    wires = load_pinout_csv(pinout_path) if pinout_path else ask_pinout()
    print(f"\nLoaded {len(wires)} wires:")
    for a, b in wires:
        print(f"   A:{a}  ->  B:{b}")

    # 2) A-620 class -> test settings
    req = ask_class(args)
    describe_requirements(req)

    # 3) Pick hardware
    if not args.sim and not args.fixture:
        choice = input("\nRun on (1) real hardware or (2) simulation? [2]: ").strip()
        args.sim = choice != "1"
        if not args.sim:
            args.fixture = input("Fixture CSV file: ").strip()

    # 4) Test loop, one harness after another
    while True:
        label = input("\nHarness serial / ID (blank to skip, 'q' to quit): ").strip()
        if label.lower() == "q":
            break
        if args.sim:
            hw = SimulatedHarness(wires, inject_fault=args.fault)
        else:
            hw = RaspberryPiHarness(load_fixture_csv(args.fixture))
            input("Plug in the harness and press Enter to test...")
        results = run_test(wires, hw)
        verdict = print_report(results, req, label)
        save_log(results, req, verdict, args.log, label)
        print(f"Results appended to {args.log}")
        if not _ask_yes_no("\nTest another harness?", default=True):
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
