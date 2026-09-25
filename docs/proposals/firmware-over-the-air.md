# Firmware over the air

## Status

Proposed 2026-09-25. Implementation in progress; see the worklog for what has run
on hardware.

## Goal

Keep a built firmware image on the hub, and update a sensor from the browser
without a cable. One button, on the sensor's own configuration page.

Today every sensor in the house has to be unplugged, carried to a computer, and
flashed over USB. That is acceptable for one sensor on a bench and unacceptable
for a household with several, which is what the rest of this project assumes.

## Two links, two jobs

**The hub commands over Bluetooth. The sensor downloads over Wi-Fi.**

The bonded BLE link is the authenticated one: only the hub that owns a sensor can
connect to it at all, and the configuration characteristic already requires
encryption. It is the right place to say *install this image*. It is the wrong
place to move 1.3 MB: at the throughput this link achieves, the transfer would
take minutes with the radio held open throughout, and any interruption would
restart it.

The sensor is already on the household network whenever its console is on, and
ESP-IDF's `esp_http_ota` streams an image into the spare slot at Wi-Fi speed with
resumable framing, header validation, and rollback support already written. So the
command carries an address, and the sensor fetches.

The consequence is a precondition, stated plainly in the interface: **a sensor can
only be updated while it is on the household network.** If its console is off, the
button says so rather than failing later.

## What the hub stores

An uploaded image is not trusted because somebody said it was firmware. The hub
reads the ESP-IDF application descriptor out of the file and refuses anything that
is not an `esp32c3` image of this project:

| Checked | Where | Refusal |
| --- | --- | --- |
| Image magic `0xE9` | byte 0 | not an ESP-IDF image |
| Chip ID 5 | header byte 12 | built for another chip |
| Descriptor magic `0xABCD5432` | offset 0x20 | no application descriptor |
| Project name `open_plant_pulse` | descriptor | another project's firmware |
| Version | descriptor | — it is read, never typed |

The version therefore comes from the image, not from a form field, and cannot
disagree with what the sensor will report after it boots.

Images live in `~/.open-plant-pulse/firmware/<sha256>.bin` with a row recording
size, digest, version, project, IDF version and upload time. The digest is the
identity: uploading the same image twice stores it once.

## The command

Protocol version 7 on the existing configuration characteristic, alongside
configuration (6), credentials (3), release (5) and report acknowledgement (1).
Forty-seven bytes:

| Bytes | Meaning |
| --- | --- |
| 0 | 7 |
| 1–4 | update ID, so a repeat of one already running is ignored |
| 5–8 | image size |
| 9–12 | hub IPv4 address |
| 13–14 | hub port |
| 15–46 | SHA-256 of the image |

The sensor builds `http://<address>:<port>/firmware/<digest>.bin` from it. Nothing
in the command names a file, so a compromised command cannot ask the sensor to
fetch something else from the hub.

## What the sensor answers

The configuration read-back already carries a station-status suffix (marker
`0xA1`). Update progress is a second suffix, marker `0xA2`, placed **before** it,
so a hub that reads the last nine bytes still finds the station status where it
was. Four bytes: marker, state, percent, failure reason.

The hub polls it while an update is in flight, on the connection it already makes
when something is pending.

Success is not taken from that report, though. Success is the sensor coming back
and reporting the new version in its station status. A sensor that says it
installed an image and then does not return has not succeeded.

## Safety

- **Integrity.** The command carries the digest; the sensor hashes what it writes
  and refuses an image that does not match. ESP-IDF separately validates the image
  header and its checksum before the bootloader will run it.
- **Rollback.** Two OTA slots with `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`. A new
  image boots as pending; it marks itself valid only once it has reached the point
  of advertising, which is the sensor's whole job. An image that crashes first is
  replaced by the previous one at the next reset, without anybody's help.
- **Authority.** The command arrives only over the bonded, encrypted link, from
  the one hub that owns the sensor.
- **Exposure.** The management API stays on loopback. Images are served by a
  separate read-only server on the household network, which answers exactly one
  shape of request and holds nothing private: the firmware is the same file this
  repository builds.

## Partitions

OTA needs two application slots, which the current single-app layout does not
have, and 4 MB of addressable flash, which the current 2 MB header does not have
although the board has always had the flash.

```
nvs,      data, nvs,     0x9000,   0x6000
otadata,  data, ota,     0xF000,   0x2000
phy_init, data, phy,     0x11000,  0x1000
ota_0,    app,  ota_0,   0x20000,  0x190000
ota_1,    app,  ota_1,   0x1B0000, 0x190000
```

`nvs` keeps its offset and size, so the one cable flash that introduces this
layout does not cost the sensor its bond, its pairing code, or its configuration.

**This first step needs a cable.** A partition table cannot be replaced over the
air by the application it is replacing. Every update after it can.

## States

Per sensor, alongside the existing configuration state:

| State | Meaning |
| --- | --- |
| `idle` | nothing requested |
| `pending` | an image is chosen; the command has not reached the sensor |
| `commanded` | the sensor has taken the command |
| `downloading` | the sensor is fetching, with a percentage |
| `installing` | written; switching slots |
| `rebooting` | the sensor has gone to restart |
| `succeeded` | the sensor came back reporting the target version |
| `failed` | with a reason: unreachable, download, digest, flash, or rejected |

## Plan

1. **Hub storage.** Image parsing, the image table, and the files on disk.
2. **Hub interface.** Upload and list images in Settings; choose and start an
   update on a sensor's configuration page; show progress.
3. **Hub delivery.** Encode the command, send it on the next advertisement the
   way configuration is sent, poll progress, and recognise the returning version.
4. **Sensor.** Partitions and flash size, the update task, the progress suffix,
   rollback confirmation, and staying awake while an update runs.
5. **Validation.** Host tests throughout; then a cable flash of the first
   OTA-capable image, and an update of that sensor over the air.

## Validation

Host checks, build, flash, and device behaviour are separate claims.

- [ ] An uploaded image is parsed, stored once per digest, and served.
- [ ] Anything that is not an `open_plant_pulse` esp32c3 image is refused.
- [ ] The command encodes and decodes, and is distinguishable from every other
      payload version on the characteristic.
- [ ] The whole flow is covered by host tests with no hardware.
- [ ] The OTA-capable image builds and is flashed by cable, keeping its bond.
- [ ] A sensor updates over the air and returns reporting the new version.
- [ ] An image that does not match its digest is refused by the sensor.
- [ ] A deliberately broken image rolls back without help.

## Not in this proposal

- Updating several sensors at once. One at a time first.
- Downloading images from the internet. The hub serves what somebody put on it.
- Signed images. Worth doing, and a different piece of work: it needs a key that
  outlives any one hub, and secure boot to mean anything.
