# Sensor enclosure

This directory owns the mechanical design for the sensor electronics housing.
Commit both the editable model and practical exports so contributors can modify
the design without preventing builders from printing it.

## Layout

```text
enclosure/
├── source/       Editable native CAD files and linked design parameters
├── step/         Neutral STEP exports for interchange and fit checks
├── stl/          Print-ready STL meshes, one oriented part per file
├── drawings/     Dimensioned PDF/DXF drawings and assembly diagrams
└── README.md     Design status, export metadata, and print instructions
```

The native model in `source/` is the source of truth. STEP, STL, PDF, and DXF
files are generated release artifacts, but they are intentionally versioned so
builders do not need the original CAD application.

## File conventions

- Design in millimetres and model parts at 1:1 scale.
- Use descriptive lowercase names, for example `controller-housing-v1.step` and
  `controller-housing-lid-v1.stl`.
- Keep separate printable parts in separate STL files.
- Include the design revision in every exported filename.
- Record the CAD application/version, source commit, export date, material,
  orientation, supports, layer height, wall count, and tolerances below.
- Do not commit slicer-generated G-code; it is printer- and material-specific.
- Do not claim water resistance or an IP rating without documented testing.

## Current design

Status: unprinted sensor-node prototype. The first design holds the Seeed RS-485
Breakout Board for XIAO, a stacked XIAO ESP32-C3, the SHT45 breakout, and a
ready-made dual-18650 holder. It does not include the final probe cable
gland.

| Property | Value |
| --- | --- |
| CAD tool and version | Blender 4.5; parametric Python source |
| Design revision | `air-node-case-v1` |
| Intended material | PETG prototype |
| Nozzle / layer height | 0.4 mm / 0.2 mm candidate |
| Required supports | None in exported orientation |
| Source commit used for exports | TBD |
| Fit-test result | Not tested |

### Air-node prototype

The editable generator is `source/air-node-case-v1.py`; running it through
Blender creates `source/air-node-case-v1.blend` and separate base/lid STL files.
All dimensions are in millimetres and the important clearances are constants at
the top of the script.

| Dimension | Value |
| --- | --- |
| XIAO ESP32-C3 footprint | 21.0 x 17.8 mm |
| SHT45 breakout footprint | 13.29 x 10.26 mm |
| Board-edge separation | 15.0 mm |
| Seeed RS-485 carrier | 42 x 25 x 14 mm published envelope |
| Provisional RS-485 plus XIAO height | 18 mm |
| RS-485 mounting bases | Four 6.5 mm OD x 3.0 mm high standoffs |
| RS-485 mounting pilots | 2.7 mm blind M3 thread-forming holes |
| Provisional RS-485 hole pattern | 2.0 mm from side edges; rows 23 and 39 mm from USB end |
| Dual-18650 holder envelope | 77.0 x 39.5 x 18.3 mm |
| Battery-holder pocket | 77.8 x 40.3 mm; 0.4 mm clearance per side |
| Electronics compartment width | 35.0 mm; 3.0 mm service space outside each RS-485 rail |
| Nominal outside size | 83.8 x 83.3 x 27.0 mm |
| Wall / floor / lid | 3.0 / 2.0 / 2.8 mm |
| Per-side board clearance | 0.4 mm |
| USB-C opening | 11.5 x 6.5 mm prototype clearance, elevated for stack |
| USB-C position | Approximately 0.15 mm inside outer wall; stack shifted 1.4 mm toward USB opening |
| External probe-cable opening | 5.8 mm circular pass-through for measured 5.0 mm NPKPHCTH-S cable |
| SHT45 side opening | 5.0 x 5.0 mm rounded aperture |
| SHT45 fastener | 3.4 mm M3 clearance with 6.4 mm head recess |
| Antenna cable pass-through | 4.0 x 3.2 mm rounded slot |
| Internal antenna cable guide | 2.2 mm channel for nominal 1.13 mm coax |
| Antenna location | Outside USB end wall, on the battery-side portion |
| Battery isolation wall | 2.0 mm full-length partition, 2.8 mm sliding-lid clearance |
| Battery-lead pass-through | 8.0 x 5.0 mm rounded opening near the XIAO |
| Lid retention | 1.2 mm captured tongues and upper rails in 2.0 mm-high grooves |
| Lid running clearance | 0.4 mm nominal in XY and Z, with a recessed 14 mm thumb notch |

