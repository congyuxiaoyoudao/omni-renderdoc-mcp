"""Decode little-endian vertex elements and retain their original bytes."""

import math
import struct


class MeshError(ValueError):
    """A draw cannot be represented from the available capture data."""


def enum_name(value):
    return str(value).rsplit(".", 1)[-1]


def _convert(value, bits, component, channel):
    if component in ("SNorm", "SInt", "SScaled") and value & (1 << (bits - 1)):
        value -= 1 << bits
    if component == "SNorm":
        return max(-1.0, value / ((1 << (bits - 1)) - 1))
    if component in ("UNorm", "UNormSRGB"):
        value /= (1 << bits) - 1
        if component == "UNormSRGB" and channel < 3:
            if value <= 0.04045:
                return value / 12.92
            return ((value + 0.055) / 1.055) ** 2.4
    return value


def _unsigned_float(value, mantissa_bits):
    mantissa = value & ((1 << mantissa_bits) - 1)
    exponent = value >> mantissa_bits
    if exponent == 31:
        return math.nan if mantissa else math.inf
    if exponent == 0:
        return math.ldexp(mantissa, -14 - mantissa_bits)
    return math.ldexp(
        (1 << mantissa_bits) + mantissa, exponent - 15 - mantissa_bits
    )


_PACKING = {
    "R10G10B10A2": (10, 10, 10, 2),
    "R5G6B5": (5, 6, 5),
    "R5G5B5A1": (5, 5, 5, 1),
    "R4G4B4A4": (4, 4, 4, 4),
    "R4G4": (4, 4),
    "R11G11B10": (11, 11, 10),
    "R9G9B9E5": (9, 9, 9),
}
_INTEGER_TYPES = {
    "UInt",
    "SInt",
    "UScaled",
    "SScaled",
    "UNorm",
    "SNorm",
    "UNormSRGB",
    "Typeless",
}


def make_decoder(fmt):
    layout = enum_name(fmt.type)
    component = enum_name(fmt.compType)
    width, count = fmt.compByteWidth, fmt.compCount
    bgra = fmt.BGRAOrder()
    if layout == "Regular":
        codes = (
            {2: "e", 4: "f", 8: "d"}
            if component == "Float"
            else {
                1: "B",
                2: "H",
                4: "I",
                8: "Q",
            }
        )
        if (
            component not in _INTEGER_TYPES
            and component != "Float"
            or width not in codes
            or count < 1
        ):
            raise MeshError("Unsupported vertex format " + fmt.Name())
        unpack = struct.Struct("<" + codes[width] * count).unpack

        def decode(data):
            values = unpack(data)
            if component not in ("Float", "UInt", "Typeless", "UScaled"):
                values = tuple(
                    _convert(v, width * 8, component, i)
                    for i, v in enumerate(values)
                )
            return values

    elif layout in _PACKING:
        bits = _PACKING[layout]

        def decode(data):
            packed = int.from_bytes(data, "little")
            offset = 0
            values = []
            for i, nbits in enumerate(bits):
                value = (packed >> offset) & ((1 << nbits) - 1)
                if layout == "R11G11B10":
                    value = _unsigned_float(value, nbits - 5)
                elif layout == "R9G9B9E5":
                    value = math.ldexp(value, (packed >> 27) - 24)
                else:
                    value = _convert(value, nbits, component, i)
                values.append(value)
                offset += nbits
            return tuple(values)

    else:
        raise MeshError("Unsupported vertex format " + fmt.Name())

    if not bgra:
        return decode

    def reordered(data):
        values = decode(data)
        return (values[2], values[1], values[0]) + values[3:]

    return reordered
