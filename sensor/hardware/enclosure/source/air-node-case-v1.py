"""Generate the Open Plant Pulse RS-485/XIAO stack + SHT45 prototype case."""

import bpy
from math import cos, pi, sin
from mathutils import Vector
from pathlib import Path


# All dimensions are millimetres. Blender units are configured as millimetres.
XIAO_LENGTH = 21.0
XIAO_WIDTH = 17.8
XIAO_PCB_HEIGHT = 1.6
RS485_LENGTH = 42.0
RS485_WIDTH = 25.0
RS485_PCB_HEIGHT = 1.6
RS485_ASSEMBLY_HEIGHT = 18.0
RS485_MOUNT_FROM_USB = (23.0, 39.0)
RS485_MOUNT_FROM_SIDE = 2.0
RS485_STANDOFF_DIAMETER = 6.5
RS485_STANDOFF_HEIGHT = 3.0
RS485_M3_PILOT_DIAMETER = 2.7
RS485_STOP_THICKNESS = 1.6
SHT_LENGTH = 13.29
SHT_WIDTH = 10.26
SHT_PCB_HEIGHT = 1.6
SHT_SENSOR_FROM_LEFT = 2.3
SHT_SENSOR_FROM_TOP = 3.1
SHT_M3_FROM_LEFT = 2.3
SHT_M3_FROM_TOP = 7.1
ANTENNA_LENGTH = 45.0
ANTENNA_WIDTH = 20.0
ANTENNA_CABLE_DIAMETER = 1.13
BOARD_EDGE_GAP = 15.0
BATTERY_HOLDER_LENGTH = 77.0
BATTERY_HOLDER_WIDTH = 39.5
BATTERY_HOLDER_HEIGHT = 18.3
BATTERY_HOLDER_CLEARANCE = 0.4
DIVIDER_THICKNESS = 2.0
DIVIDER_TOP_GAP = 2.8
BATTERY_LEAD_OPENING_WIDTH = 8.0
BATTERY_LEAD_OPENING_HEIGHT = 5.0
PROBE_CABLE_DIAMETER = 5.0
PROBE_CABLE_RADIAL_CLEARANCE = 0.4
PROBE_CABLE_HOLE_DIAMETER = PROBE_CABLE_DIAMETER + PROBE_CABLE_RADIAL_CLEARANCE * 2.0

FIT_CLEARANCE = 0.4
WALL = 3.0
FLOOR = 2.0
BASE_HEIGHT = 27.0
LID_HEIGHT = 2.4
BOARD_SUPPORT_HEIGHT = 2.0
BOARD_SUPPORT_WIDTH = 1.6
WIRE_CHANNEL_FLOOR_HEIGHT = 1.2
LID_FIT_CLEARANCE = 0.4
LID_TONGUE_ENGAGEMENT = 1.2
LID_TONGUE_OVERLAP = 0.6
LID_TONGUE_HEIGHT = 1.2
LID_GROOVE_CLEARANCE = 0.4
LID_VERTICAL_CLEARANCE = 0.4
LID_UPPER_RAIL_THICKNESS = 1.2
LID_GROOVE_DEPTH = LID_TONGUE_ENGAGEMENT + LID_GROOVE_CLEARANCE * 2.0
LID_GROOVE_HEIGHT = LID_TONGUE_HEIGHT + LID_VERTICAL_CLEARANCE * 2.0
LID_THUMB_NOTCH_DIAMETER = 14.0
STACK_USB_END_INSET = 0.6

ELECTRONICS_INNER_LENGTH = RS485_LENGTH + BOARD_EDGE_GAP + 4.0
ELECTRONICS_RAIL_SERVICE_CLEARANCE = 3.0
ELECTRONICS_INNER_WIDTH = (
    RS485_WIDTH
    + 2.0 * (FIT_CLEARANCE + BOARD_SUPPORT_WIDTH + ELECTRONICS_RAIL_SERVICE_CLEARANCE)
)
BATTERY_BAY_LENGTH = BATTERY_HOLDER_LENGTH + BATTERY_HOLDER_CLEARANCE * 2.0
BATTERY_BAY_WIDTH = BATTERY_HOLDER_WIDTH + BATTERY_HOLDER_CLEARANCE * 2.0
INNER_LENGTH = max(ELECTRONICS_INNER_LENGTH, BATTERY_BAY_LENGTH)
INNER_WIDTH = ELECTRONICS_INNER_WIDTH + DIVIDER_THICKNESS + BATTERY_BAY_WIDTH
OUTER_LENGTH = INNER_LENGTH + WALL * 2.0
OUTER_WIDTH = INNER_WIDTH + WALL * 2.0
LID_LENGTH = INNER_LENGTH + WALL

