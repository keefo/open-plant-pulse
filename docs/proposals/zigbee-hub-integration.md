# Zigbee gateway and its control panel

## Status

Proposed. The gateway firmware and one Zigbee sensor work end to end; nothing
of the hub integration described here is implemented.

What exists today, verified on hardware:

- An ESP32-C6 gateway (`gateways/zigbee-gateway`) forms a network,
  holds joining open, and prints one JSON line per reading on USB serial.
- The HX711 scale speaks Zigbee alongside Wi-Fi, reports weight, accepts
  commands, and stays reachable for over-the-air updates.
- `watch.py` follows the link from a terminal and can send commands.

What does not exist: any hub involvement at all. The hub has never spoken to
the gateway.

## Goal

A page at `/settings/zigbee` that is the whole control surface for the Zigbee
side: what the gateway is doing, which devices are on it, how healthy each
one is, and how to add, configure and remove them. No terminal.

## Address devices by EUI-64, never by short address

This decision shapes everything else, so it comes first.

A Zigbee short address is assigned by the gateway when a device joins and
is handed out again later. During one evening of testing, a single scale held
three of them in turn:

    0xc458  ->  0x63a1  ->  0x40e5

Its EUI-64, `ac:eb:e6:ff:fe:0d:7f:18`, never changed.

Anything that remembers a short address will eventually address a device that
no longer exists. That is not hypothetical: the gateway's first version kept
"the last device that reported" and sent commands there, which produced
delivery failures that looked exactly like a protocol limitation and cost an
hour of debugging.

So the hub speaks EUI-64 and only EUI-64. The gateway keeps the mapping and
translates, using `ezb_address_short_by_extended()`. An unknown EUI-64 is an
error, never a guess.

## The wire protocol

Line-oriented JSON over USB serial, both directions. From the gateway:

```json
{"t":"reading","eui":"acebe6fffe0d7f18","grams":185.0,"batt":3.9,"lqi":10,"rssi":-19}
{"t":"joined","eui":"acebe6fffe0d7f18","short":"0x40e5"}
{"t":"left","eui":"acebe6fffe0d7f18"}
{"t":"ack","id":7,"status":0}
```

To the gateway:

```json
{"t":"cmd","id":7,"eui":"acebe6fffe0d7f18","cmd":"w 0"}
{"t":"list"}
{"t":"permit","seconds":180}
```

The request id and its acknowledgement are not ceremony. Delivery genuinely
fails: a sleepy device that never collects a queued frame produces APS status
`0xA7`, and without an explicit acknowledgement that is indistinguishable from
success. The hub should hold a command as pending until acked, then retry or
surface it.

`list` lets the hub rebuild its view after a restart without waiting for every
device to report.

## What goes on the page

### Gateway health

PAN id, channel, gateway EUI-64, firmware build, uptime, and the serial
port in use. Above all, whether the gateway is *present*.

This is worth stating plainly because the failure is silent. A gateway that
has been unplugged leaves a stale `/dev/cu.*` node behind on macOS: opening it
succeeds and reading it returns nothing, forever. That is identical to a
healthy gateway with no devices reporting. The page must tell those apart, or
every quiet evening looks like a fault.

### Pairing

A button that opens joining for a few minutes, showing the time remaining, and
a live list of devices as they join.

Two reasons for the countdown. A closed join window was a repeated source of
confusion during bring-up, with devices failing to join and nothing saying
why. And the gateway currently holds joining open permanently, which was
right for bring-up and is wrong to live with: a scan from the scale found
eight neighbouring Zigbee networks. Joining should default to closed and be
opened deliberately.

### Devices

Name, label, type, the plant it is attached to, battery, last seen, and link
quality. Keyed on EUI-64; the short address appears only as a diagnostic,
marked as something that changes.

A device that has joined but has not been named or typed yet needs an
*unclaimed* state at the top of the list. Without it, a successful join is
invisible and the device simply sits there unused.

### Health, per device

- Rejoins. A count that climbs on its own is a link that keeps dropping.
- Reports received against reports expected, from the device's own configured
  interval. This is the one that catches a failing device while its last
  reading still looks perfectly reasonable.
- Command history: what was sent and whether it was acknowledged.
- The last error the device reported.

These are the slow faults. None of them is visible from a weight that looks
about right.

### Control

Tare, Wi-Fi on and off, sampling interval -- each addressed to an explicit
EUI-64.

Pending commands need to be shown as pending. A sleepy device collects on its
next poll, so "queued, waiting for the device" is a normal state and not an
error.

### Removing a device

Two different verbs, because they fail differently:

- **Remove** asks the device to leave the network and drops it from the hub.
- **Forget** drops it from the hub only, for a device that is dead, lost, or
  otherwise unable to be told anything.

With only one of them, a removed device that is still powered rejoins and
reappears, which reads as a bug.

### Live traffic

A tail of what the gateway is saying. Useful, not essential, and therefore
late in the order of work.

A serial port admits one program at a time, so once the hub owns the port
`watch.py` stops working. That is accepted: `watch.py` is a development tool,
not something to design the architecture around. It stays useful whenever the
hub is not running, which is exactly when firmware is being worked on.

### Channel and neighbours

Later: an energy scan and the neighbour list. The network currently shares
channel 11 with at least two neighbouring networks, and knowing that before
suspecting the firmware is worth something.

## Three layers, so the gateway can be replaced

The gateway is an ESP32-C6 today and may be an off-the-shelf Zigbee USB
dongle tomorrow. Those two are not interchangeable parts, and pretending they
are is how the abstraction ends up in the wrong place:

- The **C6 runs the Zigbee stack itself**. The hub receives application data
  and never sees a network key.
