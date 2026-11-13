# Watering forecast

## Status

Proposed 2026-11-12. Not implemented. Nothing here has been built; this is a
brainstorm of where the forecast could go and what the industry already knows,
written after an alocasia in a chunky aroid mix read 59 % full on its bar and
"Water now" on its chip, on the same card, at the same moment.

The convergence figures in "Converging on the thirsty weight" are simulated,
not observed: 200 runs against a known answer, using this plant's real band.
They say the method settles; they do not say it settles on what this plant
actually wants, which only the plant can say.

Corrected 2026-11-13: an earlier draft described the deferral press as
something made when its owner was going away or too busy. That was the author's
assumption and not the case -- the press is made after feeling water in the
soil. The sections on what the press means, on how much it is trusted, and on
whether a second "not now" button was needed, are rewritten accordingly.

Companion to [learned-watering-thresholds.md](learned-watering-thresholds.md),
which does the same job for the soil probe's percentages. This one is about the
scale.

## What the forecast does today

`watering_forecast` in `store.py`:

1. `to_go = latest_grams - thirsty_grams`
2. `typical = mean(grams_per_day)` over the spans since the last watering
3. `days = to_go / typical`

and the card says `days < 1 ? "Water now" : ...`.

For the alocasia on 2026-11-12:

```
usable band   full 762 - dry 607      = 155 g
where it is   (699-607)/155           = 59 %     <- the bar
thirsty mark  (669-607)/155           = 40 %
headroom      699 - 669               = 30 g
drinking      34 g/day                            (this cycle, 2 spans)
days          30 / 34                 = 0.88 -> 0.9
whole cycle   155 / 34                = 4.6 days full to dry
```

The arithmetic is right. Three things about it are not.

## What is wrong with it

**It is a straight line through a curve.** `to_go / typical` assumes the pot
loses weight at a constant rate until it hits the mark. A pot does not. Loss
falls away as the substrate dries and the plant closes its stomata, so a linear
extrapolation from a wet-end rate always predicts thirst too early. The error is
worst in exactly the case that prompted this: a mix whose easily available water
is nearly all given up in the first part of the cycle.

**It ignores what the hub already measures.** Each interval in `water_use`
already carries `drying_kpa` and `grams_per_day_per_kpa` -- a VPD-normalised
transpiration rate, computed and then unused. `light_days` already carries DLI
and lit hours. The forecast reads neither. It averages grams per day across
spans that happened under different air and different light, and calls the mean
a property of the plant.

**It claims a precision it does not have.** For this plant
`days_soonest == days_latest == days == 0.9`. The band is built from the spread
of observed spans, so a cycle with two similar spans produces a band of zero
width. The code's own comment elsewhere worries about this ("a certainty nobody
has") and then the card prints it anyway.

And the label conflates a forecast with a fact: `days < 1` reads "Water now"
whether the plant is twenty-one hours away or two hundred grams past its mark.

## What the industry does

Houseplant apps guess. Greenhouses measure, and have for forty years. The useful
prior art, roughly in order of how well it fits a hub with a load cell under one
pot:

**Weight-based (gravimetric) irrigation control.** What this hub already is: a
single-pot lysimeter. Commercial glasshouse systems trigger irrigation from
substrate weight directly, because weight is the one measurement that integrates
everything -- light, air, plant size, root health -- without a model. The hub
should lean on this harder, not less.

**The Baille model** (Baille, Baille & Delmon, 1994), the workhorse of greenhouse
transpiration:

```
E = A * (1 - exp(-k * LAI)) * Rg  +  LAI * B * VPD
```

A radiation term and a vapour-deficit term, two fitted coefficients. It is used
because it is the simplest thing that works, and because `A` and `B` can be
fitted per crop from measured transpiration. A hub with a lysimeter can fit them
per *plant*, from its own history, which is better than any published
coefficient for a houseplant nobody has published coefficients for. The canopy
term collapses into the constants for a single pot that is not changing size
week to week.

**Stanghellini (1987)** is the fuller Penman-Monteith adaptation for greenhouses,
with leaf area index, two canopy layers and aerodynamic resistance. It is more
right and needs inputs a houseplant hub cannot get (net radiation at the canopy,
leaf temperature). Worth knowing about; not worth building.

