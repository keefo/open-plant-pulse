# Prototype bring-up checklist

Record measurements, manual links, board revisions, and photos in a GitHub issue
or dated test report. Do not mark an item complete from a product listing alone.
Use the [sensor power-management design](power-management.md) when verifying
deep sleep, switched rails, wake timing, and current consumption.

## Before power

- [ ] Exact controller, expansion board, probe, and SHT45 revisions recorded.
- [ ] NPKPHCTH-S supply range and wire functions confirmed from its manual/label.
- [ ] RS485 board input/output ranges and 12 V current limit confirmed.
- [ ] Power-enable signal, polarity, and default state verified.
- [ ] UART pins verified as D6/GPIO21 TX and D7/GPIO20 RX.
- [ ] Battery holder polarity and protected-cell requirement verified.
- [ ] All power rails checked for shorts with no battery or USB attached.

## SHT45 bench test

- [ ] Exact bare-sensor or breakout pinout and supply range confirmed.
- [ ] I2C address `0x44` is detected on D4/GPIO6 SDA and D5/GPIO7 SCL.
- [ ] Pull-up configuration and bus voltage verified with the actual assembly.
- [ ] CRC-checked temperature and humidity reads complete without errors.
- [ ] Readings are compared with a reference instrument after thermal settling.
- [ ] Self-heating and nearby electronics heat are checked in final placement.
- [ ] Ventilation protects the sensing opening from splashes and condensation.

## USB-only firmware

- [ ] ESP-IDF target builds without warnings.
- [ ] Board boots and emits the startup log over USB.
- [ ] Reset and bootloader recovery procedure tested.

## Bench supply and probe

- [ ] Probe first powered from a current-limited bench supply.
- [ ] Idle, startup, and measurement currents recorded.
- [ ] Modbus baud, parity, address, function, registers, and scaling confirmed.
- [ ] Moisture, soil temperature, conductivity, pH, N, P, and K registers are
	individually identified rather than inferred from a family/model name.
- [ ] At least 100 reads complete without CRC or timeout errors.
- [ ] Open-air/water/reference-medium values are plausible and repeatable.

## Power gating and radio

- [ ] Probe rail is off at reset and during deep sleep.
- [ ] Stabilization delay established by measurement, with margin.
- [ ] Failed reads always remove probe power.
- [ ] BTHome payload parses correctly in Home Assistant.
- [ ] Soil and air temperatures appear as distinct, correctly named entities.
- [ ] Device is discovered reliably at the intended proxy distance.
- [ ] Deep-sleep and complete-cycle current profiles recorded.

## Enclosure trial

- [ ] Measured 5.0 mm NPKPHCTH-S cable passes through the 5.8 mm circular hole without jacket damage.
- [ ] Cable gland or external clamp provides strain relief; the printed pass-through alone does not retain the cable.
- [ ] No exposed conductor or battery contact can touch condensation.
- [ ] Antenna clearance and orientation are documented.
- [ ] Enclosure temperature and condensation checked in its final location.
- [ ] A service procedure exists for charging, inspection, and battery removal.