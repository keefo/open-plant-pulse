# Alerts: one rule, any device, any measurement

## What forces this to exist

The hub can tell a phone that a leak sensor is wet. That is one
hard-coded condition on one kind of device, written into
`ZigbeeService._raise_alarm`, and the next thing anybody wants —
"tell me if the bedroom drops below 16 °C" — has nowhere to go.

Two facts shape the design:

- The condition is not a property of the device kind. A thermometer,
  a soil probe and a scale all report numbers somebody might want a
  line drawn under, and the hub already keeps every measurement that
  arrives. Asking "is this a leak sensor?" to decide what can raise an
  alarm is the mistake the charts made before they were keyed to the
  record instead.
- The response is not one thing. A phone is right when nobody is in;
  a sound from the iMac is right when somebody is, and which sound
  says which device without reading the screen.

## The shape

One row is one rule:

    sensor_id      which device
    measurement    the key in its readings: wet, air_temperature_c, rh_pct, ...
    test           above | below | true
    threshold      a number; unused by `true`
    sound          a macOS system sound, or nothing
    repeat_s       0 plays it once; otherwise it sounds again every
                   N seconds until the reading leaves the condition
                   or somebody presses stop
    push           whether the phone is told as well
    enabled

Nothing about a kind of device appears in that row. A rule on
`air_temperature_c` works on anything that reports it, which is what
keeps a new sensor from arriving with no way to alarm on it.

`true` exists because `wet` is a boolean and bending it into
`above 0.5` would be a trick the next reader has to undo. Three tests
is a smaller lie than two tests and a convention.

### When a rule fires

On entering the condition, not while in it. A leak sensor repeats
itself every time it checks in and a thermometer reports every few
minutes; a rule that fires on the state fires for hours.

It re-arms when the reading leaves the condition, and will not fire
twice inside a quiet period even if it leaves and returns — a value
resting on the threshold otherwise rings every time it twitches by a
tenth of a degree.

### What a rule does

- **Sound**: `afplay /System/Library/Sounds/<name>.aiff`, in the hub's
  own login session. Verified to work from launchd. Fourteen sounds
  exist, so a device can have one of its own.
- **Push**: the ntfy address already in Settings → Alerts. One
  address for the house; the rule decides whether to use it.

Both are optional, and a rule with neither is a rule that does
nothing, so the page should not let one be saved.

## What this replaces

`_raise_alarm` and its hard-coded leak handling go. The leak becomes a
rule like any other, seeded by the migration for every leak sensor
already enrolled so nobody's alarm stops working at an upgrade. This
project keeps no second path: after this, the only thing that raises
an alarm is a rule.

## What it is not

- Not a rules engine. No and/or, no schedules, no "if this then that
  across three devices". Each of those is a thing nobody has asked
  for, and each one would need a language to express it.
- Not a notification centre. There is no inbox of past alerts beyond
  the raw reports already kept, because nothing has needed one.
- Not per-rule push addresses. One house, one phone until that is
  untrue.

## Repeating, per rule

Settled as a number of seconds on the rule rather than a switch
somewhere global: water on a floor is worth sounding until somebody
comes, and a bedroom a degree under is not, and those are rules on the
same hub. One column says both -- 0 for once, 30 for every half minute
-- which is one way to say a thing rather than a flag plus a mode.

A repeating sound stops when the reading leaves the condition, or when
somebody presses stop. It also stops if the hub restarts, which is
honest: nothing is sounding then.

## Which measurements can be alarmed on

Whatever the device has reported, read off its own kept reports, plus
battery on anything with one. The first means a new kind of sensor
gets rules without a code change; the second is deliberate, because a
leak sensor with a flat cell is an alarm that fails silently, and that
is the one failure nobody would notice.
