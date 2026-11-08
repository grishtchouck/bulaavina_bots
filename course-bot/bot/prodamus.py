"""Prodamus: payment links and webhook signature verification.

Prodamus signs data like this: all values cast to strings, keys sorted
recursively, the structure JSON-encoded, then HMAC-SHA256 with the secret
key (hex). The signature arrives in the `Sign` header of the notification.
"""
import hashlib
import hmac
import json
import re
from urllib.parse import urlencode

_KEY_RE = re.compile(r"[^\[\]]+")


def unflatten(flat: dict[str, str]) -> dict:
    """{'products[0][name]': 'x'} → {'products': [{'name': 'x'}]}"""
    root: dict = {}
    for key, value in flat.items():
        parts = _KEY_RE.findall(key) or [key]
        node = root
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return _listify(root)


def _listify(node):
    if isinstance(node, dict):
        node = {k: _listify(v) for k, v in node.items()}
        if node and all(k.isdigit() for k in node) and \
                sorted(int(k) for k in node) == list(range(len(node))):
            return [node[str(i)] for i in range(len(node))]
    return node


def _normalize(node):
    if isinstance(node, dict):
        return {k: _normalize(node[k]) for k in sorted(node)}
    if isinstance(node, list):
        return [_normalize(v) for v in node]
    if node is None:
        return ""
    if isinstance(node, bool):
        return "1" if node else "0"
    return str(node)


def _payloads(data: dict) -> list[bytes]:
    s = json.dumps(_normalize(data), ensure_ascii=False, separators=(",", ":"))
    # PHP json_encode escapes "/" unless JSON_UNESCAPED_SLASHES — accept both forms
    return [s.encode(), s.replace("/", "\\/").encode()]


def sign(data: dict, secret: str) -> str:
    return hmac.new(secret.encode(), _payloads(data)[0], hashlib.sha256).hexdigest()


def verify(data: dict, secret: str, received: str) -> bool:
    if not secret or not received:
        return False
    received = received.strip().lower()
    return any(
        hmac.compare_digest(hmac.new(secret.encode(), p, hashlib.sha256).hexdigest(), received)
        for p in _payloads(data)
    )


def _flatten(data, prefix="") -> list[tuple[str, str]]:
    out = []
    items = data.items() if isinstance(data, dict) else enumerate(data)
    for k, v in items:
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, (dict, list)):
            out += _flatten(v, key)
        else:
            out.append((key, str(v)))
    return out


def payment_link(*, base_url: str, order_id: str, user_id: int, title: str, price: float,
                 notify_url: str, success_url: str, secret: str = "", sign_link: bool = False,
                 paid_content: str = "") -> str:
    data = {
        "do": "pay",
        "order_id": order_id,
        "customer_extra": f"tg:{user_id}",
        "products": [{"name": title, "price": price, "quantity": 1}],
        "urlNotification": notify_url,
        "urlSuccess": success_url,
        "urlReturn": success_url,
    }
    if paid_content:  # «доступы к материалам» — обязательно для чека по требованиям ФНС
        data["paid_content"] = paid_content
    if sign_link and secret:
        data["signature"] = sign(data, secret)
    return f"{base_url}/?{urlencode(_flatten(data))}"