- A **dongle is a radio and nothing more**. The stack moves into the hub,
  normally via `zigpy`, and the hub becomes responsible for network
  management, security and the device table.

So the seam cannot be "swap the serial driver". It goes here:

**1. Transport.** Moving bytes to and from the gateway. For the C6, lines
of JSON over USB serial. For a dongle, EZSP or similar under `zigpy`. Nothing
above this layer knows which.

**2. Network.** Zigbee as concepts rather than wire format:

    devices()                     -> [{eui64, short, type, last_seen}]
    permit_join(seconds)
    send(eui64, endpoint, cluster, payload) -> request_id
    remove(eui64)
    events: reading | joined | left | ack

Both backends can implement all of it. This is the interface the rest of the
hub is written against.

**3. Device drivers.** What a particular device's bytes mean: that a scale
speaks cluster `0xFC00` on endpoint 10, that its reading unpacks to grams and
a battery voltage, that "tare" is the byte `t`.

The hub's sensor and plant logic sits above all three and sees none of it.

The rule that keeps the seam honest: **layer 2 must be expressed in Zigbee
terms, not C6 terms.** A `send_text("w 0")` interface would be the C6 gateway
leaking upward -- a dongle has no notion of what that string means. An APS
frame to a cluster is something both can carry.

Two things help here by accident. The reading protocol is already raw APS on
a manufacturer cluster, which `zigpy` sends and receives natively. And the
gateway already owns its routing table, so EUI-64 addressing is the interface
either way.

What the swap would still cost, honestly: network state lives in the C6's
flash today and would have to live on the hub's disk instead, so devices
would need to rejoin unless the network key were migrated deliberately.
Layering makes the change small; it does not make it free.

One consequence worth weighing now. Raw APS on a private cluster means only
this hub understands these devices. Modelling the scale as a standard ZCL
cluster instead would let Home Assistant or any other gateway read it,
at the cost of fitting weight into a data model that has no weight in it.

## Decisions worth making before any code

**Staleness is relative.** A device reporting every four hours is not late
after five minutes. "Last seen" must be judged against that device's own
interval.

**Show the effective rate, and where it came from.** A compile-time floor once
silently overrode a sampling rate chosen on a page; the page showed one number
while the radio used another, and nothing revealed the disagreement. Any rate
shown should be the rate in force, with its origin.

**Confirm and log anything destructive.** Tare has no undo -- an accidental one
moved a calibrated zero by about ten grams, and the only repair is to empty
the pan and tare again. Tare, remove and factory reset should confirm, and
every command should land in a per-device log with a timestamp.

## Order of work

1. The gateway's routing table, EUI-64 to short address. Blocks everything.
2. The wire protocol above, replacing today's bare text commands.
3. The layer 2 interface, with the C6 serial backend behind it, inside the
   hub itself. A separate process was considered and is not worth the extra
   moving part. Writing the interface first, even with one implementation,
   is what keeps the dongle a later decision rather than a rewrite.
4. Gateway presence and the device list on the page.
5. Pairing with a countdown, closed by default.

Health counters, the live traffic tail, the channel map and Zigbee-side
firmware updates follow.

## Open questions

- Zigbee has its own over-the-air update mechanism. The scale does not need it
  because it keeps Wi-Fi, but a future Zigbee-only sensor would.
- The reading payload is currently scale-shaped: thirteen packed bytes
  assuming grams. A second device type needs either a type field or a
  per-endpoint decoder before it can share the link.

## A Zigbee device is a sensor, not a parallel species

*Added after building the first version of the control panel, which got this
wrong.*

The first implementation kept Zigbee devices in a table of their own: an
accepted-EUI list in `hub_settings`, health counters in the service, and a
page that drew them from scratch. That was a mistake, and it showed up the
moment the question "how do I attach this scale to a plant?" was asked --
because the answer was "you cannot", and the reason was that none of the
machinery for attaching things to plants applied to a species the hub had
never heard of.

The `sensors` table already models all of it:

    identity_kind / identity_value   a stable identity -- "eui64" and the EUI-64
    transport                        "zigbee"
    enrollment_status                unclaimed | enrolled | archived
    display_name                     renaming
    plant_id                         attached plant
    room_id                          the room it is in

`enrollment_status = 'unclaimed'` is exactly the "waiting" state that was
reinvented, and `attach_sensor` is exactly the attachment that was missing.

So a Zigbee device that joins the radio becomes a sensor row with
`transport = 'zigbee'` and `enrollment_status = 'unclaimed'`. Accepting it
enrols it. Everything downstream -- naming, attaching, rooms, archiving,
history, the sensor pages -- then works because it is the same kind of thing
the hub already manages.

## What a device is *about* varies, and the driver says so

A scale is about a plant: what it measures is that plant's weight. A room
thermometer is about a room: the air in it, whatever is growing there. A
soil probe is about a plant again. The thing that varies is the subject, and
it varies per device kind.

So the driver declares it:

    class ScaleDriver:
        kind     = "scale"
        subject  = "plant"      # attaches to a plant

    class ThermometerDriver:
        kind     = "thermometer"
        subject  = "room"       # assigned to a room

Everything else follows from that one field. The device's page renders a
plant picker or a room picker; the reading is filed against a plant or a
room; the list says "on the white bird" or "in the kitchen". No code asks
"is this a scale?" to decide -- it asks the driver what the device is about.

A device with `subject = None` is one that observes the network rather than
the house, and gets no picker at all.

This is the part worth getting right before the second device type exists,
because the cost of the alternative is a page per kind with the same picker
copied into each, and a hub that knows what a scale is in a dozen places.
