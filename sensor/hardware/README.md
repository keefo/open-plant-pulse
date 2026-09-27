# Hardware

The procurement source of truth is the [bill of materials](BOM.md) and its
machine-readable [`bom.csv`](bom.csv). The table below summarizes the prototype
architecture and must not be used as a substitute for exact orderable parts.

## Reference prototype

| Function | Reference part | Required verification |
| --- | --- | --- |
| Controller | Seeed Studio XIAO ESP32-C3 | Board revision and pin map |
| RS485/power | XIAO-format RS485 expansion board | Input range, 12 V capacity, enable pin, idle current |
| Probe | ComWinTop NPKPHCTH-S | Product revision, wire colors, voltage, register map, scaling |
| Air sensor | Sensirion SHT45 or SHT45 breakout | Exact MPN/module, supply voltage, mounting, idle current |
| Battery | Protected 3.7 V 18650 cell and holder | Protection, polarity, peak-current capability |
| Cable entry | PG7 gland sized for probe cable | Grip and ingress protection |
| Enclosure | Printed PETG/ASA prototype | Clearances, ventilation, strain relief |

Part numbers are not interchangeable merely because they share an enclosure or
product-family name. Save each purchased part's manual and revision under a
release or issue before freezing the design.

## Pin map

This is the assembled prototype's wiring: a XIAO ESP32-C3 on the XIAO RS485
board, with the SHT45 and INA219 sharing one I2C bus.

| Signal | XIAO label | ESP32-C3 GPIO | Connected to |
| --- | --- | --- | --- |
| RS485 UART TX | D4 | GPIO6 | RS485 board transceiver input |
| RS485 UART RX | D5 | GPIO7 | RS485 board transceiver output |
| RS485 driver enable | D2 | GPIO4 | RS485 board DE |
| I2C SDA | D1 | GPIO3 | SHT45 and INA219 SDA |
| I2C SCL | D3 | GPIO5 | SHT45 and INA219 SCL |
| Probe power | D10 | GPIO10 | AO3400 gate: high powers the MT3608 and probe |

The RS485 board occupies D4/D5, so no I2C device may use them: the
transceiver's receiver output actively drives D5, which corrupts I2C. The
ESP32-C3 has one I2C controller, so every I2C device belongs on D1/D3. The
INA219 at `0x40` and the SHT45 at `0x44` do not collide; do not bridge the
INA219's A1 pad, which would move it to `0x44`.

