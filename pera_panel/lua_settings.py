"""Read a restricted Lua literal table. Never load Lua or evaluate uploaded expressions."""
import math
import re

from .storage import PanelError, validate_mods


def unsupported():
    return PanelError("The uploaded modoverrides.lua is not a supported literal settings table. "
                      "Lua expressions/functions are not executed. Export standard mod settings, "
                      "or turn off ‘Inherit mod settings’ to keep your panel’s current mods.")


def long_string(text, position):
    opening = re.match(r"\[(=*)\[", text[position:])
    if not opening:
        return None
    end_marker = "]" + opening[1] + "]"
    start = position + len(opening[0])
    end = text.find(end_marker, start)
    if end < 0:
        raise unsupported()
    content = text[start:end].replace("\r\n", "\n").replace("\r", "\n")
    if content.startswith("\n"):
        content = content[1:]
    return content, end + len(end_marker)


def tokens(text):
    result, index = [], 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if text.startswith("--", index):
            long = long_string(text, index + 2)
            index = long[1] if long else (text.find("\n", index) + 1 or len(text))
            continue
        long = long_string(text, index) if char == "[" else None
        if long:
            result.append(("value", long[0]))
            index = long[1]
        elif char in "\"'":
            quote, content = char, bytearray()
            index += 1
            while index < len(text) and text[index] != quote:
                char = text[index]
                if char in "\r\n":
                    raise unsupported()
                if char != "\\":
                    content.extend(char.encode("utf-8"))
                    index += 1
                    continue
                index += 1
                if index >= len(text):
                    raise unsupported()
                escaped = text[index]
                controls = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
                            "\\": 92, '"': 34, "'": 39}
                number = re.match(r"[0-9]{1,3}", text[index:])
                if number:
                    byte = int(number[0])
                    if byte > 255:
                        raise unsupported()
                    content.append(byte)
                    index += len(number[0])
                elif escaped in controls:
                    content.append(controls[escaped])
                    index += 1
                elif escaped in "\r\n":
                    content.append(10)
                    index += 2 if text.startswith("\r\n", index) else 1
                else:
                    raise unsupported()
            if index >= len(text):
                raise unsupported()
            try:
                result.append(("value", content.decode("utf-8")))
            except UnicodeError as exc:
                raise unsupported() from exc
            index += 1
        elif char in "{}[]=,;":
            result.append((char, char))
            index += 1
        else:
            number = re.match(r"[+-]?(?:0[xX][0-9a-fA-F]+|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)",
                              text[index:])
            name = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[index:])
            if number:
                raw = number[0]
                value = int(raw, 0) if re.match(r"[+-]?0[xX]", raw) else float(raw) if any(
                    c in raw for c in ".eE") else int(raw)
                if not math.isfinite(value):
                    raise unsupported()
                result.append(("value", value))
                index += len(raw)
            elif name:
                result.append(("name", name[0]))
                index += len(name[0])
            else:
                raise unsupported()
        if len(result) > 20000:
            raise PanelError("The uploaded mod settings are too complex.")
    return result + [("end", None)]


class LiteralTable:
    def __init__(self, text):
        self.items = tokens(text)
        self.position = 0

    def peek(self):
        return self.items[self.position]

    def take(self, kind, value=None):
        token = self.peek()
        if token[0] != kind or (value is not None and token[1] != value):
            raise unsupported()
        self.position += 1
        return token[1]

    def value(self, depth=0):
        if depth > 12:
            raise unsupported()
        kind, content = self.peek()
        if kind == "value":
            self.position += 1
            return content
        if kind == "name" and content in ("true", "false", "nil"):
            self.position += 1
            return {"true": True, "false": False, "nil": None}[content]
        self.take("{")
        entries, used, array_index = {}, set(), 1
        while self.peek()[0] != "}":
            if self.peek()[0] == "[":
                self.take("[")
                key = self.value(depth + 1)
                self.take("]")
                self.take("=")
                value = self.value(depth + 1)
            elif self.peek()[0] == "name" and self.items[self.position + 1][0] == "=":
                key = self.take("name")
                self.take("=")
                value = self.value(depth + 1)
            else:
                key, value = array_index, self.value(depth + 1)
                array_index += 1
            if type(key) not in (str, int) or key in used:
                raise unsupported()
            used.add(key)
            if value is not None:  # Lua nil fields do not exist in the resulting table.
                entries[key] = value
            if self.peek()[0] in (",", ";"):
                self.position += 1
            elif self.peek()[0] != "}":
                raise unsupported()
        self.take("}")
        if entries and all(type(key) is int for key in entries):
            if set(entries) != set(range(1, len(entries) + 1)):
                raise unsupported()
            return [entries[key] for key in range(1, len(entries) + 1)]
        if any(type(key) is not str for key in entries):
            raise unsupported()
        return entries

    def read(self):
        self.take("name", "return")
        value = self.value()
        if self.peek()[0] == ";":
            self.position += 1
        if self.peek()[0] != "end" or not isinstance(value, dict):
            raise unsupported()
        return value


def read_mods(data):
    if len(data) > 128 * 1024:
        raise PanelError("Each uploaded modoverrides.lua must be 128 KiB or smaller.")
    try:
        settings = LiteralTable(data.decode("utf-8-sig")).read()
    except (UnicodeError, ValueError, OverflowError, RecursionError) as exc:
        raise unsupported() from exc
    mods = []
    for key, value in settings.items():
        if not re.fullmatch(r"workshop-[1-9][0-9]{4,19}", key) or not isinstance(value, dict):
            raise unsupported()
        mods.append({"id": key.removeprefix("workshop-"), "enabled": value.get("enabled", False),
                     "options": value.get("configuration_options", {})})
    return sorted(validate_mods(mods), key=lambda mod: mod["id"])