ELECTRONICS_Y = -INNER_WIDTH / 2.0 + ELECTRONICS_INNER_WIDTH / 2.0
DIVIDER_Y = -INNER_WIDTH / 2.0 + ELECTRONICS_INNER_WIDTH + DIVIDER_THICKNESS / 2.0
BATTERY_BAY_Y = DIVIDER_Y + DIVIDER_THICKNESS / 2.0 + BATTERY_BAY_WIDTH / 2.0
RS485_X = -INNER_LENGTH / 2.0 + STACK_USB_END_INSET + RS485_LENGTH / 2.0
XIAO_X = RS485_X - RS485_LENGTH / 2.0 + XIAO_LENGTH / 2.0
RS485_BOARD_Z = FLOOR + RS485_STANDOFF_HEIGHT - 0.1 + RS485_PCB_HEIGHT / 2.0
XIAO_BOARD_Z = FLOOR + RS485_STANDOFF_HEIGHT + 12.0
SHT_BOARD_X = INNER_LENGTH / 2.0 - 1.2
SHT_BOARD_Z = BASE_HEIGHT / 2.0
SHT_SENSOR_Y = ELECTRONICS_Y - SHT_LENGTH / 2.0 + SHT_SENSOR_FROM_LEFT
SHT_SENSOR_Z = SHT_BOARD_Z + SHT_WIDTH / 2.0 - SHT_SENSOR_FROM_TOP
SHT_M3_Y = ELECTRONICS_Y - SHT_LENGTH / 2.0 + SHT_M3_FROM_LEFT
SHT_M3_Z = SHT_BOARD_Z + SHT_WIDTH / 2.0 - SHT_M3_FROM_TOP
ELECTRONICS_MIN_Y = -INNER_WIDTH / 2.0
ELECTRONICS_MAX_Y = ELECTRONICS_MIN_Y + ELECTRONICS_INNER_WIDTH

ROOT = Path(__file__).resolve().parent
BLEND_PATH = ROOT / "air-node-case-v1.blend"
STL_DIR = ROOT.parent / "stl"


def material(name, color, metallic=0.0, roughness=0.45):
    result = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    result.diffuse_color = (*color, 1.0)
    result.metallic = metallic
    result.roughness = roughness
    return result


CASE_MATERIAL = material("Warm Gray PETG", (0.32, 0.35, 0.34), roughness=0.58)
PCB_MATERIAL = material("PCB", (0.08, 0.25, 0.19), roughness=0.38)
SENSOR_MATERIAL = material("SHT45", (0.13, 0.13, 0.14), metallic=0.2)
USB_MATERIAL = material("USB-C", (0.58, 0.61, 0.62), metallic=0.75, roughness=0.25)
WIRE_MATERIAL = material("I2C Wires", (0.08, 0.42, 0.67), roughness=0.4)


def cube(name, size, location, collection, mat=None, bevel=0.0):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if bevel:
        modifier = obj.modifiers.new("Edge radius", "BEVEL")
        modifier.width = bevel
        modifier.segments = 3
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    if mat:
        obj.data.materials.append(mat)
    for owner in list(obj.users_collection):
        owner.objects.unlink(obj)
    collection.objects.link(obj)
    return obj


def subtract(target, cutter):
    modifier = target.modifiers.new(f"Cut {cutter.name}", "BOOLEAN")
    modifier.operation = "DIFFERENCE"
    modifier.solver = "EXACT"
    modifier.object = cutter
    bpy.context.view_layer.objects.active = target
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    bpy.data.objects.remove(cutter, do_unlink=True)


def unite(target, addition):
    modifier = target.modifiers.new(f"Join {addition.name}", "BOOLEAN")
    modifier.operation = "UNION"
    modifier.solver = "EXACT"
    modifier.object = addition
    bpy.context.view_layer.objects.active = target
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    bpy.data.objects.remove(addition, do_unlink=True)