The probe is powered from an MT3608 boost converter set to 12.4 V on the system
node, switched by an AO3400 N-MOSFET in the MT3608's ground (see
[probe power switch](#probe-power-switch)).

## Power path

The assembled prototype runs from two 18650 cells in parallel (still one 3.7 V
cell electrically) and charges them through an IP2312 USB-C charger module.

| From | To | Notes |
| --- | --- | --- |
| USB-C charger | IP2312 USB-C input | The only charging input |
| IP2312 BAT+ | System node | Short, thick wires (20-22 AWG) |
| System node | INA219 VIN+ | |
| INA219 VIN- | Battery pack P+ | The shunt sits in series with the pack |
| System node | MT3608 IN+ | Boost converter for the probe |
| System node | XIAO BAT+ pad | Powers the controller |
| MT3608 OUT+ (12.4 V) | Probe brown | Set the voltage before connecting the probe |
| MT3608 OUT- | Probe black | Same net as MT3608 IN-, switched by the AO3400 |
| MT3608 IN- | AO3400 drain (pin 3) | The MT3608's only path to ground |
| AO3400 source (pin 2) | System ground | |
| XIAO D10 (GPIO10) | AO3400 gate (pin 1) | 10-100 kOhm gate-to-source pull-down |

IP2312 BAT-, pack P- and the XIAO BAT- pad share one ground. MT3608 IN- and
OUT- reach it only through the AO3400. The
INA219 and SHT45 take ground from the XIAO GND pin, which is the same net.

Because the INA219's VIN+ is on the system node and VIN- on the pack, its
current is **positive while charging** and negative while the node runs from
the battery. The firmware reads it as a battery monitor; see
[firmware development](../../docs/firmware.md#battery-monitor). Its shunt value
and the cells' capacity are build settings that must match the parts.

### Probe power switch

The MT3608's own enable pin cannot switch the probe off: a disabled boost
converter still passes the battery voltage, less a diode drop, through its
inductor and diode to the output, leaving the probe half powered. None of the
other modules can switch a load either, and the RS485 board's 12V terminal is
an input to its 12 V to 5 V regulator, not an output.

An AO3400 N-MOSFET (SOT-23; 30 V, 5.7 A, fully on at 3.3 V gate) therefore
switches the MT3608's ground, which takes the MT3608 and the probe together:

- Pin 3 (drain, the lone pin) to MT3608 IN-; pin 2 (source) to system ground;
  pin 1 (gate) to D10, with 10-100 kOhm from gate to source so the probe stays
  off through reset. With the marking readable and the lone pin at the top,
  pin 1 is bottom left and pin 2 bottom right. In diode mode only source (red)
  to drain (black) conducts, at about 0.6 V.
- MT3608 IN- must have no other path to ground, or the switch is bypassed.
- Do not switch the probe's black wire alone: the MT3608 would keep 12.4 V on
  brown and the probe would try to power itself through A/B.
- When on, the RS485 ground reference passes through the MOSFET, a few
  millivolts of offset, well within RS485's common-mode range. When off, the
  probe's ground floats.

Measured 2026-09-26 on battery (USB unplugged), 30 s averages: probe on
-98.0 and -98.1 mA, off -69.5 mA. **The MT3608 and probe cost 28.5 mA**, about
120 mW at 4.17 V. The probe answers within 2.1 s of power-on, and moisture,
temperature and conductivity are right on the first read. pH is not: 3.3 on
the first read, 5.6 ten seconds later, 6.0-6.8 after long operation. A
short power-up per reading will need a longer warm-up, or flagged pH.

While USB is connected the INA219 cannot show this. Near full, the charger
holds the system node at 4.2 V and supplies the loads directly; the pack
current then depends only on the pack voltage, so switching a load changes
the charger's current, not the INA219's. Measure loads on battery alone.

### The node must have its battery in

With no battery fitted, the only source on the system node is the XIAO's own
small Li-ion charger, fed from the XIAO's USB port. It is sized to top up a
cell, not to run a load, and cannot supply the MT3608 and the probe (up to
0.5 W). The probe's supply then sags. Its main controller keeps running, so it
still answers Modbus and reports conductivity, but its moisture/temperature
sensor and pH electrode do not start, and it reports those as exactly `0`.
Observed 2026-09-26: moisture 0 %, temperature 0.0 C and pH 0 with EC 169
uS/cm in water on USB alone; 77.6 %, 25.7 C and pH 4.8 as soon as the battery
was fitted. Bench-test anything involving the probe with the battery in.

The XIAO's charger also charges the pack whenever the XIAO's USB is connected,
so avoid charging through the IP2312 at the same time.

## Probe wiring

From the CWT "NPK type" manual V1.4, which applies to the five-needle
NPKPHCTH-S:

| Probe conductor | Function | Connected to |
| --- | --- | --- |
| Brown | Power + (DC 4.5-30 V) | MT3608 OUT+ (12.4 V) |
| Black | Power - | MT3608 OUT- / common ground |
| Yellow or green | RS485 A+ | RS485 board A |
| Blue | RS485 B- | RS485 board B |

Reversed A/B is usually non-destructive but prevents communication; reversed
power may permanently damage the probe.

RS485 board settings: A and B terminals to the probe; the 5V, 12V, GND and INT
terminals unused; the 5V IN/OUT switch at IN; the 120R termination switch ON.
The A/B terminals are the RS485 bus side of the board's transceiver. D4, D5 and
D2 reach the transceiver's logic side through the board itself when the XIAO
is plugged in.

Replies occasionally arrive a byte or more short, or with the last bits
flipped (for example a CRC of `ef d5` where `ef 15` was due). The firmware
retries a failed read up to three times, which has recovered every sample so
far. Most of these were seen while the node ran on USB alone, so they may share
that cause; if they persist with the battery in, try the 120R switch OFF, since
a 2 m cable at 4800 baud needs no termination.

## NPKPHCTH-S soil probe

The selected `NPKPHCTH-S` is a multi-parameter insertion probe that exposes
seven advertised soil measurements through RS485 using Modbus RTU:

| Measurement | Typical unit | Project use |
| --- | --- | --- |
| Nitrogen (N) | mg/kg | Nutrient trend; not yet implemented |
| Phosphorus (P) | mg/kg | Nutrient trend; not yet implemented |
| Potassium (K) | mg/kg | Nutrient trend; not yet implemented |
| pH | pH | Acidity/alkalinity trend; not yet implemented |
| Electrical conductivity | microS/cm | Dissolved-ion/salinity trend; contract v1 |
| Soil temperature | degrees C | Root-zone temperature; contract v1 |
| Soil moisture | % | Root-zone water trend; contract v1 |

The probe is intended for continuous in-soil measurements with corrosion-resistant
316 stainless-steel electrodes and an IP68-style sealed body. Those construction
and ingress claims must be confirmed against the purchased revision's datasheet.

NPK and pH readings from a compact in-soil probe are best treated as trends until
compared with appropriate reference methods. They do not replace laboratory soil
analysis. Before enabling any field, verify the purchased unit's supply voltage,
Modbus address, baud/parity, register address, signedness, scaling, valid range,
warm-up time, and CRC behavior from its exact manual and bench measurements.

Firmware reads all seven registers (`0x0000`-`0x0006`, address 1, 4800 baud
8N1) per the manual, and all seven are broadcast to the hub in contract v3. See [firmware development](../../docs/firmware.md#soil-probe).

### Observed register map

A raw dump of the purchased probe (address 1, 4800 baud, needles in tap water,
battery fitted) matches the manual throughout:

| Register | Meaning | Raw | Scaled |
| --- | --- | --- | --- |
| `0x0000` | Moisture | 776 | 77.6 % |
| `0x0001` | Temperature | 257 | 25.7 C |
| `0x0002` | Conductivity | 166 | 166 uS/cm |
| `0x0003` | pH | 48 | 4.8 |
| `0x0004`-`0x0006` | N, P, K | 7, 11, 27 | mg/kg |
| `0x0007` | Salinity | 91 | 0.55 x conductivity |
| `0x0008` | TDS | 83 | 0.50 x conductivity |
| `0x0023`, `0x0024` | Salinity, TDS factors | 55, 50 | Manual defaults |
| `0x07D0`, `0x07D1` | Slave ID, baud code | 1, 1 | Address 1, 4800 baud |

With the needles fully submerged in a second glass of tap water, the probe
settled within about 30 s at moisture 100.0 % (raw 1000), temperature 25.7 C
(air 27.2 C), conductivity 97 uS/cm, pH 4.5, N/P/K 3/6/16, salinity 53 and TDS
48. That container was not clean: in a clean container of the same tap water
the probe settled at moisture 100.0 %, temperature 23.8 C (air 26.7 C),
conductivity 41 uS/cm, pH 6.8 (drifting down from 7.4 over two minutes) and
N/P/K 1/2/7. The residue had raised conductivity and made the water acidic,
and the probe measured both correctly. Rinse the container before any water
test.

In air, with needles wet from the water, moisture read about 4 % and then
0.0 %, conductivity and N/P/K 0, and temperature about 3 C below the air while
the wet probe cooled by evaporation. pH wandered between 3.0 and 8.2: a pH
electrode needs liquid contact, so pH in air is noise. A starved probe (see
[above](#the-node-must-have-its-battery-in)) is different: its temperature
reads exactly 0.0.

N, P and K move with conductivity (7/11/27 at 166 uS/cm, 3/6/16 at 97 uS/cm,
1/2/7 at 41 uS/cm):
they are estimates derived from it, not independent measurements. The probe answers both function `0x03` and `0x04`.
The firmware's seven-register `0x03` read is reliable; a sixteen-register
`0x03` read once returned zeros where `0x04` returned the values, so use
`0x04` when dumping wider ranges. pH settles slowly, over minutes; check it
against a buffer solution before relying on it, and see the offset register
`0x0053`.

## SHT45 air sensor

The SHT45 adds ambient air temperature and relative humidity at its default I2C
address `0x44`. These measurements are distinct from the RS485 probe's soil
temperature and soil moisture.

- Select a **standard 2.54 mm (0.1 in) pin-header solder-pad breakout** with
	castellated-edge or through-hole header termination, then record its exact MPN.
	Bare sensors and breakout boards have different power, pull-up, and mounting
	requirements.
- Mount the sensing opening in ventilated ambient air, outside any sealed
	electronics volume.
- Isolate it from heat produced by the ESP32-C3, boost converter, battery, direct
	sunlight, and warm walls to avoid biased air-temperature readings.
- Prevent liquid water, soil, and conformal coating from reaching the sensing
	opening. A ventilated protective cap is not an independently verified IP rating.
- Confirm whether I2C pull-ups are present before adding another set.
- Measure sleep and active current as part of the complete sensor-node budget.

A broken SDA or SCL joint does not always stop the SHT45 from acknowledging its
address. On 2026-09-26 it was found at `0x44` but every measurement failed its
CRC until a broken wire was repaired, so repeated CRC failures point at wiring
first. Heat from the soldering iron or nearby electronics also shows up
directly: 35 C just after rework, settling to 27.6 C.

## Power and battery safety

See [sensor power management](../../docs/power-management.md) for the intended
always-on and switched rails, RTC wake path, SHT45 standby choices, RS485 probe
shutdown requirements, and complete-system current budget.

- Use only a qualified, protected single-cell Li-ion battery in a holder.
- Verify polarity and absence of shorts with a meter before inserting the cell.
- Confirm that USB charging and load sharing are supported for the board revision.
- Measure boost-converter startup current and battery voltage sag under load.
- Fuse the prototype during bench bring-up and keep it away from soil and water.
- Do not claim an IP rating for a printed enclosure without a tested sealing plan.

The XIAO documentation lists approximately 44 microA for the board in deep
sleep, before expansion-board, regulator, and battery-protection losses. The
brainstorm's sub-20-microamp system target is therefore not a validated baseline.

## Artifact layout

Add source artifacts only when they exist:

```text
sensor/hardware/
├── bom.csv         Canonical top-level assembly BOM
├── BOM.md          BOM schema and maintenance policy
├── electronics/    KiCad schematic, PCB, libraries, and fabrication outputs
├── enclosure/      Editable CAD, STEP/STL exports, drawings, and print notes
└── datasheets/      Redistribution-permitted manuals or links/index files
```

See the [enclosure workspace](enclosure/README.md) for its model/export layout.
Generated fabrication files should identify their source revision and release.