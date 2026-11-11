"""Loads scenario.yaml and renders screens for a given user."""
from dataclasses import dataclass, field

import re
from pathlib import Path

import yaml
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CAPTION_LIMIT = 1024   # подпись к картинке
TEXT_LIMIT = 4096      # обычное сообщение
_CMD_RE = re.compile(r"^[a-z0-9_]{1,32}$")


class ScenarioError(Exception):
    pass


def check(cond: dict | None, cats: set[str]) -> bool:
    """any: хотя бы одна; all: все; not: ни одной. Все указанные части должны выполниться."""
    if not cond:
        return True
    if "any" in cond and not (cats & set(cond["any"])):
        return False
    if "all" in cond and not set(cond["all"]) <= cats:
        return False
    if "not" in cond and cats & set(cond["not"]):
        return False
    return True


@dataclass
class Rendered:
    name: str
    text: str
    photo: str | None
    keyboard: InlineKeyboardMarkup | None
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)


class Scenario:
    def __init__(self, path: str):
        self.path = path
        self.base_dir = Path(path).resolve().parent
        self.load()

    def media_path(self, photo: str | None) -> Path | None:
        """Local file for `photo`, or None if it is a URL / file_id."""
        if not photo or photo.startswith(("http://", "https://")):
            return None
        p = (self.base_dir / photo).resolve()
        return p if p.is_file() else None

    def load(self):
        with open(self.path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        self.start_screen: str = data["start_screen"]
        self.categories: dict[str, str] = data.get("categories") or {}
        self.deep_links: dict[str, list[str]] = data.get("deep_links") or {}
        self.tariffs: dict[str, dict] = data.get("tariffs") or {}
        self.screens: dict[str, dict] = data["screens"]
        # commands: {buy: {screen: buy, description: "забрать курс"}}
        self.commands: dict[str, dict] = data.get("commands") or {}
        self.start_description: str = data.get("start_description") or "начало"
        self.validate()

    def validate(self):
        errors = []
        if self.start_screen not in self.screens:
            errors.append(f"start_screen «{self.start_screen}» не найден")
        known = set(self.categories)

        def cats_ok(where, names):
            for c in names or []:
                if c not in known:
                    errors.append(f"{where}: неизвестная категория «{c}»")

        def cond_ok(where, cond):
            for k in ("any", "all", "not"):
                cats_ok(where, (cond or {}).get(k))

        for key, cats in self.deep_links.items():
            cats_ok(f"deep_links.{key}", cats)
        for tid, t in self.tariffs.items():
            cats_ok(f"tariffs.{tid}", t.get("grant"))
            if t.get("success_screen") and t["success_screen"] not in self.screens:
                errors.append(f"tariffs.{tid}: экран «{t['success_screen']}» не найден")
            if len(t.get("paid_content", "")) > 4096:
                errors.append(f"tariffs.{tid}: paid_content длиннее 4096 символов")
            if len(f"p:{tid}".encode()) > 64:
                errors.append(f"tariffs.{tid}: слишком длинный id")
        for cmd, c in self.commands.items():
            if not _CMD_RE.match(str(cmd)) or cmd == "start":
                errors.append(f"commands.{cmd}: команда — латиница/цифры/_ , не start")
            if (c or {}).get("screen") not in self.screens:
                errors.append(f"commands.{cmd}: экран «{(c or {}).get('screen')}» не найден")
        for name, s in self.screens.items():
            raw = s.get("text") or ""
            if re.search(r"&(?!(amp|lt|gt|quot|#\d+);)", raw):
                errors.append(f"screens.{name}: символ & нужно писать как &amp;")
            plain = re.sub(r"<[^>]+>", "", raw).strip()
            limit = CAPTION_LIMIT if s.get("photo") else TEXT_LIMIT
            if len(plain) > limit:
                errors.append(f"screens.{name}: текст {len(plain)} символов, лимит {limit}"
                              + (" (подпись к картинке)" if s.get("photo") else ""))
            ph = s.get("photo")
            if ph and not ph.startswith(("http://", "https://")) and "/" in ph \
                    and not (self.base_dir / ph).is_file():
                errors.append(f"screens.{name}: файл картинки «{ph}» не найден")
            if len(f"g:{name}".encode()) > 64:
                errors.append(f"screens.{name}: слишком длинное имя")
            cond_ok(f"screens.{name}.if", s.get("if"))
            if s.get("else") and s["else"] not in self.screens:
                errors.append(f"screens.{name}.else: экран «{s['else']}» не найден")
            cats_ok(f"screens.{name}.on_enter", (s.get("on_enter") or {}).get("add"))
            cats_ok(f"screens.{name}.on_enter", (s.get("on_enter") or {}).get("remove"))
            for row in s.get("buttons") or []:
                for b in row:
                    w = f"screens.{name} кнопка «{b.get('text')}»"
                    cond_ok(w, b.get("if"))
                    if "goto" in b and b["goto"] not in self.screens:
                        errors.append(f"{w}: экран «{b['goto']}» не найден")
                    if "pay" in b and b["pay"] not in self.tariffs:
                        errors.append(f"{w}: тариф «{b['pay']}» не найден")
                    if not any(k in b for k in ("goto", "url", "pay")):
                        errors.append(f"{w}: нужен goto, url или pay")
        if errors:
            raise ScenarioError("\n".join(errors))

    def render(self, name: str, cats: set[str], first_name: str = "") -> Rendered:
        # follow if/else redirects (with loop protection)
        seen = set()
        while True:
            s = self.screens[name]
            if check(s.get("if"), cats) or not s.get("else") or name in seen:
                break
            seen.add(name)
            name = s["else"]

        on_enter = s.get("on_enter") or {}
        # categories that will be added on enter count for button conditions on this screen
        effective = (cats | set(on_enter.get("add") or [])) - set(on_enter.get("remove") or [])

        rows = []
        for row in s.get("buttons") or []:
            btns = []
            for b in row:
                if not check(b.get("if"), effective):
                    continue
                if "url" in b:
                    btns.append(InlineKeyboardButton(text=b["text"], url=b["url"]))
                elif "pay" in b:
                    btns.append(InlineKeyboardButton(text=b["text"], callback_data=f"p:{b['pay']}"))
                else:
                    btns.append(InlineKeyboardButton(text=b["text"], callback_data=f"g:{b['goto']}"))
            if btns:
                rows.append(btns)

        text = (s.get("text") or "").replace("{first_name}", first_name or "")
        return Rendered(
            name=name,
            text=text.strip(),
            photo=s.get("photo"),
            keyboard=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
            add=list(on_enter.get("add") or []),
            remove=list(on_enter.get("remove") or []),
        )