**FAO-56 water balance and Management Allowed Depletion.** Irrigation scheduling
by checkbook: track the water balance, and irrigate when depletion reaches a set
fraction of the available water rather than at a single threshold weight. MAD is
a *fraction*, chosen per crop by how much stress it tolerates -- which is exactly
what the profiles already say in words ("let the top third dry", `strategy:
partial_dry_back`) and nowhere in numbers on the weight side.

**de Boodt substrate analysis.** Growing media are characterised by container
capacity, easily available water (the water released between 1 and 5 kPa
tension), and water buffering capacity. This is the vocabulary for the
alocasia's problem: a chunky bark-and-perlite mix has high air-filled porosity
and low easily available water, so its full-to-dry band is narrow and it is
crossed quickly. The hub measures that band in grams every cycle and currently
uses it only as a denominator for a percentage.

**Crop steering / dry-back targets.** Controlled-environment practice (Dutch
horticulture, and the cannabis industry which documented it heavily) manages the
*daily dry-back percentage* rather than a trigger point: a target fraction of
substrate weight to lose between irrigations, with generative or vegetative
intent. It is a steering input, not just an alarm.

## Where this hub could go

Staged, because each stage is useful alone and each needs the one before it.

### 1. Say which of three things is true

No model, only words. The chip today has two states and needs three:

| Condition | Chip |
|---|---|
| `to_go <= 0` | **Thirsty** -- it is at or past the mark now |
| `0 < days < 1` | **Today** |
| `days >= 1` | **Water in N d** |

`thirsty_now` already exists in the forecast and is already returned. Nothing
reads it. This is a one-line fix to a real misreading and should not wait for
any of what follows.

### 2. Forecast through the night

A plant transpires in the light and nearly stops in the dark. The hub knows the
photoperiod *in advance* -- it controls the lights, and `light_days` records
`lit_hours`. So instead of `to_go / typical`, integrate forward hour by hour
with a day rate and a night rate, both of which the lysimeter can separate from
spans that fall wholly inside one or the other.

A plant 30 g from its mark at 34 g/day reads 0.9 days. If 80 % of that loss
happens in 13 lit hours, and the lights have just gone off, the honest answer is
not "21 hours" but "tomorrow afternoon".

### 3. Fit the two-term model per plant

The data is already in the right shape. Each interval has grams lost, duration,
mean VPD, and (joinable) the DLI over the same window. Regress:

```
grams_per_day ~= A * DLI  +  B * VPD_daylight
```

A and B fitted per plant by least squares over the window, refitted as spans
arrive. Then the forecast integrates the *expected* next days -- known light
schedule, room's own diurnal temperature and humidity -- instead of assuming
tomorrow repeats the mean of last week.

This is Baille, with the canopy terms folded into the constants because the
canopy is one pot and not changing fast. It is also the first point at which the
forecast would answer the alocasia question correctly: a high rate measured
under a dry spell in a lit room is not the rate that will hold overnight.

Two guards: refuse to fit on fewer than a handful of spans and fall back to the
flat rate, and keep the fit per plant, never shared across plants or profiles.

### 4. A band, not a point

Replace the single `thirsty` weight with a depletion fraction taken from the
profile's existing `strategy`:

| Strategy | MAD | Meaning |
|---|---|---|
| `full_dry_back` | ~0.9 | the orchid, the sansevieria: nearly all of it |
| `partial_dry_back` | ~0.5 | the alocasia: the top third, give or take |
| `steady_moist` | ~0.3 | the fern that sulks if it ever dries |

The weight at which to water becomes `full - MAD * (full - dry)`, which is a
number the hub can state and the user can override, rather than a mark set by
hand on one day and then trusted for ever. The hand-set `thirsty` anchor stays
as the override; what changes is that there is a sensible default when there
isn't one.

### 5. Say how sure it is

The band should come from the residuals of the fit, not from the spread of raw
spans. A model that has been predicting within 10 % for a fortnight has earned a
narrow band; a new pot with three weighings has not, and should say so rather
than printing `0.9 to 0.9`.

## What was built, and what the data refused

Implemented 2026-11-13, in the order the measurements allowed.

**Stage 1, the three states.** Done. "Water now" is "Thirsty" when the pot is
at or past its mark, "Today" when the forecast is under a day, and "Water in
N d" otherwise.