def cylinder(name, radius, depth, location, collection, rotation=(0.0, 0.0, 0.0), mat=None):
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=48,
        radius=radius,
        depth=depth,
        location=location,
        rotation=rotation,
    )
    obj = bpy.context.object
    obj.name = name
    if mat:
        obj.data.materials.append(mat)
    for owner in list(obj.users_collection):
        owner.objects.unlink(obj)
    collection.objects.link(obj)
    return obj


def label(text, location, size, collection):
    curve = bpy.data.curves.new(text, "FONT")
    curve.body = text
    curve.align_x = "CENTER"
    curve.size = size
    curve.extrude = 0.08
    obj = bpy.data.objects.new(text, curve)
    obj.location = location
    collection.objects.link(obj)
    return obj


def spring(name, start, length, radius, turns, collection, mat):
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = 0.3
    curve.bevel_resolution = 2
    spline = curve.splines.new("POLY")
    point_count = turns * 16 + 1
    spline.points.add(point_count - 1)
    for index, point in enumerate(spline.points):
        progress = index / (point_count - 1)
        angle = progress * turns * 2.0 * pi
        point.co = (
            start[0] + progress * length,
            start[1] + cos(angle) * radius,
            start[2] + sin(angle) * radius,
            1.0,
        )
    obj = bpy.data.objects.new(name, curve)
    obj.data.materials.append(mat)
    collection.objects.link(obj)
    return obj


def add_rail(name, board_x, board_length, side_y, collection):
    return cube(
        name,
        (board_length + 0.8, BOARD_SUPPORT_WIDTH, BOARD_SUPPORT_HEIGHT),
        (board_x, side_y, FLOOR + BOARD_SUPPORT_HEIGHT / 2.0 - 0.1),
        collection,
        CASE_MATERIAL,
        0.3,
    )