The RS-485 carrier uses four M3 mounting bases plus side rails. Its hole pattern
is estimated from the supplied product photograph because dimensioned mounting
data is not available. Measure all four hole centers on the physical board and
update the generator constants before printing. The SHT45 mounts vertically on
the right end wall with its sensing corner facing a 5 mm opening. One M3 screw
clamps the board while three edge rails prevent rotation. The sensor and M3
offsets are estimated from the supplied photograph and must be measured before
printing. The USB-C opening, terminal-block cable slot, assembled stack height,
RS-485 mounting holes, and sliding-lid fit must also be checked when the
physical board arrives. The SHT45 opening prioritizes airflow and is not
splash-proof or IP rated.

The ready-made dual-18650 holder occupies a simple rectangular bay behind the
2 mm partition. The compartment walls locate its measured 77.0 x 39.5 mm
footprint with 0.4 mm clearance per side; the former cell cradles and end-stop
ridges are removed. The Blender reference includes the holder floor, side rails,
center spine, terminal walls, contacts, and coil springs. Only the outer envelope
is measured; verify the internal details against the physical holder. The battery harness crosses through a localized rounded
opening in the partition near the XIAO; add edge protection where the lead
crosses the wall. The separate outer-wall slot is for the external soil probe
cable, not the battery. Replace that provisional slot with a measured
cable-gland hole in the final design. Verify whether the purchased holder wires
its cells in series or parallel and add suitable pack-level protection before
connecting it. Use matched cells at the same state of charge, and measure the
holder, contacts, leads, and connector before printing.

The screwless lid slides in from the USB end and finishes flush with the top of
the rounded shell. Stepped tongues on both long edges run beneath retaining lips,
and the uncut far end wall provides the insertion stop. The 2.8 mm panel embeds
the tongues within its thickness so both broad faces are planar. A semicircular
thumb notch at the entry edge allows removal without an external tab. The four
former lid M3 bosses and matching lid holes are removed; the four RS-485 board
standoffs remain. The lid STL is oriented with its broad outer face on the build
plate so the rail tongues expand laterally in the upper layers without support.
Print a short rail-fit coupon before the full enclosure and adjust the nominal
0.4 mm clearance for the selected material and printer.

The first fit test places the adhesive BLE antenna outside on the USB end wall,
using the battery-side portion so it does not obstruct USB-C. Feed the U.FL plug
inward through the electronics-side antenna slot before connecting it to the
XIAO, then lay the coax between the guide rails without a sharp bend or tension
at the connector. This position places the cells behind the antenna, so compare
BLE range with the previous electronics-side long-wall position before fixing
the final location. The modeled 45 x 20 mm antenna paddle is a visual
placeholder; measure the supplied antenna before adding retention features.
Sealant or a grommet may be added after fit testing, but the current slot is not
water-resistant.

## Design requirements

- Retain the XIAO ESP32-C3 and RS485 board without stressing headers.
- Provide protected-cell retention and safe battery removal.
- Provide strain relief sized to the measured probe cable and gland.
- Keep conductive parts and the battery isolated from condensation.
- Place the SHT45 in ventilated ambient air with a protective opening that does
  not trap condensation or expose it to direct splashes.
- Thermally isolate the SHT45 from the controller, boost converter, battery, and
  sun-heated enclosure surfaces.
- Preserve external BLE antenna clearance and orientation.
- Allow access to USB-C, reset/boot recovery, and service fasteners.
- Avoid trapped water and document the installed orientation.

Update this document when the first source model is added. A hardware release
must export matching STEP and STL files from the tagged source revision and
record its fit and print validation in the bring-up report.