**Stage 3, the air divided out.** Done, with one change the backtest forced.
The two-coefficient fit of `a + b * kPa` was worse than the plain mean at
every sample size -- a slope through noise on this much history. Dividing by
the deficit and multiplying by what the air is doing lately was better at
every size, so the air enters as a divisor and not as a second coefficient.
It waits for eight spans; below that the plain mean is steadier. Measured
walking forward, mean error in grams a day: 19.1 against 16.7 at six spans,
15.5 against 13.0 at eight, 11.4 against 8.8 at ten.

**Stage 5, the band.** Done. It is the median miss of this plant's own
walked-forward history rather than the spread of its weather, with a floor so
three weighings cannot buy a narrow answer and a ceiling so the slow end never
divides by zero.

**Stage 2, the night, was not built.** It cannot be, here. Separating a day
rate from a night rate needs spans that sit inside one or the other, and
eighteen of this household's nineteen weighed plants are weighed about once a
day: every span straddles both. The one exception is the plant on the
automatic scale. When more pots are weighed automatically there will be
something to fit; until then it would be a model with no observations under
it.

**Stage 4, the depletion fraction, was not built.** Nineteen of twenty plants
already have a thirsty weight set by hand, and the twentieth is an air plant
with no pot weighing at all. The fraction exists to supply a default where
nobody has said; nobody here has not said. What fact forces this to exist? A
household that weighs pots and does not mark them, which is not this one.
Worth revisiting when a plant arrives without one.

**The learner is waiting on evidence.** The button is on the card and the
presses are recorded with the weight they were made at. Nothing reads them
yet, and nothing should until there are enough to measure the lapse rate and
the surprise threshold against, rather than the author's guesses at them.

## What not to build

**Not a weather API.** The room has its own sensors and its own box; outside
weather reaches these plants only through a window and a thermostat, both
already in the measurements.

**Not leaf-level Penman-Monteith.** It needs leaf temperature and a canopy
model. The lysimeter already integrates what those terms are for.

**Not a shared model across plants.** Every coefficient here is a property of
one plant in one pot in one window. The moment they are averaged across plants
the hub is guessing again, which is the thing being fixed.

**Not a second code path.** This replaces `watering_forecast`; it does not sit
beside it. The repository keeps no v1/v2 pair.

## Open questions

- How many spans before a fit is trusted over the flat mean? A guess is five;
  the honest answer is whichever leaves prediction error lower on the history
  already recorded, which can be measured offline against existing data.
- Does the daily cycle need modelling at all for plants under a fixed indoor
  photoperiod, or does stage 2 collapse into stage 3 once DLI is a term?
- Where does a watering that the scale saw but nobody logged fit in? The
  forecast resets on the `watered` anchor; a quiet top-up is a step in the
  weights with no care event.
- Should the dry-back fraction be learned too? A plant that is never allowed to
  reach its mark has no evidence about what it tolerates.

## Three layers, not one model

Everything above is physics: what the pot weighs, what the air is doing, what
the lights will do. Physics does not know that this plant has been watered
every nine days for a year, and it does not know what the person holding the
pot knows. Those are two more kinds of evidence, and they belong in order:

| Layer | Answers | Evidence |
|---|---|---|
| **Physics** | what is happening now | weight, VPD, DLI |
| **Rhythm** | what usually happens to *this* plant | its own watering history, its own moisture history |
| **Corrections** | what the person knows that neither can see | two events, below |

Each layer only overrides the one beneath it where it has evidence. A new pot
has no rhythm and no corrections, so it gets physics. A pot with four years of
history and a person who keeps saying "not yet" should end up somewhere the
physics alone would not have put it.

### The rhythm layer

Three things are already in the database and unused by the forecast:

**The interval between waterings.** 71 waterings are recorded. The gap between
them is a distribution, not a mean -- a plant watered every 8 to 11 days has a
rhythm; one watered at 3, 19 and 7 days has not, and the hub should be able to
tell those apart and say which this is.

**The depletion actually reached.** For every watering, the pot's weight just
before it says what fraction of the band the person let it lose. That is the
MAD of section 4 *measured* rather than taken from the profile -- what this
household actually tolerates for this plant, which is the number that matters.