def create_base(collection):
    base = cube(
        "Case Base",
        (OUTER_LENGTH, OUTER_WIDTH, BASE_HEIGHT),
        (0.0, 0.0, BASE_HEIGHT / 2.0),
        collection,
        CASE_MATERIAL,
        1.2,
    )
    cavity = cube(
        "Base cavity cutter",
        (INNER_LENGTH, INNER_WIDTH, BASE_HEIGHT),
        (0.0, 0.0, FLOOR + BASE_HEIGHT / 2.0),
        collection,
    )
    subtract(base, cavity)

    groove_length = OUTER_LENGTH - WALL
    groove_x = -WALL / 2.0
    groove_z = BASE_HEIGHT - LID_UPPER_RAIL_THICKNESS - LID_GROOVE_HEIGHT / 2.0
    for side in (-1.0, 1.0):
        groove = cube(
            f"Sliding lid groove {'left' if side < 0 else 'right'} cutter",
            (groove_length + 0.2, LID_GROOVE_DEPTH, LID_GROOVE_HEIGHT),
            (groove_x, side * (INNER_WIDTH / 2.0 + LID_TONGUE_ENGAGEMENT / 2.0), groove_z),
            collection,
            bevel=0.15,
        )
        subtract(base, groove)

    entry_slot_height = LID_HEIGHT + LID_VERTICAL_CLEARANCE + 0.2
    entry_slot = cube(
        "Sliding lid entry slot cutter",
        (WALL * 3.0, INNER_WIDTH + (LID_TONGUE_ENGAGEMENT + LID_GROOVE_CLEARANCE) * 2.0, entry_slot_height),
        (-OUTER_LENGTH / 2.0, 0.0, BASE_HEIGHT - entry_slot_height / 2.0 + 0.05),
        collection,
        bevel=0.2,
    )
    subtract(base, entry_slot)

    divider_height = BASE_HEIGHT - FLOOR - DIVIDER_TOP_GAP
    divider = cube(
        "Battery isolation wall",
        (INNER_LENGTH, DIVIDER_THICKNESS, divider_height),
        (0.0, DIVIDER_Y, FLOOR + divider_height / 2.0 - 0.1),
        collection,
        CASE_MATERIAL,
        0.35,
    )
    battery_lead_cut = cube(
        "Battery lead pass-through cutter",
        (BATTERY_LEAD_OPENING_WIDTH, DIVIDER_THICKNESS * 3.0, BATTERY_LEAD_OPENING_HEIGHT),
        (XIAO_X, DIVIDER_Y, FLOOR + 6.0),
        collection,
        bevel=1.0,
    )
    subtract(divider, battery_lead_cut)

    # USB-C faces the left end when the carrier is installed in this orientation.
    usb_cut = cube(
        "USB-C opening cutter",
        (WALL * 3.0, 11.5, 6.5),
        (-OUTER_LENGTH / 2.0, ELECTRONICS_Y, XIAO_BOARD_Z + 1.0),
        collection,
        bevel=0.8,
    )
    subtract(base, usb_cut)

    # Round pass-through for the measured NPKPHCTH-S cable carrying V+, GND, A,
    # and B. Add a separate gland or strain relief for the installed enclosure.
    terminal_cut = cylinder(
        "External probe cable opening cutter",
        PROBE_CABLE_HOLE_DIAMETER / 2.0,
        WALL * 3.0,
        (RS485_X + 8.0, -OUTER_WIDTH / 2.0, 8.0),
        collection,
        rotation=(1.57079632679, 0.0, 0.0),
    )
    subtract(base, terminal_cut)

    # This slot passes the small U.FL plug from outside to inside. The adhesive
    # antenna paddle remains outside and does not need to fit through the slot.
    antenna_hole_y = ELECTRONICS_Y + 9.0
    antenna_hole_z = FLOOR + 2.0
    antenna_cut = cube(
        "Antenna cable pass-through cutter",
        (WALL * 3.0, 4.0, 3.2),
        (-OUTER_LENGTH / 2.0, antenna_hole_y, antenna_hole_z),
        collection,
        bevel=0.7,
    )
    subtract(base, antenna_cut)

    # Two rails form a protected 1.8 mm guide for the nominal 1.13 mm coax.
    for name, y_offset in (("Antenna guide left", -1.5), ("Antenna guide right", 1.5)):
        cube(
            name,
            (8.0, 1.2, 1.8),
            (-OUTER_LENGTH / 2.0 + WALL + 3.9, antenna_hole_y + y_offset, FLOOR + 0.8),
            collection,
            CASE_MATERIAL,
            0.25,
        )

    rs485_rail_offset_y = RS485_WIDTH / 2.0 + FIT_CLEARANCE + BOARD_SUPPORT_WIDTH / 2.0
    add_rail("RS485 rail front", RS485_X, RS485_LENGTH, ELECTRONICS_Y - rs485_rail_offset_y, collection)
    add_rail("RS485 rail back", RS485_X, RS485_LENGTH, ELECTRONICS_Y + rs485_rail_offset_y, collection)

    rs485_mount_positions = tuple(
        (
            RS485_X - RS485_LENGTH / 2.0 + mount_from_usb,
            ELECTRONICS_Y + side_sign * (RS485_WIDTH / 2.0 - RS485_MOUNT_FROM_SIDE),
        )
        for mount_from_usb in RS485_MOUNT_FROM_USB
        for side_sign in (-1.0, 1.0)
    )
    for index, (mount_x, mount_y) in enumerate(rs485_mount_positions, start=1):
        standoff = cylinder(
            f"RS485 M3 standoff {index}",
            RS485_STANDOFF_DIAMETER / 2.0,
            RS485_STANDOFF_HEIGHT,
            (mount_x, mount_y, FLOOR + RS485_STANDOFF_HEIGHT / 2.0 - 0.1),
            collection,
            mat=CASE_MATERIAL,
        )
        unite(base, standoff)
        pilot = cylinder(
            f"RS485 M3 pilot {index} cutter",
            RS485_M3_PILOT_DIAMETER / 2.0,
            4.0,
            (mount_x, mount_y, FLOOR + 1.1),
            collection,
        )
        subtract(base, pilot)

    # The SHT45 board stands against the right end wall, component side outward.
    # One M3 screw clamps it while three rails prevent rotation.
    for name, y_pos, z_pos, size in (
        ("SHT45 bottom rail", ELECTRONICS_Y, SHT_BOARD_Z - SHT_WIDTH / 2.0 - 0.8, (1.8, SHT_LENGTH + 2.0, 1.6)),
        ("SHT45 front rail", ELECTRONICS_Y - SHT_LENGTH / 2.0 - 0.8, SHT_BOARD_Z, (1.8, 1.6, SHT_WIDTH + 2.0)),
        ("SHT45 back rail", ELECTRONICS_Y + SHT_LENGTH / 2.0 + 0.8, SHT_BOARD_Z, (1.8, 1.6, SHT_WIDTH + 2.0)),
    ):
        cube(
            name,
            size,
            (INNER_LENGTH / 2.0 - 0.7, y_pos, z_pos),
            collection,
            CASE_MATERIAL,
            0.25,
        )

    sensor_cut = cube(
        "SHT45 side sensor opening cutter",
        (WALL * 3.0, 5.0, 5.0),
        (OUTER_LENGTH / 2.0, SHT_SENSOR_Y, SHT_SENSOR_Z),
        collection,
        bevel=1.0,
    )
    subtract(base, sensor_cut)

    m3_cut = cylinder(
        "SHT45 M3 clearance cutter",
        1.7,
        WALL * 3.0,
        (OUTER_LENGTH / 2.0, SHT_M3_Y, SHT_M3_Z),
        collection,
        rotation=(0.0, 1.57079632679, 0.0),
    )
    subtract(base, m3_cut)

    m3_head_cut = cylinder(
        "SHT45 M3 head recess cutter",
        3.2,
        1.2,
        (OUTER_LENGTH / 2.0 + WALL / 2.0, SHT_M3_Y, SHT_M3_Z),
        collection,
        rotation=(0.0, 1.57079632679, 0.0),
    )
    subtract(base, m3_head_cut)

    # End stops locate each board while leaving their wired edges accessible.
    for name, x_pos, width in (
        ("RS485 rear stop", RS485_X + RS485_LENGTH / 2.0 + RS485_STOP_THICKNESS / 2.0, RS485_WIDTH + 2.0),
    ):
        cube(
            name,
            (RS485_STOP_THICKNESS, width, BOARD_SUPPORT_HEIGHT + 1.2),
            (x_pos, ELECTRONICS_Y, FLOOR + (BOARD_SUPPORT_HEIGHT + 1.2) / 2.0 - 0.1),
            collection,
            CASE_MATERIAL,
            0.25,
        )

    # A low bridge protects the four loose I2C wires up to the side-wall sensor.
    wire_channel_start_x = RS485_X + RS485_LENGTH / 2.0 + 0.5
    wire_channel_end_x = INNER_LENGTH / 2.0 - 1.5
    cube(
        "Wire channel floor",
        (wire_channel_end_x - wire_channel_start_x, 8.0, WIRE_CHANNEL_FLOOR_HEIGHT),
        ((wire_channel_start_x + wire_channel_end_x) / 2.0, ELECTRONICS_Y, FLOOR + WIRE_CHANNEL_FLOOR_HEIGHT / 2.0 - 0.1),
        collection,
        CASE_MATERIAL,
        0.35,
    )

    return base


