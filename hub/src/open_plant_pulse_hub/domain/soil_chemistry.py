"""Normalized soil chemistry: pore-water conductivity estimated from the probe.

The probe measures bulk electrical conductivity: the soil, water and air between
its needles together. Plant profile ranges describe pore-water conductivity, the
soil water on its own, as a drainage or pour-through test would. An airy potting
mix reads far lower in bulk than in its water, so the two cannot be compared
directly; this converts one into an estimate of the other.

Two published models do it:

- Topp, Davis and Annan (1980), "Electromagnetic determination of soil water
  content", Water Resources Research 16(3): a polynomial from bulk permittivity
  to volumetric water content, inverted here to go the other way.
- Hilhorst (2000), "A pore water conductivity sensor", Soil Science Society of
  America Journal 64(6): pore-water conductivity from bulk conductivity and bulk
  permittivity, linear above an offset permittivity.

The result is approximate, ±30–50 % in absolute terms, and reliable as a trend.
The probe's EC reads zero in dry soil, and the model breaks down as water leaves,
so below MIN_MOISTURE_PERCENT there is no estimate at all.
"""

from dataclasses import dataclass
from typing import Optional


# Conductivity rises about 2 % per °C; readings are compensated to 25 °C.
REFERENCE_TEMPERATURE_C = 25.0
TEMPERATURE_COEFFICIENT_PER_C = 0.02
# Topp et al. (1980): θ = a + b·ε + c·ε² + d·ε³.
TOPP_COEFFICIENTS = (-5.3e-2, 2.92e-2, -5.5e-4, 4.3e-6)
# The permittivity range searched when inverting Topp: air to water. Moisture
# beyond what the polynomial reaches at the top, as the probe in a glass of
# water reports, is taken as water.
MIN_BULK_PERMITTIVITY = 1.0
MAX_BULK_PERMITTIVITY = 80.0
BISECTION_STEPS = 60
# Permittivity of water at 20 °C and how it falls with temperature.
WATER_PERMITTIVITY_20C = 80.3
WATER_PERMITTIVITY_PER_C = 0.37
# Hilhorst's offset: the bulk permittivity at which bulk conductivity reaches zero.
HILHORST_OFFSET_PERMITTIVITY = 4.1
# How far bulk permittivity must stand above the offset before dividing by the
# difference means anything; close to it the estimate is noise.
MIN_PERMITTIVITY_MARGIN = 2.0
# Below this the probe's EC reads zero or near it and there is no estimate.
MIN_MOISTURE_PERCENT = 20.0


@dataclass(frozen=True)
class PoreWaterEstimate:
    pore_water_ec_us_cm: float
    ec25_us_cm: float
    bulk_permittivity: float


def temperature_compensated_ec(conductivity_us_cm: float, temperature_c: float) -> float:
    """Return the conductivity the same soil would read at 25 °C."""
    return conductivity_us_cm / (
        1 + TEMPERATURE_COEFFICIENT_PER_C * (temperature_c - REFERENCE_TEMPERATURE_C)
    )


def topp_water_content(permittivity: float) -> float:
    a, b, c, d = TOPP_COEFFICIENTS
    return a + b * permittivity + c * permittivity ** 2 + d * permittivity ** 3


def bulk_permittivity(volumetric_water_content: float) -> float:
    """Invert Topp by bisection. The polynomial rises throughout the range."""
    low, high = MIN_BULK_PERMITTIVITY, MAX_BULK_PERMITTIVITY
    if volumetric_water_content <= topp_water_content(low):
        return low
    if volumetric_water_content >= topp_water_content(high):
        return high
    for _ in range(BISECTION_STEPS):
        middle = (low + high) / 2
        if topp_water_content(middle) < volumetric_water_content:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def water_permittivity(temperature_c: float) -> float:
    return WATER_PERMITTIVITY_20C - WATER_PERMITTIVITY_PER_C * (temperature_c - 20.0)


def is_too_dry(moisture_percent: Optional[float]) -> bool:
    return moisture_percent is not None and moisture_percent < MIN_MOISTURE_PERCENT


def estimate_pore_water_ec(
    conductivity_us_cm: Optional[float],
    moisture_percent: Optional[float],
    soil_temperature_c: Optional[float],
) -> Optional[PoreWaterEstimate]:
    """Return the pore-water estimate for one reading, or None when there is none.

    None when a measurement is missing, the conductivity is zero, or the soil
    is too dry for the model to hold.
    """
    if (
        conductivity_us_cm is None
        or moisture_percent is None
        or soil_temperature_c is None
        or conductivity_us_cm <= 0
        or is_too_dry(moisture_percent)
    ):
        return None
    permittivity = bulk_permittivity(moisture_percent / 100.0)
    if permittivity - HILHORST_OFFSET_PERMITTIVITY < MIN_PERMITTIVITY_MARGIN:
        return None
    ec25 = temperature_compensated_ec(conductivity_us_cm, soil_temperature_c)
    pore_ec = water_permittivity(soil_temperature_c) * ec25 / (
        permittivity - HILHORST_OFFSET_PERMITTIVITY
    )
    return PoreWaterEstimate(
        pore_water_ec_us_cm=pore_ec,
        ec25_us_cm=ec25,
        bulk_permittivity=permittivity,
    )


def nutrient_level(pore_water_ec_us_cm: Optional[float], ideal: Optional[list]) -> Optional[str]:
    """Place the estimate against a profile's conductivity ideal range."""
    if pore_water_ec_us_cm is None or not ideal:
        return None
    low, high = ideal
    if pore_water_ec_us_cm < low:
        return "low"
    if pore_water_ec_us_cm > high:
        return "high"
    return "ok"