**The moisture at watering time.** Where there is a probe, the reading just
before each watering is the same quantity in the other instrument's units. This
is exactly what [learned-watering-thresholds.md](learned-watering-thresholds.md)
proposes to learn, and the two should learn it once and share it: the probe says
when the root zone is dry, the scale says how much water that was. Two
instruments, one threshold, not two numbers that can disagree.

The rhythm is a **prior**, the physical model a **likelihood**. With two spans
and no history, lean on the rhythm. With a fortnight of spans fitting well,
lean on the model. The weight between them should be the model's own residual
error, which section 5 already has to compute.

### Two corrections the person can make

**"Still too wet"** -- the hub said water, the person looked and disagreed.

This is the one piece of evidence nothing else can supply, and it is worth
being exact about what it is. The press is made by putting a finger into the
soil and feeling water under the surface. It is not a mood, a postponement, or
somebody being out on Tuesday: it is a second instrument reading the root zone
directly, at a moment when the weight, the depletion and the probe's own figure
are all known.

That is a stronger thing than this proposal first assumed. It had the press
down as the noisy input and the scale as the truth, and it is closer to the
reverse: the scale measures the water in the whole pot and *infers* the root
zone, while a finger reads the root zone and infers nothing. When the two
disagree it is not obvious the scale is right.

So there is no second verb. An earlier draft had "still too wet" beside a "not
now, I am busy" that would move the reminder and teach nothing, to keep
convenience presses out of the arithmetic. Those presses are not made, and a
second way to say a thing that earns nothing is a bug waiting for somebody to
pick the wrong one. One button, and it means what it says.

**"I watered it"** -- recorded after the fact, for a watering the hub never saw.

The hub already catches this where it can: a sudden gain on the scale is read as
a watering, with a proportional threshold, a floor and a ceiling, and it asks
("That is a big change -- Yes, I watered it / No, just heavier"). What it cannot
catch is a plant with no scale, a top-up smaller than the floor, or a pot
watered while it was off the scale.

Recording one late is not just a log entry. The forecast takes its rate from
the spans *since the last watering*, so inserting a watering retroactively
re-segments the history: spans that were read as one long dry-down turn out to
be two cycles, and the rate computed across them was never real. Any fitted
coefficients over that window need recomputing, not patching.

### The hub has to record what it asked

`watering_due` exists as a care kind, is queried in four places, and **has no
rows**. The hub asks on the card and never writes down that it asked.

Nothing above can be learned without that record. A correction is only
meaningful against a question: "said water on the 3rd, person deferred as too
wet, watered on the 6th" is a training example; "watered on the 6th" alone is
not. Writing the ask is the smallest prerequisite for every other idea in this
section, and it is independent of all of them.

### What could go wrong

**The threshold drifts down for ever.** If every reminder is deferred, the
trigger follows, and the plant gets drier each cycle until something dies. The
profile's `concern_below` and the measured dry weight are floors that
corrections must not cross, and when corrections have moved the threshold far
the hub should say so rather than quietly obey.

**Deferrals are never contradicted.** A person who defers and is wrong gets no
feedback, because the hub cannot see a wilt. The only honest signal is the
next watering: deferred on the 3rd, watered on the 4th without being asked,
means the deferral was wrong, and that is learnable. Deferred and then watered
on schedule a week later means it was right.

**A rhythm that is really a habit.** Watering every Sunday is a property of the
person, not the plant. A rhythm that matches the calendar better than it
matches the weighings should be reported as what it is, not fitted as though
the plant chose it.

## Converging on the thirsty weight

The section above says corrections "move the threshold" and then proposes
asking the person to confirm each move. That is the wrong shape: it turns every
hint into a question, and the hint was offered precisely so that no question
would be needed. What follows replaces it.

### Which number is being learned

Not grams. The **depletion fraction** -- how far through the band from full to
dry the plant is willing to go:

```
theta in [0, 1]        thirsty_grams = full - theta * (full - dry)
```

Grams are a property of this pot with this soil at this moment; the fraction
survives a repotting, a changed `full`, a heavier cache pot. It is also the MAD
of section 4, which means the profile's `strategy` supplies its starting point
and the plant's own history refines it.

### Each event is a one-sided fact

| Event | At depletion `d` | What it says |
|---|---|---|
| "Still too wet" | the plant did not want water at `d` | `theta > d` |
| Watered (prompted or not) | the plant wanted water by `d` | `theta <= d` |

