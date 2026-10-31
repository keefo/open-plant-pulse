# Holding the box at a humidity

## What forces this to exist

The room called `vzbot` is a VzBoT 3D-printer enclosure used as a
plant box. Sealed, it runs to 86 % RH and swings 7.5 °C in a day,
against 53 % and 3.9 °C in the living room. The begonias in it want
55-75 %, and the way out of the top of that range is the enclosure's
exhaust fan, driven by hand today:

    SET_FAN_SPEED FAN=Exhaust_fan SPEED=0.1

A hand on a fan is a person watching a number. The hub already has
the number.

## The loop

    error  = measured RH - target RH        positive means too humid
    output = Kp*error + Ki*∫error dt - Kd*d(measured)/dt
    fan    = clamp(output, 0, 1)

Three things about this control problem shape the code more than the
arithmetic does.

**The measurement arrives every few minutes, not every second.** A
Zigbee thermometer reports when it feels like it -- seven minutes is
typical, half an hour is allowed. So the loop steps *when a reading
arrives*, using the real seconds since the previous one, and never on
a timer: integrating the same measurement once a second for seven
minutes is how an integral runs away from you.

**The derivative term is nearly useless here and defaults to zero.**
A difference between two readings seven minutes apart, each rounded to
a tenth of a percent, is mostly noise amplified by a short baseline.
It is taken on the measurement rather than the error, so changing the
target does not kick the fan, and it is there to be turned up by
somebody who wants it rather than to be on by default.

**The fan does not start at 0.01.** Below a floor it does nothing but
hum, so anything under it is sent as zero, and the integral is held
while the output is clamped so it cannot wind up against a limit it
can never beat.

## When the hub takes its hand off

- **While the printer is printing or paused.** The print owns that
  fan; some of them set it themselves for an ASA layer. `print_stats.
  state` says which, and the controller stands down and reports that
  it has.
- **When the sensor has gone quiet**, by default half an hour. A
  controller acting on an old number is guessing, and the fan goes
  off rather than being left running on a reading from this morning.
- **When somebody sets a speed by hand**, which switches the mode to
  manual and leaves it there until they say otherwise. The hub does
  not quietly take a control back.

## What it is not

- Not a temperature controller. The box's heat is the printer's and
  the room's, and a second loop fighting the first over one fan is a
  thing to want only once somebody has wanted it.
- Not a schedule. Humidity is not a time of day.
- Not a framework of fans and drivers. There is one fan, reached over
  one HTTP call, and one row of settings says where it is.
- Not woven into the room pages. This is one box with one fan and a
  printer behind it, and it was asked for as a page of its own:
  Settings has a tab, the rest of the hub carries on not knowing. A
  room keeps meaning a room.

## Where it lives

    application/airflow.py        the fan, the loop, and the printer
    Settings -> Box               the page: mode, target, gains, speed
    /api/fan                      read it, set it

Nothing outside those needs to change. The sensor is read through the
store the same way every other reading is.