def create_lid(collection):
    lid_x = OUTER_LENGTH + 8.0
    lid = cube(
        "Sliding Lid",
        (LID_LENGTH - LID_FIT_CLEARANCE, INNER_WIDTH - LID_FIT_CLEARANCE * 2.0, LID_HEIGHT),
        (lid_x, 0.0, LID_HEIGHT / 2.0),
        collection,
        CASE_MATERIAL,
        0.35,
    )

    tongue_width = LID_TONGUE_OVERLAP + LID_FIT_CLEARANCE + LID_TONGUE_ENGAGEMENT
    tongue_y = INNER_WIDTH / 2.0 - LID_FIT_CLEARANCE - LID_TONGUE_OVERLAP + tongue_width / 2.0
    for side in (-1.0, 1.0):
        tongue = cube(
            f"Sliding lid {'left' if side < 0 else 'right'} tongue",
            (LID_LENGTH - LID_FIT_CLEARANCE, tongue_width, LID_TONGUE_HEIGHT),
            (lid_x, side * tongue_y, LID_HEIGHT - LID_TONGUE_HEIGHT / 2.0 + LID_VERTICAL_CLEARANCE),
            collection,
            CASE_MATERIAL,
            0.15,
        )
        unite(lid, tongue)

    thumb_notch = cylinder(
        "Sliding lid thumb notch cutter",
        LID_THUMB_NOTCH_DIAMETER / 2.0,
        LID_HEIGHT * 3.0,
        (lid_x - LID_LENGTH / 2.0, 0.0, LID_HEIGHT / 2.0),
        collection,
    )
    subtract(lid, thumb_notch)
    return lid


