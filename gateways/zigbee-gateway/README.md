# Zigbee gateway

The iMac speaks Wi-Fi and Bluetooth and has no 802.15.4 radio, so it cannot
hear a Zigbee device directly. This ESP32-C6 is that radio: it forms the
Zigbee network, and the hub reaches it over USB.

At the moment it forms a network and reports it. Nothing joins it yet and
there is no bridge to the hub.

## Build and flash

    . ~/esp/esp-idf/export.sh
    idf.py build
    idf.py -p /dev/cu.usbmodem<N> flash monitor

The board is an ESP32-C6 on its native USB; `/dev/cu.usbmodem*` is the
chip's own USB-serial, not a separate bridge chip.

## What it reports

    I (261) gateway: stack ready; commissioning
    I (264) gateway: network up: pan 0x25e7, channel 15, short 0x0000
    I (265) gateway: gateway ac:eb:e6:ff:fe:0c:8e:68

The network survives a reflash: it is kept in NVS, and the same PAN comes
back. `esp_zigbee_factory_reset()` is what discards it.

## Two things that cost an evening

**The partition table.** Version 2 of esp-zigbee-lib keeps its datasets in
the ordinary `nvs` partition. The 1.x examples that most writing still
shows declare a separate `zb_storage` FAT partition; declare one here and
`ezb_plat_datasets_init` aborts at every boot, in a reset loop that never
reaches `app_main`. See `partitions.csv`.

**The console.** The DevKitC has two USB sockets, and by default the log
goes to the UART bridge on GPIO 16/17 rather than the native USB. If the
boot ROM chatter appears but the application's own lines never do, that is
why; `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` in `sdkconfig.defaults` moves
it.

## The API this is written against

esp-zigbee-lib **2.x**, whose API differs from the 1.x one in most
examples: `esp_zigbee_init`/`esp_zigbee_start` rather than
`esp_zb_init`/`esp_zb_start`, and signals arrive at handlers registered
with `ezb_app_signal_add_handler` rather than at one global
`esp_zb_app_signal_handler`. The 1.x names still exist under
`include/compat/`, which is a trap worth avoiding in new code.

Started without autostart, so forming the network is a decision taken in
`on_signal` rather than behind the application's back.