Neither pins `theta` down; each rules out part of the range. That is the exact
shape of threshold estimation from binary responses, which psychophysics has
been doing since the 1950s and solved well.

### The algorithm: a posterior on a grid

`theta` is bounded in [0, 1] and one-dimensional, so there is no need for
conjugacy, gradients or step sizes. Keep a probability over a grid of 101 bins
(one per percent of the band) and multiply by a likelihood on each event:

```
P(answer | theta) = 1 - lapse   when the answer agrees with theta
                  = lapse       when it does not
```

Then renormalise. The estimate is the posterior mean; the spread is the band
that section 5 wants anyway. This is the QUEST procedure (Watson & Pelli, 1983)
in its simplest form, which exists because waterings are scarce -- roughly one a
week -- and every event has to count.

Three parts are not optional:

**A lapse rate, small but not zero.** A finger in the soil is a measurement,
so the floor is there for the way two honest instruments disagree rather than
for carelessness: a finger reaches a few centimetres, and a pot can be dry on
top and wet at the base, or the reverse in a chunky mix that drains fast. Both
readings can be right and still differ. Without any floor one unusual pot
assigns probability zero to the truth and it never recovers; at around one in
twenty, no single press is decisive and an odd one is repaired by the next few.
Nothing here needs the person to be unreliable -- it needs the hub not to treat
any one observation as infallible.

**Forgetting, but only on evidence.** The posterior must be able to move when
the plant itself changes, or it will argue with a plant that has grown. The
obvious way -- blend a little flat prior back after every event -- does not
work, and the measurements below say why: it is a permanent noise floor, so the
estimate never converges at all. Instead, watch how well the belief predicts
each incoming answer, and flatten it only when it has been wrong consistently
over the last several events. Nothing is forgotten while the belief is right.

**A reset on repotting.** New soil, new band, new everything. The hub already
records repottings, and the posterior should return to the profile's prior
rather than carry the old pot's conclusion into a pot that does not exist. This
is not something to discover through surprise: the event is already recorded, so
the reset should be immediate rather than waiting two dozen waterings to be
inferred.

### What it does on this plant

Plant 20's real band (full 762 g, dry 607 g), told `thirsty` is 669 g, with the
plant actually wanting water at 629 g, and a person who is wrong one time in
ten. Simulated 200 times over 400 events, measured after the first hundred:

```
                               settles   swing   reverses   shown moves
  flatten a little every event   631.3 g  59.2 g     50 %      32.7 %
  flatten on sustained surprise  628.7 g   2.9 g      3 %       0.5 %
  never flatten                  628.7 g   0.8 g      2 %       0.4 %
```

The first row is why constant forgetting is not in this proposal. Reversing
direction on half of all events is a random walk, not an estimate, and a 59 g
wander on a 155 g band would be plainly visible on the page: the number would
never look settled, and would not deserve to.

The second row converges. Under 2 % of the band, direction reversing 3 % of the
time -- which is to say it has stopped moving -- and with a 2 g deadband the
displayed figure changes about twice in four hundred events.

It is also not a compromise. On a plant whose true want moves from 629 g to
600 g part-way through, flattening on surprise notices in a median of 24 events
and in 99 % of runs; never flattening takes 64 events and misses it in half of
them. Better on both axes at once, which is rare enough to be worth stating
plainly.

Those figures assume the hub asks wherever the plant happens to be, not at the
most informative weight it could choose. Asking at its own estimate is about
1 g more accurate -- worth knowing, not worth engineering.

How quickly it gets there depends on how much a press is trusted, and a finger
in the soil is trusted more than the first draft assumed. Presses needed before
the estimate is within 5 g of the truth, from 300 runs:

```
  wrong 1 press in 10        4 presses      3.6 g out after ten
  wrong 1 press in 20        5 presses      2.5 g out after ten
  wrong 1 press in 50        2 presses      2.1 g out after ten
```

The estimate lands in a handful of presses at any of these. What takes longer
is the confidence around it -- knowing it has settled rather than happening to
be near -- so the first weeks are about earning a narrow band, not about
finding the number.

### Not all events weigh the same

