# Wire Harness Tester

A Python continuity tester for wire harnesses. It checks a harness against its
pinout diagram (for example, pin 1 on connector A must reach pin A8 on
connector B) and finds **opens**, **miswires** and **shorts**. It knows the
IPC/WHMA-A-620 product classes, so each report shows what that class requires.

It runs on a Raspberry Pi wired to the harness through a test fixture. There is
also a simulation mode, so you can try it on any computer without hardware.

## Quick start (no hardware)

```bash
python3 harness_tester.py --pinout examples/pinout_example.csv --sim --class 3
python3 harness_tester.py --pinout examples/pinout_example.csv --sim --fault   # random faults
```

Run `python3 harness_tester.py` with no options and it asks for everything.

## How it works

1. **Pinout.** Load a CSV (`side_a,side_b`, one wire per row) or type it in.
   A pin may appear on several rows, for splices.
2. **IPC/WHMA-A-620 class.** Pick Class 1, 2 or 3. For Class 2 and 3 it also
   asks about coax, conductor clearance under 2 mm, harness length and wire
   resistance, because those change the limits.
3. **Test.** Each pin is driven LOW in turn while every other pin is read
   through a pull-up. Any pin that goes LOW is connected.
4. **Report.** You get a per-wire result plus an A-620 checklist, and a line is
   added to `harness_log.csv` for each wire.

### A-620 class settings

| | Class 1 | Class 2 | Class 3 |
|---|---|---|---|
| Continuity | tester default | tester default | ≤ 2 Ω, or 1 Ω + wire max (greater) |
| Shorts (low V) | required | required unless DWV/IR done | required unless DWV/IR done |
| DWV / hipot | — | 1000 VDC, ≤1 mA, ≥0.1 s *if clearance < 2 mm or coax* | 1500 VDC, ≤1 mA, ≥1 s |
| Insulation resistance | — | Class 3 values *if clearance < 2 mm or coax* | ≥100 MΩ (≤3 m), ≥10 MΩ (>3 m), ≥500 MΩ coax |

> ⚠️ These are the commonly published A-620 defaults. Check them against your
> copy of the current revision (A-620F, Oct 2025) and against the customer's
> drawing or contract, which take priority. To change them, edit `A620_CLASSES`
> in `harness_tester.py`.

### What this tester does and doesn't do

| Check | Done here? |
|---|---|
| Pinout, opens, miswires | ✅ |
| Low-voltage shorts | ✅ |
| Continuity **resistance** (Ω) | ❌ use a 4-wire ohmmeter (marked PENDING) |
| DWV / hipot | ❌ use a hipot tester (marked PENDING) |
| Insulation resistance | ❌ use a megohmmeter (marked PENDING) |

When a class requires a test this hardware can't do, the result reads
**WIRING PASS - NOT YET ACCEPTED** rather than PASS.

## Hardware (Raspberry Pi)

- Wire each harness pin to its own GPIO through mating connectors (the fixture).
- Map each pin to its GPIO in a fixture CSV. See `examples/fixture_example.csv`:
  ```
  connector,pin,gpio
  A,1,4
  B,A8,20
  ```
- Install the GPIO library: `sudo apt install python3-rpi.gpio`
- Run: `python3 harness_tester.py --pinout my_pinout.csv --fixture my_fixture.csv --class 3`

A Pi has about 26 usable GPIOs, which covers about 13 wires. Larger harnesses
need I/O expanders such as the MCP23017.

**Safety:** the tester uses 3.3 V and microamp currents. Only connect it to a
passive harness with no power on it.

## Tests

```bash
python3 -m unittest discover tests
```

## Files

```
harness_tester.py            main program
examples/pinout_example.csv  sample pinout (1->A8 ... 8->A1)
examples/fixture_example.csv sample GPIO map
tests/                       unit tests (simulation)
```
