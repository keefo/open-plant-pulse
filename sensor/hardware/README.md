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

## Candidate pin map

| Signal | XIAO label | ESP32-C3 GPIO | Confidence |
| --- | --- | --- | --- |
| UART TX | D6 | GPIO21 | Confirmed by XIAO pin map |
| UART RX | D7 | GPIO20 | Confirmed by XIAO pin map |
| I2C SDA | D4 | GPIO6 | Candidate SHT45 bus; verify assembled board routing |
| I2C SCL | D5 | GPIO7 | Candidate SHT45 bus; verify assembled board routing |
| Expansion power enable | Possibly D3 | GPIO5 | Unverified; inspect board schematic/jumpers |

The original sample defines the power pin as GPIO3 while its prose says D3,
which maps to GPIO5. Firmware must not enable probe power until this is resolved
with a continuity test or the exact expansion-board schematic.

## Candidate probe wiring

| Probe conductor | Function | Interface terminal |
| --- | --- | --- |
| Red | Positive supply | VCC/12 V |
| Black | Ground | GND |
| Yellow | RS485 A | A |
| Green or blue | RS485 B | B |

Wire colors are only a hypothesis until checked against the exact probe label or
manual. Reversed A/B is usually non-destructive but prevents communication;
reversed power may permanently damage the probe.

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

Current firmware tests cover only three consecutive registers for moisture, soil
temperature, and conductivity. Selecting the seven-parameter model does not mean
that nitrogen, phosphorus, potassium, or pH are already read or broadcast.

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