# Prototype bring-up checklist

Record measurements, manual links, board revisions, and photos in a GitHub issue
or dated test report. Do not mark an item complete from a product listing alone.
Use the [sensor power-management design](power-management.md) when verifying
deep sleep, switched rails, wake timing, and current consumption.

## Before power

- [ ] Exact controller, expansion board, probe, and SHT45 revisions recorded.
- [x] NPKPHCTH-S supply range and wire functions confirmed from its manual/label
	(CWT "NPK type" manual V1.4: DC 4.5-30 V; brown +, black -, yellow/green A, blue B).
- [ ] RS485 board input/output ranges and 12 V current limit confirmed.
- [ ] Power-enable signal, polarity, and default state verified.
- [x] UART pins verified as D4/GPIO6 TX, D5/GPIO7 RX and D2/GPIO4 DE by working
	Modbus reads through the XIAO RS485 board (2026-09-26).
- [ ] Battery holder polarity and protected-cell requirement verified.
- [ ] All power rails checked for shorts with no battery or USB attached.

## SHT45 bench test

- [ ] Exact bare-sensor or breakout pinout and supply range confirmed.
- [x] I2C address `0x44` is detected on D1/GPIO3 SDA and D3/GPIO5 SCL, shared with
	the INA219 at `0x40`. D4/D5 belong to the RS485 board (2026-09-26).
- [ ] Pull-up configuration and bus voltage verified with the actual assembly.
- [x] CRC-checked temperature and humidity reads complete without errors
	(firmware 0.13.2 and 0.13.3 serial logs, 2026-09-26).
- [ ] Readings are compared with a reference instrument after thermal settling.
- [ ] Self-heating and nearby electronics heat are checked in final placement.
- [ ] Ventilation protects the sensing opening from splashes and condensation.

## USB-only firmware

- [x] ESP-IDF target builds without warnings.
- [x] Board boots and emits the startup log over USB (needs a data cable; a
	charge-only cable powers the board but no port appears).
- [ ] Reset and bootloader recovery procedure tested.

## Bench supply and probe

- [ ] Probe first powered from a current-limited bench supply.
- [x] Probe tested with the battery fitted. On USB alone the XIAO's charger
	cannot supply it, and moisture, temperature and pH read 0 (2026-09-26).
- [ ] Idle, startup, and measurement currents recorded.
- [x] Modbus baud, parity, address, function, registers, and scaling confirmed
	by a raw register dump and a tap-water test (2026-09-26).
- [ ] Moisture, soil temperature, conductivity, pH, N, P, and K registers are
	individually identified rather than inferred from a family/model name.
- [ ] At least 100 reads complete without CRC or timeout errors. Not yet: replies
	occasionally arrive short and are recovered by retries.
- [ ] Open-air/water/reference-medium values are plausible and repeatable. Clean tap
	water: 100 %, 23.8 C, 41 uS/cm, pH 6.8; air: 0 %, 0 uS/cm. No reference medium
	or soil yet.

## Power gating and radio

- [ ] Probe rail is off at reset and during deep sleep.
- [ ] Stabilization delay established by measurement, with margin.
- [ ] Failed reads always remove probe power.
- [ ] One bounded BTHome advertising burst is received and stored by the hub.
- [ ] The hub being absent does not extend the fixed advertisement window.
- [ ] Duplicate callbacks from one burst do not create duplicate stored samples.
- [ ] BTHome payload parses correctly in Home Assistant and the hub.
- [ ] Soil and air temperatures appear as distinct, correctly named values.
- [x] Hub plant name, room, and interval are written during a report and read back.
- [x] Configuration survives reset and appears on the sensor dashboard.
- [x] The next always-awake report window uses the applied interval.
- [ ] Other physical sensors remain independent while configuration is delivered.
- [ ] Deep-sleep and complete-cycle current profiles recorded.

## Enclosure trial

- [ ] Measured 5.0 mm NPKPHCTH-S cable passes through the 5.8 mm circular hole without jacket damage.
- [ ] Cable gland or external clamp provides strain relief; the printed pass-through alone does not retain the cable.
- [ ] No exposed conductor or battery contact can touch condensation.
- [ ] Antenna clearance and orientation are documented.
- [ ] Enclosure temperature and condensation checked in its final location.
- [ ] A service procedure exists for charging, inspection, and battery removal.
