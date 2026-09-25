"""Platform filesystem paths used only at the host I/O boundary."""

import os
from pathlib import Path


def native_path(path):
    """Return an extended-length Windows path without changing logical paths.

    The prefix is intentionally introduced only for direct filesystem calls;
    model-visible paths, Git paths, manifests, and security checks keep using
    ordinary resolved paths.
    """

    path = Path(path)
    if os.name != "nt":
        return path
    value = str(path.resolve())
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def logical_path(path):
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + value[8:])
    if value.startswith("\\\\?\\"):
        return Path(value[4:])
    return Path(value)
