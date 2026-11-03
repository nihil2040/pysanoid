"""Three config-loading deviations (DEV-005, DEV-007, DEV-006), stated here apart from load_config so the harnesses can check pysanoid
against them. The section order: a section named for a dataset under a recursive or skip_children section's path must
come after that section; upstream loads such a file, and the later section's recursion replaces the earlier one's
settings, or its skip_children deletes it. Shared datasets: no two entries of the merged config may take or prune the
same dataset; upstream takes and prunes it by each. Inheritance: a section named under a recursive section starts from
that section's settings whether or not the child lookup listed its dataset; upstream builds an unlisted one from the
defaults."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pysanoid as p


def first_misplaced(conf: Path) -> tuple[str, str, bool] | None:
    """(child, parent, dropped) for the first recursive or skip_children section, in file order, that a section for one
    of its datasets comes before; dropped when the parent's skip_children deletes the child instead of its recursion
    copying over it. None when every such section comes after its parent."""
    ini = p._read_ini(conf, user_editable=True)
    seen: list[str] = []
    for name, raw in ini.items():
        if name.startswith("template_"):
            continue
        own = p._section_dict(ini, name)
        recursive, skip = own.get("recursive", ""), own.get("skip_children", "")
        zfs = "zfs" in recursive.lower()
        if zfs or recursive in p.TRUE_VALUES or skip in p.TRUE_VALUES:
            path = own["path"] if raw.get("path") else name  # upstream's `defined`: an empty here-doc leaves it undefined
            child = next((s for s in seen if s.startswith(f"{path}/")), None)
            if child is not None:
                return child, name, skip in p.TRUE_VALUES and not zfs
        seen.append(name)
    return None


def refusal(conf: Path | str, child: str, parent: str, dropped: bool) -> str:
    """pysanoid's words for it, naming the file as load_config was given it (`<configdir>/sanoid.conf`)."""
    effect = "skip_children would drop it" if dropped else "recursion would replace its settings"
    return (f"[{child}] in {conf} must come after [{parent}]: sections are read in order, and [{parent}]'s {effect}. "
            f"Move it below [{parent}].")


def _on(value: Any) -> bool:
    """A merged toggle: a bool from pysanoid's merge, or "1"/"0" as the harnesses normalize one."""
    return value is True or value == "1"


def first_shared(conf: Path, config: dict[str, dict[str, Any]]) -> tuple[str, list[str], str | None] | None:
    """(dataset, entries, recursive section) for the first dataset, in the merged config's order, that two entries take
    or prune, by take's and prune's own skip rules (a name starting "template", process_children_only, neither autosnap
    nor autoprune). Entries are named as the refusal names them: a section of the file as [section], any other entry as
    the recursion that made it, [section]'s recursion, where section is the last recursive one, in file order, whose
    path the dataset is under. None when every dataset has at most one."""
    ini = p._read_ini(conf, user_editable=True)
    sections = [name for name in ini if not name.startswith("template_")]
    recursions: list[tuple[str, str]] = []  # (section, path) of each section whose recursion copies its settings
    for name in sections:
        own = p._section_dict(ini, name)
        recursive, skip = own.get("recursive", ""), own.get("skip_children", "")
        if "zfs" in recursive.lower() or (recursive in p.TRUE_VALUES and skip not in p.TRUE_VALUES):
            recursions.append((name, own["path"] if ini[name].get("path") else name))
    owners: dict[str, list[str]] = {}
    for name, cfg in config.items():
        if name.startswith("template") or _on(cfg.get("process_children_only")):
            continue
        if _on(cfg.get("autosnap")) or _on(cfg.get("autoprune")):
            owners.setdefault(cfg["path"], []).append(name)
    for path, names in owners.items():
        if len(names) < 2:
            continue
        recursion = None
        who: list[str] = []
        for name in names:
            if name in sections:
                who.append(f"[{name}]")
            else:
                recursion = [section for section, top in recursions if name.startswith(f"{top}/")][-1]
                who.append(f"[{recursion}]'s recursion")
        return path, who, recursion
    return None


def shared_refusal(conf: Path | str, path: str, who: list[str], recursion: str | None) -> str:
    """pysanoid's words for it, naming the file as load_config was given it."""
    advice = (f"To give {path} settings of its own, name its section [{path}] and place it below [{recursion}]."
              if recursion else f"Keep one section for {path}.")
    return (f"{', '.join(who[:-1])} and {who[-1]} in {conf} {'both' if len(who) == 2 else 'all'} take or prune {path}, "
            f"each by its own settings. {advice}")


def recursion_above(conf: Path) -> dict[str, str]:
    """{section: the recursive section it starts from} for every section named for a dataset under the path of a
    section above it whose recursion hands its settings down (recursive, and not skip_children unless the recursion is
    zfs's). Where several are above it, the last in file order: each recursion copies over the one before."""
    ini = p._read_ini(conf, user_editable=True)
    recursions: list[tuple[str, str]] = []  # (section, path)
    above: dict[str, str] = {}
    for name in ini:
        if name.startswith("template_"):
            continue
        for parent, top in recursions:
            if name.startswith(f"{top}/"):
                above[name] = parent
        own = p._section_dict(ini, name)
        recursive, skip = own.get("recursive", ""), own.get("skip_children", "")
        if "zfs" in recursive.lower() or (recursive in p.TRUE_VALUES and skip not in p.TRUE_VALUES):
            recursions.append((name, own["path"] if ini[name].get("path") else name))
    return above


def own_settings(conf: Path, name: str) -> tuple[set[str], bool]:
    """(the keys a section sets for itself, whether its own recursion is zfs's). Its own keys but for template,
    recursive and skip_children ones, and its templates' but for template and recursive ones: what load_config lays
    over the settings the section starts from."""
    ini = p._read_ini(conf, user_editable=True)
    own = p._section_dict(ini, name)
    keys = {key for key in own if not any(word in key for word in ("template", "recursive", "skip_children"))}
    for template in own.get("use_template", "").split(","):
        if template.strip():
            keys |= {key for key in p._section_dict(ini, f"template_{template.strip()}")
                     if "template" not in key and "recursive" not in key}
    return keys, "zfs" in own.get("recursive", "").lower()


def starts_from_parent(entry: dict[str, str], parent: dict[str, str], upstream: dict[str, str], own: set[str],
                       own_zfs: bool) -> list[str]:
    """The keys of a section's merged `entry` that break the inheritance rule, given its recursive `parent`'s merged
    entry, `upstream`'s entry for the section (built from the defaults when its lookup didn't list it), and the keys
    the section sets itself (own_settings). Every key it doesn't set itself is its parent's, but for the keys a
    recursion never copies; what it sets itself, and its path, are what upstream gives it. Under a zfs recursion its
    autosnap is off, whatever it says. Values as the harnesses normalize them: toggles "1" or "0"."""
    in_zfs_recursion = parent.get("zfs_recursion") == "1"
    wrong: list[str] = []
    for key in sorted(set(entry) | set(parent)):
        got = entry.get(key)
        if key == "initialized":
            want: str | None = "1"
        elif key == "zfs_recursion":
            want = "1" if in_zfs_recursion or own_zfs else None
        elif key == "autosnap" and in_zfs_recursion:
            want = "0"
        elif key == "path" or key in own:
            want = upstream.get(key)
        elif any(word in key for word in ("template", "recursive", "children_only")):
            want = None  # never copied by a recursion, and not set by the section
        else:
            want = parent.get(key)
        if got != want:
            wrong.append(key)
    return wrong
