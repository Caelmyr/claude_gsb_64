"""用户代码模板 API。

存储：每个用户一个分片 data/templates/<user_id>.json，文件内为
{"items": [模板, ...]}。用户之间天然按文件隔离；读-改-写通过
locked_update 按分片加锁，保证同一用户并发操作不丢数据。
"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth
from backend.storage import read_json, locked_update
from backend.utils import now_iso, gen_id, sanitize_id

templates_bp = Blueprint("templates", __name__)

MAX_NAME_LEN = 100
MAX_CONTENT_LEN = 64 * 1024  # 单个模板内容上限 64KB


def _path(user_id):
    return os.path.join(config.TEMPLATES_DIR, f"{sanitize_id(user_id)}.json")


def _public(t):
    """返回给前端的模板字段。"""
    return {
        "id": t["id"],
        "name": t.get("name", ""),
        "language": t.get("language", ""),
        "content": t.get("content", ""),
        "is_default": bool(t.get("is_default")),
        "created_at": t.get("created_at", ""),
        "updated_at": t.get("updated_at", ""),
    }


def _validate(data, partial=False):
    """校验请求字段，返回 (name, language, content) 或错误字符串。"""
    name = (data.get("name") or "").strip()
    language = (data.get("language") or "").strip()
    content = data.get("content")
    if content is None:
        content = ""
    content = str(content)

    if not partial or data.get("name") is not None:
        if not name:
            return None, "模板名称不能为空"
        if len(name) > MAX_NAME_LEN:
            return None, "模板名称不能超过 %d 个字符" % MAX_NAME_LEN
    if not partial or data.get("language") is not None:
        if language not in config.LANGUAGES:
            return None, "不支持的编程语言"
    if content and len(content) > MAX_CONTENT_LEN:
        return None, "模板内容不能超过 64KB"
    return (name, language, content), None


@templates_bp.get("/templates")
@require_auth
def list_templates():
    data = read_json(_path(request.user["id"]))
    items = [_public(t) for t in (data or {}).get("items", [])]
    language = request.args.get("language")
    if language:
        items = [t for t in items if t["language"] == language]
    items.sort(key=lambda t: (t.get("created_at", ""), t.get("id", "")))
    return ok({"total": len(items), "items": items})


@templates_bp.post("/templates")
@require_auth
def create_template():
    data = request.get_json(silent=True) or {}
    parsed, error = _validate(data)
    if error:
        return err(error, 400)
    name, language, content = parsed

    template = {
        "id": gen_id("tpl"),
        "name": name,
        "language": language,
        "content": content,
        "is_default": False,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    make_default = bool(data.get("is_default"))

    def _upd(d):
        if d is None:
            d = {"items": []}
        items = d.setdefault("items", [])
        # 同一语言至多一个默认模板
        if make_default:
            for t in items:
                if t.get("language") == language:
                    t["is_default"] = False
            template["is_default"] = True
        # 一份模板都没有时，首个模板自动成为默认，打开编辑器即可直接生效
        elif not any(t.get("language") == language for t in items):
            template["is_default"] = True
        items.append(template)
        return d

    locked_update(_path(request.user["id"]), _upd, default=None)
    return ok(_public(template))


@templates_bp.get("/templates/<tpl_id>")
@require_auth
def get_template(tpl_id):
    data = read_json(_path(request.user["id"]))
    for t in (data or {}).get("items", []):
        if t.get("id") == tpl_id:
            return ok(_public(t))
    return err("模板不存在", 404)


def _find(items, tpl_id):
    return next((t for t in items or [] if t.get("id") == tpl_id), None)


@templates_bp.put("/templates/<tpl_id>")
@require_auth
def update_template(tpl_id):
    data = request.get_json(silent=True) or {}
    parsed, error = _validate(data, partial=True)
    if error:
        return err(error, 400)
    name, language, content = parsed
    path = _path(request.user["id"])
    if _find((read_json(path) or {}).get("items", []), tpl_id) is None:
        return err("模板不存在", 404)
    result = [None]

    def _upd(d):
        t = _find((d or {}).get("items", []), tpl_id)
        if t is None:
            return d
        if name:
            t["name"] = name
        if language:
            # 跨语言移动时，模板在原语言的默认标记不能带到新语言
            if t.get("language") != language:
                t["is_default"] = False
            t["language"] = language
        if data.get("content") is not None:
            t["content"] = content
        t["updated_at"] = now_iso()
        result[0] = dict(t)
        return d

    locked_update(path, _upd, default=None)
    if result[0] is None:
        return err("模板不存在", 404)
    return ok(_public(result[0]))


@templates_bp.post("/templates/<tpl_id>/default")
@require_auth
def set_default(tpl_id):
    path = _path(request.user["id"])
    if _find((read_json(path) or {}).get("items", []), tpl_id) is None:
        return err("模板不存在", 404)
    result = [None]

    def _upd(d):
        target = _find((d or {}).get("items", []), tpl_id)
        if target is None:
            return d
        for t in d["items"]:
            # 同一语言只能有一个默认模板
            if t.get("language") == target.get("language"):
                t["is_default"] = t is target
        target["updated_at"] = now_iso()
        result[0] = dict(target)
        return d

    locked_update(path, _upd, default=None)
    return ok(_public(result[0]))


@templates_bp.delete("/templates/<tpl_id>")
@require_auth
def delete_template(tpl_id):
    path = _path(request.user["id"])
    if _find((read_json(path) or {}).get("items", []), tpl_id) is None:
        return err("模板不存在", 404)

    def _upd(d):
        d["items"] = [t for t in d.get("items", []) if t.get("id") != tpl_id]
        return d

    locked_update(path, _upd, default=None)
    return ok()
