# Making the interface feel its age, or not

## What forces this to exist

"Too old fashioned." That is a judgement about a feeling, so the first
job is to find what causes it. Counted in `app.css`:

    124 distinct hex colours        ten of them are tokens
    180 colour values               written into rules, not referenced
     26 font sizes                  41 uses of 12px, 29 of 13px, 17 of 11px
      8 corner radii                3, 4, 5, 6, 8, 12, 50%, 999px
     61 hairline borders            against 16 shadows
      0 dark mode rules

The look is not wrong. There is no system underneath it, so every
screen decides again, and the decisions have drifted apart over a
year. That drift is what reads as dated -- not the green, not the
serif, not the density.

See `ui-refresh-prototype.html` beside this file for the three states
rendered: today, proposed, and proposed after dark.

## What causes the feeling, specifically

**It is drawn with lines.** Sixty-one hairline borders. Every card,
every panel, every input is a box with an outline. Interfaces stopped
being drawn this way around 2016; they are now drawn with light --
surfaces lifted off a tinted canvas by a shadow a few percent dark.
A 1px grey rule reads as a table cell from a desktop application.

**The graph paper.** A 32px grid behind everything, which was a
texture people used when flat design needed warming up. It dates the
page on its own.

**The type is too small and there is too much of it.** The dominant
sizes are 11, 12 and 13px, with two things at `xx-small` (9px). The
density is right for a console, but density comes from spacing and
restraint, not from shrinking. A 15px base with fewer things said
reads denser than a 12px base with everything said.

**Georgia over a Gill Sans fallback.** Georgia for every heading is
an editorial choice that reads as a blog. And `"Avenir Next", "Gill
Sans", sans-serif` resolves to Avenir Next only on macOS; everywhere
else it falls to Gill Sans, which is a 1928 typeface, or to whatever
generic sans the browser keeps.

**Corners disagree.** Eight radii, and the common ones are 4, 5 and
6px, which is the 2014 idiom. The current one is larger and
consistent.

**No dark mode.** A hub looked at from a phone in the evening, in a
room whose lights it is switching, with no dark mode. This is the
single loudest signal, and it is not reachable from here: 180 colour
values are in the rules themselves, so there is nothing to flip.

## What the refresh is

Not a restyle. A system, and then the styling falls out of it.

**Two ramps.** A neutral from `#f7f9f7` to `#121714`, a brand green
from wash to `#0f3d29`, and semantic names on top -- `--surface`,
`--canvas`, `--text`, `--dim`. Dark mode becomes a twenty-line
redefinition of the ramp, which the prototype demonstrates.

**One type scale**: 12, 13, 15, 16, 20, 27, 40. Nothing smaller than
12. Tabular figures throughout, because this page is mostly numbers
and they should line up between rows.

**Three radii**: 10 for controls, 14 for cards, pill for chips.

**Light instead of lines.** Cards lose their border and gain
`0 1px 2px` at 5% with a wider, softer second shadow; hover lifts
2px. The canvas is a tint, so a white surface reads as raised
without being outlined.

**A quantity drawn as a quantity.** "8% water left" as a chip is a
number to read. As a bar it is a thing to glance at, and the eye
sorts seventeen of them without reading any.

## What does not change

- **The writing.** "Water in the pot", "Nothing reported yet",
  "Learning watering rhythm". It is the best thing in this product
  and none of it is dated.
- **The density.** A console, not a brochure. Seventeen plants on one
  screen is correct.
- **The green.** Deepened a little for contrast, same identity.
- **The photograph first on a card**, which is how two pothos are
  told apart.
- **The serif**, but earned: a display number, a plant's own name.
  Not every heading on every page.

## How it would be done

The ramps and the scale go in first, as tokens. Then one surface at a
time is moved onto them -- card, panel, metric, chip, control -- each
a small diff that can be looked at. The 180 loose hex values are
replaced as their rules are touched, not in one sweep. Dark mode is
last and should be nearly free by then.

The test for finishing: the count at the top of this file comes down.
If it has not, the system was added beside the old one rather than
replacing it, which this project does not do.