def create_references(collection):
    rs485 = cube(
        "REFERENCE Seeed RS485 carrier 42x25",
        (RS485_LENGTH, RS485_WIDTH, RS485_PCB_HEIGHT),
        (RS485_X, ELECTRONICS_Y, RS485_BOARD_Z),
        collection,
        PCB_MATERIAL,
        1.2,
    )
    rs485["dimensions_mm"] = "42 x 25 x 14 published; 18 mm provisional with XIAO components"
    for index, mount_from_usb in enumerate(RS485_MOUNT_FROM_USB, start=1):
        for side_sign in (-1.0, 1.0):
            mount_hole = cylinder(
                f"REFERENCE RS485 M3 hole {index} cutter",
                1.7,
                RS485_PCB_HEIGHT * 3.0,
                (
                    RS485_X - RS485_LENGTH / 2.0 + mount_from_usb,
                    ELECTRONICS_Y + side_sign * (RS485_WIDTH / 2.0 - RS485_MOUNT_FROM_SIDE),
                    RS485_BOARD_Z,
                ),
                collection,
            )
            subtract(rs485, mount_hole)
    rs485["mounting_pattern_status"] = "Estimated from product image; measure physical board before printing"

    xiao = cube(
        "REFERENCE XIAO ESP32-C3 21x17.8",
        (XIAO_LENGTH, XIAO_WIDTH, XIAO_PCB_HEIGHT),
        (XIAO_X, ELECTRONICS_Y, XIAO_BOARD_Z),
        collection,
        PCB_MATERIAL,
        0.8,
    )
    xiao["dimensions_mm"] = "21.0 x 17.8 (official board footprint)"
    cube(
        "REFERENCE USB-C",
        (4.5, 9.0, 3.2),
        (XIAO_X - XIAO_LENGTH / 2.0 - 1.2, ELECTRONICS_Y, XIAO_BOARD_Z + 1.3),
        collection,
        USB_MATERIAL,
        0.5,
    )
    for name, x_pos, y_pos in (
        ("REFERENCE power terminal", RS485_X + 8.0, ELECTRONICS_Y - 8.5),
        ("REFERENCE RS485 terminal", RS485_X + 8.0, ELECTRONICS_Y + 8.5),
    ):
        cube(
            name,
            (13.0, 7.0, 8.0),
            (x_pos, y_pos, RS485_BOARD_Z + 4.8),
            collection,
            material("Terminal blocks", (0.15, 0.55, 0.25), roughness=0.5),
            0.6,
        )

    sht = cube(
        "REFERENCE SHT45 breakout 13.29x10.26",
        (SHT_PCB_HEIGHT, SHT_LENGTH, SHT_WIDTH),
        (SHT_BOARD_X, ELECTRONICS_Y, SHT_BOARD_Z),
        collection,
        PCB_MATERIAL,
        0.5,
    )
    sht["dimensions_mm"] = "13.29 x 10.26 (user measurement)"
    sht_hole = cylinder(
        "REFERENCE SHT45 M3 hole cutter",
        1.7,
        SHT_PCB_HEIGHT * 3.0,
        (SHT_BOARD_X, SHT_M3_Y, SHT_M3_Z),
        collection,
        rotation=(0.0, 1.57079632679, 0.0),
    )
    subtract(sht, sht_hole)
    cube(
        "REFERENCE SHT45 sensor",
        (0.5, 1.5, 1.5),
        (SHT_BOARD_X + 1.05, SHT_SENSOR_Y, SHT_SENSOR_Z),
        collection,
        SENSOR_MATERIAL,
        0.15,
    )

    start_x = XIAO_X + XIAO_LENGTH / 2.0 - 1.5
    end_x = SHT_BOARD_X - 0.5
    for index, y_offset in enumerate((-3.0, -1.0, 1.0, 3.0)):
        y_pos = ELECTRONICS_Y + y_offset
        curve = bpy.data.curves.new(f"Wire {index + 1}", "CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.35
        curve.bevel_resolution = 3
        spline = curve.splines.new("BEZIER")
        spline.bezier_points.add(2)
        for point, coordinate in zip(
            spline.bezier_points,
            (
                (start_x, y_pos, XIAO_BOARD_Z + 1.2),
                ((start_x + end_x) / 2.0, ELECTRONICS_Y + y_offset * 0.7, FLOOR + 2.0),
                (end_x, ELECTRONICS_Y + y_offset * 0.55, SHT_BOARD_Z - 2.0),
            ),
        ):
            point.co = coordinate
            point.handle_left_type = "AUTO"
            point.handle_right_type = "AUTO"
        wire = bpy.data.objects.new(f"REFERENCE I2C wire {index + 1}", curve)
        wire.data.materials.append(WIRE_MATERIAL)
        collection.objects.link(wire)

    antenna_hole_y = ELECTRONICS_Y + 9.0
    antenna_hole_z = FLOOR + 2.0
    antenna_mount_y = OUTER_WIDTH / 2.0 - ANTENNA_LENGTH / 2.0 - 0.5
    antenna_material = material("External antenna", (0.035, 0.035, 0.04), roughness=0.65)
    cube(
        "REFERENCE external adhesive antenna",
        (0.5, ANTENNA_LENGTH, ANTENNA_WIDTH),
        (-OUTER_LENGTH / 2.0 - 0.25, antenna_mount_y, BASE_HEIGHT / 2.0),
        collection,
        antenna_material,
        1.0,
    )["dimensions_status"] = "45 x 20 mm visual placeholder; measure supplied antenna"

    antenna_curve = bpy.data.curves.new("Antenna coax path", "CURVE")
    antenna_curve.dimensions = "3D"
    antenna_curve.bevel_depth = ANTENNA_CABLE_DIAMETER / 2.0
    antenna_curve.bevel_resolution = 3
    antenna_spline = antenna_curve.splines.new("BEZIER")
    antenna_spline.bezier_points.add(3)
    for point, coordinate in zip(
        antenna_spline.bezier_points,
        (
            (XIAO_X + 5.0, ELECTRONICS_Y + 5.0, XIAO_BOARD_Z + 1.0),
            (-OUTER_LENGTH / 2.0 + WALL + 5.0, antenna_hole_y, FLOOR + 1.5),
            (-OUTER_LENGTH / 2.0 - 1.0, antenna_hole_y, antenna_hole_z),
            (-OUTER_LENGTH / 2.0 - 1.0, antenna_mount_y - ANTENNA_LENGTH / 2.0 + 4.0, 8.0),
        ),
    ):
        point.co = coordinate
        point.handle_left_type = "AUTO"
        point.handle_right_type = "AUTO"
    antenna_wire = bpy.data.objects.new("REFERENCE antenna coax", antenna_curve)
    antenna_wire.data.materials.append(antenna_material)
    collection.objects.link(antenna_wire)

    holder_material = material("Dual 18650 holder", (0.055, 0.06, 0.065), roughness=0.55)
    contact_material = material("Battery holder contacts", (0.58, 0.61, 0.62), metallic=0.8, roughness=0.25)
    holder_floor_height = 1.6
    holder_end_wall = 2.2
    holder_side_wall = 1.6
    holder_center_wall = 1.8
    holder_channel_offset = BATTERY_HOLDER_WIDTH / 4.0
    holder_z = FLOOR

    holder_parts = [
        cube(
            "REFERENCE dual 18650 holder",
            (BATTERY_HOLDER_LENGTH, BATTERY_HOLDER_WIDTH, holder_floor_height),
            (0.0, BATTERY_BAY_Y, holder_z + holder_floor_height / 2.0),
            collection,
            holder_material,
            0.5,
        ),
        cube(
            "REFERENCE battery holder center spine",
            (BATTERY_HOLDER_LENGTH - holder_end_wall * 2.0, holder_center_wall, 8.0),
            (0.0, BATTERY_BAY_Y, holder_z + 4.0),
            collection,
            holder_material,
            0.5,
        ),
    ]
    for side in (-1.0, 1.0):
        holder_parts.append(
            cube(
                f"REFERENCE battery holder side rail {'left' if side < 0 else 'right'}",
                (BATTERY_HOLDER_LENGTH, holder_side_wall, 10.0),
                (0.0, BATTERY_BAY_Y + side * (BATTERY_HOLDER_WIDTH - holder_side_wall) / 2.0, holder_z + 5.0),
                collection,
                holder_material,
                0.6,
            )
        )
    for end in (-1.0, 1.0):
        end_x = end * (BATTERY_HOLDER_LENGTH - holder_end_wall) / 2.0
        holder_parts.append(
            cube(
                f"REFERENCE battery holder end wall {'negative' if end < 0 else 'positive'}",
                (holder_end_wall, BATTERY_HOLDER_WIDTH, BATTERY_HOLDER_HEIGHT),
                (end_x, BATTERY_BAY_Y, holder_z + BATTERY_HOLDER_HEIGHT / 2.0),
                collection,
                holder_material,
                0.7,
            )
        )
        for channel in (-1.0, 1.0):
            channel_y = BATTERY_BAY_Y + channel * holder_channel_offset
            contact_x = end_x - end * 1.25
            cube(
                f"REFERENCE holder contact {end:+.0f} {channel:+.0f}",
                (0.7, 8.0, 8.0),
                (contact_x, channel_y, holder_z + 9.0),
                collection,
                contact_material,
                0.5,
            )
            if end < 0:
                spring(
                    f"REFERENCE holder spring {channel:+.0f}",
                    (contact_x + 0.4, channel_y, holder_z + 9.0),
                    5.0,
                    3.2,
                    5,
                    collection,
                    contact_material,
                )
    for part in holder_parts:
        part["assembly"] = "77.0 x 39.5 x 18.3 mm dual-18650 holder"
    holder_parts[0]["dimensions_mm"] = "77.0 x 39.5 x 18.3 measured outer envelope"
    holder_parts[0]["detail_status"] = "Internal rails and contacts are visual approximations; verify physical holder"


def export_stl(objects, path):
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.wm.stl_export(filepath=str(path), export_selected_objects=True)


def main():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)

    scene_collection = bpy.context.scene.collection
    default = bpy.data.collections.new("PRINT_PARTS")
    references = bpy.data.collections.new("REFERENCE_HARDWARE")
    annotations = bpy.data.collections.new("DIMENSIONS_AND_NOTES")
    scene_collection.children.link(default)
    scene_collection.children.link(references)
    scene_collection.children.link(annotations)

    base = create_base(default)
    lid = create_lid(default)
    create_references(references)

    label("DUAL 18650 HOLDER BAY", (0.0, BATTERY_BAY_Y, BASE_HEIGHT + 2.0), 1.8, annotations)
    label("USB-C", (-OUTER_LENGTH / 2.0 - 1.0, ELECTRONICS_Y - 8.0, 7.0), 1.8, annotations)
    label("SHT45 SIDE OPENING + M3", (OUTER_LENGTH / 2.0, ELECTRONICS_Y - 10.0, BASE_HEIGHT + 2.0), 1.5, annotations)

    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.length_unit = "MILLIMETERS"
    scene.unit_settings.scale_length = 0.001
    scene["design_revision"] = "air-node-case-v1"
    scene["outer_dimensions_mm"] = f"{OUTER_LENGTH:.2f} x {OUTER_WIDTH:.2f} x {BASE_HEIGHT:.2f}"
    scene["electronics_compartment_mm"] = f"{INNER_LENGTH:.2f} x {ELECTRONICS_INNER_WIDTH:.2f}"
    scene["electronics_rail_service_clearance_mm"] = ELECTRONICS_RAIL_SERVICE_CLEARANCE
    scene["stack_usb_end_inset_mm"] = STACK_USB_END_INSET
    scene["probe_cable_hole_mm"] = PROBE_CABLE_HOLE_DIAMETER
    scene["lid_fit"] = "Flush sliding panel; 0.4 mm clearance; 1.2 mm tongues and upper rails"
    scene["board_edge_gap_mm"] = BOARD_EDGE_GAP
    scene["battery_configuration"] = "Dual 18650 holder; 77.0 x 39.5 x 18.3 mm measured envelope"
    scene["lid_fasteners"] = "None; captured sliding rails with closed far-end stop"
    scene["minimum_structural_feature_mm"] = 1.2
    scene["fit_status"] = "UNTESTED PROTOTYPE - verify boards, protected-cell pack, ports and assembled height"

    STL_DIR.mkdir(parents=True, exist_ok=True)
    export_stl([base] + [obj for obj in default.objects if obj.name.startswith(("RS485 rail", "SHT45", "RS485 rear", "Wire channel", "Antenna guide", "Battery"))], STL_DIR / "air-node-case-base-v1.stl")
    export_stl([lid], STL_DIR / "air-node-case-lid-v1.stl")

    bpy.ops.wm.save_as_mainfile(filepath=str(BLEND_PATH))

    print(
        f"Created air-node-case-v1: outer={OUTER_LENGTH:.2f} x {OUTER_WIDTH:.2f} x "
        f"{BASE_HEIGHT:.2f} mm, flush sliding lid, "
        f"board gap={BOARD_EDGE_GAP:.2f} mm"
    )


main()