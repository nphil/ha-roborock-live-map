"""Serialize parsed Roborock map data for the live 3D card.

The Roborock map blob is parsed by vacuum_map_parser_roborock into a MapData
object, but the raw occupancy grid is consumed straight into a rendered PNG and
never exposed. The 3D card needs the grid itself in order to extrude walls, so
this module re-walks the raw blob and lifts out the IMAGE block.

Block layout (little-endian), mirroring RoborockMapDataParser.parse:

  map header: int16 at 0x02 = header length
  each block: int16 at +0x00 = type, int16 at +0x02 = block header length,
              int32 at +0x04 = data length; data follows the block header.
  IMAGE block header tail: int32 top, left, height, width (last 16 bytes).

Cell encoding (RoborockImageParser): 0x00 outside, 0x01 wall, 0xFF inside,
0x07 scan; otherwise the low three bits classify the cell and, when they equal
7, the room segment id is cell >> 3.

Map coordinates are millimetres; one grid pixel is 50 mm.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import logging
from typing import Any

from vacuum_map_parser_base.map_data import MapData, Path, Point
from vacuum_map_parser_roborock.map_data_parser import (
    RoborockBlockType,
    RoborockMapDataParser,
)

_LOGGER = logging.getLogger(__name__)

MM_PER_PIXEL = 50


def _u8(data: bytes, addr: int) -> int:
    return data[addr] & 0xFF


def _u16(data: bytes, addr: int) -> int:
    return int.from_bytes(data[addr : addr + 2], "little")


def _u32(data: bytes, addr: int) -> int:
    return int.from_bytes(data[addr : addr + 4], "little")


def extract_grid(raw_api_response: bytes | None) -> dict[str, Any] | None:
    """Lift the raw occupancy grid out of a raw map blob.

    Returns the grid bytes plus its origin and extent in map pixels, or None
    when the blob carries no usable IMAGE block.
    """
    if not raw_api_response:
        return None
    try:
        raw = gzip.decompress(raw_api_response)
    except (OSError, EOFError):
        raw = raw_api_response

    try:
        position = _u16(raw, 0x02)
        while position + 8 <= len(raw):
            block_header_length = _u16(raw, position + 0x02)
            if block_header_length < 8:
                return None
            header = raw[position : position + block_header_length]
            block_type = _u16(header, 0x00)
            data_length = _u32(header, 0x04)
            data_start = position + block_header_length

            if block_type == RoborockBlockType.IMAGE.value:
                top = _u32(header, block_header_length - 16)
                left = _u32(header, block_header_length - 12)
                height = _u32(header, block_header_length - 8)
                width = _u32(header, block_header_length - 4)
                cells = raw[data_start : data_start + data_length]
                if width <= 0 or height <= 0 or len(cells) < width * height:
                    return None
                return {
                    "top": top,
                    "left": left,
                    "width": width,
                    "height": height,
                    "mm_per_pixel": MM_PER_PIXEL,
                    "hash": hashlib.sha1(cells).hexdigest()[:16],
                    "cells": base64.b64encode(cells).decode("ascii"),
                }

            # The parser advances by the data length plus the block header
            # length (read as its low byte), which lands on the next block.
            position = data_start + data_length
    except (IndexError, ValueError) as err:
        _LOGGER.debug("Could not extract occupancy grid: %s", err)
        return None
    return None


def _point(point: Point | None) -> dict[str, float] | None:
    if point is None:
        return None
    result: dict[str, float] = {"x": point.x, "y": point.y}
    if point.a is not None:
        result["a"] = point.a
    return result


def _path(path: Path | None) -> list[list[list[float]]] | None:
    """Flatten a Path into subpaths of [x, y] pairs."""
    if path is None or not path.path:
        return None
    return [[[p.x, p.y] for p in subpath] for subpath in path.path]


def _obstacles(obstacles: list[Any] | None, with_photo: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for obstacle in obstacles or []:
        details = obstacle.details
        entry: dict[str, Any] = {"x": obstacle.x, "y": obstacle.y}
        if details.type is not None:
            entry["type"] = details.type
            entry["label"] = RoborockMapDataParser.KNOWN_OBSTACLE_TYPES.get(
                details.type, details.description or "unknown"
            )
        elif details.description:
            entry["label"] = details.description
        if details.confidence_level is not None:
            entry["confidence"] = details.confidence_level
        if with_photo and details.photo_name:
            entry["photo"] = details.photo_name
        result.append(entry)
    return result


def _areas(areas: list[Any] | None) -> list[list[float]] | None:
    if not areas:
        return None
    return [[a.x0, a.y0, a.x1, a.y1, a.x2, a.y2, a.x3, a.y3] for a in areas]


def serialize(
    map_data: MapData,
    raw_api_response: bytes | None,
    *,
    include_grid: bool = True,
) -> dict[str, Any]:
    """Build the JSON payload the 3D card consumes."""
    payload: dict[str, Any] = {
        "map_name": map_data.map_name,
        "sequence": map_data.additional_parameters.get("map_sequence"),
        "vacuum_position": _point(map_data.vacuum_position),
        "charger": _point(map_data.charger),
        "goto": _point(map_data.goto),
        "vacuum_room": map_data.vacuum_room,
        "vacuum_room_name": map_data.vacuum_room_name,
        "path": _path(map_data.path),
        "predicted_path": _path(map_data.predicted_path),
        "mop_path": _path(map_data.mop_path),
        "obstacles": _obstacles(map_data.obstacles, False)
        + _obstacles(map_data.obstacles_with_photo, True),
        "ignored_obstacles": _obstacles(map_data.ignored_obstacles, False)
        + _obstacles(map_data.ignored_obstacles_with_photo, True),
        "no_go_areas": _areas(map_data.no_go_areas),
        "no_mopping_areas": _areas(map_data.no_mopping_areas),
        "cleaned_rooms": sorted(map_data.cleaned_rooms) if map_data.cleaned_rooms else None,
    }

    if map_data.rooms:
        payload["rooms"] = [
            {
                "id": number,
                "name": getattr(room, "name", None),
                "x0": room.x0,
                "y0": room.y0,
                "x1": room.x1,
                "y1": room.y1,
            }
            for number, room in map_data.rooms.items()
        ]

    if map_data.walls:
        payload["virtual_walls"] = [[w.x0, w.y0, w.x1, w.y1] for w in map_data.walls]

    if map_data.image is not None and not map_data.image.is_empty:
        dimensions = map_data.image.dimensions
        payload["image"] = {
            "top": dimensions.top,
            "left": dimensions.left,
            "width": dimensions.width,
            "height": dimensions.height,
            "scale": dimensions.scale,
            "rotation": dimensions.rotation,
        }
        payload["calibration_points"] = map_data.calibration()

    if include_grid:
        payload["grid"] = extract_grid(raw_api_response)

    return payload