A deliberate "still too wet" is a considered judgement about a plant somebody
has just looked at. A routine watering is weaker evidence: it may mean the
plant asked, or that it was Sunday. They should carry different lapse rates --
the considered answer narrow, the routine watering wide -- rather than being
counted alike.

An unprompted watering well above the current estimate is weakest of all, and
may be no evidence about thirst at all.

### Why not the obvious alternatives

**Robbins-Monro stochastic approximation** (`theta += a_n * signal`, with
`a_n = c/n`) is the classic answer and converges provably. It needs a step size
chosen by hand, it gives no uncertainty, and its shrinking step is exactly what
stops it following a plant that genuinely changes.

**An exponentially weighted average** tracks change and is two lines, but it
has no notion of confidence, cannot treat a considered answer differently from
a routine one, and -- being a constant step -- never settles. That last point
is the one that decides it: a fixed gain keeps stepping for ever because the
step does not know the answer has been found. Convergence is the property being
bought here, and a constant-gain loop cannot have it.

**Any of these fed the raw answer.** All that arrives is one bit: "too wet"
says the mark is lower, not by how much. An update driven by the sign of an
error and nothing else chatters, in the way a bang-bang controller does. The
likelihood in the grid version exists precisely to turn a bit into evidence,
and the lapse rate is what stops a single bit being decisive.

**Logistic regression on (depletion, answer)** is the batch version of the same
idea and would work; the grid is the online form of it, needs no refitting, and
hands back a full distribution rather than a point and a standard error.

### Still visible, still the person's

Converging silently is what was asked for, and it does not mean hiding the
result. The learned weight should be on the page, labelled as learned, with
what it was before. Re-stating `Thirsty` by hand stays the override it is
today, and should collapse the posterior onto the stated value rather than
being averaged in as one more vote -- a typed number is not an opinion about a
threshold, it is the threshold.

## Where the hint is given

On the dashboard card, beside the chip that prompted it. The card already ends
with a row of tags:

```html
<div class="fleet-card-tags">
  <small class="fleet-card-due"></small>   <!-- "Water now" / "Water in 3d" -->
  <small></small>                          <!-- the sensor's own state -->
  <small class="fleet-card-rate"></small>  <!-- "34 ml/d" -->
</div>
```

The hint belongs next to `.fleet-card-due`, because that is the sentence being
disagreed with. Correcting the hub where it is wrong, rather than on a settings
page two taps away, is the difference between a feature that is used and one
that is not.

### Three rules about when it appears

**Only when the hub is asking.** No prompt, no button. A plant three days from
thirsty has not made a claim worth disputing, and a row of buttons on every
card for a question nobody asked is noise. The control is bound to the "Water
now" state, not to the card.

**Not again once answered.** Having said "still too wet", the person should not
be asked again that afternoon. The hub has the answer; repeating the question
is how a helpful control becomes an irritation.

**Again when the question is genuinely new.** This is the part that needs a
rule rather than a timer. Deferring at 699 g and being asked again at 697 g is
the same question; being asked at 650 g is not -- the plant has moved and so
has the evidence. So the hint returns once the pot has lost a material fraction
of its remaining headroom since the deferral, or once it is watered, whichever
comes first. A fixed cooldown in hours would be wrong in both directions: too
short for a slow pot in winter, too long for this alocasia, which crosses its
whole band in 4.6 days.

### The same rule keeps the arithmetic honest

Suppressing the repeat is not only manners. Each press is an observation fed to
the posterior, and two presses at 699 g and 697 g are not two observations --
they are one fact, counted twice. A posterior that counts them twice narrows
its band as though it had learned something, and the confidence it reports is
then false.

So the suppression window and the evidence window are the same window, and the
rule is one rule: **a deferral is one observation until the pot has moved.**
The UI and the estimator agree because they are expressing the same thing.

### What the button says

One verb, not a menu. "Still too wet" is the considered judgement, and it is
the only one that should teach:

- it is pressed while looking at the plant,
- it disputes a specific claim the card just made,
- and the weight at that moment is what makes it evidence.

There is no second verb for postponing. An earlier draft proposed one, to keep
convenience presses out of the arithmetic; they are not made, and the button
would have been a control that existed for a user who does not.

The other correction, "I watered it and the hub did not notice", does not
belong here at all: by the time it is true the card is no longer saying "Water
now". It belongs in the care log, where `Watered today` already is.
